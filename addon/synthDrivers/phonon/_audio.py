# SPDX-License-Identifier: GPL-2.0-or-later
from ctypes import c_short

from ._client import SAMPLE_RATE


def percent(value):
    return max(0, min(100, int(value)))


class Processor:
    """Use NVDA's bundled Sonic, retaining its state across incoming PCM blocks."""
    def __init__(self, rate, pitch, volume, boost):
        self._mute = percent(volume) == 0
        speed = 2 ** ((percent(rate) - 50) / 50)
        if boost:
            speed *= 2
        pitch_factor = 2 ** ((percent(pitch) - 50) / 100)
        self._stream = None
        if speed != 1 or pitch_factor != 1 or volume != 100:
            from synthDrivers import _sonic
            _sonic.initialize()
            self._stream = _sonic.SonicStream(SAMPLE_RATE, 1)
            self._stream.speed = speed
            self._stream.pitch = pitch_factor
            self._stream.volume = percent(volume) / 100

    def process(self, data):
        if self._stream is None:
            return data
        samples = (c_short * (len(data) // 2)).from_buffer_copy(data)
        self._stream.writeShort(samples, len(samples))
        output = bytes(self._stream.readShort())
        return bytes(len(output)) if self._mute else output

    def finish(self):
        if self._stream is None:
            return b""
        self._stream.flush()
        output = bytes(self._stream.readShort())
        return bytes(len(output)) if self._mute else output
