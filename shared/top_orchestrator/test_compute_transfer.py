import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from shared.top_orchestrator.compute_transfer import receive_archive, upload_validation_archive
from shared.top_orchestrator.remote import RemoteConfig


class ComputeTransferTests(unittest.TestCase):
    def receive(self, root, entries, manifest=None, job='777', expected_job='777', create=False):
        if manifest is None:
            manifest = {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                        for name, data in entries}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, data in entries:
                archive.writestr(name, data)
        script = inspect.getsource(receive_archive) + '\nreceive_archive(' + ', '.join(
            repr(value) for value in (str(root), manifest, expected_job, create)) + ')'
        return subprocess.run([sys.executable, '-c', script], input=stream.getvalue(),
                              capture_output=True, env={**os.environ, 'SLURM_JOB_ID': job}, timeout=10)

    def test_round_trip_verifies_bytes_and_preserves_existing_files(self):
        entries = [('input/rtl/top.sv', b'module top; endmodule\n'), ('runner.py', b'# launcher\n')]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.receive(root, entries)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'"verified_files": 2', result.stdout)
            for name, data in entries:
                self.assertEqual((root / name).read_bytes(), data)
            result = self.receive(root, entries)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'already exists', result.stderr)
            self.assertEqual((root / 'input/rtl/top.sv').read_bytes(), entries[0][1])

    def test_corrupt_or_wrong_inventory_never_produces_receipt(self):
        good = b'expected'
        manifest = {'input/top.sv': {'size': len(good), 'sha256': hashlib.sha256(good).hexdigest()}}
        for entries in ([('input/top.sv', b'tampered')], [('extra', good)],
                        [('input/top.sv', good), ('input/top.sv', good)]):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                result = self.receive(Path(temporary), entries, manifest)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b'CAPSTONE_TRANSFER_RESULT:', result.stdout)

    def test_standalone_compute_creates_workspace_exclusively_and_requires_a_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'mailbox_inputs' / 'fresh workspace'
            entries = [('nested/top.sv', b'module top; endmodule')]
            result = self.receive(root, entries, expected_job=None, create=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'"job_id": "777"', result.stdout)
            result = self.receive(root, entries, expected_job=None, create=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b'CAPSTONE_TRANSFER_RESULT:', result.stdout)
            self.assertEqual((root / 'nested/top.sv').read_bytes(), entries[0][1])
            other = root.parent / 'unallocated'
            result = self.receive(other, entries, job='', expected_job=None, create=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(other.exists())

    def test_traversal_symlink_and_wrong_node_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'workspace'
            root.mkdir()
            result = self.receive(root, [('../escape', b'x')])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root.parent / 'escape').exists())
            (root / 'input').symlink_to(root.parent, target_is_directory=True)
            result = self.receive(root, [('input/escape', b'x')])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root.parent / 'escape').exists())
            result = self.receive(root, [('runner.py', b'x')], job='different')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'runner.py').exists())

    def test_interrupted_transfer_cancels_only_its_receiver_step(self):
        remote = RemoteConfig('fake', 'fake', '/project', 'fake', 'fake')
        lease = {'job_id': '777', 'cpus': 6, 'host': 'fake', 'username': 'fake', 'port': 22}
        for failure in (KeyboardInterrupt(), {'ok': False, 'exit_code': -1, 'stderr': 'timed out'}):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / 'source'
                source.write_bytes(b'RTL')
                ssh = Mock()
                ssh.run.side_effect = [failure, {'ok': True}]
                with patch('shared.top_orchestrator.allocation.active_allocation', return_value=lease), patch(
                        'shared.top_orchestrator.compute_transfer.active_allocation', return_value=lease):
                    with self.assertRaises((KeyboardInterrupt, RuntimeError)):
                        upload_validation_archive(ssh, remote, '/workspace', {'input/top.sv': source},
                                                  Path(temporary) / 'archive.zip', 'owned-transfer', Mock())
                self.assertEqual(ssh.run.call_count, 2)
                self.assertIn('stdin_path', ssh.run.call_args_list[0].kwargs)
                self.assertIn('owned-transfer', ssh.run.call_args_list[1].args[0])
                self.assertNotEqual(ssh.run.call_args_list[1].args[0], 'scancel 777')


if __name__ == '__main__':
    unittest.main()
