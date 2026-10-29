"""Exercise the real post-backend adapter/wrapper without server or EDA tools."""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.contracts import RunConfig, StageResult
from shared.top_orchestrator.gdsii_validation import gdsii_validation_node, assess, backend_request
from shared.top_orchestrator.mailbox import publish, verify
from shared.top_orchestrator.orchestrator import run, next_stage
from shared.top_orchestrator.remote import RemoteConfig
from shared.top_orchestrator.auth import password_session, _passwords
from shared.top_orchestrator.test_validation import LocalSSH


AGENT = '''
import json
from pathlib import Path
class GDSIIValidationAgent:
    def __init__(self, **options):
        self.options = options
    def collect_backend_reports(self):
        return [str(p) for folder in self.options['report_dirs'] for p in Path(folder).rglob('*') if p.is_file()]
    def validate(self):
        mode = Path(__file__).with_name('mode').read_text()
        assert self.options['top_module'] == 'top'
        if mode == 'mutate':
            Path(self.options['gds_file']).write_bytes(b'changed')
        return dict(status=mode if mode in ('FAIL','ERROR') else 'PASS_WITH_GAPS',
                    needs_review=True, allow_signoff=False, structural_checks_passed=True,
                    netlist_file=self.options['netlist_file'], gds_file=self.options['gds_file'],
                    checks={}, coverage_limitations=['No LVS/DRC'])
'''


class GDSIIValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        frontend = self.root / 'frontend'
        (frontend / 'rtl_output').mkdir(parents=True)
        (frontend / 'rtl_output/top.sv').write_text('module top; endmodule')
        (frontend / 'rtl_output/manifest.json').write_text(json.dumps(dict(top_module='top', modules=['top'])))
        (frontend / 'expanded').mkdir()
        (frontend / 'expanded/top.yaml').write_text('design_name: top\n')
        self.mailbox = publish(frontend, self.root / 'mailbox/revision_001')
        self.server = self.root / 'server project'
        self.gds_dir = self.server / 'backend/final implementation'
        (self.gds_dir / 'results').mkdir(parents=True)
        (self.gds_dir / 'reports').mkdir()
        self.gds = self.gds_dir / 'results/top.gds'
        self.gds.write_bytes(b'fake gds')
        (self.gds_dir / 'results/top.v').write_text('module top; endmodule')
        (self.gds_dir / 'reports/timing.rpt').write_text('slack 0.1')
        (self.server / 'agent.py').write_text(AGENT)
        (self.server / 'mode').write_text('gaps')
        self.backend = self.root / 'backend_result.json'
        self.payload = dict(current_stage='done', mailbox_revision=verify(self.mailbox)['rtl_revision'],
            timing_target_clock_period_ns=5, top_module='top', remote_project_root=str(self.server / 'backend'),
            outputs=dict(gdsii_remote_dir=str(self.gds_dir)),
            stage_status=dict.fromkeys(('rtl_prep','netlist','mapped_netlist','gdsii'), 'success'))
        self.backend.write_text(json.dumps(self.payload))
        self.remote_file = self.root / 'remote.json'
        self.remote_file.write_text(json.dumps(dict(host='fake', username='fake', project_dir=str(self.server),
            partition='fake', qos='fake', python=sys.executable, gdsii_validation_script='agent.py')))
        self.config = RunConfig('backend', mailbox=self.mailbox, target_mhz=200,
            backend_result=self.backend, remote_config=self.remote_file)
        LocalSSH.instances = []

    def execute(self, ssh_type=LocalSSH):
        destination = self.root / 'run'
        destination.mkdir()
        with patch('shared.top_orchestrator.gdsii_validation.SSHExecutor', ssh_type):
            result = gdsii_validation_node(self.config, destination)
        record = json.loads(Path(result.artifacts['result']).read_text())
        return result, record

    def test_real_adapter_runner_round_trip_preserves_gaps_and_reports(self):
        before = self.gds.read_bytes()
        result, record = self.execute()
        self.assertEqual(result.status, 'needs_attention', result.message)
        lines = Path(result.artifacts['log']).read_text().splitlines()
        self.assertTrue(any('[GDSII] Result: PASS_WITH_GAPS' in line for line in lines))
        for line in lines:
            if line:
                self.assertRegex(line, r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]+\] ')
        remote = record['remote_result']
        self.assertEqual(remote['report']['status'], 'PASS_WITH_GAPS')
        self.assertTrue(remote['input_unchanged'])
        self.assertEqual(remote['input_digests'][str(self.gds)], hashlib.sha256(before).hexdigest())
        self.assertEqual(remote['backend_digest'], hashlib.sha256(self.backend.read_bytes()).hexdigest())
        self.assertEqual(self.gds.read_bytes(), before)
        reports = Path(result.artifacts['reports']) / 'verification_reports'
        self.assertTrue((reports / 'gdsii_validation_report.json').is_file())
        self.assertEqual(next((reports / 'backend_reports').iterdir()).read_text(), 'slack 0.1')
        self.assertFalse((self.gds_dir / 'verification_reports').exists())

    def test_shared_allocation_validates_gdsii_without_requesting_new_resources(self):
        from shared.top_orchestrator.allocation import ENVIRONMENT
        lease = dict(job_id='777', cpus=6, host='fake', username='fake', port=22)
        with patch.dict('os.environ', {ENVIRONMENT: json.dumps(lease)}):
            result, record = self.execute()
        self.assertEqual(result.status, 'needs_attention', result.message)
        self.assertEqual(record['remote_result']['report']['status'], 'PASS_WITH_GAPS')
        self.assertIn('--jobid=777', record['command'])
        self.assertIn('/shared/allocations/777/gdsii_validation_runs/', record['remote_directory'])

    def test_ambiguous_gds_stops_before_agent(self):
        (self.gds_dir / 'results/old.gds').write_bytes(b'old')
        result, record = self.execute()
        self.assertEqual(result.status, 'needs_attention')
        self.assertIn('found 2', record['remote_result']['error'])
        self.assertIsNone(record['remote_result']['report'])

    def test_explicit_gds_resolves_ambiguity(self):
        (self.gds_dir / 'results/old.gds').write_bytes(b'old')
        data = json.loads(self.remote_file.read_text())
        data['gdsii_file'] = 'results/top.gds'
        self.remote_file.write_text(json.dumps(data))
        result, record = self.execute()
        self.assertEqual(record['remote_result']['gds_file'], str(self.gds))
        self.assertEqual(result.status, 'needs_attention')
        self.assertNotIn('error', record['remote_result'])

    def test_explicit_artifact_outside_backend_is_rejected(self):
        data = json.loads(self.remote_file.read_text())
        data['gdsii_file'] = '../../other.gds'
        self.remote_file.write_text(json.dumps(data))
        result, record = self.execute()
        self.assertIn('must belong', record['remote_result']['error'])

    def test_mapped_netlist_fallback_is_recorded(self):
        (self.gds_dir / 'results/top.v').unlink()
        mapped = self.server / 'backend/mapped'
        mapped.mkdir()
        (mapped / 'top.v').write_text('module top; endmodule')
        self.payload['outputs']['mapped_remote_dir'] = str(mapped)
        self.backend.write_text(json.dumps(self.payload))
        result, record = self.execute()
        self.assertEqual(record['remote_result']['netlist_source'], 'mapped')
        self.assertEqual(record['remote_result']['netlist_file'], str(mapped / 'top.v'))
        self.assertNotIn('error', record['remote_result'])

    def test_missing_and_ambiguous_netlists_stop(self):
        for mode in ('missing', 'ambiguous'):
            with self.subTest(mode=mode):
                path = self.gds_dir / 'results/top.v'
                if mode == 'missing':
                    path.unlink()
                else:
                    path.write_text('module top; endmodule')
                    (path.parent / 'other.v').write_text('module other; endmodule')
                result, record = self.execute()
                self.assertEqual(result.status, 'needs_attention')
                self.assertIn('Expected one', record['remote_result']['error'])
                shutil.rmtree(self.root / 'run')

    def test_input_mutation_blocks_acceptance(self):
        (self.server / 'mode').write_text('mutate')
        result, record = self.execute()
        self.assertEqual(result.status, 'needs_attention')
        self.assertFalse(record['remote_result']['input_unchanged'])
        self.assertIn('changed during', record['remote_result']['error'])

    def test_native_fail_and_error_are_failures(self):
        for mode in ('FAIL', 'ERROR'):
            (self.server / 'mode').write_text(mode)
            result, record = self.execute()
            self.assertEqual(result.status, 'failed')
            self.assertEqual(record['remote_result']['report']['status'], mode)
            shutil.rmtree(self.root / 'run')

    def test_backend_binding_rejects_wrong_snapshot_target_or_failed_gate(self):
        remote = RemoteConfig.load(self.remote_file)
        for change in (dict(mailbox_revision='old'), dict(timing_target_clock_period_ns=4),
                       dict(current_stage='failed'), dict(physical_verification_enabled=True),
                       dict(timing_closure_enabled=True)):
            self.backend.write_text(json.dumps({**self.payload, **change}))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Backend evidence'):
                backend_request(self.config, remote)

    def test_backend_server_and_top_must_match(self):
        remote = RemoteConfig.load(self.remote_file)
        for change in (dict(ssh_host='other'), dict(ssh_port=2222), dict(top_module='wrong')):
            self.backend.write_text(json.dumps({**self.payload, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                backend_request(self.config, remote)

    def test_libraries_are_hashed_and_excluded_from_layout_selection(self):
        library = self.gds_dir / 'results/standard_cells.gds'
        library.write_bytes(b'library')
        data = json.loads(self.remote_file.read_text())
        data['gdsii_libraries'] = [str(library)]
        self.remote_file.write_text(json.dumps(data))
        result, record = self.execute()
        self.assertNotIn('error', record['remote_result'])
        self.assertEqual(record['remote_result']['input_digests'][str(library)],
                         hashlib.sha256(b'library').hexdigest())

    def test_cached_password_is_reused_and_not_saved(self):
        secret = 'test-secret-only'
        class AuthSSH(LocalSSH):
            def run(self, *args, **kwargs):
                if self.password != secret:
                    raise AssertionError('Cached credential not used')
                return super().run(*args, **kwargs)
        with password_session(), patch('getpass.getpass', side_effect=AssertionError('Unexpected prompt')):
            _passwords.get()[('fake', 'fake')] = secret
            result, record = self.execute(AuthSSH)
        self.assertIsNone(LocalSSH.instances[-1].password)
        self.assertNotIn(secret, json.dumps(record))
        for path in (self.root / 'run').rglob('*'):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_remote_options_validate_paths_lists_and_timeout(self):
        original = json.loads(self.remote_file.read_text())
        for change in (dict(gdsii_file=''), dict(gdsii_libraries='wrong'),
                       dict(gdsii_validation_timeout_seconds=1), dict(gdsii_validation_script='')):
            self.remote_file.write_text(json.dumps({**original, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                RemoteConfig.load(self.remote_file)

    def test_disabled_output_directories_stop_before_ssh(self):
        data = json.loads(self.remote_file.read_text())
        data['allow_validation_output_dirs'] = False
        self.remote_file.write_text(json.dumps(data))
        result, record = self.execute()
        self.assertEqual(result.status, 'needs_attention')
        self.assertEqual(LocalSSH.instances, [])

    def test_failed_transfer_does_not_launch(self):
        class Broken(LocalSSH):
            def upload_file(self, *args, **kwargs):
                raise OSError('transfer failed')
        result, record = self.execute(Broken)
        self.assertEqual(result.status, 'needs_attention')
        self.assertFalse(any(c.startswith('srun ') for c in LocalSSH.instances[-1].commands))

    def test_timeout_cancels_only_invocation_job(self):
        class Timeout(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    self.commands.append(command)
                    return dict(ok=False, exit_code=-1, stdout='', stderr='timeout')
                if command.startswith('scancel '):
                    self.commands.append(command)
                    return dict(ok=True, exit_code=0)
                return super().run(command, **kwargs)
        result, record = self.execute(Timeout)
        self.assertEqual(result.status, 'needs_attention')
        self.assertTrue(LocalSSH.instances[-1].commands[-1].startswith('scancel --name capstone-gdsii-'))

    def test_corrupt_archive_and_stale_result_are_rejected(self):
        for mode in ('archive', 'identity'):
            class Corrupt(LocalSSH):
                def fetch_file(self, remote, local):
                    outcome = super().fetch_file(remote, local)
                    if mode == 'archive' and str(remote).endswith('reports.zip'):
                        Path(local).write_bytes(b'wrong')
                    if mode == 'identity' and str(remote).endswith('result.json'):
                        data = json.loads(Path(local).read_text())
                        data['invocation_id'] = 'old'
                        Path(local).write_text(json.dumps(data))
                    return outcome
            result, record = self.execute(Corrupt)
            self.assertEqual(result.status, 'needs_attention')
            self.assertIn('does not match' if mode == 'identity' else 'digest mismatch', result.message)
            shutil.rmtree(self.root / 'run')

    def test_routing_hands_backend_result_to_new_stage_and_stops_on_failure(self):
        for backend_status in ('passed', 'failed'):
            destination = self.root / backend_status
            def gdsii(config, directory):
                self.assertEqual(config.backend_result, self.backend)
                return StageResult('gdsii-validation', 'needs_attention', 'review required')
            with patch('shared.top_orchestrator.orchestrator.prepare_password'), patch.dict(
                    'shared.top_orchestrator.orchestrator.NODES', {
                        'backend': lambda c, d: StageResult('backend', backend_status, 'backend', {'result': str(self.backend)}),
                        'gdsii-validation': gdsii}):
                state = run(self.config, destination)
            self.assertEqual([r.stage for r in state.results], ['backend', 'gdsii-validation']
                             if backend_status == 'passed' else ['backend'])
            self.assertEqual(state.status, 'needs_attention' if backend_status == 'passed' else 'failed')
        self.assertIsNone(next_stage(StageResult('gdsii-validation', 'failed', 'failed')))

    def test_nonzero_or_missing_result_cannot_pass(self):
        payload = dict(input_unchanged=True, report=dict(status='PASS_WITH_GAPS'))
        self.assertEqual(assess(payload, 1)[0], 'failed')
        for payload in ({}, dict(input_unchanged=False, report={})):
            with self.assertRaises(ValueError):
                assess(payload, 0)


if __name__ == '__main__':
    unittest.main()
