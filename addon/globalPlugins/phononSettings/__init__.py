# SPDX-License-Identifier: GPL-2.0-or-later
import os
import threading

import addonHandler
import globalPluginHandler
import globalVars
import gui
import gui.guiHelper
import gui.settingsDialogs
import synthDriverHandler
import wx

from synthDrivers.phonon import _settings, _catalogue
from synthDrivers.phonon._client import Cancelled, Client, ServerError, Settings, validate
from synthDrivers.phonon._profiles import family, model_key
from ._updater import GitHubReleaseUpdater

addonHandler.initTranslation()
_activeUpdater = None
_INTERVALS = ("never", "hourly", "daily")


def _remember(address, models):
    _catalogue.remember(address, models)
    active = synthDriverHandler.getSynth()
    if getattr(active, "name", None) == "phonon" and active.serverAddress == address:
        active.setModels(models)
        active.refreshControls()


def _preferred(models, current):
    current = model_key(current)
    if current in models:
        return current
    return next((name for name in models if family(name) == "phonon"), next(iter(models), current))


class PhononSettingsPanel(gui.settingsDialogs.SettingsPanel):
    title = _("Ollama Speech")

    def makeSettings(self, settingsSizer):
        current = _settings.load()
        self._probe = self._probeCancel = None
        self._queriedAddress = None
        self._models = {}
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
        self.address = helper.addLabeledControl(_("Server &address:"), wx.TextCtrl)
        self.address.SetName(_("Server address"))
        self.address.SetValue(current.address)
        self.check = helper.addItem(wx.Button(self, label=_("&Refresh models")))
        self.check.Bind(wx.EVT_BUTTON, self.onTest)
        self.use = helper.addItem(wx.Button(self, label=_("Use &Ollama Speech")))
        self.use.Bind(wx.EVT_BUTTON, self.onUse)
        self.use.Disable()
        self.fallback = helper.addItem(wx.CheckBox(self, label=_("Use &eSpeak NG if the speech server fails")))
        self.fallback.SetValue(current.fallback)
        self.status = helper.addLabeledControl(_("Connection &status:"), wx.TextCtrl, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName(_("Connection status"))
        self.status.SetValue(_("Discovering installed speech models..."))
        self.interval = helper.addLabeledControl(_("Check for &updates:"), wx.Choice,
                                               choices=[_("Never"), _("Hourly"), _("Daily")])
        self.interval.SetName(_("Check for updates"))
        self.interval.SetSelection(_INTERVALS.index(_settings.update_interval()))
        self.update = helper.addItem(wx.Button(self, label=_("&Check for updates now")))
        self.update.Bind(wx.EVT_BUTTON, self.onUpdate)
        self.help = helper.addItem(wx.Button(self, label=_("&Help")))
        self.help.Bind(wx.EVT_BUTTON, self.onHelp)
        self.Bind(wx.EVT_WINDOW_DESTROY, self.onDestroy)
        wx.CallAfter(self.onTest, None)

    def _values(self):
        current = _settings.load()
        address = self.address.GetValue().strip()
        active = synthDriverHandler.getSynth()
        model = active.model if getattr(active, "name", None) == "phonon" else current.model
        models = self._models if address == self._queriedAddress else {}
        return Settings(address, _preferred(models, model), self.fallback.GetValue())

    def isValid(self):
        try:
            settings = self._values()
            validate(settings)
            if settings.address != _settings.load().address and settings.address != self._queriedAddress:
                raise ValueError(_("Refresh models to verify the new server before saving."))
            if settings.address != _settings.load().address and not self._models:
                raise ValueError(_("No usable speech models were found on the new server."))
        except ValueError as error:
            gui.messageBox(str(error), _("Ollama Speech settings"), wx.OK | wx.ICON_ERROR)
            return False
        return True

    def _apply(self, activate=False):
        settings = self._values()
        current = _settings.load()
        active = synthDriverHandler.getSynth()
        models = self._models if settings.address == self._queriedAddress else {}
        if activate and not models:
            raise ValueError("Refresh models to find a usable speech model first.")
        _settings.save(settings)
        try:
            if getattr(active, "name", None) == "phonon":
                if settings != current:
                    names = models.get(settings.model) or list(active.availableVoices)
                    active.reconfigure(settings, names)
                if models:
                    active.setModels(models)
                active.refreshControls(settings.model)
            elif activate and not synthDriverHandler.setSynth("phonon"):
                raise RuntimeError("Ollama Speech could not be loaded.")
        except Exception:
            _settings.save(current)
            raise

    def onSave(self):
        self._apply()
        interval = _INTERVALS[self.interval.GetSelection()]
        _settings.save_interval(interval)
        if _activeUpdater is not None:
            _activeUpdater.setInterval(interval)

    def onUse(self, event):
        if not self.isValid():
            return
        try:
            self._apply(activate=True)
            self.status.SetValue(_("Ollama Speech is active. Choose Model and Voice in Speech settings or the synth settings ring."))
        except Exception:
            self.status.SetValue(_("Could not activate Ollama Speech. Your previous connection is saved; check the NVDA log."))
            from logHandler import log
            log.warning("Ollama Speech activation failed.")
        self.status.SetFocus()

    def onUpdate(self, event):
        if _activeUpdater is not None:
            _activeUpdater.checkNow()

    def onHelp(self, event):
        addon = addonHandler.getCodeAddon()
        path = addon.getDocFilePath() if addon is not None else None
        if path:
            os.startfile(path)
        else:
            gui.messageBox(_("The installed manual could not be found."), _("Ollama Speech help"), wx.OK | wx.ICON_ERROR)

    def onTest(self, event):
        if not self or self.IsBeingDeleted():
            return
        try:
            settings = self._values()
            validate(settings)
        except ValueError as error:
            self.status.SetValue(str(error))
            return
        if self._probeCancel is not None:
            self._probeCancel.set()
            self._probe.abort(self._probeCancel)
        client, cancel = Client(settings), threading.Event()
        self._probe, self._probeCancel = client, cancel
        self.use.Disable()
        self.status.SetValue(_("Discovering installed speech models..."))

        def probe():
            try:
                models = client.models(cancel)
                result = _("Found {count} installed speech models.").format(count=len(models))
            except Cancelled:
                return
            except ServerError as error:
                models, result = {}, _("Connection failed: {reason}").format(reason=str(error))
            except Exception:
                models, result = {}, _("Connection failed. Check the server address and server availability.")
            if not cancel.is_set():
                wx.CallAfter(self._showResult, cancel, settings, models, result, event is not None)

        threading.Thread(target=probe, name="Ollama model discovery", daemon=True).start()

    def _showResult(self, cancel, settings, models, result, focus=False):
        if cancel.is_set() or cancel is not self._probeCancel or not self or self.IsBeingDeleted():
            return
        self._probe, self._probeCancel = None, None
        if self.address.GetValue().strip() != settings.address:
            return
        _remember(settings.address, models)
        self._models, self._queriedAddress = _catalogue.get(settings.address), settings.address
        self.use.Enable(bool(models))
        self.status.SetValue(result)
        if focus:
            self.status.SetFocus()

    def onDestroy(self, event):
        if event.GetEventObject() is self and self._probeCancel is not None:
            self._probeCancel.set()
            self._probe.abort(self._probeCancel)
        event.Skip()


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    def __init__(self):
        global _activeUpdater
        super().__init__()
        self._registered = False
        self._updater = self._client = None
        self._cancel = threading.Event()
        if getattr(globalVars.appArgs, "secure", False):
            return
        settings = _settings.load()
        _settings.update_interval()
        self._updater = GitHubReleaseUpdater("phonon", "Ollama Speech", "OnjLouis", "ollamaSpeech")
        _activeUpdater = self._updater
        self._updater.start()
        categories = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
        if PhononSettingsPanel not in categories:
            categories.append(PhononSettingsPanel)
        self._registered = True
        try:
            self._client = Client(settings)
        except ValueError:
            return
        self._discovery = threading.Thread(target=self._discover, args=(settings,), name="Ollama startup discovery", daemon=True)
        self._discovery.start()

    def _discover(self, settings):
        try:
            models = self._client.models(self._cancel)
        except (Cancelled, ServerError):
            return
        if not self._cancel.is_set():
            wx.CallAfter(self._discovered, settings.address, models)

    def _discovered(self, address, models):
        if not self._cancel.is_set():
            _remember(address, models)

    def terminate(self):
        global _activeUpdater
        self._cancel.set()
        if self._client is not None:
            self._client.abort(self._cancel)
            self._discovery.join(timeout=0.2)
        if self._updater is not None:
            self._updater.stop()
            if _activeUpdater is self._updater:
                _activeUpdater = None
        if self._registered:
            categories = gui.settingsDialogs.NVDASettingsDialog.categoryClasses
            if PhononSettingsPanel in categories:
                categories.remove(PhononSettingsPanel)
        super().terminate()
