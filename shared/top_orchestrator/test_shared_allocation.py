"""Exercise shared allocation ownership, reuse and cleanup without submitting jobs."""

import io
import json
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace

from shared.top_orchestrator import allocation
from shared.top_orchestrator.contracts import RunConfig, StageResult
from shared.top_orchestrator.orchestrator import run
from shared.top_orchestrator.remote import RemoteConfig, slurm_command


class AllocationSSH:
    instances = []
    fail = False

    def __init__(self, *args):
        self.password = None
        self.commands = []
        self.closed = False
        type(self).instances.append(self)

    def run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command.startswith('salloc '):
            message = 'Requested nodes are busy' if self.fail else 'salloc: Granted job allocation 777\n'
            if kwargs.get('on_output'):
                kwargs['on_output']('stderr', message)
            return dict(ok=not self.fail, exit_code=1 if self.fail else 0, stderr=message, stdout='')
        return dict(ok=True, exit_code=0, stdout='', stderr='')

    def close(self):
        self.closed = True


class SharedAllocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = RemoteConfig(host='fake', username='user', project_dir='/project',
                                   partition='academic', qos='test')
        self.config = RunConfig('generate', shared_allocation=True)
        AllocationSSH.instances = []
        AllocationSSH.fail = False
        for stub in (patch.object(allocation, 'SSHExecutor', AllocationSSH),
                     patch('shared.top_orchestrator.remote.RemoteConfig.load', return_value=self.remote),
                     patch.dict('os.environ', {}, clear=True), patch('sys.stdout', io.StringIO()),
                     patch('sys.stderr', io.StringIO())):
            stub.start()
            self.addCleanup(stub.stop)

    def test_one_reservation_is_reused_by_every_remote_step_and_released(self):
        with allocation.shared_session(self.config, self.root):
            self.assertIsNone(allocation.active_allocation())
            allocation.ensure_allocation()
            allocation.ensure_allocation()
            for stage in ('verification', 'lec', 'gdsii-validator'):
                command = slurm_command(self.remote, ['python3', 'runner.py'], stage)
                self.assertIn('--jobid=777', command)
                self.assertIn('--cpus-per-task=6', command)
                self.assertIn('export MAKEFLAGS=-j6', command)
                self.assertNotIn('--immediate', command)
                self.assertNotIn('--partition', command)
                self.assertNotIn('--time=', command)
            env = allocation.backend_environment(self.remote)
            self.assertEqual(json.loads(env[allocation.ENVIRONMENT])['job_id'], '777')
            self.assertEqual(str(allocation.workspace(self.remote)), '/project/shared/allocations/777')
        ssh = AllocationSSH.instances[0]
        self.assertEqual(sum(command.startswith('salloc ') for command, _ in ssh.commands), 1)
        request = next(command for command, _ in ssh.commands if command.startswith('salloc '))
        self.assertIn('--no-shell', request)
        self.assertIn('--time=04:00:00', request)
        self.assertEqual(ssh.commands[-1][0], 'scancel 777')
        self.assertTrue(ssh.closed)
        self.assertIsNone(allocation.active_allocation())
        self.assertEqual(json.loads((self.root / 'shared_allocation.json').read_text())['status'], 'released')

    def test_failure_and_interrupt_release_the_owned_allocation(self):
        for error in (RuntimeError('failed stage'), KeyboardInterrupt()):
            AllocationSSH.instances = []
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                with allocation.shared_session(self.config, self.root):
                    allocation.ensure_allocation()
                    raise error
            self.assertEqual(AllocationSSH.instances[0].commands[-1][0], 'scancel 777')

    def test_allocation_failure_cancels_only_the_unique_requested_name(self):
        AllocationSSH.fail = True
        with self.assertRaisesRegex(RuntimeError, 'Requested nodes are busy'):
            with allocation.shared_session(self.config, self.root):
                allocation.ensure_allocation()
        command = AllocationSSH.instances[0].commands[-1][0]
        self.assertTrue(command.startswith('scancel --name capstone-shared-'))

    def test_disabled_or_local_only_flow_never_allocates(self):
        for config in (replace(self.config, shared_allocation=False), self.config):
            with allocation.shared_session(config, self.root):
                if not config.shared_allocation:
                    allocation.ensure_allocation()
        self.assertEqual(AllocationSSH.instances, [])

    def test_endpoint_mismatch_and_malformed_inherited_ids_are_rejected(self):
        with allocation.shared_session(self.config, self.root):
            allocation.ensure_allocation()
            with self.assertRaisesRegex(ValueError, 'different SSH'):
                slurm_command(replace(self.remote, host='other'), ['true'], 'stage')
        bad = dict(job_id='777;scancel other', cpus=6, host='fake', username='user', port=22)
        with patch.dict('os.environ', {allocation.ENVIRONMENT: json.dumps(bad)}), self.assertRaises(ValueError):
            allocation.active_allocation()

    def test_backend_process_can_recover_lease_from_its_environment(self):
        lease = dict(job_id='777', cpus=6, host='fake', username='user', port=22)
        with patch.dict('os.environ', {allocation.ENVIRONMENT: json.dumps(lease)}):
            command = slurm_command(self.remote, ['true'], 'lec-from-backend')
        self.assertIn('--jobid=777', command)

    def test_step_cancellation_does_not_cancel_allocation_or_other_steps(self):
        lease = dict(job_id='777', cpus=6, host='fake', username='user', port=22)
        with patch.dict('os.environ', {allocation.ENVIRONMENT: json.dumps(lease)}):
            command = allocation.cancellation_command(self.remote, 'owned-stage')
        self.assertEqual(command[0:2], [self.remote.python, '-c'])
        listing = ('777.0|owned-stage\n777.1|different-stage\n'
                   '888.0|owned-stage\n777|owned-stage\n777.extern|owned-stage\n')
        with patch.object(sys, 'argv', ['cleanup', '777', 'owned-stage']), patch.object(
                subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout=listing, stderr=''),
                                               SimpleNamespace(returncode=0)]) as execute:
            with self.assertRaises(SystemExit) as exit_status:
                exec(command[2], {})
        self.assertEqual(exit_status.exception.code, 0)
        self.assertEqual(execute.call_args_list[1].args[0], ['scancel', '777.0'])
        self.assertEqual(execute.call_count, 2)

    def test_termination_signal_releases_allocation_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as stopped:
            with allocation.shared_session(self.config, self.root):
                allocation.ensure_allocation()
                signal.raise_signal(signal.SIGTERM)
        self.assertEqual(stopped.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(AllocationSSH.instances[0].commands[-1][0], 'scancel 777')

    def test_cli_plan_accepts_shared_flags_without_allocating(self):
        from shared.top_orchestrator.__main__ import main
        with patch.object(sys, 'argv', ['flow', '--shared-allocation',
                '--shared-allocation-cpus', '6', '--target-mhz', '200', '--plan']), patch(
                'sys.stdout', io.StringIO()) as output:
            self.assertEqual(main(), 0)
        self.assertIn('Reserve 6 CPUs once', output.getvalue())
        self.assertEqual(AllocationSSH.instances, [])

    def test_invalid_shared_routes_and_budgets_fail_before_execution(self):
        from shared.top_orchestrator.__main__ import main
        for args in (['--shared-allocation', '--no-backend'],
                     ['--shared-allocation', '--skip-verification'],
                     ['--shared-allocation', '--shared-allocation-cpus', '0']):
            with self.subTest(args=args), patch.object(sys, 'argv', ['flow', *args]):
                with self.assertRaises(SystemExit) as stopped:
                    main()
                self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(AllocationSSH.instances, [])

    def test_interrupted_backend_stream_exits_before_allocation_cleanup(self):
        from shared.top_orchestrator.nodes import stream_command
        create = subprocess.Popen
        children = []
        def launch(*args, **kwargs):
            child = create(*args, **kwargs)
            children.append(child)
            return child
        with patch('shared.top_orchestrator.nodes.subprocess.Popen', side_effect=launch), patch(
                'sys.stdout.write', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                stream_command([sys.executable, '-u', '-c',
                    'import time; print("ready"); time.sleep(60)'], self.root/'backend.log', cwd=self.root)
        self.assertIsNotNone(children[0].poll())

    def test_full_flow_allocates_after_frontend_and_shares_until_validator(self):
        events = []
        frontend = self.root / 'frontend'
        (frontend / 'rtl_output').mkdir(parents=True)
        (frontend / 'expanded').mkdir()
        (frontend / 'rtl_output/top.sv').write_text('module top; endmodule')
        (frontend / 'rtl_output/manifest.json').write_text(json.dumps(dict(top_module='top', modules=['top'])))
        (frontend / 'expanded/top.yaml').write_text('design_name: top\n')
        approved = self.root / 'approved.yaml'
        approved.write_text('test: true\n')

        def generate(config, directory):
            self.assertEqual(AllocationSSH.instances, [])
            events.append('frontend')
            return StageResult('generate', 'passed', 'generated')

        def remote_stage(name):
            def execute(config, directory):
                events.append(name)
                self.assertEqual(allocation.active_allocation()['job_id'], '777')
                self.assertIn('--jobid=777', slurm_command(self.remote, ['true'], name))
                return StageResult(name, 'passed', 'passed', {'result': str(directory / 'result.json')})
            return execute

        with patch('shared.top_orchestrator.orchestrator.prepare_generation',
                   side_effect=lambda config, _: replace(config, input_yaml=approved, target_mhz=200)), patch(
                   'shared.top_orchestrator.orchestrator.prepare_password'), patch(
                   'shared.top_orchestrator.orchestrator.FRONTEND', frontend), patch(
                   'shared.top_orchestrator.orchestrator.REPO_ROOT', self.root), patch.dict(
                   'shared.top_orchestrator.orchestrator.NODES', {
                       'generate': generate, 'validation': remote_stage('validation'),
                       'backend': remote_stage('backend'), 'gdsii-validation': remote_stage('gdsii-validation')}):
            state = run(self.config, self.root / 'run')
        self.assertEqual(events, ['frontend', 'validation', 'backend', 'gdsii-validation'])
        self.assertEqual(state.status, 'complete')
        self.assertEqual(len(AllocationSSH.instances), 1)
        self.assertEqual(AllocationSSH.instances[0].commands[-1][0], 'scancel 777')


if __name__ == '__main__':
    unittest.main()
