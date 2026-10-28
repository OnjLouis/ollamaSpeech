# SPDX-License-Identifier: GPL-2.0-or-later
"""Session-only, server-scoped metadata shared by discovery and the driver."""
import threading
from ._profiles import model_key

_lock = threading.Lock()
_models = {}


def remember(address, models):
    with _lock:
        _models[address] = {model_key(name): tuple(voices) for name, voices in models.items()}


def get(address):
    with _lock:
        return {name: list(voices) for name, voices in _models.get(address, {}).items()}


def clear():
    with _lock:
        _models.clear()
