"""Selected-run navigation, scoped summaries, and active-worker isolation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from .window import RunWindow
from .run_reader import load_run
from .physical_details import measured_results, physical_steps


class WorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runs = self.root / 'runs'
        self.runs.mkdir()
        self.old = self.make_run('older', 'failed', 'backend')
        self.active = self.make_run('active', 'running', 'generate')
        self.preferences = self.root / 'ui.json'
        with patch('shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            self.window = RunWindow(self.runs, preferences_path=self.preferences)
        self.window.resize(1366, 900)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.setup.process = None
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def make_run(self, name, status, stage):
        directory = self.runs / name
        directory.mkdir()
        (directory / 'state.json').write_text(json.dumps(dict(status=status,
            current_stage=stage, config={'target_mhz': 200}, results=[])))
        return directory

    def labels(self):
        return '\n'.join(widget.text() for widget in self.window.results.findChildren(QLabel))

    def test_history_updates_every_tab_without_hijacking_active_run(self):
        self.assertIsNone(self.window.run)
        self.window.setup.directory = self.active
        worker = self.window.setup.process = Mock()
        worker.poll.return_value = None
        self.window.display_run(load_run(self.old))
        self.window.tabs.setCurrentWidget(self.window.results)
        self.window.review.open_stage('validation')
        self.window.setup.input_panel.request = {'kind': 'credential'}
        self.window.update_history_statuses()
        self.assertEqual(self.window.run.directory, self.old)
        self.assertIs(self.window.run_page.currentWidget(), self.window.review)
        self.assertIs(self.window.tabs.currentWidget(), self.window.results)
        self.assertEqual(self.window.review.selected_stage, 'validation')
        self.assertIn('Waiting for You', self.window.active_run.text())
        self.assertFalse(self.window.active_run.isHidden())
        self.assertFalse(self.window.new_design.isEnabled())
        self.window.active_run.click()
        self.assertEqual(self.window.run.directory, self.active)
        self.assertIs(self.window.run_page.currentWidget(), self.window.setup)
        self.assertIs(self.window.tabs.currentWidget(), self.window.run_page)
        self.assertEqual(self.window.selection_status.text(), 'Waiting for You')
        self.assertIs(self.window.setup.process, worker)

    def test_gap_summary_deep_evidence_and_stage_navigation(self):
        report = self.old / '001_validation/reports/rtl/verification_reports/session/final_system_report.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps(dict(status='PASS_WITH_GAPS', requirements=dict(passed=15, failed=0),
            coverage_limitations=[dict(timing_rule='tFAW', reason='Boundary was not observed')],
            simulation=dict(uvm_error=0, uvm_fatal=0))))
        self.window.display_run(load_run(self.old))
        self.assertIn('passed with gaps', self.labels())
        self.assertIn('Requirements: passed: 15', self.labels())
        self.assertIn('not available', self.labels())
        self.window.results.evidence_requested.emit(report)
        self.assertIs(self.window.tabs.currentWidget(), self.window.files_card)
        self.assertEqual(self.window.files.currentItem().data(0, 256), report)
        self.assertIn('PASS_WITH_GAPS', self.window.text.toPlainText())
        self.window.results.stage_requested.emit('validation')
        self.assertIs(self.window.tabs.currentWidget(), self.window.run_page)
        self.assertEqual(self.window.review.selected_stage, 'validation')
        # Repeated refreshes preserve the inspected stage and file selection.
        self.window.display_run(load_run(self.old))
        self.assertEqual(self.window.files.currentItem().data(0, 256), report)
        self.assertEqual(self.window.review.selected_stage, 'validation')

    def test_latest_attempt_does_not_reuse_old_success_and_limits_are_not_measurements(self):
        earlier = self.old / '001_backend'
        latest = self.old / '003_backend'
        earlier.mkdir(); latest.mkdir()
        (earlier / 'backend_result.json').write_text(json.dumps(dict(stage_status={'gdsii': 'success'})))
        (latest / 'backend_result.json').write_text(json.dumps(dict(
            stage_status={'rtl_prep': 'failed', 'netlist': 'pending'}, prep_iteration=3,
            timing_target_clock_period_ns=5, timing_closure_limits={'max_power_w': 2}, outputs={})))
        steps = physical_steps(load_run(self.old))
        self.assertEqual(steps[0][2], 'Failed')
        self.assertEqual(steps[1][2], 'Not reached')
        self.assertEqual(steps[-1][2], 'Evidence unavailable')
        self.assertEqual(measured_results({'timing_target_clock_period_ns': 5,
            'timing_closure_limits': {'max_power_w': 2}}), [])
        values = dict(measured_results({'timing_closure_best_attempt': dict(
            wns_ns=0.0, hold_wns_ns=-0.01, limits=dict(die_area_mm2=0.1, total_power_w=0.04))}))
        self.assertEqual(values['Setup WNS'], '0 ns')
        self.assertEqual(values['Reported Power'], '0.04 W')

    def test_new_design_clears_selection_and_restart_does_not_restore_popups(self):
        self.window.display_run(load_run(self.old))
        self.window.open_design()
        self.assertIsNone(self.window.run)
        self.assertEqual(self.window.files.topLevelItemCount(), 0)
        self.assertEqual(self.window.selection_label.text(), 'New Design')
        self.assertFalse(self.window.setup.scroll.isHidden())
        self.preferences.write_text(json.dumps(dict(show_run_log=True, files_split=[210, 430],
                                                   history_split=[850, 410])))
        self.window.preferences = json.loads(self.preferences.read_text())
        self.window.restore_layout()
        self.assertFalse(self.window.setup.scroll.isHidden())
        self.assertIsNone(self.window.spec_dialog)
        self.assertIsNone(self.window.setup.problem_dialog)
        self.assertTrue(self.window.review.log_toggle.isChecked())
        self.window.remember_layout()
        self.assertTrue(json.loads(self.preferences.read_text())['show_run_log'] is False)

    def test_live_files_appear_without_changing_current_evidence(self):
        self.window.display_run(load_run(self.active))
        state = self.window.files.currentItem()
        report = self.active / 'new-stage/report.txt'
        report.parent.mkdir(); report.write_text('new evidence')
        self.window.display_run(load_run(self.active))
        self.assertIs(self.window.files.currentItem(), state)
        self.window.show_evidence(report)
        self.assertEqual(self.window.text.toPlainText(), 'new evidence')

    def test_result_cards_reflow_without_losing_navigation_or_inventing_totals(self):
        from .results import ResultsPanel
        from PySide6.QtTest import QSignalSpy
        report = self.old / '001_validation/reports/rtl/verification_reports/session/final_system_report.json'
        report.parent.mkdir(parents=True)
        # An incomplete requirements summary must not fabricate a denominator.
        report.write_text(json.dumps({'requirements': {'passed': 15}}))
        panel = ResultsPanel()
        try:
            panel.resize(1100, 800)
            panel.display(load_run(self.old))
            panel.show()
            self.app.processEvents()
            self.assertEqual(panel._columns, 2)
            self.assertEqual([w.text() for w in panel.findChildren(QLabel)
                              if w.objectName() == 'metricValue'], ['15 Passed'])
            panel.resize(700, 800)
            self.app.processEvents()
            self.assertEqual(panel._columns, 1)
            for index in range(4):
                self.assertEqual(panel.stage_grid.getItemPosition(index), (index, 0, 1, 1))
            requested = QSignalSpy(panel.stage_requested)
            button = next(w for w in panel.stage_cards[1].findChildren(QPushButton)
                          if w.text() == 'Open Stage Details')
            button.click()
            self.assertEqual(requested.at(0), ['validation'])
        finally:
            panel.close()
