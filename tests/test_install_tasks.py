# SPDX-License-Identifier: GPL-2.0-or-later
"""Identity migration never edits configuration or removes unrelated add-ons."""
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch

import test_ollama_speech as fixture


class MigrationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('migration_install_tasks', fixture.ADDON / 'installTasks.py')
        self.module = importlib.util.module_from_spec(spec)
        self.removed = []
        self.old = types.SimpleNamespace(name='phonon', isPendingInstall=False, isPendingRemove=False,
            manifest={'url': 'https://github.com/OnjLouis/ollamaSpeech'},
            requestRemove=lambda: self.removed.append('phonon'))
        self.api = types.SimpleNamespace(getCodeAddon=lambda: types.SimpleNamespace(name='ollamaSpeech'),
            getAvailableAddons=lambda: [self.old, types.SimpleNamespace(name='phononLocal')])
        with patch.dict(sys.modules, {'addonHandler': self.api}):
            spec.loader.exec_module(self.module)

    def test_current_install_retires_only_the_legacy_network_addon(self):
        self.module.onInstall()
        self.assertEqual(self.removed, ['phonon'])

    def test_bridge_does_not_remove_itself(self):
        self.api.getCodeAddon = lambda: types.SimpleNamespace(name='phonon')
        self.module.onInstall()
        self.assertEqual(self.removed, [])

    def test_pending_install_and_foreign_repository_are_preserved(self):
        for attribute, value in (('isPendingInstall', True), ('manifest', {'url': 'https://example.invalid'})):
            previous = getattr(self.old, attribute)
            setattr(self.old, attribute, value)
            with self.assertRaises(RuntimeError):
                self.module.onInstall()
            setattr(self.old, attribute, previous)
        self.assertEqual(self.removed, [])

    def test_unidentified_installer_is_rejected(self):
        self.api.getCodeAddon = lambda: None
        with self.assertRaises(RuntimeError):
            self.module.onInstall()
        self.assertEqual(self.removed, [])

    def test_already_pending_removal_is_not_repeated(self):
        self.old.isPendingRemove = True
        self.module.onInstall()
        self.assertEqual(self.removed, [])

    def test_legacy_synth_entry_preserves_the_profile_identity(self):
        from synthDrivers.phonon import SynthDriver
        self.assertIs(SynthDriver, fixture.phonon.SynthDriver)
        self.assertEqual(SynthDriver.name, 'phonon')


if __name__ == '__main__':
    unittest.main()
