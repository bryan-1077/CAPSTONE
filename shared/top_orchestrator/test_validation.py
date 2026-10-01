"""Run the real transfer adapter and remote wrapper against local fake tools."""

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.contracts import RunConfig, StageResult
from shared.top_orchestrator.mailbox import publish, verify
from shared.top_orchestrator.orchestrator import run
from shared.top_orchestrator.validation import validation_node, unpack_reports


PIPELINE = '''
import json, pathlib, sys
rtl, specs = map(pathlib.Path, sys.argv[1:3])
root = rtl.parent
mode = pathlib.Path(__file__).with_name('mode').read_text()
def summary():
    return dict(overall_status='PASS', verification_complete=True, execution_completed=True,
                execution_succeeded=True, needs_review=False, requested_stages=['agent1','agent2','agent3'],
                stages={name: dict(status='PASS', exit_code=0) for name in ('agent1','agent2','agent3')})
result = summary()
result.update(input_directory=str(root), inputs=[str(rtl),str(specs)], run_id='this_run', kind='batch')
result['designs'] = [dict(module_name=p.stem, yaml_file=str(p), result=summary()) for p in sorted(specs.glob('*.yaml'))]
if mode == 'gaps':
    result.update(overall_status='PASS_WITH_GAPS', verification_complete=False, needs_review=True)
if mode == 'missing_stage':
    result['designs'][0]['result']['requested_stages'] = ['agent1']
if mode == 'missing_design':
    result['designs'].pop()
if mode == 'rtl_failure':
    result.update(overall_status='FAIL', verification_complete=False)
if mode == 'wrong_input':
    result['input_directory'] = '/other/invocation'
if mode == 'mutated_input':
    (rtl / 'a.sv').write_text('changed')
if mode == 'single':
    result.pop('kind')
    result.pop('designs')
out = root / 'verification_reports/this_run'
out.mkdir(parents=True)
(out / 'summary.json').write_text(json.dumps(result))
(out / 'details.log').write_text('verification evidence')
print('fake pipeline finished', flush=True)
'''


