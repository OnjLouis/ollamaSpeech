# SPDX-License-Identifier: GPL-2.0-or-later
import config
from ._client import Settings

SECTION = "phononConnection"
SPEC = {
    "address": "string(default='http://localhost:11434')",
    "model": "string(default='phonon')",
    "fallback": "boolean(default=True)",
}
UPDATE_SECTION = "ollamaSpeechUpdates"
UPDATE_SPEC = {"interval": "option('never', 'hourly', 'daily', default='daily')"}


def update_interval():
    config.conf.spec[UPDATE_SECTION] = UPDATE_SPEC
    return config.conf[UPDATE_SECTION]["interval"]


def save_interval(interval):
    if interval not in ("never", "hourly", "daily"):
        raise ValueError("Unknown update interval.")
    config.conf.spec[UPDATE_SECTION] = UPDATE_SPEC
    config.conf[UPDATE_SECTION]["interval"] = interval


def load():
    config.conf.spec[SECTION] = SPEC
    values = config.conf[SECTION]
    try:
        model = config.conf["speech"]["phonon"].get("model") or values["model"]
    except KeyError:
        model = values["model"]
    return Settings(values["address"].strip(), model.strip(), values["fallback"])


def save(settings):
    config.conf.spec[SECTION] = SPEC
    values = config.conf[SECTION]
    for key, value in (("address", settings.address), ("model", settings.model), ("fallback", settings.fallback)):
        values[key] = value
    try:
        config.conf["speech"]["phonon"]["model"] = settings.model
    except KeyError:
        pass
