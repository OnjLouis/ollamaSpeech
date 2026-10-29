# SPDX-License-Identifier: GPL-2.0-or-later
"""Isolated network and driver lifecycle regressions; no live NVDA configuration."""
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

ADDON = Path(os.environ.get("PHONON_ADDON_ROOT", Path(__file__).resolve().parents[1] / "addon"))
sys.path.insert(0, str(ADDON))


class Command:
    def __init__(self, value=None, **kwargs):
        self.__dict__.update(kwargs)
        self.index = value
        self.state = value
        self.time = value
        self.newValue = value
        self.isDefault = value is None


class BaseSynth:
    @staticmethod
    def setting(*args, **kwargs):
        return None

    VoiceSetting = RateSetting = RateBoostSetting = PitchSetting = VolumeSetting = setting

    def __getattr__(self, name):
        getter = getattr(type(self), "_get_" + name, None)
        if getter:
            return getter(self)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        setter = getattr(type(self), "_set_" + name, None) if not name.startswith("_") else None
        if setter:
            setter(self, value)
        else:
            object.__setattr__(self, name, value)

    def terminate(self):
        pass

    def invalidateCache(self):
        pass

    def initSettings(self):
        pass

    def loadSettings(self, onlyChanged=False):
        pass


class Notice:
    def __init__(self):
        self.items = []

    def notify(self, **kwargs):
        self.items.append(kwargs)


class FakePlayer:
    def __init__(self, *args, **kwargs):
        self.pending = []
        self.audio = []
        self.stops = 0
        self.closed = False
        self.paused = False

    def feed(self, data, size=None, onDone=None):
        if self.closed:
            raise AssertionError("Feed after close")
        if data:
            self.audio.append(bytes(data))
        if onDone:
            self.pending.append(onDone)

    def stop(self):
        self.stops += 1
        self.pending.clear()

    def pause(self, value):
        self.paused = value

    def idle(self):
        pass

    def close(self):
        self.closed = True

    def drain(self):
        while self.pending:
            self.pending.pop(0)()


class Config(dict):
    spec = {}


config_module = types.ModuleType("config")
config_module.conf = Config(audio={"outputDevice": "test"}, phononConnection={
    "address": "http://localhost:11434", "model": "phonon", "fallback": True,
})
sys.modules["config"] = config_module
sys.modules["globalVars"] = types.SimpleNamespace(appArgs=types.SimpleNamespace(secure=False), appDir=r"C:\Program Files\NVDA")
sys.modules["nvwave"] = types.SimpleNamespace(WavePlayer=FakePlayer)
sys.modules["logHandler"] = types.SimpleNamespace(log=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None))
commands = types.ModuleType("speech.commands")
for name in ("IndexCommand", "PitchCommand", "BreakCommand", "CharacterModeCommand"):
    setattr(commands, name, type(name, (Command,), {}))
sys.modules["speech"] = types.ModuleType("speech")
sys.modules["speech.commands"] = commands
sys.modules["autoSettingsUtils"] = types.ModuleType("autoSettingsUtils")
sys.modules["autoSettingsUtils.driverSetting"] = types.SimpleNamespace(
    DriverSetting=lambda id, label, **kwargs: types.SimpleNamespace(id=id, **kwargs))
sys.modules["autoSettingsUtils.utils"] = types.SimpleNamespace(
    StringParameterInfo=lambda id, name: types.SimpleNamespace(id=id, displayName=name))
index_notice, done_notice = Notice(), Notice()
handler = types.SimpleNamespace(SynthDriver=BaseSynth,
    VoiceInfo=lambda id, name, language=None: types.SimpleNamespace(id=id, displayName=name, language=language),
    synthIndexReached=index_notice, synthDoneSpeaking=done_notice)
sys.modules["synthDriverHandler"] = handler
phonon = importlib.import_module("synthDrivers._ollamaSpeech")
from synthDrivers._ollamaSpeech._client import Client, Settings, ServerError, Cancelled, validate
from synthDrivers._ollamaSpeech._sequence import chunks, events, language
from synthDrivers._ollamaSpeech._audio import Processor


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Operation exceeded its test deadline")
        time.sleep(0.005)


class HttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, OSError):
            pass

    def do_GET(self):
        self.server.requests.append((self.path, None))
        data = json.dumps({"models": [{"name": name} for name in
            ("phonon:latest", "phonon:40m-multilingual", "lfm3-audio:latest", "chat:latest")]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, payload))
        mode = payload.get("input", payload.get("model"))
        data = b"\x01\x00" * 200
        status, content_type = 200, "audio/pcm"
        if self.path == "/api/show":
            data = json.dumps({"capabilities": ["speech"], "voices": ["default", "Zoey"]}).encode()
            content_type = "application/json"
        if mode == "bad-model":
            data = b'{"capabilities":["completion"],"voices":[]}'
        elif mode == "bad-voices":
            data = b'{"capabilities":["speech"],"voices":["bad\\nvoice"]}'
        elif mode == "error":
            status, data = 400, b'Private submitted text must not appear in errors'
        elif mode == "redirect":
            status = 302
        elif mode == "wrong-format":
            content_type = "audio/wav"
        elif mode == "odd":
            data = b"\x00"
        elif mode == "empty":
            data = b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data) + (2 if mode == "truncated" else 0)))
        if mode in ("truncated", "stall-close"):
            self.send_header("Connection", "close")
        if mode == "redirect":
            self.send_header("Location", "/untrusted")
        self.end_headers()
        try:
            if mode in ("stall", "stall-close"):
                self.wfile.write(data[:2])
                self.wfile.flush()
                self.server.release.wait(3)
                self.wfile.write(data[2:])
            else:
                # Exercise odd transport boundaries without changing the 16-bit PCM stream.
                self.wfile.write(data[:3])
                self.wfile.flush()
                self.wfile.write(data[3:])
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        if mode == "truncated":
            self.close_connection = True


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), HttpHandler)
        self.server.daemon_threads = True
        self.server.release = threading.Event()
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = Settings("http://127.0.0.1:%d" % self.server.server_port)
        self.client = Client(self.settings)
        self.cancel = threading.Event()

    def tearDown(self):
        self.cancel.set()
        self.client.abort(self.cancel)
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_metadata_and_exact_pcm(self):
        self.assertEqual(self.client.voices(self.cancel), ["default", "Zoey"])
        self.assertEqual(b"".join(self.client.audio("hello", "Zoey", self.cancel)), b"\x01\x00" * 200)
        self.assertEqual(self.server.requests[-1][1]["response_format"], "pcm")
        self.assertIsNone(self.client._active)

    def test_discovery_requires_each_models_speech_capability(self):
        models = self.client.models(self.cancel)
        self.assertEqual(list(models), ["phonon:latest", "phonon:40m-multilingual", "lfm3-audio:latest"])
        self.assertEqual(models["lfm3-audio:latest"], ["default", "Zoey"])
        self.assertFalse(any(payload and payload.get("model") == "chat:latest" for _, payload in self.server.requests))

    def test_text_is_sent_exactly(self):
        text = 'Read exactly: "caf\u00e9", iOS, \u00a353.15.\nSecond line!'
        list(self.client.audio(text, "Zoey", self.cancel))
        self.assertEqual(self.server.requests[-1][1]["input"], text)

    def test_invalid_metadata(self):
        for name in ("bad-model", "bad-voices"):
            with self.subTest(name=name), self.assertRaises(ServerError):
                Client(Settings(self.settings.address, name)).voices(self.cancel)

    def test_invalid_audio_and_redirect(self):
        for text in ("error", "redirect", "wrong-format", "odd", "empty", "truncated"):
            with self.subTest(text=text), self.assertRaises(ServerError) as caught:
                list(self.client.audio(text, "default", self.cancel))
            self.assertNotIn("Private submitted", str(caught.exception))
            self.assertIsNone(self.client._active)
        self.assertFalse(any(path == "/untrusted" for path, _ in self.server.requests))

    def test_cancel_blocked_read_and_reuse(self):
        for mode in ("stall", "stall-close"):
            with self.subTest(mode=mode):
                cancel = threading.Event()
                stream = self.client.audio(mode, "default", cancel)
                self.assertEqual(next(stream), b"\x01\x00")
                finished = threading.Event()
                def read():
                    try:
                        list(stream)
                    except (Cancelled, OSError, ValueError):
                        pass
                    finally:
                        finished.set()
                worker = threading.Thread(target=read, daemon=True)
                worker.start()
                time.sleep(0.02)
                start = time.monotonic()
                cancel.set()
                self.client.abort(cancel)
                self.assertTrue(finished.wait(0.5), "Cancellation did not wake the read")
                self.assertLess(time.monotonic() - start, 0.5)
                worker.join(1)
                self.assertEqual(len(b"".join(self.client.audio("new", "default", self.cancel))), 400)

    def test_cancelled_before_request_and_length_limit(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            list(self.client.audio("hello", "default", self.cancel))
        with self.assertRaises(ValueError):
            list(self.client.audio("x" * 4097, "default", threading.Event()))
        self.assertEqual(self.server.requests, [])

    def test_read_timeout_is_bounded(self):
        from synthDrivers._ollamaSpeech import _client
        with patch.object(_client, "READ_TIMEOUT", 0.1):
            stream = self.client.audio("stall", "default", self.cancel)
            self.assertEqual(len(next(stream)), 2)
            start = time.monotonic()
            with self.assertRaises(ServerError):
                list(stream)
            self.assertLess(time.monotonic() - start, 0.5)
        self.assertIsNone(self.client._active)

    def test_stale_abort_does_not_cancel_new_request(self):
        old = threading.Event()
        stream = self.client.audio("stall", "default", self.cancel)
        self.assertEqual(len(next(stream)), 2)
        self.client.abort(old)
        self.server.release.set()
        self.assertEqual(len(b"".join(stream)), 398)


class SequenceTests(unittest.TestCase):
    def test_validation(self):
        for address in ("localhost:11434", "file:///tmp/test", "http://user:secret@host", "http://host/path", "http://host?x", "http://host:70000"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate(Settings(address))
        self.assertEqual(validate(Settings("https://[::1]:11434")).hostname, "::1")

    def test_long_requests_keep_all_text(self):
        text = "hello " * 2000
        result = list(chunks(text))
        self.assertTrue(all(0 < len(part) <= 4096 for part in result))
        self.assertEqual(" ".join(result).split(), text.split())
        self.assertEqual("".join(chunks("x" * 10000)), "x" * 10000)

    def test_commands_and_spelling_boundaries(self):
        sequence = [commands.IndexCommand(1), commands.CharacterModeCommand(True), "Andre", commands.CharacterModeCommand(False), "space", commands.PitchCommand(70), "Louis", commands.PitchCommand(), commands.BreakCommand(100), commands.IndexCommand(2)]
        self.assertEqual(events(sequence, 50), [("index", 1), ("text", "A n d r e"), ("text", "space"), ("pitch", 70), ("text", "Louis"), ("pitch", 50), ("break", 100), ("index", 2)])

    def test_language_tags(self):
        self.assertEqual(language("phonon:40m-fr"), "fr")
        self.assertEqual(language("phonon"), "en")

    def test_only_tested_families_are_exposed(self):
        from synthDrivers._ollamaSpeech._profiles import require_supported
        require_supported("phonon:40m-fr")
        require_supported("lfm3-audio:latest")
        with self.assertRaises(ValueError):
            require_supported("chat")

    def test_voice_language_is_model_specific(self):
        from synthDrivers._ollamaSpeech._profiles import voice_language
        self.assertEqual(voice_language("lfm3-audio", "female-british-english"), "en_GB")
        self.assertEqual(voice_language("lfm3-audio", "male-french"), "fr")
        self.assertEqual(voice_language("phonon:40m-de", "default"), "de")
        self.assertIsNone(voice_language("phonon:40m-multilingual", "default"))


class FakeClient:
    def __init__(self, settings):
        self.settings = settings
        self.requests = []
        self.gate = None
        self.started = threading.Event()
        self.fail = False

    def voices(self, cancel):
        return ["default", "Zoey"]

    def audio(self, text, voice, cancel):
        self.requests.append((text, voice))
        self.started.set()
        if self.gate:
            self.gate.wait(2)
        if cancel.is_set():
            raise Cancelled()
        if self.fail:
            raise ServerError("test")
        yield b"\x01\x00" * 100

    def abort(self, cancel):
        if self.gate:
            self.gate.set()


class DriverTests(unittest.TestCase):
    def setUp(self):
        from synthDrivers._ollamaSpeech import _catalogue
        _catalogue.clear()
        index_notice.items.clear()
        done_notice.items.clear()
        self.patch = patch.object(phonon, "Client", FakeClient)
        self.patch.start()
        self.driver = phonon.SynthDriver()

    def tearDown(self):
        self.driver.terminate()
        self.patch.stop()
        self.assertFalse(self.driver._worker.is_alive())
        self.assertTrue(self.driver._player.closed)

    def settle(self):
        wait_for(lambda: self.driver._jobs.unfinished_tasks == 0)

    def test_indexes_are_playback_timed_and_ordered(self):
        self.driver.speak(["one", commands.IndexCommand(1), "two", commands.IndexCommand(2)])
        self.settle()
        self.assertEqual(index_notice.items, [])
        self.assertEqual(done_notice.items, [])
        self.driver._player.drain()
        self.assertEqual([item["index"] for item in index_notice.items], [1, 2])
        self.assertEqual(len(done_notice.items), 1)

    def test_multiple_speak_calls_continue(self):
        for index, text in enumerate(["A", "dot", "space", "B", "next line"]):
            self.driver.speak([text, commands.IndexCommand(index)])
        self.settle()
        self.driver._player.drain()
        self.assertEqual([item["index"] for item in index_notice.items], list(range(5)))
        self.assertEqual(len(done_notice.items), 1)
        self.assertEqual(len(self.driver._client.requests), 5)

    def test_cancel_drops_old_audio_and_queued_fragments(self):
        client = self.driver._client
        client.gate = threading.Event()
        self.driver.speak(["old", commands.IndexCommand(1)])
        self.assertTrue(client.started.wait(1))
        self.driver.speak(["queued old", commands.IndexCommand(2)])
        self.driver.cancel()
        self.driver.speak(["new", commands.IndexCommand(3)])
        self.settle()
        self.driver._player.drain()
        self.assertEqual([item["index"] for item in index_notice.items], [3])
        self.assertEqual([text for text, _ in client.requests], ["old", "new"])
        self.assertEqual(len(self.driver._player.audio), 1)

    def test_voice_and_parameters_snapshot(self):
        self.driver.voice = "Zoey"
        with patch.object(phonon, "Processor", wraps=Processor) as processor:
            self.driver.speak(["hello"])
            self.driver.voice = "default"
            self.settle()
            processor.assert_called_once_with(50, 50, 100, False)
        self.assertEqual(self.driver._client.requests, [("hello", "Zoey")])

    def test_model_switch_cancels_old_work_and_preserves_controls(self):
        import synthDrivers
        synthDrivers.__path__.append(r"C:\Program Files\NVDA\library.zip\synthDrivers")
        sys.path.append(r"C:\Program Files\NVDA\library.zip")
        self.driver.voice = "Zoey"
        self.driver.rate, self.driver.pitch, self.driver.volume = 65, 40, 75
        old = self.driver._client
        old.gate = threading.Event()
        self.driver.speak(["old model", commands.IndexCommand(1)])
        self.assertTrue(old.started.wait(1))
        self.driver.reconfigure(Settings(model="lfm3-audio"), ["default", "female-french"])
        self.driver.speak(["new model", commands.IndexCommand(2)])
        self.settle()
        self.driver._player.drain()
        self.assertEqual([n["index"] for n in index_notice.items], [2])
        self.assertEqual((self.driver.rate, self.driver.pitch, self.driver.volume), (65, 40, 75))
        self.assertEqual(self.driver._client.requests, [("new model", "default")])
        self.driver.reconfigure(Settings(), ["default", "Zoey"])
        self.assertEqual(self.driver.voice, "Zoey")

    def test_model_ring_switch_uses_cache_only_and_replaces_voice_choices(self):
        self.driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french"]})
        self.assertEqual(list(self.driver.availableModels), ["phonon", "lfm3-audio"])
        self.assertTrue(self.driver.supportedSettings[0].availableInSettingsRing)
        handler.getSynth = lambda: object()
        with patch.object(FakeClient, "voices", side_effect=AssertionError("Model switching must not query the server")):
            self.driver.model = "lfm3-audio"
        self.assertEqual(self.driver.model, "lfm3-audio")
        self.assertEqual(list(self.driver.availableVoices), ["default", "female-french"])
        self.driver.model = "removed-model"
        self.assertEqual(self.driver.model, "lfm3-audio")

    def test_catalogue_is_scoped_to_server_and_does_not_share_mutable_values(self):
        from synthDrivers._ollamaSpeech import _catalogue
        models = {"phonon": ["default"]}
        _catalogue.remember("http://one:11434", models)
        models["phonon"].append("unverified")
        self.assertEqual(_catalogue.get("http://one:11434"), {"phonon": ["default"]})
        self.assertEqual(_catalogue.get("http://two:11434"), {})

    def test_new_model_setting_is_seeded_before_nvda_loads_old_configuration(self):
        with patch.dict(config_module.conf, {"speech": {"phonon": {"voice": "Zoey"}}}):
            def native_init(synth):
                self.assertEqual(config_module.conf["speech"]["phonon"]["model"], synth.model)
            with patch.object(BaseSynth, "initSettings", native_init):
                self.driver.initSettings()

    def test_profile_restores_model_before_voice(self):
        self.driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french"]})
        handler.getSynth = lambda: object()
        with patch.dict(config_module.conf, {"speech": {"phonon": {"model": "lfm3-audio", "voice": "female-french"}}}):
            def native_load(synth, onlyChanged):
                synth.voice = config_module.conf["speech"]["phonon"]["voice"]
            with patch.object(BaseSynth, "loadSettings", native_load):
                self.driver.loadSettings()
            self.assertEqual((self.driver.model, self.driver.voice), ("lfm3-audio", "female-french"))

    def test_failed_nvda_settings_initialization_closes_the_worker(self):
        with patch.dict(config_module.conf, {"speech": {"phonon": {}}}):
            with patch.object(BaseSynth, "initSettings", side_effect=RuntimeError("injected settings error")):
                with self.assertRaises(RuntimeError):
                    self.driver.initSettings()
        self.assertFalse(self.driver._worker.is_alive())
        self.assertTrue(self.driver._player.closed)

    def test_model_change_refreshes_dependent_gui_without_saving_over_cancel_state(self):
        self.driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french"]})
        scheduled, changed, updated, saved = [], [], [], []
        driver = self.driver
        class VoicePanel:
            def __init__(self):
                self._voices = list(driver.availableVoices.values())
                self._locales = list(driver.availableLocales.values())
                self.voiceList = types.SimpleNamespace(SetItems=lambda items: setattr(self, 'voiceItems', items))
                self.localeList = types.SimpleNamespace(SetItems=lambda items: setattr(self, 'localeItems', items))
            def IsBeingDeleted(self):
                return False
            def getSettings(self):
                return driver
            def updateDriverSettings(self, **kwargs):
                updated.append(kwargs)
        panel = VoicePanel()
        fake_gui = types.ModuleType("gui")
        fake_gui.settingsDialogs = types.SimpleNamespace(VoiceSettingsPanel=VoicePanel)
        fake_wx = types.SimpleNamespace(CallAfter=lambda fn, *args: scheduled.append((fn, args)),
            Window=types.SimpleNamespace(FindFocus=lambda: panel))
        driver.saveSettings = lambda: saved.append(True)
        handler.getSynth = lambda: driver
        handler.changeVoice = lambda synth, voice: changed.append(synth)
        with patch.dict(sys.modules, {"wx": fake_wx, "gui": fake_gui, "gui.settingsDialogs": fake_gui.settingsDialogs}):
            driver.model = "lfm3-audio"
            self.assertEqual(len(scheduled), 1)
            fn, args = scheduled.pop()
            fn(*args)
        self.assertEqual(changed, [driver])
        self.assertEqual(updated, [{"changedSetting": "model"}])
        self.assertEqual(saved, [], "Dialog Cancel must still be able to restore prior settings")
        self.assertEqual([v.id for v in panel._voices], list(driver.availableVoices))
        self.assertEqual([v.id for v in panel._locales], list(driver.availableLocales))
        self.assertEqual(panel.voiceItems, [v.displayName for v in driver.availableVoices.values()])

    def test_language_change_refreshes_the_open_dialog_voice_cache(self):
        handler.getSynth = lambda: object()
        driver = self.driver
        driver.reconfigure(Settings(model='phonon:40m-multilingual'), ['Maeve', 'Pippa', 'Zoey', 'Juniper'])
        driver.locale = 'en_GB'
        scheduled = []
        class VoicePanel:
            def __init__(self):
                self._voices = list(driver.availableVoices.values())
                self._locales = list(driver.availableLocales.values())
                self.voiceList = types.SimpleNamespace(SetItems=lambda items: setattr(self, 'voiceItems', items))
                self.localeList = types.SimpleNamespace(SetItems=lambda items: None)
            def IsBeingDeleted(self): return False
            def getSettings(self): return driver
            def updateDriverSettings(self, **kwargs): pass
        panel = VoicePanel()
        gui = types.ModuleType('gui')
        gui.settingsDialogs = types.SimpleNamespace(VoiceSettingsPanel=VoicePanel)
        wx = types.SimpleNamespace(CallAfter=lambda fn, *args: scheduled.append((fn, args)),
            Window=types.SimpleNamespace(FindFocus=lambda: panel))
        with patch.dict(sys.modules, {'wx': wx, 'gui': gui, 'gui.settingsDialogs': gui.settingsDialogs}), \
                patch.object(handler, 'getSynth', lambda: driver), \
                patch.object(handler, 'changeVoice', lambda *args: None, create=True):
            driver.locale = 'en_US'
            fn, args = scheduled.pop()
            fn(*args)
            self.assertEqual([v.id for v in panel._voices], ['Zoey', 'Juniper'])
            self.assertEqual(panel.voiceItems, ['Zoey', 'Juniper'])
            driver.voice = panel._voices[1].id
            self.assertEqual(driver.voice, 'Juniper')

    def test_model_change_outside_speech_dialog_saves_new_voice(self):
        driver = self.driver
        driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french"]})
        scheduled, saved = [], []
        driver.saveSettings = lambda: saved.append((driver.model, driver.voice))
        handler.getSynth = lambda: driver
        handler.changeVoice = lambda synth, voice: None
        fake_gui = types.ModuleType("gui")
        fake_gui.settingsDialogs = types.SimpleNamespace(VoiceSettingsPanel=type("VoicePanel", (), {}))
        fake_wx = types.SimpleNamespace(CallAfter=lambda fn, *args: scheduled.append((fn, args)),
            Window=types.SimpleNamespace(FindFocus=lambda: None))
        with patch.dict(sys.modules, {"wx": fake_wx, "gui": fake_gui, "gui.settingsDialogs": fake_gui.settingsDialogs}):
            driver.model = "lfm3-audio"
            fn, args = scheduled.pop()
            fn(*args)
        self.assertEqual(saved, [("lfm3-audio", "default")])

    def test_stale_saved_voice_falls_back_to_model_default(self):
        self.driver.voice = "voice from a removed model"
        self.assertEqual(self.driver.voice, "default")

    def test_language_filter_uses_only_voices_available_on_the_server(self):
        handler.getSynth = lambda: object()
        self.driver.reconfigure(Settings(model="phonon:40m-multilingual"), ["default", "Zoey", "Maeve", "Noémie", "New voice"])
        self.assertEqual(set(self.driver.availableLocales), {"all", "und", "en_US", "en_GB", "fr_FR"})
        self.driver.locale = "en_GB"
        self.assertEqual(list(self.driver.availableVoices), ["Maeve"])
        self.assertEqual(self.driver.voice, "Maeve")
        self.assertEqual(self.driver.language, "en_GB")
        self.driver.locale = "und"
        self.assertEqual(list(self.driver.availableVoices), ["default", "New voice"])
        self.driver.locale = "all"
        self.assertEqual(len(self.driver.availableVoices), 5)

    def test_model_switch_restores_language_and_voice_without_server_queries(self):
        handler.getSynth = lambda: object()
        self.driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french", "male-french"]})
        self.driver.model = "lfm3-audio"
        self.driver.locale = "fr"
        self.driver.voice = "male-french"
        self.driver.model = "phonon"
        self.assertEqual(self.driver.locale, "all")
        self.driver.model = "lfm3-audio"
        self.assertEqual((self.driver.locale, self.driver.voice), ("fr", "male-french"))
        self.driver.locale = "not-a-language"
        self.assertTrue(self.driver.availableVoices)
        self.assertIn(self.driver.voice, self.driver.availableVoices)

    def test_profile_restores_model_then_language_then_voice(self):
        handler.getSynth = lambda: object()
        self.driver.setModels({"phonon": ["default", "Zoey"], "lfm3-audio": ["default", "female-french", "male-french"]})
        saved = {"model": "lfm3-audio", "locale": "fr", "voice": "male-french"}
        with patch.dict(config_module.conf, {"speech": {"phonon": saved}}):
            def native_load(synth, onlyChanged):
                self.assertEqual(list(synth.availableVoices), ["female-french", "male-french"])
                synth.voice = saved["voice"]
            with patch.object(BaseSynth, "loadSettings", native_load):
                self.driver.loadSettings()
        self.assertEqual((self.driver.locale, self.driver.voice), ("fr", "male-french"))

    def test_old_config_seeds_language_before_nvda_settings_loader(self):
        with patch.dict(config_module.conf, {"speech": {"phonon": {"voice": "Zoey"}}}):
            def native_init(synth):
                self.assertEqual(config_module.conf["speech"]["phonon"]["locale"], "all")
            with patch.object(BaseSynth, "initSettings", native_init):
                self.driver.initSettings()

    def test_phonon_catalogue_handles_unicode_and_character_voices_without_guessing(self):
        from synthDrivers._ollamaSpeech._profiles import voice_language
        self.assertEqual(voice_language("phonon:40m-multilingual", "Noe\u0301mie"), "fr_FR")
        self.assertEqual(voice_language("phonon:40m-multilingual", "Fairy Godmother"), "en")
        self.assertIsNone(voice_language("phonon:40m-multilingual", "default"))
        self.assertIsNone(voice_language("phonon:40m-multilingual", "Uncatalogued"))

    def test_phonon_catalogue_contains_the_complete_pinned_metadata_snapshot(self):
        from synthDrivers._ollamaSpeech._voice_languages import LANGUAGES, GROUPS
        self.assertEqual(len(LANGUAGES), 102)
        self.assertEqual({key: len(value) for key, value in GROUPS.items()}, {
            "en": 12, "en_GB": 13, "en_US": 12, "de_DE": 13,
            "es_ES": 13, "es_MX": 12, "fr_FR": 15, "pt_BR": 12})

    def test_character_group_has_a_distinct_label_without_changing_its_language(self):
        handler.getSynth = lambda: object()
        self.driver.reconfigure(Settings(model="phonon:40m-multilingual"), ["default", "Fairy Godmother", "Zoey"])
        self.assertEqual(self.driver.availableLocales["en"].displayName, "English Character Voices")
        self.driver.locale = "en"
        self.assertEqual(list(self.driver.availableVoices), ["Fairy Godmother"])
        self.assertEqual(self.driver.language, "en")
        self.driver.reconfigure(Settings(model="phonon"), ["default", "Zoey"])
        self.assertNotEqual(self.driver.availableLocales["en"].displayName, "English Character Voices")

    def test_disappearing_language_group_cannot_leave_an_empty_voice_list(self):
        handler.getSynth = lambda: object()
        self.driver.reconfigure(Settings(model="lfm3-audio"), ["default", "female-french"])
        self.driver.locale = "fr"
        self.driver.reconfigure(Settings(model="lfm3-audio"), ["default", "male-german"])
        self.assertEqual(self.driver.locale, "all")
        self.assertIn(self.driver.voice, self.driver.availableVoices)

    def test_pause_and_stale_callbacks(self):
        self.driver.speak(["hello", commands.IndexCommand(5)])
        self.settle()
        callbacks = list(self.driver._player.pending)
        self.driver.pause(True)
        self.assertTrue(self.driver._player.paused)
        self.driver.pause(False)
        self.driver.cancel()
        for callback in callbacks:
            callback()
        self.assertEqual(index_notice.items, [])
        self.assertEqual(done_notice.items, [])

    def test_rapid_interruptions_keep_one_worker(self):
        for i in range(100):
            self.driver.cancel()
            self.driver.speak([str(i), commands.IndexCommand(i)])
        self.settle()
        self.driver._player.drain()
        self.assertEqual(index_notice.items[-1]["index"], 99)
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "Ollama speech"]), 1)

    def test_fallback_only_if_still_active(self):
        calls = []
        handler.getSynth = lambda: object()
        handler.setSynth = lambda name: calls.append(name) or True
        self.driver._useFallback()
        self.assertEqual(calls, [])

    def test_failure_uses_main_thread_fallback_once(self):
        scheduled, switches, messages = [], [], []
        self.driver._client.fail = True
        fake_wx = types.SimpleNamespace(CallAfter=lambda callback: scheduled.append(callback))
        with patch.dict(sys.modules, {"wx": fake_wx}):
            self.driver.speak(["test"])
            self.settle()
            self.assertEqual(len(scheduled), 1)
            self.driver.speak(["queued after failure"])
            self.settle()
            self.assertEqual(len(self.driver._client.requests), 1)
        handler.getSynth = lambda: self.driver
        handler.setSynth = lambda name: switches.append(name) or True
        with patch.dict(sys.modules, {"ui": types.SimpleNamespace(message=messages.append)}):
            scheduled[0]()
        self.assertEqual(switches, ["espeak"])
        self.assertEqual(len(messages), 1)

    def test_missing_model_on_load_recovers_via_main_thread(self):
        self.driver.terminate()
        scheduled = []
        with patch.object(FakeClient, "voices", side_effect=ServerError("HTTP 404")):
            self.driver = phonon.SynthDriver()
        with patch.dict(sys.modules, {"wx": types.SimpleNamespace(CallAfter=lambda callback: scheduled.append(callback))}):
            self.driver.speak(["initial speech"])
            self.settle()
        self.assertTrue(self.driver._unavailable)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(self.driver._client.requests, [])


class NativeAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import synthDrivers
        synthDrivers.__path__.append(r"C:\Program Files\NVDA\library.zip\synthDrivers")
        sys.path.append(r"C:\Program Files\NVDA\library.zip")

    def test_actual_configobj_settings_round_trip(self):
        import configobj
        class RealConf(configobj.ConfigObj):
            spec = {}
        conf = RealConf({"phononConnection": {"address": "http://localhost:11434", "model": "phonon", "fallback": True}})
        with patch.object(config_module, "conf", conf):
            chosen = Settings("https://example.invalid:11434", "phonon:fr", False)
            phonon._settings.save(chosen)
            self.assertEqual(phonon._settings.load(), chosen)

    def test_save_supports_nvda_aggregated_section_without_update(self):
        class Section:
            def __init__(self):
                self.values = {"address": "http://localhost:11434", "model": "phonon", "fallback": True}
            def __getitem__(self, key):
                return self.values[key]
            def __setitem__(self, key, value):
                self.values[key] = value
        conf = Config(phononConnection=Section())
        with patch.object(config_module, "conf", conf):
            chosen = Settings("https://example.invalid:11434", "lfm3-audio", False)
            phonon._settings.save(chosen)
            self.assertEqual(phonon._settings.load(), chosen)

    def test_identity_pcm(self):
        data = struct.pack("<4h", -32768, 0, 100, 32767)
        processor = Processor(50, 50, 100, False)
        self.assertEqual(processor.process(data), data)
        self.assertEqual(processor.finish(), b"")

    def test_actual_nvda_sonic_rate_pitch_and_volume(self):
        import math
        data = struct.pack("<24000h", *(int(10000 * math.sin(2 * math.pi * 220 * i / 24000)) for i in range(24000)))
        lengths = []
        for rate, pitch, volume, boost in [(50, 50, 50, False), (100, 50, 100, False), (50, 75, 100, False), (50, 50, 100, True), (50, 50, 0, False)]:
            processor = Processor(rate, pitch, volume, boost)
            output = b"".join(processor.process(data[i:i+2048]) for i in range(0, len(data), 2048)) + processor.finish()
            self.assertEqual(len(output) % 2, 0)
            self.assertTrue(output)
            peak = max(abs(x) for x in struct.unpack("<%dh" % (len(output)//2), output))
            if volume == 50:
                self.assertLessEqual(peak, 5050)
            if volume == 0:
                self.assertEqual(peak, 0)
            lengths.append(len(output))
        self.assertLess(lengths[1], lengths[0] * 0.7)
        self.assertLess(lengths[3], lengths[0] * 0.7)


if __name__ == "__main__":
    unittest.main()
