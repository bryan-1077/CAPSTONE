"""Offline checks: python -m unittest shared.gui.test_viewer."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from .run_reader import load_run, discover, preview, summary


class ReaderTests(unittest.TestCase):
    def test_moved_run_and_pending_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / 'sample'
            run_dir.mkdir()
            (run_dir / 'validation.log').write_text('evidence')
            (run_dir / 'state.json').write_text(json.dumps({
                'status': 'running', 'current_stage': 'validation',
                'config': {'skip_verification': True},
                'results': [{'stage': 'generate', 'status': 'passed',
                             'artifacts': {'log': '/old/sample/validation.log'}}]}))
            run = load_run(run_dir)
            self.assertEqual(run.resolve('/old/sample/validation.log'), run_dir / 'validation.log')
            self.assertIn('liveness is unknown', summary(run))
            self.assertIn('skipped by configuration', summary(run))
            self.assertEqual(len(discover(tmp)), 1)
            self.assertIn(run_dir / 'validation.log', run.files())

    def test_invalid_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            for content in ('{', '[]', '{"results": [null]}'):
                (path / 'state.json').write_text(content)
                self.assertTrue(load_run(path).error)
            self.assertEqual(discover(path / 'missing'), [])

    def test_bounded_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'log'
            path.write_bytes(b'x' * 300000 + b'end')
            self.assertTrue(preview(path).endswith('end'))
            self.assertLess(len(preview(path)), 263000)
            path.write_bytes(b'\x00binary')
            self.assertIn('Binary artifact', preview(path))


class WindowTests(unittest.TestCase):
    def test_saved_results_and_empty_history(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from .window import RunWindow
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            window = RunWindow(root)
            self.assertIn('No saved runs', window.overview.toPlainText())
            run = root / 'example'
            run.mkdir()
            (run / 'state.json').write_text(json.dumps({
                'status': 'needs_attention', 'config': {},
                'results': [{'stage': 'validation', 'status': 'needs_attention',
                             'message': 'System coverage pending', 'artifacts': {}}]}))
            window.refresh()
            window.show()
            app.processEvents()
            self.assertEqual(window.history.count(), 1)
            self.assertEqual(window.stages.topLevelItemCount(), 1)
            self.assertIn('System coverage pending', window.overview.toPlainText())
            window.files.setCurrentRow(0)
            self.assertIn('needs_attention', window.text.toPlainText())
            window.close()


if __name__ == '__main__':
    unittest.main()

class OutcomeTests(unittest.TestCase):
    def test_recorded_outcomes_preserve_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for outcome in ('passed', 'failed', 'not_run', 'needs_attention', 'waiting'):
                with self.subTest(outcome=outcome):
                    (directory / 'state.json').write_text(json.dumps({
                        'status': outcome, 'config': {'no_bist': True},
                        'results': [{'stage': 'validation', 'status': outcome,
                                     'message': 'Module scope; system coverage pending',
                                     'artifacts': {}}]}))
                    rendered = summary(load_run(directory))
                    self.assertIn('validation: ' + outcome, rendered)
                    self.assertIn('BIST: skipped', rendered)
                    self.assertIn('system coverage pending', rendered)
