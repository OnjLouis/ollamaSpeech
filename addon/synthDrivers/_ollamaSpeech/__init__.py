# SPDX-License-Identifier: GPL-2.0-or-later
"""Ollama Speech with NVDA audio and playback-timed notifications."""

from collections import OrderedDict
import queue
import threading

import config
import globalVars
import nvwave
from autoSettingsUtils.driverSetting import DriverSetting
from autoSettingsUtils.utils import StringParameterInfo
from logHandler import log
from speech.commands import BreakCommand, CharacterModeCommand, IndexCommand, PitchCommand
from synthDriverHandler import SynthDriver as BaseSynth, VoiceInfo, synthDoneSpeaking, synthIndexReached

from . import _settings, _catalogue
from ._audio import Processor, percent
from ._client import Cancelled, Client, ServerError, SAMPLE_RATE, Settings
from ._sequence import events
from ._profiles import require_supported, voice_language, model_key, family


class SynthDriver(BaseSynth):
    # Keep the persisted synth ID for existing profiles; implementation and add-on
    # identity are Ollama Speech. The public entry point is a compatibility shim.
    name = "phonon"
    description = "Ollama Speech"
    supportedSettings = (
        DriverSetting("model", "M&odel", availableInSettingsRing=True),
        DriverSetting("locale", "Language &filter", availableInSettingsRing=True, defaultVal="all"),
        BaseSynth.VoiceSetting(), BaseSynth.RateSetting(), BaseSynth.RateBoostSetting(),
        BaseSynth.PitchSetting(), BaseSynth.VolumeSetting(),
    )
    supportedCommands = {IndexCommand, PitchCommand, BreakCommand, CharacterModeCommand}
    supportedNotifications = {synthIndexReached, synthDoneSpeaking}

    @classmethod
    def check(cls):
        return not getattr(globalVars.appArgs, "secure", False)

    def __init__(self):
        if not self.check():
            raise RuntimeError("Network speech is not available on secure screens.")
        super().__init__()
        self._connection = _settings.load()
        self._connection = Settings(self._connection.address, model_key(self._connection.model), self._connection.fallback)
        require_supported(self._connection.model)
        self._client = Client(self._connection)
        self._epoch = threading.Event()
        self._unavailable = False
        try:
            names = self._client.voices(self._epoch)
        except ServerError:
            if not self._connection.fallback:
                raise
            # Let NVDA finish assigning the driver before scheduling access recovery.
            names = ["default"]
            self._unavailable = True
            log.warning("Ollama Speech saved model is unavailable; eSpeak recovery will follow on speech.")
        self._voices = self._voice_infos(self._connection, names)
        self._models = _catalogue.get(self._connection.address)
        if not self._unavailable:
            self._models[self._connection.model] = list(names)
        self._voice = "default" if "default" in self._voices else names[0]
        self._modelVoices = {}
        self._modelLanguages = {}
        self._locale = "all"
        self._rate, self._pitch, self._volume, self._rateBoost = 50, 50, 100, False
        self._lock = threading.Lock()
        self._control = threading.RLock()
        self._stopping = threading.Event()
        self._fallbackPending = threading.Event()
        self._latest_job = 0
        self._jobs = queue.Queue()
        self._player = nvwave.WavePlayer(1, SAMPLE_RATE, 16, outputDevice=config.conf["audio"]["outputDevice"])
        self._worker = threading.Thread(target=self._run, name="Ollama speech", daemon=True)
        self._worker.start()
        if not self._unavailable:
            log.info("Ollama Speech 1.0 connected; %d voices available.", len(names))

    def _get_serverAddress(self):
        return self._connection.address

    def _get_model(self):
        return self._connection.model

    def _get_availableModels(self):
        names = list(self._models)
        if self.model not in names:
            names.insert(0, self.model)
        return OrderedDict((name, StringParameterInfo(name, name)) for name in names)

    def setModels(self, models):
        """Replace discovery metadata without changing the current speaking model."""
        self._models = {model_key(name): list(voices) for name, voices in models.items()}
        self.invalidateCache()

    def _set_model(self, value):
        value = model_key(value) if value else value
        if not value or value == self.model:
            return
        names = self._models.get(value)
        if not names:
            return
        self.reconfigure(Settings(self.serverAddress, value, self._connection.fallback), names)
        self.refreshControls(value)

    def refreshControls(self, model=None, changedSetting="model"):
        """Refresh only the active synth's NVDA controls, never a render instance."""
        import synthDriverHandler
        if synthDriverHandler.getSynth() is not self:
            return
        import wx
        wx.CallAfter(self._refreshControls, model, changedSetting)

    def _refreshControls(self, model, changedSetting="model"):
        import synthDriverHandler
        if self._stopping.is_set() or synthDriverHandler.getSynth() is not self or (model is not None and self.model != model):
            return
        import gui.settingsDialogs
        import wx
        synthDriverHandler.changeVoice(self, None)
        # NVDA refreshes dependent controls for voice changes, but not custom model settings.
        window = wx.Window.FindFocus()
        while window is not None:
            if isinstance(window, gui.settingsDialogs.VoiceSettingsPanel):
                if not window.IsBeingDeleted() and window.getSettings() is self:
                    # Generic NVDA refreshes selections, not dependent choice lists.
                    for name, options in (("locale", self.availableLocales), ("voice", self.availableVoices)):
                        values = list(options.values())
                        cached = getattr(window, "_%ss" % name)
                        if [(v.id, v.displayName) for v in cached] != [(v.id, v.displayName) for v in values]:
                            setattr(window, "_%ss" % name, values)
                            getattr(window, "%sList" % name).SetItems([v.displayName for v in values])
                    window.updateDriverSettings(changedSetting=changedSetting)
                return
            window = window.GetParent()
        if model is not None:
            self.saveSettings()

    @staticmethod
    def _voice_infos(settings, names):
        return OrderedDict((name, VoiceInfo(name, name, language=voice_language(settings.model, name))) for name in names)

    def initSettings(self):
        try:
            saved = config.conf["speech"][self.name]
        except KeyError:
            pass
        else:
            # NVDA's synth loader indexes new settings directly, unlike AutoSettings.
            if saved.get("model") is None:
                saved["model"] = self.model
            if saved.get("locale") is None:
                saved["locale"] = "all"
        try:
            super().initSettings()
        except Exception:
            self.terminate()
            raise

    def loadSettings(self, onlyChanged=False):
        saved = config.conf["speech"][self.name]
        model = saved.get("model")
        if model:
            # A restored voice must be interpreted against its model, not the old list.
            self.model = model
        self.locale = saved.get("locale") or "all"
        super().loadSettings(onlyChanged)

    def reconfigure(self, settings, names):
        """Apply already-probed connection metadata on NVDA's main thread."""
        require_supported(settings.model)
        if not names:
            raise ValueError("The selected model has no voices.")
        client = Client(settings)
        voices = self._voice_infos(settings, names)
        with self._control:
            self.cancel()
            self._modelVoices[(self._connection.address, model_key(self._connection.model))] = self._voice
            self._modelLanguages[(self._connection.address, model_key(self._connection.model))] = self._locale
            self._connection, self._client, self._voices = settings, client, voices
            previous_language = self._modelLanguages.get((settings.address, model_key(settings.model)), "all")
            self._locale = previous_language if previous_language in self._get_availableLocales() else "all"
            self._unavailable = False
            previous = self._modelVoices.get((settings.address, model_key(settings.model)))
            available = self._get_availableVoices()
            self._voice = previous if previous in available else ("default" if "default" in available else next(iter(available)))
            self._fallbackPending.clear()
            self.invalidateCache()

    def _get_availableVoices(self):
        if self._locale == "all":
            return self._voices
        return OrderedDict((name, info) for name, info in self._voices.items()
            if (voice_language(self.model, name) or "und") == self._locale)

    def _get_availableLocales(self):
        try:
            from languageHandler import getLanguageDescription
        except ImportError:
            getLanguageDescription = lambda code: code
        codes = {voice_language(self.model, name) or "und" for name in self._voices}
        labels = {code: ("Unspecified language" if code == "und" else getLanguageDescription(code) or code) for code in codes}
        if "en" in codes and family(self.model) == "phonon" and "multilingual" in self.model.lower():
            labels["en"] = "English Character Voices"
        values = OrderedDict([("all", StringParameterInfo("all", "All languages"))])
        values.update((code, StringParameterInfo(code, label)) for code, label in sorted(labels.items(), key=lambda item: item[1].casefold()))
        return values

    def _get_locale(self):
        return self._locale

    def _set_locale(self, value):
        value = value if value in self._get_availableLocales() else "all"
        if value == self._locale:
            return
        self.cancel()
        self._locale = value
        self.invalidateCache()
        self.voice = self._voice
        self.refreshControls(self.model, changedSetting="locale")

    def _get_voice(self):
        return self._voice

    def _set_voice(self, value):
        voices = self._get_availableVoices()
        if value not in voices:
            # NVDA can restore a voice belonging to the previously selected model.
            value = "default" if "default" in voices else next(iter(voices))
        self._voice = value

    def _get_language(self):
        return voice_language(self._connection.model, self._voice)

    def _get_rate(self):
        return self._rate

    def _set_rate(self, value):
        self._rate = percent(value)

    def _get_pitch(self):
        return self._pitch

    def _set_pitch(self, value):
        self._pitch = percent(value)

    def _get_volume(self):
        return self._volume

    def _set_volume(self, value):
        self._volume = percent(value)

    def _get_rateBoost(self):
        return self._rateBoost

    def _set_rateBoost(self, value):
        self._rateBoost = bool(value)

    def _current(self, epoch):
        return not epoch.is_set() and not self._stopping.is_set()

    def speak(self, speechSequence):
        prepared = events(speechSequence, self._pitch)
        with self._control:
            with self._lock:
                if self._stopping.is_set():
                    return
                self._latest_job += 1
                settings = (self._voice, self._rate, self._pitch, self._volume, self._rateBoost)
                self._jobs.put((self._epoch, self._latest_job, settings, prepared))

    def cancel(self):
        with self._control:
            with self._lock:
                epoch = self._epoch
                epoch.set()
                self._epoch = threading.Event()
                try:
                    while True:
                        self._jobs.get_nowait()
                        self._jobs.task_done()
                except queue.Empty:
                    pass
            self._client.abort(epoch)
            self._player.stop()

    def pause(self, switch):
        self._player.pause(switch)

    def _feed(self, epoch, data=None, callback=None):
        if not self._current(epoch):
            return False
        self._player.feed(data, None if data is not None else 0, onDone=callback)
        # A native feed can block across cancellation; stop stale audio before the next job.
        if not self._current(epoch):
            self._player.stop()
            return False
        return True

    def _index(self, epoch, index):
        if self._current(epoch):
            synthIndexReached.notify(synth=self, index=index)

    def _done(self, epoch, job):
        with self._lock:
            current = self._current(epoch) and job == self._latest_job
        if current:
            synthDoneSpeaking.notify(synth=self)

    def _render(self, epoch, job, settings, prepared):
        if self._unavailable:
            raise ServerError("The saved speech model is unavailable. Refresh models in Ollama Speech settings.")
        voice, rate, pitch, volume, boost = settings
        for kind, value in prepared:
            if not self._current(epoch):
                return
            if kind == "pitch":
                pitch = percent(value)
            elif kind == "index":
                self._feed(epoch, callback=lambda index=value: self._index(epoch, index))
            elif kind == "break":
                self._feed(epoch, b"\0\0" * (SAMPLE_RATE * value // 1000))
            elif kind == "text":
                processor = Processor(rate, pitch, volume, boost)
                stream = self._client.audio(value, voice, epoch)
                try:
                    for data in stream:
                        if not self._current(epoch):
                            return
                        audio = processor.process(data)
                        if audio and not self._feed(epoch, audio):
                            return
                    if self._current(epoch):
                        tail = processor.finish()
                        if tail:
                            self._feed(epoch, tail)
                finally:
                    stream.close()
        self._feed(epoch, callback=lambda: self._done(epoch, job))

    def _failure(self):
        if not self._connection.fallback or self._fallbackPending.is_set():
            return
        self._fallbackPending.set()
        import wx
        wx.CallAfter(self._useFallback)

    def _useFallback(self):
        import synthDriverHandler
        if self._stopping.is_set() or synthDriverHandler.getSynth() is not self:
            return
        log.warning("Ollama Speech failed; switching to eSpeak NG for continued access.")
        if synthDriverHandler.setSynth("espeak"):
            import ui
            ui.message("Ollama Speech is unavailable. Switched to eSpeak NG.")

    def _run(self):
        try:
            self._work()
        finally:
            self._player.close()

    def _work(self):
        while True:
            job = self._jobs.get()
            try:
                if job is None:
                    return
                if self._current(job[0]) and not self._fallbackPending.is_set():
                    try:
                        self._render(*job)
                    except Cancelled:
                        pass
                    except Exception as error:
                        if self._current(job[0]):
                            # Log the class only, not speech, server bodies or network credentials.
                            detail = str(error) if isinstance(error, ServerError) else type(error).__name__
                            log.warning("Ollama Speech request failed: %s", detail)
                            self._feed(job[0], callback=lambda epoch=job[0], number=job[1]: self._done(epoch, number))
                            self._failure()
            finally:
                self._jobs.task_done()
            if self._jobs.empty():
                try:
                    self._player.idle()
                except Exception:
                    log.warning("Ollama Speech audio drain failed.")
                    self._player.stop()

    def terminate(self):
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.cancel()
        self._jobs.put(None)
        self._worker.join(timeout=1)
        if self._worker.is_alive():
            log.warning("Ollama Speech worker is still finishing a bounded network operation.")
        super().terminate()
