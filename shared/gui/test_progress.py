import os
import unittest
from .progress import progress_state


class ProgressTests(unittest.TestCase):
    def test_running_waiting_and_disconnect(self):
        state = {'current_stage': 'validation', 'results': [{'stage': 'generate', 'status': 'passed'}]}
        statuses, title, busy, *_ = progress_state(state, alive=True, connected=True)
        self.assertEqual(statuses['generate'], 'passed')
        self.assertEqual(statuses['validation'], 'running')
        self.assertTrue(busy)
        statuses, title, busy, *_ = progress_state(state, alive=True, connected=True, pending={'kind': 'credential'})
        self.assertEqual(statuses['validation'], 'waiting')
        self.assertIn('SSH', title)
        self.assertFalse(busy)
        self.assertFalse(progress_state(state, alive=True, connected=False)[2])

    def test_skips_failure_and_cancellation(self):
        state = {'config': {'skip_verification': True, 'no_backend': True},
                 'results': [{'stage': 'generate', 'status': 'failed'}], 'status': 'failed'}
        statuses, _, busy, complete, _ = progress_state(state)
        self.assertEqual(statuses['generate'], 'failed')
        self.assertEqual(statuses['validation'], 'skipped')
        self.assertEqual(statuses['gdsii-validation'], 'skipped')
        self.assertFalse(busy or complete)
        self.assertIn('Stopping', progress_state({}, alive=True, connected=True, lifecycle='cancelling')[1])
        self.assertFalse(progress_state(state, lifecycle='cancelled')[3])

    def test_widget_and_fusion(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .progress import FlowStatus
        from .theme import apply_theme
        app = QApplication.instance() or QApplication([])
        self.assertEqual(apply_theme(app).lower(), 'fusion')
        widget = FlowStatus()
        widget.update_run({'current_stage': 'backend'}, alive=True, connected=True)
        self.assertEqual(widget.bar.maximum(), 0)
        widget.update_run({'status': 'complete'})
        self.assertEqual(widget.bar.maximum(), 1)
        self.assertEqual(widget.bar.value(), 1)
        widget.close()

    def test_four_nodes_group_active_repair_under_generation(self):
        state = {'current_stage': 'debug', 'results': [
            {'stage': 'generate', 'status': 'passed'},
            {'stage': 'validation', 'status': 'failed', 'failure': {'source': 'verif'}}]}
        statuses, title, *_ = progress_state(state, alive=True, connected=True)
        self.assertEqual(statuses['generate'], 'running')
        self.assertEqual(statuses['validation'], 'failed')
        self.assertEqual(len(statuses), 4)
        self.assertEqual(title, 'Repairing RTL')
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .progress import FlowStatus
        app = QApplication.instance() or QApplication([])
        widget = FlowStatus()
        widget.update_run(state, alive=True, connected=True)
        widget.show()
        app.processEvents()
        self.assertEqual(len(widget.stages), 4)
        self.assertEqual(widget.stages['generate'].property('flowState'), 'running')
        self.assertFalse(widget.graph.grab().isNull())
        widget.close()

    def test_forge_keeps_ticking_across_cycles_and_status_refreshes(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest, QSignalSpy
        from .progress import FlowStatus
        app = QApplication.instance() or QApplication([])
        widget = FlowStatus()
        widget.show()
        running = dict(alive=True, connected=True)
        widget.update_run({'current_stage': 'generate'}, **running)
        ticks = QSignalSpy(widget.forge.timer.timeout)
        QTest.qWait(1150)
        first = ticks.count()
        widget.update_run({'current_stage': 'generate'}, **running)
        QTest.qWait(1150)
        self.assertGreater(first, 15)
        self.assertGreater(ticks.count(), first + 15)
        self.assertGreater(widget.forge.clock.elapsed(), 2000)
        self.assertGreater(widget.activity.geometry().x(), widget.forge.geometry().x())
        widget.forge.timer.stop()
        widget.update_run({'current_stage': 'generate'}, **running)
        self.assertTrue(widget.forge.timer.isActive())
        widget.update_run({'status': 'complete'})
        self.assertFalse(widget.forge.timer.isActive())
        self.assertEqual(widget.activity.text(), 'Your design is ready')
        widget.close()

    def test_forge_runs_for_entire_worker_lifetime(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .progress import FlowStatus
        app = QApplication.instance() or QApplication([])
        widget = FlowStatus()
        widget.show()
        for runtime in (
            dict(alive=True, connected=True),
            dict(alive=True, connected=True, pending={'kind': 'spec_approval'}),
            dict(alive=True, connected=False),
            dict(alive=True, connected=True, lifecycle='cancelling'),
        ):
            widget.update_run({'current_stage': 'generate'}, **runtime)
            self.assertTrue(widget.forge.running)
            self.assertTrue(widget.forge.timer.isActive())
            self.assertTrue(widget.bar.running)
            self.assertEqual(widget.bar.timer.isActive(), not bool(runtime.get('pending')))
            self.assertEqual(widget.bar.maximum(), 0)
        widget.update_run({}, alive=False, lifecycle='cancelled')
        self.assertFalse(widget.forge.running)
        self.assertFalse(widget.forge.timer.isActive())
        self.assertFalse(widget.bar.timer.isActive())
        self.assertEqual(widget.bar.value(), 0)
        widget.close()

    def test_bar_frames_keep_moving_across_refreshes_and_multiple_cycles(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest, QSignalSpy
        from .progress import FlowStatus
        from .theme import apply_theme
        app = QApplication.instance() or QApplication([])
        apply_theme(app)
        widget = FlowStatus()
        widget.show()
        widget.update_run({}, alive=True, connected=True)
        ticks = QSignalSpy(widget.bar.timer.timeout)
        for runtime in (dict(connected=True),
                        dict(connected=True),
                        dict(connected=False),
                        dict(connected=True, lifecycle='cancelling')):
            QTest.qWait(800)
            elapsed = widget.bar.clock.elapsed()
            widget.update_run({}, alive=True, **runtime)
            self.assertGreaterEqual(widget.bar.clock.elapsed(), elapsed)
            before = widget.bar.grab().toImage()
            QTest.qWait(160)
            self.assertNotEqual(before, widget.bar.grab().toImage())
        self.assertGreater(ticks.count(), 65)
        self.assertGreater(widget.bar.clock.elapsed(), 3600)
        widget.hide()
        self.assertFalse(widget.bar.timer.isActive())
        widget.show()
        self.assertTrue(widget.bar.timer.isActive())
        widget.update_run({'status': 'complete'}, alive=False)
        self.assertFalse(widget.bar.timer.isActive())
        self.assertEqual(widget.bar.value(), 1)
        widget.close()

    def test_bar_pauses_for_input_and_resumes_after_response(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from .progress import FlowStatus
        app = QApplication.instance() or QApplication([])
        widget = FlowStatus()
        widget.show()
        widget.update_run({}, alive=True, connected=True)
        QTest.qWait(220)
        for kind in ('spec_approval', 'credential', 'frequency', 'description'):
            runtime = dict(alive=True, connected=True, pending={'kind': kind})
            widget.update_run({}, **runtime)
            self.assertFalse(widget.bar.timer.isActive())
            self.assertTrue(widget.forge.timer.isActive())
            frozen = widget.bar.grab().toImage()
            QTest.qWait(120)
            widget.update_run({}, **runtime)
            self.assertEqual(frozen, widget.bar.grab().toImage())
            widget.hide()
            widget.show()
            self.assertFalse(widget.bar.timer.isActive())
            paused_elapsed = widget.bar.pause_started - widget.bar.paused_total
            widget.update_run({}, alive=True, connected=True)
            self.assertTrue(widget.bar.timer.isActive())
            self.assertLess(abs(widget.bar.clock.elapsed() - widget.bar.paused_total - paused_elapsed), 40)
            resumed = widget.bar.grab().toImage()
            self.assertNotEqual(frozen, resumed)
            QTest.qWait(160)
            self.assertNotEqual(resumed, widget.bar.grab().toImage())
        widget.update_run({'status': 'complete'})
        self.assertEqual(widget.bar.value(), 1)
        widget.close()
