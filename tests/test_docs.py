# SPDX-License-Identifier: GPL-2.0-or-later
"""Keep the installed manual and repository README aligned."""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

from test_ollama_speech import ADDON


class Manual(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.headings = []
        self.text = []
        self._heading = False
        self._body = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._body = True
        if tag == "h2":
            self._heading = True

    def handle_endtag(self, tag):
        if tag == "body":
            self._body = False
        if tag == "h2":
            self._heading = False

    def handle_data(self, data):
        if not self._body:
            return
        self.text.append(data)
        if self._heading:
            self.headings.append(data)


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        self.readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.manual = Manual((ADDON / "doc" / "en" / "readme.html").read_text(encoding="utf-8"))

    def test_changelog_is_first_and_not_duplicated(self):
        headings = re.findall(r"^## (.+)$", self.readme, re.MULTILINE)
        self.assertEqual(headings, self.manual.headings)
        self.assertEqual(headings[0], "Changelog")
        self.assertEqual(headings.count("Changelog"), 1)
        self.assertNotIn("Changes", headings)

    def test_download_commands_and_credits_match(self):
        text = " ".join(self.manual.text)
        for value in ("ollama pull phonon", "ollama pull phonon:40m-multilingual", "ollama pull lfm3-audio",
                      "Andre Louis: creator and maintainer", "OpenAI Codex: development, testing and documentation assistance"):
            self.assertIn(value, self.readme)
            self.assertIn(value, text)

    def test_language_handling_is_distinguished_from_voice_preset_selection(self):
        for text in (self.readme, " ".join(self.manual.text)):
            self.assertIn("LFM3-Audio can read mixed-language passages", text)
            self.assertIn("does not translate text", text)
            self.assertNotIn("Automatic language-specific voice selection is not claimed", text)
            self.assertNotIn("passed exact-reading and cancellation checks", text)

    def test_complete_manual_text_matches_readme(self):
        text = re.sub(r"(?m)^```[^\n]*\n?", "", self.readme)
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"(?m)^\d+\. ", "", text)
        actual = re.sub(r"\bVersion \d+\.\d+(?:\.\d+)?\.", "", " ".join(self.manual.text), count=1)
        self.assertEqual(re.findall(r"\w+", text), re.findall(r"\w+", actual))


if __name__ == "__main__":
    unittest.main()
