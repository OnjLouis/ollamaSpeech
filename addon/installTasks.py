# SPDX-License-Identifier: GPL-2.0-or-later
"""Retire the former add-on identity through NVDA's normal pending-removal API."""
import addonHandler


def onInstall():
    current = addonHandler.getCodeAddon()
    if current is None:
        raise RuntimeError("NVDA could not identify the Ollama Speech installation.")
    if current.name != "ollamaSpeech":
        return
    for addon in addonHandler.getAvailableAddons():
        if addon.name != "phonon":
            continue
        if addon.manifest.get("url") != "https://github.com/OnjLouis/ollamaSpeech":
            raise RuntimeError("The old phonon add-on is not the expected Ollama Speech installation.")
        if getattr(addon, "isPendingInstall", False):
            raise RuntimeError("Restart NVDA to finish the previous Ollama Speech update first.")
        if not getattr(addon, "isPendingRemove", False):
            addon.requestRemove()
