# SPDX-License-Identifier: GPL-2.0-or-later
"""Model-specific capabilities; new families require explicit playback testing."""

from ._sequence import language
from ._voice_languages import LANGUAGES, normalise

VERIFIED_FAMILIES = frozenset({"phonon", "lfm3-audio"})
LFM_LANGUAGES = {
    "american-english": "en_US", "british-english": "en_GB", "arabic": "ar",
    "chinese": "zh", "danish": "da", "dutch": "nl", "finnish": "fi",
    "french": "fr", "german": "de", "greek": "el", "hebrew": "he",
    "italian": "it", "japanese": "ja", "korean": "ko", "polish": "pl",
    "portuguese": "pt", "spanish": "es", "swedish": "sv", "turkish": "tr",
}


def family(model):
    return model.split(":", 1)[0].rsplit("/", 1)[-1].lower()


def model_key(model):
    return model.removesuffix(":latest")


def require_supported(model):
    if family(model) not in VERIFIED_FAMILIES:
        raise ValueError("Select a tested speech model: Phonon or LFM3-Audio.")


def voice_language(model, voice):
    if family(model) == "lfm3-audio":
        return LFM_LANGUAGES.get(voice.removeprefix("female-").removeprefix("male-"))
    if "multilingual" in model.lower():
        return LANGUAGES.get(normalise(voice))
    return language(model)
