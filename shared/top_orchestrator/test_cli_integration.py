"""Main CLI boundaries for Experimental batch and offline staging."""
import io
import json
import unittest
from unittest.mock import patch

from .__main__ import main
from . import test_specification_backend as fixtures_backend
from .specification_backend import verify_offline_handoff


class IntegrationCliTests(unittest.TestCase):
    def setUp(self):
        fixtures_backend.OfflineBackendTests.setUp(self)
        self.publish()

    def publish(self):
        return fixtures_backend.OfflineBackendTests.publish(self)

    def invoke(self, *args):
        with patch('sys.argv', ['orchestrator', *map(str, args)]), patch('sys.stdout', io.StringIO()) as output:
            code = main()
        return code, output.getvalue()

    def test_both_plans_never_load_remote_or_prompt_or_create_outputs(self):
        output = self.root / 'new'
        with patch('shared.top_orchestrator.remote.RemoteConfig.load', side_effect=AssertionError('remote setup')), patch(
                'getpass.getpass', side_effect=AssertionError('password prompt')):
            for entry, option in [('backend', '--offline-handoff'), ('validation', '--module-batch')]:
                code, text = self.invoke(entry, option, '--mailbox', self.mailbox, '--run-dir', output, '--plan')
                self.assertEqual(code, 0)
                self.assertIn('readiness remains false', text)
                self.assertFalse(output.exists())

    def test_offline_execution_writes_state_and_independently_recheckable_handoff(self):
        output = self.root / 'offline-cli'
        with patch('shared.top_orchestrator.remote.RemoteConfig.load', side_effect=AssertionError('remote setup')), patch(
                'getpass.getpass', side_effect=AssertionError('password prompt')):
            code, _ = self.invoke('backend', '--offline-handoff', '--mailbox', self.mailbox,
                                  '--run-dir', output)
        self.assertEqual(code, 0)
        state = json.loads((output / 'state.json').read_text())
        self.assertEqual(state['status'], 'offline_checks_passed')
        self.assertFalse(state['backend_ready'])
        report = verify_offline_handoff(self.mailbox, output / 'backend_handoff')
        self.assertEqual(state['rtl_revision'], report['rtl_revision'])

    def test_invalid_combinations_fail_before_side_effects(self):
        combinations = [
            ['generate', '--offline-handoff'],
            ['backend', '--offline-handoff', '--ask-password'],
            ['backend', '--offline-handoff', '--allow-unvalidated'],
            ['validation', '--module-batch', '--module-probe', 'unit'],
            ['validation', '--module-batch', '--target-mhz', '200'],
            ['validation', '--module-batch', '--repair'],
            ['validation', '--module-batch', '--frontend-flow', 'expand-spec'],
            ['backend', '--offline-handoff', '--no-lec'],
            ['backend', '--offline-handoff', '--shared-allocation'],
            ['validation', '--module-batch', '--no-lec'],
            ['validation', '--module-batch', '--shared-allocation'],
            ['validation', '--module-probe', 'unit', '--no-lec'],
        ]
        with patch('getpass.getpass', side_effect=AssertionError('prompt')), patch('sys.stderr', io.StringIO()):
            for args in combinations:
                with self.subTest(args=args), self.assertRaises(SystemExit) as stopped:
                    self.invoke(*args, '--mailbox', self.mailbox)
                self.assertEqual(stopped.exception.code, 2)

    def test_batch_preflight_and_execution_have_distinct_status(self):
        for preflight in (True, False):
            output = self.root / ('preflight' if preflight else 'execution')
            def batch(config, directory):
                self.assertTrue(config.no_backend)
                self.assertEqual(config.preflight_only, preflight)
                directory.mkdir()
                return {'dispatch_status': 'passed', 'module_coverage': 'incomplete' if preflight else 'complete',
                        'snapshot_sha256': 'snapshot', 'rtl_revision': 'revision'}
            with patch('shared.top_orchestrator.auth.prepare_password'), patch(
                    'shared.top_orchestrator.specification_validation.run_modules', side_effect=batch):
                args = ['validation', '--module-batch', '--mailbox', self.mailbox, '--run-dir', output]
                if preflight:
                    args.append('--preflight-only')
                code, _ = self.invoke(*args)
            self.assertEqual(code, 0)
            state = json.loads((output / 'state.json').read_text())
            self.assertEqual(state['status'], 'consumer_preflights_passed' if preflight else 'module_checks_passed')
            self.assertEqual(state['system_verification'], 'not_accepted')

    def test_incomplete_collection_is_not_accepted_despite_successful_dispatch(self):
        output = self.root / 'incomplete'
        def batch(config, directory):
            directory.mkdir()
            return dict(dispatch_status='passed', module_coverage='incomplete',
                        snapshot_sha256='snapshot', rtl_revision='revision')
        with patch('shared.top_orchestrator.auth.prepare_password'), patch(
                'shared.top_orchestrator.specification_validation.run_modules', side_effect=batch):
            code, _ = self.invoke('validation', '--module-batch', '--mailbox', self.mailbox, '--run-dir', output)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((output / 'state.json').read_text())['status'], 'needs_attention')

    def test_preexisting_run_is_preserved_before_password_prompt(self):
        output = self.root / 'existing'
        output.mkdir()
        (output / 'sentinel').write_text('preserve')
        with patch('getpass.getpass', side_effect=AssertionError('prompt')), patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                self.invoke('validation', '--module-batch', '--mailbox', self.mailbox, '--run-dir', output)
        self.assertEqual((output / 'sentinel').read_text(), 'preserve')
