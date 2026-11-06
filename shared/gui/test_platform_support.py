"""Portability boundaries, without launching workers or contacting servers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from . import platform_support, registry
from .execution import launch
from .history import run_status
from .setup import SetupPanel


class PlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_viewer_import_does_not_require_unix_locking(self):
        script = '''
import builtins
original = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == 'fcntl':
        raise ImportError('Unix locking unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = without_fcntl
from shared.gui.window import RunWindow
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsupported_hosts_keep_history_and_do_not_restore_workers(self):
        for system in ('Windows', 'Darwin'):
            with self.subTest(system=system), patch.object(
                    platform_support.platform, 'system', return_value=system):
                self.assertFalse(platform_support.host_capabilities().local_execution)
                with patch('shared.gui.execution.validate') as validate:
                    with self.assertRaisesRegex(ValueError, 'browse saved runs'):
                        launch({}, '')
                    validate.assert_not_called()
                with patch.object(registry, 'read_record') as read:
                    self.assertIsNone(registry.discover_worker())
                    panel = SetupPanel()
                    try:
                        self.assertFalse(panel.start.isEnabled())
                        self.assertIn('browse saved runs', panel.platform_notice.text())
                        self.assertIsNone(panel.process)
                        read.assert_not_called()
                    finally:
                        panel.close()
                        panel.deleteLater()

    def test_foreign_active_receipt_remains_unconfirmed_and_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            receipt = registry.lifecycle_path(directory)
            saved = json.dumps(dict(lifecycle='running', run_dir=str(directory),
                                    identity=dict(pid=123, start_ticks='1', boot_id='old')))
            receipt.write_text(saved)
            run = SimpleNamespace(directory=directory, status='running', error=None)
            with patch.object(platform_support.platform, 'system', return_value='Darwin'):
                self.assertEqual(run_status(run)[0], 'unconfirmed')
                self.assertEqual(receipt.read_text(), saved)
                receipt.write_text(json.dumps(dict(lifecycle='complete')))
                self.assertEqual(run_status(run)[0], 'complete')

    def test_wsl_is_identified_without_claiming_tool_readiness(self):
        with patch.object(platform_support.platform, 'system', return_value='Linux'), patch.object(
                platform_support.platform, 'release', return_value='6.6.0-microsoft-standard-WSL2'):
            self.assertTrue(platform_support.host_capabilities().wsl)

    def test_font_registration_is_once_and_failures_are_reported(self):
        from . import fonts
        with tempfile.TemporaryDirectory() as tmp, patch.object(fonts, 'FONT_DIRECTORY', Path(tmp)):
            (Path(tmp) / 'sample.ttf').write_bytes(b'not a font')
            (Path(tmp) / 'LICENSE.txt').write_text('license')
            self.app.setProperty('capstoneBundledFonts', None)
            try:
                with patch('PySide6.QtGui.QFontDatabase.addApplicationFont', return_value=-1) as add:
                    self.assertEqual(fonts.load_bundled_fonts(self.app), {'sample.ttf': False})
                    fonts.load_bundled_fonts(self.app)
                    add.assert_called_once()
            finally:
                self.app.setProperty('capstoneBundledFonts', None)
