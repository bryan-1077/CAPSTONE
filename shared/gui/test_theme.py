"""Theme switching and local preference persistence without design execution."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication
from . import theme
from .window import RunWindow


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_launch_uses_blue_even_with_an_old_green_preference(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                theme, 'SETTINGS', Path(tmp) / 'settings.json'), patch(
                'shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            theme.SETTINGS.write_text('{"theme": "green"}')
            window = RunWindow(tmp, preferences_path=__import__('pathlib').Path(tmp) / 'ui.json')
            try:
                self.assertFalse(hasattr(window, 'theme_selector'))
                self.assertEqual(self.app.palette().color(QPalette.ColorRole.Window).name(), '#142638')
                self.assertEqual(theme.theme_colors()['board'], '#142638')
            finally:
                window.close()
            reopened = RunWindow(tmp, preferences_path=__import__('pathlib').Path(tmp) / 'ui.json')
            try:
                self.assertEqual(theme.theme_colors()['board'], '#142638')
            finally:
                reopened.close()

    def test_bad_settings_fall_back_and_other_preferences_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                theme, 'SETTINGS', Path(tmp) / 'settings.json'):
            for value in ('{', '[]', '{"theme": []}', '{"theme": "unknown"}'):
                theme.SETTINGS.write_text(value)
                self.assertEqual(theme.load_theme(), 'blue')
            theme.SETTINGS.write_text('{"other": "keep"}')
            theme.save_theme('green')
            self.assertEqual(json.loads(theme.SETTINGS.read_text()), {'other': 'keep', 'theme': 'green'})
