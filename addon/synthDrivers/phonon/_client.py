# SPDX-License-Identifier: GPL-2.0-or-later
"""Bounded, interruptible Ollama requests. This module has no NVDA dependency."""

from dataclasses import dataclass
import http.client
import io
import json
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

SAMPLE_RATE = 24000
MAX_INPUT = 4096
MAX_METADATA = 1024 * 1024
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 600
CONNECT_TIMEOUT = 3
READ_TIMEOUT = 12
REQUEST_DEADLINE = 45
BLOCK_BYTES = 2048
READ_POLL_SECONDS = 0.05


class Cancelled(Exception):
    pass


class ServerError(RuntimeError):
    pass


class _SocketReader(io.RawIOBase):
    """Poll recv without poisoning Python's socket.makefile after a timeout."""
    def __init__(self, owner, cancel):
        self.owner, self.cancel = owner, cancel
        self.network_time = 0

    def readable(self):
        return True

    def readinto(self, buffer):
        start = time.monotonic()
        try:
            while True:
                if self.cancel.is_set():
                    raise Cancelled()
                elapsed = time.monotonic() - start
                if elapsed >= READ_TIMEOUT or self.network_time + elapsed >= REQUEST_DEADLINE:
                    raise ServerError("The speech server stopped responding.")
                try:
                    return self.owner.socket.recv_into(buffer)
                except (socket.timeout, ssl.SSLWantReadError):
                    continue
        finally:
            self.network_time += time.monotonic() - start

    def close(self):
        if not self.closed:
            self.owner.release()
        super().close()


class _ResponseSocket:
    """Let HTTPResponse finish Connection: close bodies before releasing the socket."""
    def __init__(self, sock, cancel):
        self.socket, self.cancel = sock, cancel
        self.readers = 0
        self.close_requested = False

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Only HTTP response reads are supported.")
        self.readers += 1
        return io.BufferedReader(_SocketReader(self, self.cancel))

    def release(self):
        self.readers -= 1
        if self.close_requested and self.readers == 0:
            self.socket.close()

    def close(self):
        self.close_requested = True
        if self.readers == 0:
            self.socket.close()


@dataclass(frozen=True)
class Settings:
    address: str = "http://localhost:11434"
    model: str = "phonon"
    fallback: bool = True


def validate(settings):
    try:
        url = urlsplit(settings.address.strip())
        port = url.port
    except ValueError as error:
        raise ValueError("Enter a valid server address, including http:// or https://.") from error
    if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment
            or url.path not in ("", "/") or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Use http://host:port or https://host:port without a path or password.")
    if not settings.model.strip() or len(settings.model) > 128 or any(ord(c) < 32 for c in settings.model):
        raise ValueError("Enter a speech model name, such as phonon.")
    return url


