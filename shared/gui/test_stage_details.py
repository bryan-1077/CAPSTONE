import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from .setup import SetupPanel
from .run_reader import SavedRun
from .stage_details import evidence_rows


class StageDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            self.panel = SetupPanel()
        self.panel.resize(1100, 750)
        self.panel.show()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()

    def test_nodes_leave_draft_visible_until_run_starts_and_after_reset(self):
        self.assertTrue(self.panel.flow_status.graph.isHidden())
        self.assertTrue(self.panel.start.isVisible())
        self.panel.description.setPlainText('DDR4 controller with four banks')
        self.panel.frequency.setText('240')
        for node in self.panel.flow_status.graph.nodes.values():
            QTest.mouseClick(node, Qt.MouseButton.LeftButton)
            self.assertFalse(self.panel.scroll.isHidden())
            self.assertTrue(self.panel.run_workspace.isHidden())
            self.assertEqual(node.cursor().shape(), Qt.CursorShape.ArrowCursor)
        self.panel.open_stage('validation')
        self.assertEqual(self.panel.description.toPlainText(), 'DDR4 controller with four banks')
        self.assertEqual(self.panel.frequency.text(), '240')
        self.assertIsNone(self.panel.selected_stage)
        run = SavedRun(Path('/tmp/stage-test'), dict(current_stage='generate', status='running'))
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertTrue(self.panel.flow_status.graph.isVisible())
        node = self.panel.flow_status.graph.nodes['validation']
        QTest.mouseClick(node, Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.selected_stage, 'validation')
        self.assertEqual(node.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.panel.reset_design()
        self.assertTrue(self.panel.flow_status.graph.isHidden())
        self.assertTrue(self.panel.start.isVisible())
        self.panel.description.setPlainText('Next draft')
        QTest.mouseClick(node, Qt.MouseButton.LeftButton)
        self.assertFalse(self.panel.scroll.isHidden())
        self.assertEqual(self.panel.description.toPlainText(), 'Next draft')

    def test_transitions_close_old_details_but_node_click_reopens_them(self):
        run = SavedRun(Path('/tmp/stage-test'), dict(current_stage='generate', status='running'))
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertEqual(self.panel.selected_stage, 'generate')
        run.state['results'] = [dict(stage='generate', status='passed', message='Generation and BIST completed.')]
        run.state['current_stage'] = 'validation'
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertEqual(self.panel.selected_stage, 'validation')
        self.assertEqual(self.panel.stage_details.title.text(), 'Verification')
        node = self.panel.flow_status.graph.nodes['generate']
        QTest.mouseClick(node, Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.selected_stage, 'generate')
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertEqual(self.panel.selected_stage, 'generate')
        self.assertFalse(self.panel.active_stage_button.isHidden())
        self.panel.active_stage_button.click()
        self.assertEqual(self.panel.selected_stage, 'validation')
        run.state['current_stage'] = 'backend'
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertEqual(self.panel.selected_stage, 'backend')
        self.panel.update_stage_workspace(run, alive=True, pending={'kind': 'credential'})
        self.assertTrue(self.panel.flow_status.graph.isHidden())
        self.assertTrue(self.panel.run_workspace.isHidden())
        self.assertFalse(self.panel.scroll.isHidden())
        QTest.mouseClick(node, Qt.MouseButton.LeftButton)
        self.assertTrue(self.panel.run_workspace.isHidden())
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertTrue(self.panel.flow_status.graph.isVisible())
        self.assertTrue(self.panel.run_workspace.isVisible())

    def test_log_has_no_outer_scroll_area_and_splitter_size_survives_refresh(self):
        run = SavedRun(Path('/tmp/stage-test'), dict(current_stage='validation', status='running'))
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.app.processEvents()
        parent = self.panel.output.parentWidget()
        while parent is not None:
            self.assertNotIsInstance(parent, QScrollArea)
            parent = parent.parentWidget()
        self.assertTrue(self.panel.details.isVisible())
        self.panel.module_split.setSizes([650, 350])
        sizes = self.panel.module_split.sizes()
        self.panel.update_stage_workspace(run, alive=True, pending=None)
        self.assertEqual(self.panel.module_split.sizes(), sizes)
        self.assertTrue(self.panel.scroll.isHidden())

    def test_stage_evidence_keeps_attempts_failures_and_scope(self):
        run = SavedRun(Path('/tmp/stage-test'), dict(config={'target_mhz': 200}, results=[
            dict(stage='validation', status='failed', message='Connection refused'),
            dict(stage='validation', status='passed', message='Module checks only',
                 artifacts={'result': '/tmp/validation.json'})]))
        rows = dict(evidence_rows(run, 'validation'))
        self.assertEqual(rows['Status'], 'Passed')
        self.assertEqual(rows['Attempt 1'], 'Connection refused')
        self.assertEqual(rows['Attempt 2'], 'Module checks only')
        self.assertEqual(rows['Result'], '/tmp/validation.json')
        self.assertEqual(dict(evidence_rows(run, 'gdsii-validation'))['Status'], 'Not reached')
