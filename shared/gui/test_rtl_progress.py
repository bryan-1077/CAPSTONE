import json
from pathlib import Path
import tempfile
import unittest
from .rtl_progress import module_progress, module_count_text
from .run_reader import SavedRun


class ModuleProgressTests(unittest.TestCase):
    def test_count_distinguishes_observed_modules_from_generated_modules(self):
        description = module_count_text([
            ('active', 'running', 'Generating'), ('done', 'passed', 'Generated'),
            ('bad', 'failed', 'Generation failed')])
        self.assertIn('3 modules observed', description)
        self.assertIn('1 generated', description)
        self.assertIn('1 generating', description)
        self.assertIn('1 failed', description)
        self.assertIn('verification is a separate stage', description)

    def test_log_tracks_modules_without_promoting_them_to_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            log = directory.parent / 'run.worker.log'
            log.write_text('--- CREATING --- queue.sv file --- LLM\n'
                           '--- FINISHED --- queue.sv file --- SUCCESS\n'
                           '--- CREATING --- scheduler.sv file --- LLM\n')
            run = SavedRun(directory, {})
            self.assertEqual(module_progress(run, alive=True), [
                ('scheduler', 'running', 'Generating'), ('queue', 'passed', 'Generated')])
            self.assertEqual(module_progress(run)[1],
                             ('scheduler', 'unconfirmed', 'Generation unconfirmed'))
            with log.open('a') as handle:
                handle.write('--- FINISHED --- scheduler.sv file --- FAILURE\n')
            self.assertEqual(module_progress(run)[1], ('scheduler', 'failed', 'Generation failed'))

    def test_snapshot_adds_integration_modules_and_ignores_mutable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'run'
            directory.mkdir()
            snapshot = Path(tmp) / 'snapshot' / 'rtl'
            snapshot.mkdir(parents=True)
            (snapshot / 'manifest.json').write_text(json.dumps({
                'generated_modules': ['controller_top', 'bank_top']}))
            run = SavedRun(directory, {'config': {'mailbox': str(snapshot.parent)},
                'results': [{'stage': 'generate', 'status': 'passed', 'artifacts': {
                    'manifest': str(Path(tmp) / 'mutable_manifest.json')}}]})
            self.assertEqual(module_progress(run), [
                ('controller_top', 'passed', 'Generated'), ('bank_top', 'passed', 'Generated')])
            (snapshot / 'manifest.json').write_text('{')
            self.assertEqual(module_progress(run), [])
            run.state['config'].clear()
            (Path(tmp) / 'mutable_manifest.json').write_text('{"generated_modules": ["old_module"]}')
            self.assertEqual(module_progress(run), [])
