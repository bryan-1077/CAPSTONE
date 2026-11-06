import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from .artifacts import ArtifactBrowser, PATH_ROLE
from .run_reader import SavedRun
from .window import RunWindow


class ArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def child(self, item, name):
        return next(item.child(i) for i in range(item.childCount()) if item.child(i).text(0) == name)

    def test_deep_reports_expand_and_preview_in_the_window(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            run = Path(tmp) / 'run'
            report = run / '001_validation/reports/rtl/verification_reports/session/agent1/report.json'
            report.parent.mkdir(parents=True)
            report.write_text('{"status":"PASS","checks":{"hierarchy":true}}')
            (run / 'state.json').write_text(json.dumps({'status': 'complete', 'results': [
                {'stage': 'validation', 'status': 'passed', 'artifacts': {'reports': str(run / '001_validation/reports')}}]}))
            window = RunWindow(tmp, preferences_path=Path(tmp) / 'ui.json')
            try:
                window.history.setCurrentRow(0)
                tree = window.files
                item = tree.run_root
                stage = self.child(item, '001_validation')
                self.assertEqual(stage.childCount(), 0)
                for name in ('001_validation', 'reports', 'rtl', 'verification_reports', 'session', 'agent1'):
                    item = self.child(item, name)
                    item.setExpanded(True)
                    self.app.processEvents()
                leaf = self.child(item, 'report.json')
                tree.setCurrentItem(leaf)
                self.assertIn('hierarchy', window.text.toPlainText())
                with patch.object(window, 'open_path') as open_path:
                    window.open_selected()
                    open_path.assert_called_once_with(report)
            finally:
                window.close()

    def test_external_mailbox_is_browsable_without_rebasing_it_into_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / 'runs/gui_test'
            directory.mkdir(parents=True)
            snapshot = root / 'mailbox/gui_test/revision_001'
            snapshot.mkdir(parents=True)
            (snapshot / 'manifest.json').write_text('{}')
            run = SavedRun(directory, {'results': [{'stage': 'generate', 'artifacts': {'mailbox': str(snapshot)}}]})
            browser = ArtifactBrowser()
            try:
                browser.load_run(run)
                group = browser.topLevelItem(1)
                item = group.child(0)
                self.assertEqual(item.text(0), 'Published RTL Snapshot')
                self.assertEqual(item.data(0, PATH_ROLE), snapshot)
                item.setExpanded(True)
                self.assertEqual(item.child(0).data(0, PATH_ROLE), snapshot / 'manifest.json')
            finally:
                browser.close()

    def test_directory_symlinks_do_not_expand_into_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'loop').symlink_to(directory, target_is_directory=True)
            browser = ArtifactBrowser()
            try:
                browser.load_run(SavedRun(directory, {}))
                link = browser.run_root.child(0)
                browser.populate_folder(link)
                self.assertEqual(link.childCount(), 0)
            finally:
                browser.close()

    def test_explorer_folder_clicks_keep_preview_and_refresh_keeps_selection(self):
        with tempfile.TemporaryDirectory() as tmp, patch(
                'shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            run = Path(tmp) / 'run'
            folder = run / 'reports'
            folder.mkdir(parents=True)
            (folder / 'report.json').write_text('{"status":"PASS"}')
            (run / 'state.json').write_text('{"status":"complete"}')
            window = RunWindow(tmp, preferences_path=Path(tmp) / 'ui.json')
            try:
                window.resize(1150, 740)
                window.show()
                window.history.setCurrentRow(0)
                window.tabs.setCurrentWidget(window.files_card)
                self.app.processEvents()
                self.assertEqual(window.artifact_split.orientation(), Qt.Orientation.Horizontal)
                tree = window.files
                directory_item = self.child(tree.run_root, 'reports')
                rect = tree.visualItemRect(directory_item)
                QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                                 pos=rect.topLeft() + QPoint(55, rect.height() // 2))
                self.assertTrue(directory_item.isExpanded())
                self.assertEqual(window.preview_path, run / 'state.json')
                report = self.child(directory_item, 'report.json')
                tree.setCurrentItem(report)
                self.assertEqual(window.preview_title.text(), 'report.json')
                self.assertEqual(window.preview_location.text(), 'reports › report.json')
                # Arrow clicks toggle once; they do not also trigger name-click handling.
                rect = tree.visualItemRect(directory_item)
                point = rect.topLeft() + QPoint(-8, rect.height() // 2)
                QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton, pos=point)
                self.assertFalse(directory_item.isExpanded())
                self.assertIn('PASS', window.text.toPlainText())
                tree.setCurrentItem(directory_item)
                QTest.keyClick(tree, Qt.Key.Key_Right)
                self.assertTrue(directory_item.isExpanded())
                QTest.keyClick(tree, Qt.Key.Key_Left)
                self.assertFalse(directory_item.isExpanded())
                directory_item.setExpanded(True)
                tree.setCurrentItem(report)
                (folder / 'a-log.txt').write_text('new output')
                window.refresh_files()
                self.assertIs(tree.currentItem(), report)
                self.assertEqual(directory_item.child(0).text(0), 'a-log.txt')
                tree.collapseAll()
                self.assertEqual(window.preview_path, folder / 'report.json')
            finally:
                window.close()
