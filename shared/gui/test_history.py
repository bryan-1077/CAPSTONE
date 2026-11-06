"""History labels use recorded hardware and verified process identities."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .history import hardware_summary, run_status
from .registry import lifecycle_path
from .run_reader import SavedRun


class HistoryTests(unittest.TestCase):
    def test_hardware_uses_saved_approval_before_external_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'gui_approved_specs.yaml').write_text(
                'memory:\n  protocol: DDR4\ntopology:\n  banks: 4\n')
            run = SavedRun(root, {'config': {'target_mhz': 200.0, 'input_yaml': '/missing.yaml'}})
            self.assertEqual(hardware_summary(run), 'DDR4 · 4 banks · 200 MHz')
            (root / 'gui_approved_specs.yaml').unlink()
            self.assertEqual(hardware_summary(run), '200 MHz')
            self.assertEqual(hardware_summary(SavedRun(root, {})), 'Specification not recorded')

    def test_stale_running_is_not_presented_as_live(self):
        run = SavedRun(Path('/tmp/history-test'), {'status': 'running'})
        with patch('shared.gui.history.read_record', return_value={}):
            self.assertEqual(run_status(run)[0], 'unconfirmed')
        record = dict(run_dir=str(run.directory), lifecycle='running', identity={'pid': 1})
        with patch('shared.gui.history.read_record', return_value=record), patch(
                'shared.gui.history.alive', return_value=False):
            self.assertEqual(run_status(run)[0], 'interrupted')
        with patch('shared.gui.history.read_record', return_value=record), patch(
                'shared.gui.history.alive', return_value=True):
            self.assertEqual(run_status(run)[0], 'running')
            self.assertEqual(run_status(SavedRun(Path('/tmp/another-run'), {'status': 'running'}))[0], 'unconfirmed')

    def test_terminal_worker_receipt_overrides_stale_state(self):
        run = SavedRun(Path('/tmp/history-test'), {'status': 'running'})
        for outcome in ('complete', 'cancelled', 'needs_attention', 'failed'):
            with patch('shared.gui.history.read_record', return_value={'lifecycle': outcome}):
                self.assertEqual(run_status(run)[0], outcome)

    def test_cards_keep_names_and_refresh_status_without_changing_selection(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .window import RunWindow
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None):
            root = Path(tmp)
            directory = root / 'keep_this_run_name'
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({
                'status': 'running', 'config': {'target_mhz': 200}}))
            window = RunWindow(root, preferences_path=Path(tmp) / 'ui.json')
            try:
                window.show()
                window.history_toggle.setChecked(True)
                app.processEvents()
                window.history.setCurrentRow(0)
                item = window.history.currentItem()
                card = window.history.itemWidget(item)
                self.assertEqual(card.name.text(), 'keep_this_run_name')
                self.assertEqual(card.description.text(), '200 MHz')
                self.assertEqual(card.badge.text(), 'Unconfirmed')
                lifecycle_path(directory).write_text('{"lifecycle": "cancelled"}')
                window.update_history_statuses()
                self.assertIs(window.history.currentItem(), item)
                self.assertEqual(card.badge.text(), 'Stopped')
                self.assertEqual(card.badge.property('historyState'), 'cancelled')
            finally:
                window.close()
