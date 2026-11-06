import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from . import run_storage


class RunStorageTests(unittest.TestCase):
    def test_log_moves_into_run_while_open_and_preserves_startup_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                run_storage, 'RUNTIME_WORKERS', Path(tmp) / 'private'):
            run = Path(tmp) / 'runs' / 'gui_test'
            storage = run_storage.RunStorage(run, {'lifecycle': 'running'})
            self.assertFalse(run.exists())
            with storage.log.open('w', encoding='utf-8') as stream:
                output = run_storage.RunOutput(storage, stream)
                output.write('Starting worker\n')
                output.flush()
                self.assertEqual(run_storage.worker_log_path(run), storage.log)
                # The orchestrator must still be able to create a fresh folder.
                run.mkdir(parents=True, exist_ok=False)
                output.write('Generating RTL\n')
                output.flush()
                self.assertEqual(run_storage.worker_log_path(run), run / 'worker.log')
                self.assertEqual((run / 'worker.log').read_text(), 'Starting worker\nGenerating RTL\n')
                self.assertTrue((run / 'gui_lifecycle.json').is_file())
                self.assertFalse(storage.startup.exists())
            self.assertEqual(list(run.parent.iterdir()), [run])

    def test_legacy_files_are_read_without_migration_and_contained_files_take_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'old_run'
            run.mkdir()
            legacy_log = Path(tmp) / 'old_run.worker.log'
            legacy_receipt = Path(tmp) / 'old_run.lifecycle.json'
            legacy_log.write_text('old output')
            legacy_receipt.write_text('{"lifecycle":"complete"}')
            self.assertEqual(run_storage.worker_log_path(run), legacy_log)
            self.assertEqual(run_storage.lifecycle_path(run), legacy_receipt)
            (run / 'worker.log').write_text('contained output')
            (run / 'gui_lifecycle.json').write_text('{}')
            self.assertEqual(run_storage.worker_log_path(run), run / 'worker.log')
            self.assertEqual(run_storage.lifecycle_path(run), run / 'gui_lifecycle.json')
            self.assertTrue(legacy_log.exists())

    def test_consolidation_preserves_receipt_and_skips_live_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'old_run'
            receipt = Path(tmp) / 'old_run.lifecycle.json'
            log = Path(tmp) / 'old_run.worker.log'
            text = '{"lifecycle":"complete","remote_cleanup":{"status":"cleanup_unconfirmed"}}'
            receipt.write_text(text)
            log.write_text('original output')
            with patch('shared.gui.registry.alive', return_value=True):
                self.assertEqual(run_storage.consolidate_completed_run(run), [])
                self.assertTrue(log.exists())
            with patch('shared.gui.registry.alive', return_value=False):
                self.assertEqual(run_storage.consolidate_completed_run(run), ['gui_lifecycle.json', 'worker.log'])
            self.assertEqual((run / 'gui_lifecycle.json').read_text(), text)
            self.assertEqual((run / 'worker.log').read_text(), 'original output')
            self.assertFalse(receipt.exists())
            self.assertFalse(log.exists())
            self.assertEqual(run_storage.consolidate_completed_run(run), [])

    def test_worker_main_contains_artifacts_on_success_and_early_failure(self):
        script = '''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace
from shared.gui import worker, registry, run_storage, lifecycle, interaction
root, fail = Path(sys.argv[1]), sys.argv[2] == 'fail'
run = root / 'runs' / 'gui_test'
run_storage.RUNTIME_WORKERS = root / 'private'
registry.ACTIVE = root / 'active.json'
registry.atomic_write(registry.ACTIVE, {'worker_id': 'gui_test'})
def execute(config, password, directory, adapter):
    assert not directory.exists()
    if fail:
        raise RuntimeError('early startup failure')
    directory.mkdir(parents=True, exist_ok=False)
    print('Worker log is now contained', flush=True)
    assert (directory / 'worker.log').is_file()
    registry.atomic_write(directory / 'state.json', {'status': 'complete', 'config': {}})
    return SimpleNamespace(status='complete')
worker.execute = execute
interaction.Interaction = lambda *a, **kw: SimpleNamespace(close=lambda: None)
lifecycle.cleanup_local = lambda: {'status': 'terminated'}
lifecycle.remote_cleanup = lambda *a: {'status': 'not_required'}
lock = os.open(root / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
read_fd, write_fd = os.pipe()
with os.fdopen(write_fd, 'w') as stream:
    json.dump({'config': {'skip_verification': True, 'no_backend': True}, 'password': 'not-journaled'}, stream)
sys.argv = ['worker', str(run), str(lock), str(read_fd), str(root / 'fake-socket')]
original_out, original_err = sys.stdout, sys.stderr
code = worker.main()
sys.stdout, sys.stderr = original_out, original_err
assert code == (1 if fail else 0), code
assert sorted(p.name for p in run.parent.iterdir()) == ['gui_test']
assert (run / 'worker.log').is_file()
receipt = json.loads((run / 'gui_lifecycle.json').read_text())
assert receipt['lifecycle'] == ('failed' if fail else 'complete')
assert 'not-journaled' not in (run / 'gui_lifecycle.json').read_text()
assert json.loads((run / 'state.json').read_text())['status'] == receipt['lifecycle']
assert not run_storage.startup_directory(run).exists()
'''
        for mode in ('success', 'fail'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, '-c', script, tmp, mode],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