class LocalSSH:
    """Uses local temp paths; strips only the Slurm/environment layer for tests."""
    instances = []
    def __init__(self, *args):
        self.host, self.username = args[:2]
        self.password = None
        self.uploads = []
        self.commands = []
        self.instances.append(self)

    def run(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("srun "):
            script = shlex.split(command)[-1]
            argv = shlex.split(script.splitlines()[-1])[1:]
            result = subprocess.run(argv, capture_output=True, text=True)
        else:
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", command], capture_output=True, text=True)
        if kwargs.get("on_output"):
            kwargs["on_output"]("stdout", result.stdout)
            kwargs["on_output"]("stderr", result.stderr)
        return dict(ok=result.returncode == 0, exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    def upload_file(self, local, remote, exclusive=False):
        self.uploads.append(remote)
        with Path(remote).open("xb" if exclusive else "wb") as output:
            output.write(Path(local).read_bytes())

    def fetch_file(self, remote, local):
        if not Path(remote).is_file():
            return dict(ok=False, error='missing report')
        shutil.copyfile(remote, local)
        return dict(ok=True)

    def close(self):
        pass


class ValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frontend = self.root / "frontend"
        rtl = self.frontend / "rtl_output"
        specs = self.frontend / "expanded"
        rtl.mkdir(parents=True)
        specs.mkdir()
        for name in ('a', 'b'):
            (rtl / f'{name}.sv').write_text(f'module {name}; endmodule')
            (specs / f'{name}.yaml').write_text(f'design_name: {name}\n')
        (rtl / 'manifest.json').write_text(json.dumps(dict(top_module='a', modules=['a','b'])))
        self.mailbox = publish(self.frontend, self.root / 'mailbox/revision_001')
        self.server = self.root / 'server project'
        self.server.mkdir()
        (self.server / 'pipeline.py').write_text(PIPELINE)
        (self.server / 'mode').write_text('pass')
        self.remote_config = self.root / 'remote.json'
        self.remote_config.write_text(json.dumps(dict(host='fake', username='fake', project_dir=str(self.server),
            partition='fake', qos='fake', python=sys.executable, validation_script='pipeline.py',
            allow_validation_output_dirs=True)))
        self.prepare_workspace('initial')
        self.config = RunConfig('validation', mailbox=self.mailbox, remote_config=self.remote_config)
        LocalSSH.instances = []

    def prepare_workspace(self, name):
        workspace = self.server / 'shared/validation_runs' / name
        for directory in ('input/rtl', 'input/specs', 'work'):
            (workspace / directory).mkdir(parents=True)
        data = json.loads(self.remote_config.read_text())
        data['validation_workspace'] = str(workspace)
        self.remote_config.write_text(json.dumps(data))
        return workspace

    def test_validation_recovers_from_rejected_password(self):
        original = LocalSSH.run
        attempts = []
        def login(ssh, command, **kwargs):
            attempts.append(ssh.password)
            if len(attempts) == 1:
                return dict(ok=False, exit_code=-1, stdout="", stderr="Authentication failed",
                            error_type="AuthenticationException")
            return original(ssh, command, **kwargs)
        run_dir = self.root / "retry_run"
        run_dir.mkdir()
        with patch("shared.top_orchestrator.validation.SSHExecutor", LocalSSH), patch.object(
                LocalSSH, "run", login), patch("getpass.getpass", side_effect=["wrong", "correct"]) as prompt:
            result = validation_node(replace(self.config, ask_password=True), run_dir)
        self.assertEqual(result.status, "passed", result.message)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(attempts[:2], ["wrong", "correct"])
        self.assertIsNone(LocalSSH.instances[-1].password)

    def test_default_blocks_pipeline_directory_creation_without_ssh(self):
        data = json.loads(self.remote_config.read_text())
        data['allow_validation_output_dirs'] = False
        self.remote_config.write_text(json.dumps(data))
        output = self.root / 'blocked'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor') as ssh:
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn('creates report and scratch directories', result.message)
        ssh.assert_not_called()

    def test_missing_workspace_is_created(self):
        data = json.loads(self.remote_config.read_text())
        missing = self.server / 'shared/missing'
        data['validation_workspace'] = str(missing)
        self.remote_config.write_text(json.dumps(data))
        output = self.root / 'missing_workspace'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        for directory in ('input/rtl', 'input/specs', 'work'):
            self.assertTrue((missing / directory).is_dir())

    def test_automatic_workspaces_are_unique_and_preserve_previous_results(self):
        data = json.loads(self.remote_config.read_text())
        data.pop('validation_workspace')
        data.pop('allow_validation_output_dirs')
        self.remote_config.write_text(json.dumps(data))
        nested = self.frontend / 'rtl_output/include/nested'
        nested.mkdir(parents=True)
        (nested / 'defs.svh').write_text('// definitions')
        mailbox = publish(self.frontend, self.root / 'mailbox/nested')
        workspaces = []
        for index in range(2):
            output = self.root / f'auto_{index}'
            output.mkdir()
            with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                result = validation_node(replace(self.config, mailbox=mailbox), output)
            self.assertEqual(result.status, 'passed', result.message)
            record = json.loads((output / 'validation_result.json').read_text())
            workspace = Path(record['remote_directory'])
            self.assertEqual(workspace.parent, self.server / 'shared/validation_runs')
            self.assertTrue((workspace / 'input/rtl/include/nested/defs.svh').is_file())
            workspaces.append(workspace)
        self.assertNotEqual(*workspaces)
        self.assertTrue(all((workspace / 'reports.zip').is_file() for workspace in workspaces))

    def test_used_explicit_workspace_is_rejected_without_overwriting(self):
        workspace = self.prepare_workspace('used')
        previous = workspace / 'request.json'
        previous.write_text('previous invocation')
        output = self.root / 'used'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertEqual(previous.read_text(), 'previous invocation')
        self.assertEqual(LocalSSH.instances[-1].uploads, [])
        self.assertFalse(any(c.startswith('srun ') for c in LocalSSH.instances[-1].commands))

    def test_remote_roundtrip_and_evidence_gates(self):
        for mode, status in [('pass','passed'), ('gaps','needs_attention'), ('missing_stage','needs_attention'),
                             ('missing_design','needs_attention'), ('rtl_failure','failed'),
                             ('wrong_input','needs_attention'), ('mutated_input','needs_attention')]:
            with self.subTest(mode=mode):
                self.prepare_workspace(mode)
                (self.server / 'mode').write_text(mode)
                output = self.root / mode
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, status, result.message)
                self.assertIsNone(result.failure)
                verify(self.mailbox)
                report = json.loads((output / 'validation_result.json').read_text())
                self.assertEqual(set(report['expected_designs']), {'a','b'})
                self.assertTrue((output / 'reports/verification_reports/this_run/details.log').is_file())
                self.assertIn('--time=04:00:00', report['command'])

    def test_specs_to_verification_stops_without_backend(self):
        approved = self.root / 'specs.yaml'
        approved.write_text('memory: {speed: 3200}')
        def preparation(config, directory):
            return replace(config, input_yaml=approved, target_mhz=200)
        with patch('shared.top_orchestrator.orchestrator.prepare_generation', preparation), patch(
                'shared.top_orchestrator.orchestrator.FRONTEND', self.frontend), patch(
                'shared.top_orchestrator.orchestrator.REPO_ROOT', self.root), patch(
                'shared.top_orchestrator.validation.SSHExecutor', LocalSSH), patch.dict(
                'shared.top_orchestrator.orchestrator.NODES', {
                    'generate': lambda c, d: StageResult('generate','passed','frontend checked'),
                    'backend': lambda c, d: self.fail('Backend must not run')}):
            result = run(RunConfig('generate', remote_config=self.remote_config), self.root / 'flow')
        self.assertEqual(result.status, 'complete')
        self.assertEqual([r.stage for r in result.results], ['generate', 'validation'])

    def test_failed_transfer_never_launches_pipeline(self):
        class BrokenSSH(LocalSSH):
            def upload_file(self, *args, **kwargs):
                raise OSError('transfer interrupted')
        output = self.root / 'failed_transfer'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', BrokenSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertFalse(any(c.startswith('srun ') for c in LocalSSH.instances[-1].commands))

    def test_workspace_alias_and_parent_segments_use_canonical_identity(self):
        for mode in ('symlink', 'parent'):
            with self.subTest(mode=mode):
                workspace = self.prepare_workspace('canonical_' + mode)
                if mode == 'symlink':
                    alias = self.server / 'workspace_alias'
                    alias.symlink_to(workspace, target_is_directory=True)
                else:
                    alias = workspace / '..' / workspace.name
                data = json.loads(self.remote_config.read_text())
                data['validation_workspace'] = str(alias)
                self.remote_config.write_text(json.dumps(data))
                output = self.root / ('alias_' + mode)
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'passed', result.message)
                record = json.loads((output / 'validation_result.json').read_text())
                self.assertEqual(record['configured_remote_directory'], str(alias))
                self.assertEqual(record['remote_directory'], str(workspace.resolve()))
                self.assertEqual(record['remote_result']['input_directory'], str(workspace.resolve() / 'input'))
                self.assertTrue(all(path.startswith(str(workspace.resolve()) + '/')
                                    for path in LocalSSH.instances[-1].uploads))

    def test_missing_remote_pipeline_preserves_diagnostic(self):
        (self.server / 'pipeline.py').unlink()
        output = self.root / 'missing_pipeline'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn('entry point does not exist', result.message)

    def test_missing_dependency_preserves_traceback_and_result(self):
        (self.server / 'pipeline.py').write_text(
            "raise ModuleNotFoundError(\"No module named 'yaml'\")\n"
        )
        output = self.root / 'missing_dependency'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn("No module named 'yaml'", result.message)
        payload = json.loads((output / 'remote_validation_result.json').read_text())
        self.assertEqual(payload['pipeline_returncode'], 1)
        self.assertEqual(payload['python_executable'], sys.executable)
        self.assertIn('ModuleNotFoundError', payload['pipeline_stderr_tail'])
        self.assertTrue(payload['input_unchanged'])
        self.assertTrue((output / 'reports.zip').is_file())

    def test_missing_result_preserves_remote_execution_diagnostic(self):
        class MissingResultSSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    self.commands.append(command)
                    return dict(ok=False, exit_code=1, stdout='',
                                stderr="ModuleNotFoundError: No module named 'yaml'")
                return super().run(command, **kwargs)

        output = self.root / 'missing_result'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', MissingResultSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn('Could not retrieve result.json', result.message)
        self.assertIn("No module named 'yaml'", result.message)

    def test_unusable_downloads_cannot_pass(self):
        for mode in ('missing', 'stale', 'corrupt'):
            with self.subTest(mode=mode):
                self.prepare_workspace(mode)
                class BadDownloadSSH(LocalSSH):
                    def fetch_file(self, remote, local):
                        if mode == 'missing':
                            return dict(ok=False, error='download interrupted')
                        result = super().fetch_file(remote, local)
                        if mode == 'stale' and remote.endswith('result.json'):
                            data = json.loads(Path(local).read_text())
                            data['invocation_id'] = 'previous-run'
                            Path(local).write_text(json.dumps(data))
                        if mode == 'corrupt' and remote.endswith('reports.zip'):
                            Path(local).write_bytes(b'corrupt')
                        return result
                output = self.root / mode
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', BadDownloadSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'needs_attention', result.message)

    def test_interrupted_remote_job_requests_cancellation(self):
        class TimedOutSSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    self.commands.append(command)
                    return dict(ok=False, exit_code=-1, stdout='', stderr='timeout')
                if command.startswith('scancel '):
                    self.commands.append(command)
                    return dict(ok=True, exit_code=0, stdout='', stderr='')
                return super().run(command, **kwargs)
        output = self.root / 'timeout'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', TimedOutSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertTrue(LocalSSH.instances[-1].commands[-1].startswith('scancel --name capstone-verif-'))

    def test_cli_accepts_remote_options_for_specs_and_validation(self):
        from shared.top_orchestrator.__main__ import main
        from io import StringIO
        for entry in (['generate'], ['validation', '--mailbox', str(self.mailbox)]):
            with patch('sys.argv', ['orchestrator', *entry, '--remote-config', str(self.remote_config),
                                    '--ask-password', '--plan']), patch('sys.stdout', StringIO()) as output, patch(
                    'shared.top_orchestrator.validation.SSHExecutor') as transport:
                self.assertEqual(main(), 0)
                transport.assert_not_called()
                self.assertIn('no backend', output.getvalue())

    def test_archive_cannot_escape_report_directory(self):
        archive = self.root / 'malicious.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('../escape.txt', 'bad')
        with self.assertRaises(ValueError):
            unpack_reports(archive, self.root / 'reports')
        self.assertFalse((self.root / 'escape.txt').exists())

    def test_single_design_pipeline_is_supported(self):
        # Publish a one-design snapshot and exercise the existing single-run schema.
        (self.frontend / 'expanded/b.yaml').unlink()
        mailbox = publish(self.frontend, self.root / 'mailbox/revision_002')
        (self.server / 'mode').write_text('single')
        output = self.root / 'single'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(replace(self.config, mailbox=mailbox), output)
        self.assertEqual(result.status, 'passed', result.message)
        self.assertIn('No individual YAML verification for: b', result.message)


if __name__ == '__main__':
    unittest.main()
