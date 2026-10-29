"""Run the real LEC transport/wrapper against an isolated fake Agent 4."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.lec import lec_request, run_lec_gate, verify_lec_inputs
from shared.top_orchestrator.mailbox import publish, verify
from shared.top_orchestrator.remote import RemoteConfig
from shared.top_orchestrator.test_validation import LocalSSH


AGENT = '''
import json
from pathlib import Path
def system_main(args):
    handoff = json.loads(Path(args[args.index('--lec-handoff') + 1]).read_text())
    mode = Path(__file__).with_name('mode').read_text()
    if mode == 'mutate':
        Path(handoff['implementation']['netlist']).write_text('changed mapped input')
    report = dict(status='PASS', equivalence='EQUIVALENT',
                  verification_readiness='READY_FOR_BACKEND_IMPLEMENTATION',
                  ready_for_backend_implementation=True, allow_physical_design=True,
                  reference_hashes_verified=True, agent123_lineage_verified=True,
                  formal_invoked=True, exit_code=0)
    if mode == 'unknown': report['equivalence'] = 'UNKNOWN'
    if mode == 'mismatch':
        report.update(status='FAIL', equivalence='NOT_EQUIVALENT', exit_code=1)
    if mode == 'no_proof': report['formal_invoked'] = False
    root = Path(args[args.index('--report-root') + 1]) / 'new_run/agent4'
    root.mkdir(parents=True)
    (root / 'lec_report.json').write_text(json.dumps(report))
    print('[AGENT4] ' + report['equivalence'], flush=True)
    return 0 if report['status'] == 'PASS' else 1
'''


class LECTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        frontend = self.root / 'frontend'
        (frontend / 'rtl_output').mkdir(parents=True)
        (frontend / 'rtl_output/top.sv').write_text('module top; endmodule')
        (frontend / 'rtl_output/manifest.json').write_text(json.dumps(dict(
            top_module='top', modules=['top'], run_root='.', rtl_files=['top.sv'])))
        (frontend / 'expanded').mkdir()
        (frontend / 'expanded/top.yaml').write_text('design_name: top\n')
        self.mailbox = publish(frontend, self.root / 'mailbox')
        self.metadata = verify(self.mailbox)
        self.server = self.root / 'server project'
        self.server.mkdir()
        self.snapshot = self.server / 'validation/input'
        shutil.copytree(self.mailbox, self.snapshot)
        self.backend_rtl = self.server / 'backend_inputs'
        shutil.copytree(self.mailbox / 'rtl', self.backend_rtl)
        self.mapped = self.server / 'mapped'
        (self.mapped / 'results').mkdir(parents=True)
        self.netlist = self.mapped / 'results/top_mapped.v'
        self.netlist.write_text('module top; endmodule // mapped')
        (self.mapped / 'run_genus_mapped.tcl').write_text('set TOP top\n')
        (self.mapped / 'setup_summary.txt').write_text('Top: top\n')
        self.library = self.server / 'functional.lib'
        self.library.write_text('library (demo) {}')
        (self.server / 'agent4.py').write_text(AGENT)
        (self.server / 'mode').write_text('pass')
        self.remote_path = self.root / 'remote.json'
        self.remote_path.write_text(json.dumps(dict(host='fake', username='fake',
            project_dir=str(self.server), partition='fake', qos='fake', python=__import__('sys').executable,
            lec_script='agent4.py', lec_libraries=[str(self.library)])))
        self.remote = RemoteConfig.load(self.remote_path)
        self.validation = dict(validation_mode='system', host='fake', username='fake',
            remote_directory=str(self.snapshot.parent), snapshot_digest=hashlib.sha256(
                (self.mailbox / 'snapshot.json').read_bytes()).hexdigest(),
            remote_result=dict(summary=dict(run_id='approved_run')))
        self.state = dict(history=[], stage_status=dict.fromkeys(('rtl_prep', 'netlist', 'mapped_netlist'), 'success'),
            mailbox_source=str(self.mailbox), mailbox_revision=self.metadata['rtl_revision'],
            mailbox_file_hashes={name[4:]: value for name, value in self.metadata['files'].items() if name.startswith('rtl/')},
            lec_validation_result=str(self.root / 'validation.json'), lec_remote_config=str(self.remote_path),
            ssh_host='fake', ssh_user='fake', ssh_port=22, remote_project_root=str(self.server),
            remote_prepare_input=str(self.backend_rtl), top_module='top', mapped_candidate_path='mapped',
            backend_log_dir=str(self.root / 'logs'))
        self.receipt = patch('shared.top_orchestrator.lec.verify_backend_handoff', return_value=self.validation)
        self.receipt.start()
        self.addCleanup(self.receipt.stop)

    def execute(self, mode='pass', ssh_type=LocalSSH):
        (self.server / 'mode').write_text(mode)
        return run_lec_gate(self.state, ssh_type('fake', 'fake'))

    def test_real_adapter_wrapper_roundtrip_and_timestamped_log(self):
        result = self.execute()
        self.assertEqual(result['lec_status'], 'passed', result['last_error'])
        self.assertEqual(result['stage_status']['lec'], 'success')
        record = json.loads(Path(result['lec_result']).read_text())
        self.assertEqual(record['remote_result']['netlist'], str(self.netlist))
        request = record['request']
        self.assertEqual(request['validation_run'], str(self.snapshot / 'rtl/verification_reports/approved_run'))
        self.assertEqual(request['mapped_dir'], str(self.mapped))
        self.assertIn(str(self.library), result['lec_input_hashes'])
        lines = (Path(result['lec_report_dir']) / 'lec.log').read_text().splitlines()
        self.assertTrue(any('[AGENT4]' in line for line in lines))
        for line in lines:
            if line:
                self.assertRegex(line, r'^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]+\] ')
        verify_lec_inputs(result, LocalSSH('fake', 'fake'))
        self.netlist.write_text('changed after proof')
        with self.assertRaisesRegex(ValueError, 'changed'):
            verify_lec_inputs(result, LocalSSH('fake', 'fake'))

    def test_shared_allocation_reuses_job_id_and_isolates_remote_reports(self):
        from shared.top_orchestrator.allocation import ENVIRONMENT
        lease = dict(job_id='777', cpus=6, host='fake', username='fake', port=22)
        with patch.dict('os.environ', {ENVIRONMENT: json.dumps(lease)}):
            result = self.execute()
        self.assertEqual(result['lec_status'], 'passed', result['last_error'])
        record = json.loads(Path(result['lec_result']).read_text())
        self.assertIn('--jobid=777', record['command'])
        self.assertIn('/shared/allocations/777/lec_runs/', record['remote_directory'])

    def test_unknown_mismatch_and_empty_proof_block_gdsii(self):
        for mode in ('unknown', 'mismatch', 'no_proof', 'mutate'):
            with self.subTest(mode=mode):
                result = self.execute(mode)
                self.assertEqual(result['lec_status'], 'failed')
                self.assertEqual(result['stage_status']['lec'], 'failed')
                self.assertFalse(result['lec_input_hashes'])

    def test_missing_libraries_and_stale_rtl_fail_before_agent(self):
        for bad_input in ('libraries', 'rtl', 'reference'):
            with self.subTest(bad_input=bad_input):
                original = self.library.read_bytes()
                target = self.backend_rtl / 'top.sv' if bad_input == 'rtl' else self.snapshot / 'rtl/top.sv'
                data = target.read_bytes()
                if bad_input == 'libraries': self.library.unlink()
                else: target.write_text('changed')
                try:
                    result = self.execute()
                    self.assertEqual(result['lec_status'], 'failed')
                    report = json.loads(Path(result['lec_result']).read_text())
                    self.assertIsNone(report['remote_result']['report'])
                finally:
                    self.library.write_bytes(original)
                    target.write_bytes(data)

    def test_models_are_discovered_from_actual_synthesis_provenance(self):
        generic = self.server / 'generic'
        generic.mkdir()
        (generic / 'run_genus_netlist.tcl').write_text('set LIBERTY "' + str(self.library) + '"\n')
        (self.mapped / 'setup_summary.txt').write_text('Generic dir : ' + str(generic) + '\n')
        data = json.loads(self.remote_path.read_text())
        data['lec_libraries'] = []
        self.remote_path.write_text(json.dumps(data))
        result = self.execute()
        self.assertEqual(result['lec_status'], 'passed', result['last_error'])
        self.assertIn(str(self.library), result['lec_input_hashes'])
        self.assertIn(str(generic / 'run_genus_netlist.tcl'), result['lec_input_hashes'])

    def test_receipt_cannot_be_reused_for_another_mapped_build(self):
        result = self.execute()
        self.assertEqual(result['lec_status'], 'passed', result['last_error'])
        for override in (dict(mapped_candidate_path='other'), dict(mailbox_revision='other')):
            with self.subTest(override=override), self.assertRaisesRegex(ValueError, 'receipt'):
                verify_lec_inputs({**result, **override}, LocalSSH('fake', 'fake'))

    def test_remote_lec_configuration_validation(self):
        original = json.loads(self.remote_path.read_text())
        for changes in (dict(lec_timeout_seconds=10), dict(lec_timeout_seconds=21689),
                        dict(lec_queue_wait_seconds=0), dict(lec_proof_seconds=7200),
                        dict(lec_libraries='bad'), dict(lec_cpus=0), dict(lec_script=''), dict(lec_yosys='')):
            with self.subTest(changes=changes):
                self.remote_path.write_text(json.dumps({**original, **changes}))
                with self.assertRaises(ValueError):
                    RemoteConfig.load(self.remote_path)

    def test_normal_queueing_preserves_resources_and_allows_queue_plus_execution(self):
        calls = []
        class RecordingSSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    calls.append((command, kwargs))
                return super().run(command, **kwargs)
        result = self.execute(ssh_type=RecordingSSH)
        self.assertEqual(result['lec_status'], 'passed', result['last_error'])
        command, options = calls[0]
        self.assertNotIn('--immediate', command)
        self.assertIn('--cpus-per-task=6', command)
        self.assertIn('--time=02:00:00', command)
        self.assertEqual(options['timeout'], 21690)

    def test_allocation_rejection_preserves_slurm_error_without_fetching_reports(self):
        diagnostic = ('srun: job 720853 queued and waiting for resources\n'
                      'srun: error: Unable to allocate resources: Requested nodes are busy\n')
        class BusySSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    kwargs['on_output']('stderr', diagnostic)
                    return dict(ok=False, exit_code=1, stdout='', stderr=diagnostic)
                return super().run(command, **kwargs)

            def fetch_file(self, remote, local):
                raise AssertionError('An allocation rejection cannot produce LEC reports')

        result = self.execute(ssh_type=BusySSH)
        self.assertEqual(result['lec_status'], 'failed')
        self.assertIn('Slurm allocation failed', result['last_error'])
        self.assertIn('Requested nodes are busy', result['last_error'])
        self.assertNotIn('collect', result['last_error'])

    def test_runner_startup_failure_preserves_execution_error_if_report_is_missing(self):
        class BrokenRunnerSSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    return dict(ok=False, exit_code=2, stdout='', stderr="python3: can't open runner.py")
                return super().run(command, **kwargs)
        result = self.execute(ssh_type=BrokenRunnerSSH)
        self.assertIn('exited with code 2', result['last_error'])
        self.assertIn("can't open runner.py", result['last_error'])

    def test_request_rejects_wrong_mapping_revision_server_and_top(self):
        for override in (dict(mailbox_revision='old'), dict(ssh_host='other'), dict(top_module='other'),
                         dict(stage_status={'mapped_netlist': 'success'}), dict(mailbox_file_hashes={})):
            with self.subTest(override=override), self.assertRaises(ValueError):
                lec_request({**self.state, **override}, self.remote)

    def test_timeout_cancels_only_its_job(self):
        class TimeoutSSH(LocalSSH):
            def run(self, command, **kwargs):
                if command.startswith('srun '):
                    return dict(ok=False, exit_code=-1, stderr='timeout')
                return super().run(command, **kwargs)
        result = self.execute(ssh_type=TimeoutSSH)
        record = json.loads(Path(result['lec_result']).read_text())
        self.assertEqual(result['lec_status'], 'failed')
        self.assertIn('cancellation', record)

    def test_corrupt_or_wrong_invocation_reports_are_rejected(self):
        for kind in ('token', 'archive'):
            class CorruptSSH(LocalSSH):
                def fetch_file(self, remote, local):
                    result = super().fetch_file(remote, local)
                    if kind == 'token' and remote.endswith('/result.json'):
                        data = json.loads(Path(local).read_text())
                        data['invocation_id'] = 'old'
                        Path(local).write_text(json.dumps(data))
                    if kind == 'archive' and remote.endswith('/reports.zip'):
                        Path(local).write_bytes(b'corrupted')
                    return result
            with self.subTest(kind=kind):
                self.assertEqual(self.execute(ssh_type=CorruptSSH)['lec_status'], 'failed')


if __name__ == '__main__':
    unittest.main()
