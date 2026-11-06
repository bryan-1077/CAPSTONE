"""Exercise the real review/edit payloads through a simulated Slurm boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workers.workers import (
    _build_prepare_review_command, _build_netlist_review_command,
    _build_mapped_review_command, _build_gdsii_review_command,
    _build_prepare_fix_command, _build_netlist_edit_command,
    _build_mapped_edit_command, _build_gdsii_edit_command,
)


class ComputeReviewTests(unittest.TestCase):
    def fixture(self, root, shared):
        project = root / "project with spaces $(touch injected)"
        project.mkdir()
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        scheduler = bin_dir / 'srun'
        scheduler.write_text(f'#!{sys.executable}\n' + '''
import json, os, subprocess, sys
from pathlib import Path
arguments = sys.argv[1:]
Path(os.environ['COMPUTE_TEST_DISPATCH']).write_text(json.dumps({
    'arguments': arguments, 'cwd_before_dispatch': os.getcwd(),
}))
if os.environ.get('COMPUTE_TEST_REJECT'):
    raise SystemExit(42)
shell = arguments.index('/bin/bash')
payload = arguments[shell + 2].replace('source ~/.bashrc', ':', 1)
raise SystemExit(subprocess.run(['/bin/bash', '-c', payload],
    env=dict(os.environ, HOSTNAME='compute-fixture', SLURM_JOB_ID='777')).returncode)
''')
        scheduler.chmod(0o755)
        state = {'remote_project_root': str(project), 'remote_python_cmd': sys.executable}
        if shared:
            state.update(shared_allocation_id='777', shared_allocation_cpus=6)
        environment = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
                       'COMPUTE_TEST_DISPATCH': str(root / 'dispatch.json')}
        return project, state, environment

    def execute(self, root, command, environment):
        result = subprocess.run(['/bin/bash', '-c', command], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('running on compute-fixture', result.stderr)
        # Review callers require the final stdout line to remain valid JSON.
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        dispatch = json.loads((root / 'dispatch.json').read_text())
        self.assertEqual(dispatch['cwd_before_dispatch'], str(root))
        self.assertFalse((root / 'injected').exists())
        return payload, dispatch

    def test_all_four_reviews_execute_in_compute_and_keep_failure_findings(self):
        for shared in (True, False):
            with self.subTest(shared=shared), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, state, environment = self.fixture(root, shared)
                (project / 'empty prepared').mkdir()
                commands = [
                    _build_prepare_review_command(state, 'empty prepared'),
                    _build_netlist_review_command(state, 'missing prepared', 'missing netlist'),
                    _build_mapped_review_command(state, 'missing netlist', 'missing mapped'),
                    _build_gdsii_review_command(state, 'missing mapped', 'missing GDSII'),
                ]
                for command in commands:
                    payload, dispatch = self.execute(root, command, environment)
                    self.assertFalse(payload['pass'])
                    self.assertTrue(payload['errors'])
                    if shared:
                        self.assertIn('--jobid=777', dispatch['arguments'])
                        self.assertIn('--cpus-per-task=6', dispatch['arguments'])
                    else:
                        self.assertIn('--partition=adademic', dispatch['arguments'])
                self.assertEqual(list(project.rglob('*')), [project / 'empty prepared'])

    def test_all_stage_editors_keep_bounded_edits_and_literal_text_on_compute(self):
        for shared in (True, False):
            with self.subTest(shared=shared), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project, state, environment = self.fixture(root, shared)
                outside = root / 'outside.txt'
                outside.write_text('original')
                (project / 'alias.txt').symlink_to(outside)
                target = project / 'target.txt'
                replacement = "literal '$(touch injected)'\nsecond line"
                actions = [{'type': 'replace_text', 'path': name,
                            'search_text': 'original', 'replacement_text': replacement}
                           for name in ('target.txt', '../outside.txt', 'alias.txt')]
                commands = [
                    _build_prepare_fix_command(state, '.', actions),
                    _build_netlist_edit_command(state, '.', actions),
                    _build_mapped_edit_command(state, '.', actions),
                    _build_gdsii_edit_command(state, actions),
                ]
                for command in commands:
                    target.write_text('original')
                    payload, _ = self.execute(root, command, environment)
                    self.assertEqual(len(payload['applied_actions']), 1, payload)
                    self.assertEqual(len(payload['skipped_actions']), 2, payload)
                    self.assertEqual(target.read_text(), replacement)
                    self.assertEqual(outside.read_text(), 'original')
                    self.assertFalse((project / 'injected').exists())

    def test_rejected_compute_step_never_applies_edits_on_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, state, environment = self.fixture(root, shared=True)
            target = project / 'target.txt'
            target.write_text('original')
            command = _build_prepare_fix_command(state, '.', [
                {'type': 'replace_text', 'path': 'target.txt',
                 'search_text': 'original', 'replacement_text': 'changed'},
            ])
            result = subprocess.run(['/bin/bash', '-c', command], cwd=root,
                env={**environment, 'COMPUTE_TEST_REJECT': '1'}, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 42)
            self.assertEqual(target.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
