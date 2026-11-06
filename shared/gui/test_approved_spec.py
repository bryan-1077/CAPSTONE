import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from .approved_spec import ApprovedSpecification, approved_spec_path
from .window import RunWindow


class ApprovedSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_viewer_uses_exact_gui_approval_and_keeps_it_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            approved = root / 'gui_approved_specs.yaml'
            text = 'memory:\n  protocol: DDR4\ntopology:\n  banks: 4\n'
            approved.write_text(text)
            (root / 'approved_specs.yaml').write_text('topology:\n  banks: 1\n')
            self.assertEqual(approved_spec_path(root), approved)
            dialog = ApprovedSpecification(root)
            try:
                self.assertTrue(dialog.yaml.isReadOnly())
                self.assertEqual(dialog.yaml.toPlainText(), text)
                self.assertEqual(dialog.fields.topLevelItemCount(), 2)
                self.assertEqual(dialog.fields.topLevelItem(1).child(0).text(1), '4')
                self.assertEqual(approved.read_text(), text)
            finally:
                dialog.close()

    def test_missing_and_invalid_saved_specs_have_clear_empty_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(approved_spec_path(root))
            dialog = ApprovedSpecification(root)
            self.assertIn('No saved approved specification', dialog.source.text())
            dialog.close()
            (root / 'approved_specs.yaml').write_text('[')
            dialog = ApprovedSpecification(root)
            self.assertIn('Cannot display', dialog.source.text())
            dialog.close()

    def test_history_click_preserves_tab_marks_selection_and_loads_matching_spec(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            root = Path(tmp)
            for name, banks in [('first', 4), ('second', 2)]:
                directory = root / name
                directory.mkdir()
                (directory / 'state.json').write_text(json.dumps({'status': 'complete'}))
                (directory / 'approved_specs.yaml').write_text(f'topology:\n  banks: {banks}\n')
            window = RunWindow(root, preferences_path=Path(tmp) / 'ui.json')
            try:
                window.show()
                window.history_toggle.setChecked(True)
                self.app.processEvents()
                first = window.history.item(0)
                window.history.setCurrentItem(first)
                window.tabs.setCurrentWidget(window.run_page)
                # Selecting history must not navigate away from the current tab.
                QTest.mouseClick(window.history.viewport(), Qt.MouseButton.LeftButton,
                                 pos=window.history.visualItemRect(first).center())
                self.assertIs(window.tabs.currentWidget(), window.run_page)
                first_card = window.history.itemWidget(first)
                self.assertFalse(first_card.selected_label.isHidden())
                self.assertEqual(first_card.selected_label.text(), 'Selected')
                second = window.history.item(1)
                window.tabs.setCurrentWidget(window.results)
                QTest.mouseClick(window.history.viewport(), Qt.MouseButton.LeftButton,
                                 pos=window.history.visualItemRect(second).center())
                self.assertTrue(first_card.selected_label.isHidden())
                self.assertIs(window.tabs.currentWidget(), window.results)
                self.assertFalse(window.history.itemWidget(second).selected_label.isHidden())
                self.assertIn(window.run.directory.name, window.selection_label.text())
                self.assertTrue(window.review.spec.isEnabled())
                for tab in range(window.tabs.count()):
                    window.tabs.setCurrentIndex(tab)
                    QTest.mouseClick(window.history.viewport(), Qt.MouseButton.LeftButton,
                                     pos=window.history.visualItemRect(second).center())
                    self.assertEqual(window.tabs.currentIndex(), tab)
                with patch.object(window, 'open_path') as open_path:
                    window.open_run_folder_action.trigger()
                    open_path.assert_called_once_with(window.run.directory)
                window.review.spec.click()
                self.assertIn(str(window.run.directory), window.spec_dialog.source.text())
                self.assertEqual(window.spec_dialog.yaml.toPlainText(),
                                 (window.run.directory / 'approved_specs.yaml').read_text())
            finally:
                window.close()
