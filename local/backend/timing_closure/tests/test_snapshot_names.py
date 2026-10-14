import shlex
import tempfile
import unittest
from pathlib import Path

from services.mapped_snapshot import snapshot_directory, snapshot_mapped
from timing_closure.tests import test_closure_runner as fixtures
from workers.workers import _build_gdsii_command


class SnapshotNameTests(unittest.TestCase):
    def test_suffix_and_parent_are_preserved(self):
        for source, expected in (
            ('build_mapped_200MHz_04', 'build_mapped_snapshot_200MHz_04'),
            ('build_mapped_200MHz_05', 'build_mapped_snapshot_200MHz_05'),
            ('runs/team/build_mapped_200MHz_04', 'runs/team/build_mapped_snapshot_200MHz_04'),
            ('/remote/project/build_mapped_200MHz_05', '/remote/project/build_mapped_snapshot_200MHz_05'),
            ('build_mapped_custom_suffix_04', 'build_mapped_snapshot_custom_suffix_04'),
        ):
            with self.subTest(source=source):
                self.assertEqual(snapshot_directory(source), expected)

    def test_unsupported_names_fail_clearly(self):
        for name in ('', '.', '/remote/custom', 'build_mapped_'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'build_mapped_<suffix>'):
                snapshot_directory(name)

    def test_old_snapshot_untouched_and_same_build_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / 'build_mapped_snapshot_200MHz_01'
            old.mkdir()
            (old / 'keep.txt').write_text('original')
            for index in ('04', '05'):
                source = root / f'build_mapped_200MHz_{index}'
                (source / 'results').mkdir(parents=True)
                (source / 'results/top.v').write_text('module top; endmodule\n')
                (source / 'results/top.sdc').write_text('create_clock -period 5 clk\n')
                destination = Path(snapshot_directory(str(source)))
                snapshot_mapped(source, destination)
                before = (destination / 'results/top.v').read_bytes()
                (source / 'results/top.v').write_text('changed\n')
                with self.assertRaises(FileExistsError):
                    snapshot_mapped(source, destination)
                self.assertEqual((destination / 'results/top.v').read_bytes(), before)
            self.assertEqual((old / 'keep.txt').read_text(), 'original')


class SnapshotLauncherTests(unittest.TestCase):
    setUp = fixtures.ClosureTests.setUp
    implementation = fixtures.ClosureTests.implementation
    run_closure = fixtures.ClosureTests.run_closure

    def test_selected_mapped_build_controls_snapshot_and_launch_arguments(self):
        for index in ('04', '05'):
            with self.subTest(index=index):
                # Separate fixture log/staging directories for each invocation.
                self.state['backend_log_dir'] = str(self.repo / index)
                source = f'/remote/team/build_mapped_200MHz_{index}'
                expected = f'/remote/team/build_mapped_snapshot_200MHz_{index}'
                self.state['mapped_candidate_path'] = source
                self.state['remote_mapped_dir'] = 'build_mapped_ignored_01'
                self.states.clear()
                ssh = fixtures.FakeSSH([0.012])
                result = self.run_closure(ssh)
                self.assertEqual(result['timing_closure_status'], 'passed')
                self.assertEqual(result['timing_closure_source_mapped_dir'], source)
                self.assertEqual(result['timing_closure_mapped_snapshot_dir'], expected)
                self.assertEqual(result['timing_closure_attempts'][0]['mapped_snapshot'], expected)
                self.assertEqual(shlex.split(ssh.reservations[0])[-2:], [source, expected])
                launched = self.states[0]
                self.assertEqual(launched['mapped_candidate_path'], expected)
                self.assertEqual(launched['remote_mapped_dir'], expected)
                command = _build_gdsii_command(launched)
                self.assertIn('MAPPEDDIR=' + expected + ';', command)
                self.assertIn('--mappeddir "$MAPPEDDIR"', command)


if __name__ == '__main__':
    unittest.main()
