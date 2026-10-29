"""Run the real transfer adapter and remote wrapper against local fake tools."""

import json
import hashlib
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
from shared.top_orchestrator.nodes import backend_node
from shared.top_orchestrator.validation import validation_node, unpack_reports


PIPELINE = '''
import json, pathlib, sys
system = '--system' in sys.argv
rtl = pathlib.Path(sys.argv[1])
specs = rtl.parent / 'specs' if system else pathlib.Path(sys.argv[2])
if system:
    assert sys.argv[2:] == ['--system'], sys.argv
root = rtl.parent
mode = pathlib.Path(__file__).with_name('mode').read_text()
def summary():
    return dict(overall_status='PASS', verification_complete=True, execution_completed=True,
                execution_succeeded=True, needs_review=False, requested_stages=['agent1','agent2','agent3'],
                stages={name: dict(status='PASS', exit_code=0) for name in ('agent1','agent2','agent3')})
result = summary()
result.update(input_directory=str(root), inputs=[str(rtl),str(specs)], run_id='this_run', kind='batch')
result['designs'] = [dict(module_name=p.stem, yaml_file=str(p), result=summary()) for p in sorted(specs.glob('*.yaml'))]
if system:
    result.pop('kind')
    result.update(mode='system', input_directory=str(rtl), inputs=[str(rtl / 'manifest.json')],
                  manifest_path=str(rtl / 'manifest.json'), top_module='a', ready_for_backend=True,
                  verification_readiness='READY_FOR_BACKEND',
                  final_system_report='final_system_report.json')
    result.pop('designs')
if mode == 'gaps':
    result.update(overall_status='PASS_WITH_GAPS', verification_complete=False, needs_review=True)
    result['stages']['agent3'].update(status='PASS_WITH_GAPS',
                                    coverage_limitations=['unobservable protocol timing'])
if mode == 'summary_not_ready':
    result['verification_readiness'] = 'BLOCKED_SYSTEM_VALIDATION'
if mode == 'summary_missing_readiness':
    result.pop('verification_readiness')
if mode == 'ready_boolean_false':
    result['ready_for_backend'] = False
if mode == 'missing_stage':
    (result if system else result['designs'][0]['result'])['requested_stages'] = ['agent1']
if mode == 'missing_design':
    result['designs'].pop()
if mode == 'rtl_failure':
    result.update(overall_status='FAIL', verification_complete=False)
if mode in ('agent2_failure', 'agent3_failure'):
    result.update(overall_status='FAIL', verification_complete=False)
    result['stages'][mode.split('_')[0]]['status'] = 'FAIL'
if mode == 'wrong_input':
    result['input_directory'] = '/other/invocation'
if mode == 'wrong_inputs':
    result['inputs'] = [str(specs)]
if mode == 'batch_in_system':
    result['mode'] = 'batch'
if mode == 'mutated_input':
    (rtl / 'a.sv').write_text('changed')
if mode == 'single':
    result.pop('kind')
    result.pop('designs')
out = (rtl if system else root) / 'verification_reports/this_run'
out.mkdir(parents=True)
(out / 'summary.json').write_text(json.dumps(result))
if system:
    final = dict(mode='system', top_module='a', status='PASS', ready_for_backend=True,
                 verification_readiness='READY_FOR_BACKEND',
                 functional_rtl='PASS', protocol_timing_compliance='PASS', coverage_limitations=[])
    if mode == 'gaps':
        final.update(status='PASS_WITH_GAPS', protocol_timing_compliance='PASS_WITH_GAPS',
                     coverage_limitations=['unobservable protocol timing'])
    if mode == 'final_gaps_only':
        final.update(status='PASS_WITH_GAPS', protocol_timing_compliance='PASS_WITH_GAPS')
    if mode == 'wrong_final_top':
        final['top_module'] = 'other_controller'
    if mode == 'not_ready':
        final['ready_for_backend'] = False
        final['verification_readiness'] = 'BLOCKED_SYSTEM_AGENT_3'
    if mode == 'missing_readiness':
        final.pop('verification_readiness')
    if mode == 'ready_boolean_false':
        final['ready_for_backend'] = False
    if mode in ('agent2_failure', 'agent3_failure'):
        final.update(status='FAIL', ready_for_backend=False,
                     verification_readiness='BLOCKED_SYSTEM_' + mode.split('_')[0].upper())
    (out / 'final_system_report.json').write_text(json.dumps(final))
(out / 'details.log').write_text('verification evidence')
if mode == 'report_symlink':
    (out / 'work').mkdir()
    (out / 'work/simulator').symlink_to(sys.executable)
if mode == 'input_symlink':
    (rtl / 'alias.sv').symlink_to(rtl / 'a.sv')
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
            allow_validation_output_dirs=True, validation_mode='modules')))
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

    def test_system_mode_runs_mailbox_rtl_and_collects_nested_reports(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        output = self.root / 'system_run'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        payload = json.loads((output / 'remote_validation_result.json').read_text())
        self.assertEqual(payload['validation_mode'], 'system')
        self.assertEqual(payload['command'][-1], '--system')
        self.assertEqual(payload['command'][3], payload['input_directory'] + '/rtl')
        self.assertNotIn(payload['input_directory'] + '/specs', payload['command'])
        self.assertTrue(payload['input_unchanged'])
        self.assertEqual(payload['summary_file'], 'rtl/verification_reports/this_run/summary.json')
        self.assertTrue(Path(result.artifacts['summary']).is_file())
        self.assertEqual(verify(self.mailbox), verify(output / 'validation_input'))
        self.assertIn('Readiness: READY_FOR_BACKEND', result.message)

    def test_shared_job_verifies_snapshot_inside_existing_allocation(self):
        from shared.top_orchestrator.allocation import ENVIRONMENT
        data = json.loads(self.remote_config.read_text())
        data.pop('validation_workspace')
        self.remote_config.write_text(json.dumps(data))
        destination = self.root / 'shared_run'
        destination.mkdir()
        lease = dict(job_id='777', cpus=6, host='fake', username='fake', port=22)
        with patch.dict('os.environ', {ENVIRONMENT: json.dumps(lease)}), patch(
                'shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, destination)
        self.assertEqual(result.status, 'passed', result.message)
        record = json.loads((destination / 'validation_result.json').read_text())
        self.assertIn('--jobid=777', record['command'])
        self.assertIn('/shared/experiments/shared-allocation/777/validation_runs/', record['remote_directory'])

    def test_behavioral_failure_routes_to_debug_only_for_matching_live_rtl(self):
        from shared.top_orchestrator.mailbox import verify_frontend_revision
        from shared.top_orchestrator.orchestrator import next_stage
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        for mode, changed in (('agent2_failure', False), ('agent3_failure', False),
                              ('agent3_failure', True)):
            with self.subTest(mode=mode, changed=changed):
                name = mode + str(changed)
                self.prepare_workspace(name)
                (self.server / 'mode').write_text(mode)
                output = self.root / name
                output.mkdir()
                if changed:
                    (self.frontend / 'rtl_output/a.sv').write_text('module a; wire changed; endmodule')
                def verify_live(frontend, mailbox):
                    verify_frontend_revision(self.frontend, mailbox)
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH), \
                     patch('shared.top_orchestrator.validation.verify_frontend_revision', side_effect=verify_live):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'failed', result.message)
                if changed:
                    self.assertIsNone(result.failure)
                    self.assertIsNone(next_stage(result, self.config))
                else:
                    self.assertEqual(result.failure.source, 'verif')
                    self.assertTrue((result.failure.log / 'summary.json').is_file())
                    self.assertEqual(next_stage(result, self.config), 'debug')

    def test_system_mode_trusts_readiness_with_gaps_or_different_evidence(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        for mode in ('gaps', 'final_gaps_only', 'missing_stage', 'ready_boolean_false'):
            with self.subTest(mode=mode):
                self.prepare_workspace('ready_' + mode)
                (self.server / 'mode').write_text(mode)
                output = self.root / ('ready_' + mode)
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'passed', result.message)
                final = json.loads(Path(result.artifacts['final_system_report']).read_text())
                self.assertEqual(final['verification_readiness'], 'READY_FOR_BACKEND')
                if mode == 'gaps':
                    self.assertEqual(final['status'], 'PASS_WITH_GAPS')
                    self.assertTrue(final['coverage_limitations'])

    def test_system_mode_rejects_wrong_scope_and_mutated_rtl(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        for mode in ('wrong_input', 'wrong_inputs', 'batch_in_system', 'mutated_input', 'input_symlink'):
            with self.subTest(mode=mode):
                self.prepare_workspace('system_' + mode)
                (self.server / 'mode').write_text(mode)
                output = self.root / ('system_' + mode)
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'needs_attention', result.message)

    def test_system_reports_allow_simulator_symlinks_without_mutating_inputs(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        (self.server / 'mode').write_text('report_symlink')
        output = self.root / 'system_symlink_reports'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        with zipfile.ZipFile(output / 'reports.zip') as archive:
            self.assertFalse(any(name.endswith('/simulator') for name in archive.namelist()))

    def test_system_final_report_is_required_for_readiness_and_identity(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        for mode in ('wrong_final_top', 'not_ready', 'missing_readiness',
                     'summary_not_ready', 'summary_missing_readiness'):
            with self.subTest(mode=mode):
                self.prepare_workspace(mode)
                (self.server / 'mode').write_text(mode)
                output = self.root / mode
                output.mkdir()
                with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
                    result = validation_node(self.config, output)
                self.assertEqual(result.status, 'needs_attention', result.message)
                self.assertTrue(Path(result.artifacts['final_system_report']).is_file())

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
                'shared.top_orchestrator.validation.SSHExecutor', LocalSSH), patch(
                'shared.top_orchestrator.auth.getpass.getpass', return_value='flow-test-password') as prompt, patch.dict(
                'shared.top_orchestrator.orchestrator.NODES', {
                    'generate': lambda c, d: StageResult('generate','passed','frontend checked'),
                    'backend': lambda c, d: self.fail('Backend must not run')}):
            result = run(RunConfig('generate', remote_config=self.remote_config, no_backend=True), self.root / 'flow')
        self.assertEqual(result.status, 'complete')
        self.assertEqual([r.stage for r in result.results], ['generate', 'validation'])
        prompt.assert_called_once()
        for path in (self.root / 'flow').rglob('*.json'):
            self.assertNotIn('flow-test-password', path.read_text())

    def test_default_flow_runs_backend_on_ready_snapshot_with_shared_password(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        (self.server / 'mode').write_text('gaps')
        secret = 'full-flow-test-password'
        digest = hashlib.sha256(secret.encode()).hexdigest()
        approved = self.root / 'approved.yaml'
        approved.write_text('memory: {speed: 3200}')
        seen = []
        def backend(config, directory):
            seen.append(config)
            metadata = verify(config.mailbox)
            payload = dict(current_stage='done', mailbox_revision=metadata['rtl_revision'],
                           lec_status='passed',
                           timing_target_clock_period_ns=5.0,
                           stage_status=dict.fromkeys(('rtl_prep', 'netlist', 'mapped_netlist', 'lec', 'gdsii'), 'success'))
            script = ("import hashlib, os, pathlib, sys\n"
                      "assert os.environ['CAPSTONE_SSH_USER'] == 'fake'\n"
                      "assert os.environ['CAPSTONE_SSH_HOST'] == 'fake'\n"
                      "fd = int(sys.argv[sys.argv.index('--ssh-password-fd') + 1])\n"
                      "with os.fdopen(fd, 'rb') as credential: password = credential.read()\n"
                      f"assert hashlib.sha256(password).hexdigest() == {digest!r}\n"
                      "path = pathlib.Path(sys.argv[sys.argv.index('--result-json') + 1])\n"
                      f"path.write_text({json.dumps(payload)!r})\n")
            with patch('shared.top_orchestrator.nodes.backend_command', return_value=[sys.executable, '-c', script]):
                return backend_node(config, directory)
        with patch('shared.top_orchestrator.orchestrator.prepare_generation',
                   side_effect=lambda config, directory: replace(config, input_yaml=approved, target_mhz=200)), patch(
                'shared.top_orchestrator.orchestrator.FRONTEND', self.frontend), patch(
                'shared.top_orchestrator.orchestrator.REPO_ROOT', self.root), patch(
                'shared.top_orchestrator.validation.SSHExecutor', LocalSSH), patch(
                'shared.top_orchestrator.auth.getpass.getpass', return_value=secret) as prompt, patch.dict(
                'shared.top_orchestrator.orchestrator.NODES', {
                    'generate': lambda c, d: StageResult('generate', 'passed', 'frontend checked'),
                    'backend': backend,
                    'gdsii-validation': lambda c, d: StageResult('gdsii-validation', 'needs_attention', 'review required')}):
            state = run(RunConfig('generate', remote_config=self.remote_config), self.root / 'full_flow')
        self.assertEqual(state.status, 'needs_attention', state.results[-1].message)
        self.assertEqual([result.stage for result in state.results], ['generate', 'validation', 'backend', 'gdsii-validation'])
        self.assertFalse(seen[0].allow_unvalidated)
        self.assertEqual(seen[0].mailbox, state.config.mailbox)
        self.assertEqual(seen[0].target_mhz, 200)
        self.assertEqual(state.results[-2].artifacts['validation'], 'passed')
        prompt.assert_called_once()
        for path in (self.root / 'full_flow').rglob('*.json'):
            self.assertNotIn(secret, path.read_text())

    def test_backend_rejects_stale_or_blocked_validation_before_launch(self):
        data = json.loads(self.remote_config.read_text())
        data['validation_mode'] = 'system'
        self.remote_config.write_text(json.dumps(data))
        output = self.root / 'accepted_validation'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        receipt = Path(result.artifacts['result'])
        original = json.loads(receipt.read_text())
        for mode in ('stopped', 'other_mailbox', 'other_revision', 'blocked'):
            with self.subTest(mode=mode):
                record = json.loads(json.dumps(original))
                if mode == 'stopped':
                    record['status'] = 'needs_attention'
                elif mode == 'other_mailbox':
                    record['mailbox'] = str(self.root / 'other')
                elif mode == 'other_revision':
                    record['snapshot_digest'] = 'stale'
                else:
                    record['system_result']['verification_readiness'] = 'BLOCKED_SYSTEM_AGENT_3'
                receipt.write_text(json.dumps(record))
                with patch('shared.top_orchestrator.nodes.stream_command') as command:
                    outcome = backend_node(replace(self.config, target_mhz=200, validation_result=receipt), self.root)
                self.assertEqual(outcome.status, 'needs_attention', outcome.message)
                command.assert_not_called()

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

    def test_report_symlinks_do_not_invalidate_published_inputs(self):
        (self.server / 'mode').write_text('report_symlink')
        output = self.root / 'report_symlink'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        payload = json.loads((output / 'remote_validation_result.json').read_text())
        self.assertTrue(payload['input_unchanged'])
        with zipfile.ZipFile(output / 'reports.zip') as archive:
            self.assertFalse(any(name.endswith('/simulator') for name in archive.namelist()))

    def test_input_symlinks_still_invalidate_snapshot(self):
        (self.server / 'mode').write_text('input_symlink')
        output = self.root / 'input_symlink'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn('Symlink in validation inputs', result.message)

    def test_python_diagnostics_use_pipeline_import_directory(self):
        # A dependency available beside the deployed scripts must be probed
        # there, even though the uploaded wrapper lives in another directory.
        (self.server / 'dotenv.py').write_text('def load_dotenv(): pass\n')
        output = self.root / 'python_diagnostics'
        output.mkdir()
        with patch('shared.top_orchestrator.validation.SSHExecutor', LocalSSH):
            result = validation_node(self.config, output)
        self.assertEqual(result.status, 'passed', result.message)
        payload = json.loads((output / 'remote_validation_result.json').read_text())
        runtime = payload['python_environment']
        self.assertEqual(runtime['executable'], sys.executable)
        self.assertTrue(runtime['username'])
        self.assertEqual(runtime['dependencies']['dotenv'],
                         dict(available=True, file=str(self.server / 'dotenv.py')))
        record = json.loads((output / 'validation_result.json').read_text())
        self.assertEqual(record['username'], 'fake')

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
                self.assertIn('no backend' if entry[0] == 'validation' else 'remote validation -> backend', output.getvalue())

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
