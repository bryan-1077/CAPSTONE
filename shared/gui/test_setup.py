"""Offline checks for actionable run failure notifications."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from .setup import SetupPanel
from .registry import lifecycle_path


class RunProblemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch('shared.gui.registry.discover_worker', return_value=None), patch(
                'shared.gui.registry.read_record', return_value={}):
            self.panel = SetupPanel()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()

    def test_defaults_and_frontend_only_launch_keep_skip_lec_valid(self):
        self.assertTrue(self.panel.flags['skip_lec'].isChecked())
        self.assertEqual(self.panel.frequency.text(), '')
        self.panel.flags['skip_lec'].setChecked(False)
        self.panel.frequency.setText('240')
        self.panel.reset_design()
        self.assertTrue(self.panel.flags['skip_lec'].isChecked())
        self.assertEqual(self.panel.frequency.text(), '')
        self.panel.flags['no_backend'].setChecked(True)
        self.assertFalse(self.panel.flags['skip_lec'].isEnabled())
        with patch('shared.gui.setup.launch', return_value=(Mock(), Path('/tmp/test-run'))) as launch, patch.object(
                self.panel, 'poll'):
            self.panel.start_run()
            self.assertFalse(launch.call_args.args[0]['skip_lec'])
        self.panel.timer.stop()
        self.panel.process = None
        self.panel.flags['no_backend'].setChecked(False)
        self.assertTrue(self.panel.flags['skip_lec'].isEnabled())
        self.assertTrue(self.panel.flags['skip_lec'].isChecked())

    def test_new_design_clears_finished_error_and_elapsed_without_changing_saved_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            state = directory / 'state.json'
            saved = '{"status": "failed"}'
            state.write_text(saved)
            self.panel.directory = directory
            self.panel.process = Mock()
            self.panel.process.poll.return_value = 1
            self.panel.description.setPlainText('Old design')
            self.panel.remote.setText('/saved/server.json')
            self.panel.problem = ('ValueError', 'Previous run failed')
            self.panel.notified_problem = 'previous'
            self.panel.problem_button.show()
            self.panel.status.setText('ValueError')
            self.panel.output.setPlainText('Old traceback')
            self.panel.flow_status.update_run({'status': 'failed'}, elapsed=95)
            self.panel.timer.start()

            self.assertTrue(self.panel.reset_design())

            self.assertIsNone(self.panel.process)
            self.assertIsNone(self.panel.directory)
            self.assertFalse(self.panel.timer.isActive())
            self.assertIsNone(self.panel.problem)
            self.assertIsNone(self.panel.notified_problem)
            self.assertTrue(self.panel.problem_button.isHidden())
            self.assertTrue(self.panel.flow_status.elapsed_label.isHidden())
            self.assertEqual(self.panel.flow_status.activity.text(), 'Ready to start')
            self.assertEqual(self.panel.status.text(), '')
            self.assertEqual(self.panel.output.toPlainText(), '')
            self.assertEqual(self.panel.description.toPlainText(), '')
            self.assertEqual(self.panel.remote.text(), '/saved/server.json')
            self.assertEqual(state.read_text(), saved)

    def test_new_design_cannot_reset_a_living_worker(self):
        worker = self.panel.process = Mock()
        worker.poll.return_value = None
        self.panel.description.setPlainText('Active controller')
        self.assertFalse(self.panel.reset_design())
        self.assertIs(self.panel.process, worker)
        self.assertEqual(self.panel.description.toPlainText(), 'Active controller')
        self.panel.process = None

    def test_server_timeout_explains_vpn_and_keeps_raw_error_in_details(self):
        from .run_reader import SavedRun
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.panel.directory = directory
            message = 'RuntimeError: Shared Slurm allocation failed: TimeoutError: timed out'
            run = SavedRun(directory, dict(status='failed', results=[
                dict(stage='validation', status='failed', message=message)]))
            self.panel.report_problem(run, dict(lifecycle='failed',
                remote_cleanup={'status': 'cleanup_unconfirmed'}), 2,
                directory / 'worker.log', notify=False)
            title, reason, guidance, details = self.panel.problem
            self.assertEqual(title, 'Server connection error')
            self.assertEqual(reason, 'Could not connect to the design server.')
            self.assertIn('VPN', guidance)
            self.assertIn('cleanup is unconfirmed', guidance)
            self.assertIn(message, details)
            self.assertIn('Check VPN', self.panel.flow_status.activity.text())

    def test_poll_explains_missing_dependency_once_and_can_reopen(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            reason = "Pre-generation selection stopped: ModuleNotFoundError: No module named 'requests'"
            (directory / 'state.json').write_text(json.dumps({
                'status': 'needs_attention', 'results': [{
                    'stage': 'generate', 'status': 'needs_attention', 'message': reason}]}))
            lifecycle_path(directory).write_text(json.dumps({'lifecycle': 'needs_attention'}))
            self.panel.process = Mock()
            self.panel.process.poll.return_value = 2
            self.panel.directory = directory
            self.panel.started = datetime.now()
            self.panel.poll()
            dialog = self.panel.problem_dialog
            self.assertEqual(dialog.text(), reason)
            self.assertIn('requirements-execution.txt', dialog.informativeText())
            self.assertIn(reason, self.panel.flow_status.activity.text())
            self.assertFalse(self.panel.problem_button.isHidden())
            self.assertTrue(self.panel.start.isEnabled())
            self.assertFalse(self.panel.timer.isActive())
            dialog.accept()
            self.panel.poll()
            self.assertIs(self.panel.problem_dialog, dialog)
            self.assertFalse(dialog.isVisible())
            self.panel.problem_button.click()
            self.assertIsNot(self.panel.problem_dialog, dialog)
            self.assertEqual(self.panel.problem_dialog.text(), reason)

    def test_interrupted_worker_has_log_details_and_cleanup_guidance(self):
        from .run_reader import SavedRun
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'run.worker.log'
            log.write_text('RuntimeError: tool could not start')
            self.panel.directory = Path(tmp) / 'run'
            self.panel.report_problem(SavedRun(self.panel.directory, {}), {
                'lifecycle': 'interrupted', 'remote_cleanup': {'status': 'cleanup_unconfirmed'}}, 1, log)
            dialog = self.panel.problem_dialog
            self.assertIn('without recording a reason', dialog.text())
            self.assertIn('Remote cleanup is unconfirmed', dialog.informativeText())
            self.assertIn('RuntimeError: tool could not start', dialog.detailedText())

    def test_startup_does_not_reopen_previous_error_dialog(self):
        from datetime import datetime
        from PySide6.QtCore import Qt
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            (directory / 'state.json').write_text(json.dumps({
                'status': 'needs_attention', 'results': [{
                    'stage': 'generate', 'status': 'needs_attention', 'message': 'Previous run error'}]}))
            lifecycle_path(directory).write_text('{"lifecycle": "needs_attention"}')
            previous = Mock(record=dict(run_dir=str(directory), started=datetime.now().timestamp()))
            previous.poll.return_value = 2
            with patch('shared.gui.registry.discover_worker', return_value=previous):
                reopened = SetupPanel()
            try:
                self.assertIsNone(reopened.problem_dialog)
                self.assertIsNotNone(reopened.problem)
                self.assertFalse(reopened.problem_button.isHidden())
                reopened.poll()
                self.assertIsNone(reopened.problem_dialog)
                reopened.problem_button.click()
                self.assertIsNotNone(reopened.problem_dialog)
                self.assertEqual(reopened.problem_dialog.windowModality(), Qt.WindowModality.NonModal)
            finally:
                reopened.close()
                reopened.deleteLater()

    def test_success_and_cancel_do_not_show_problem(self):
        from .run_reader import SavedRun
        for outcome in ('complete', 'cancelled'):
            self.panel.report_problem(SavedRun(Path('/tmp/run'), {}),
                                      {'lifecycle': outcome}, 0, Path('/tmp/run.worker.log'))
            self.assertIsNone(self.panel.problem_dialog)

    def test_close_stop_does_not_ask_for_a_second_confirmation(self):
        self.panel.process = Mock(gui_socket='/unused', worker_id='worker')
        self.panel.process.poll.return_value = None
        with patch('shared.gui.interaction.rpc', return_value={'accepted': True}) as rpc, patch(
                'shared.gui.setup.QMessageBox.question') as question:
            self.assertTrue(self.panel.stop_run(confirm=False))
            rpc.assert_called_once_with('/unused', 'cancel', worker_id='worker')
            question.assert_not_called()

    def test_start_leaves_password_for_the_pending_input_step(self):
        self.assertFalse(hasattr(self.panel, 'password'))
        with patch('shared.gui.setup.launch', return_value=(Mock(), Path('/tmp/test-run'))) as launch, patch.object(
                self.panel, 'poll'):
            self.panel.start_run()
            self.assertEqual(launch.call_args.args[1], '')
            self.assertTrue(self.panel.timer.isActive())
        self.panel.timer.stop()

    def test_reopening_finished_run_keeps_recorded_elapsed_time(self):
        from datetime import datetime
        from .registry import WorkerHandle
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            (directory / 'state.json').write_text('{"status": "complete"}')
            lifecycle_path(directory).write_text(json.dumps({
                'lifecycle': 'complete', 'returncode': 0, 'finished': 1095}))
            record = dict(run_dir=str(directory), started=1000, socket_path='/unused',
                          worker_id='worker', identity={'pid': 99999999})
            for now in (2000, 8000):
                with patch('shared.gui.registry.discover_worker', return_value=WorkerHandle(record)), patch(
                        'shared.gui.registry.alive', return_value=False), patch(
                        'shared.gui.setup.datetime', wraps=datetime) as clock:
                    clock.now.return_value = datetime.fromtimestamp(now)
                    reopened = SetupPanel()
                    try:
                        self.assertEqual(reopened.flow_status.elapsed_label.text(), 'Elapsed 1m 35s')
                    finally:
                        reopened.close()
                        reopened.deleteLater()

    def test_legacy_elapsed_uses_saved_evidence_and_actions_beside_chip(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            state = directory / 'state.json'
            state.write_text('{"status": "complete"}')
            os.utime(state, (1060, 1060))
            lifecycle_path(directory).write_text('{"lifecycle": "complete"}')
            self.panel.directory = directory
            self.panel.started = datetime.fromtimestamp(1000)
            self.panel.process = Mock()
            self.panel.process.poll.return_value = 0
            self.panel.resize(1050, 750)
            self.panel.show()
            self.panel.poll()
            self.app.processEvents()
            self.assertEqual(self.panel.flow_status.elapsed_label.text(), 'Elapsed 1m 00s')
            self.assertIs(self.panel.start.parentWidget(), self.panel.flow_status)
            self.assertLess(self.panel.start.x() + self.panel.start.width(), self.panel.flow_status.forge.x())
            self.assertLess(self.panel.flow_status.forge.x(), self.panel.flow_status.activity.x())
            self.assertGreaterEqual(self.panel.start.height(), 64)

    def test_approval_replaces_setup_and_stays_visible_without_resetting_edits(self):
        from datetime import datetime
        from .theme import apply_theme
        apply_theme(self.app)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            (directory / 'state.json').write_text('{}')
            lifecycle_path(directory).write_text('{"lifecycle": "complete"}')
            self.panel.process = Mock(gui_socket='/unused', worker_id='worker')
            self.panel.process.poll.return_value = None
            self.panel.directory = directory
            self.panel.started = datetime.now()
            self.panel.description.setPlainText('My controller')
            self.panel.resize(1050, 750)
            self.panel.show()
            pending = dict(request_id='approval', kind='spec_approval', context=dict(
                candidate='design_name: controller\nprofile:\n  type: basic\ntopology:\n  banks: 4',
                assumptions=['Default timing'], preset='basic', revision=1,
                digest='digest', description='My controller'))
            snapshot = dict(worker_id='worker', lifecycle='running', pending=pending)
            with patch('shared.gui.interaction.rpc', return_value=snapshot):
                self.panel.poll()
                self.app.processEvents()
                self.assertTrue(self.panel.setup_fields.isHidden())
                self.assertTrue(self.panel.start.isHidden())
                self.assertIn('Waiting for you', self.panel.flow_status.activity.text())
                self.assertEqual(self.panel.flow_status.activity.property('activityState'), 'waiting')
                panel = self.panel.input_panel
                self.assertTrue(panel.isVisible())
                point = panel.continue_button.mapTo(self.panel.scroll.viewport(),
                                                    panel.continue_button.rect().center())
                self.assertTrue(self.panel.scroll.viewport().rect().contains(point))
                banks = panel.spec_editor.fields[('topology', 'banks')][0]
                banks.setCurrentIndex(0)
                self.panel.poll()
                self.assertEqual(banks.currentData(), 1)
                snapshot['pending'] = None
                self.panel.poll()
                self.assertTrue(panel.isHidden())
                self.assertIsNone(panel.request)
            self.panel.process.poll.return_value = 0
            self.panel.poll()
            self.assertFalse(self.panel.setup_fields.isHidden())
            self.assertFalse(self.panel.start.isHidden())
            self.assertEqual(self.panel.description.toPlainText(), 'My controller')