class Client:
    def __init__(self, settings):
        self.settings = settings
        self._url = validate(settings)
        self._lock = threading.Lock()
        self._active = None

    def abort(self, cancel):
        with self._lock:
            active = self._active
            if active is None or active[1] is not cancel:
                return
            connection = active[0]
            sock = active[2]
        # shutdown wakes a blocked read; HTTPResponse.close alone can wait for that read.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _request(self, path, payload, cancel):
        if cancel.is_set():
            raise Cancelled()
        connection_type = http.client.HTTPSConnection if self._url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self._url.hostname, self._url.port, timeout=CONNECT_TIMEOUT)
        response = None
        try:
            with self._lock:
                if cancel.is_set():
                    raise Cancelled()
                if self._active is not None:
                    raise RuntimeError("A speech client cannot run simultaneous requests.")
                self._active = (connection, cancel, None)
            connection.connect()
            with self._lock:
                self._active = (connection, cancel, connection.sock)
            if cancel.is_set():
                raise Cancelled()
            connection.sock.settimeout(READ_TIMEOUT)
            connection.request("GET" if payload is None else "POST", path,
                               None if payload is None else json.dumps(payload).encode("utf-8"),
                               {"Content-Type": "application/json", "User-Agent": "Ollama Speech NVDA/1.0"})
            # Closing a socket from another thread does not reliably wake recv on Windows.
            # Keep the standard HTTP parser, but poll its underlying reads for cancellation.
            connection.sock.settimeout(READ_POLL_SECONDS)
            connection.sock = _ResponseSocket(connection.sock, cancel)
            response = connection.getresponse()
            if response.status != 200:
                # Do not echo server error bodies: some servers repeat the submitted speech.
                raise ServerError("The speech server returned HTTP %d." % response.status)
            yield response
        except (OSError, http.client.HTTPException) as error:
            if cancel.is_set():
                raise Cancelled() from error
            raise ServerError("The speech server could not be reached or stopped responding.") from error
        finally:
            if response is not None:
                response.close()
            connection.close()
            with self._lock:
                if self._active is not None and self._active[0] is connection:
                    self._active = None

    def _metadata(self, path, payload, cancel):
        request = self._request(path, payload, cancel)
        try:
            response = next(request)
            data = response.read(MAX_METADATA + 1)
            if cancel.is_set():
                raise Cancelled()
            if len(data) > MAX_METADATA:
                raise ServerError("The server's model information is too large.")
            try:
                model = json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise ServerError("The server returned invalid model information.") from error
            if not isinstance(model, dict):
                raise ServerError("The server returned invalid model information.")
            return model
        finally:
            request.close()

    def voices(self, cancel, *, model=None):
        model = self._metadata("/api/show", {"model": model or self.settings.model}, cancel)
        capabilities = model.get("capabilities")
        if not isinstance(capabilities, list) or "speech" not in capabilities:
            raise ServerError("The selected model does not support speech.")
        names = model.get("voices")
        if not isinstance(names, list) or not names or len(names) > 1000:
            raise ServerError("The selected model did not provide a voice list.")
        if any(not isinstance(n, str) or not n.strip() or len(n) > 160
               or any(ord(c) < 32 for c in n) for n in names):
            raise ServerError("The server returned an invalid voice name.")
        return list(dict.fromkeys(names))

    def models(self, cancel):
        from ._profiles import VERIFIED_FAMILIES, family
        metadata = self._metadata("/api/tags", None, cancel)
        entries = metadata.get("models")
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ServerError("The server returned an invalid model list.")
        supported = []
        for item in entries:
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name or len(name) > 128 or any(ord(c) < 32 for c in name):
                raise ServerError("The server returned an invalid model name.")
            if family(name) in VERIFIED_FAMILIES and name not in supported:
                supported.append(name)
        if len(supported) > 32:
            raise ServerError("The server has too many speech models to inspect in one refresh.")
        result = {}
        for name in supported:
            if cancel.is_set():
                raise Cancelled()
            try:
                result[name] = self.voices(cancel, model=name)
            except ServerError:
                continue
        return result

    def audio(self, text, voice, cancel):
        if not text or len(text) > MAX_INPUT:
            raise ValueError("Speech requests must contain 1 to 4096 characters.")
        request = self._request("/v1/audio/speech", {
            "model": self.settings.model, "input": text, "voice": voice, "response_format": "pcm",
        }, cancel)
        try:
            response = next(request)
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("audio/pcm", "application/octet-stream"):
                raise ServerError("The server did not return raw PCM audio.")
            expected_length = response.length
            if expected_length is not None and expected_length > MAX_AUDIO_BYTES:
                raise ServerError("The server returned too much audio for one request.")
            pending = b""
            total = 0
            while True:
                if cancel.is_set():
                    raise Cancelled()
                block = response.read1(BLOCK_BYTES)
                if cancel.is_set():
                    raise Cancelled()
                if not block:
                    break
                total += len(block)
                if total > MAX_AUDIO_BYTES:
                    raise ServerError("The server returned too much audio for one request.")
                pending += block
                aligned = len(pending) & ~1
                if aligned:
                    yield pending[:aligned]
                    pending = pending[aligned:]
            if pending or total == 0 or (expected_length is not None and total != expected_length):
                raise ServerError("The server returned incomplete or empty PCM audio.")
        finally:
            request.close()
