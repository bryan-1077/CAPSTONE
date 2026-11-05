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
        self.assertLess(widget.activity.geometry().x(), widget.forge.geometry().x())
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
        widget.update_run({}, alive=False, lifecycle='cancelled')
        self.assertFalse(widget.forge.running)
        self.assertFalse(widget.forge.timer.isActive())
        widget.close()
