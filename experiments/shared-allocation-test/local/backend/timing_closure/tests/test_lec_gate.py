"""Verify the backend cannot cross the mapped-netlist LEC gate on a failed proof."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import app
from workers import orchestrator


class LECGateRoutingTests(unittest.TestCase):
    def test_resume_loads_mapping_without_reupload_or_reserving_builds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = dict(history=['mapping completed'], stage_status=dict.fromkeys(
                ('rtl_prep', 'netlist', 'mapped_netlist'), 'success'), lec_enabled=True,
                mailbox_source=str(root / 'mailbox'), mailbox_revision='revision',
                lec_validation_result=str(root / 'validation.json'), ssh_host='fake', ssh_user='fake',
                ssh_port=22, timing_target_clock_period_ns=5, reserve_named_builds=True,
                mapped_candidate_path='existing_mapping', outputs={'mapped_remote_dir': '/remote/existing_mapping'})
            saved = root / 'backend_result.json'
            saved.write_text(json.dumps(original))
            args = app._parse_args(['--resume-state', str(saved), '--mailbox', str(root / 'mailbox'),
                                    '--log-dir', str(root / 'new_reports'), '--target-mhz', '200'])
            with patch.object(app, '_load_service_exports'), patch('shared.top_orchestrator.mailbox.verify',
                    return_value={'rtl_revision': 'revision'}), patch(
                    'shared.top_orchestrator.validation.verify_backend_handoff'), patch.object(
                    app, 'run_flow', side_effect=lambda **kwargs: kwargs['initial_state']) as flow, patch.object(
                    app, 'stage_mailbox') as staging:
                result = app.main(args)
            staging.assert_not_called()
            self.assertEqual(result['current_stage'], 'lec')
            self.assertFalse(result['reserve_named_builds'])
            self.assertEqual(result['mapped_candidate_path'], 'existing_mapping')
            self.assertEqual(result['outputs'], original['outputs'])
            self.assertEqual(result['backend_log_dir'], str(root / 'new_reports'))
            self.assertEqual(json.loads(saved.read_text()), original)
            flow.assert_called_once()

    def test_resume_rejects_missing_mapping_and_already_started_gdsii(self):
        with tempfile.TemporaryDirectory() as temporary:
            saved = Path(temporary) / 'state.json'
            args = app._parse_args(['--resume-state', str(saved)])
            cases = [dict(lec_enabled=True, stage_status={'mapped_netlist': 'failed'}),
                     dict(lec_enabled=True, stage_status={**dict.fromkeys(
                         ('rtl_prep', 'netlist', 'mapped_netlist'), 'success'), 'gdsii': 'success'})]
            for state in cases:
                saved.write_text(json.dumps(state))
                with self.subTest(state=state), patch.object(app, '_load_service_exports'), patch.object(
                        app, 'run_flow') as flow, self.assertRaises(ValueError):
                    app.main(args)
                flow.assert_not_called()

    def execute(self, *, proof_passes=True, intact=True, start='idle'):
        order = []

        def stage(name):
            def run(state):
                order.append(name)
                return {**state, 'stage_status': {**state['stage_status'], name: 'success'}}
            return run

        def lec(state, ssh):
            order.append('lec')
            return {**state, 'lec_status': 'passed' if proof_passes else 'failed',
                    'stage_status': {**state['stage_status'], 'lec': 'success' if proof_passes else 'failed'},
                    'last_error': None if proof_passes else 'NOT_EQUIVALENT'}

        def inputs(state, ssh):
            order.append('check_lec_inputs')
            if not intact or state.get('lec_status') != 'passed':
                raise ValueError('No valid mapped proof')

        with ExitStack() as stack:
            stack.enter_context(patch.object(orchestrator, 'SSHExecutor', return_value=MagicMock()))
            for name, factory in (('rtl_prep', '_make_rtl_prep_runner'), ('netlist', '_make_netlist_runner'),
                                  ('mapped_netlist', '_make_mapped_runner'), ('gdsii', '_make_gdsii_runner')):
                stack.enter_context(patch.object(orchestrator, factory, return_value=stage(name)))
            stack.enter_context(patch('shared.top_orchestrator.lec.run_lec_gate', side_effect=lec))
            stack.enter_context(patch('shared.top_orchestrator.lec.verify_lec_inputs', side_effect=inputs))
            result = orchestrator.run_flow(dict(history=[], stage_status={}, current_stage=start,
                                                lec_enabled=True), {})
        return result, order

    def test_exact_sequence_runs_mapping_once_and_proof_before_gdsii(self):
        result, order = self.execute()
        self.assertEqual(order, ['rtl_prep', 'netlist', 'mapped_netlist', 'lec', 'check_lec_inputs', 'gdsii'])
        self.assertEqual(result['current_stage'], 'done')

    def test_failed_proof_stops_before_gdsii(self):
        result, order = self.execute(proof_passes=False)
        self.assertEqual(order, ['rtl_prep', 'netlist', 'mapped_netlist', 'lec'])
        self.assertEqual(result['current_stage'], 'failed')
        self.assertEqual(result['last_error'], 'NOT_EQUIVALENT')

    def test_changed_inputs_stop_before_gdsii(self):
        result, order = self.execute(intact=False)
        self.assertNotIn('gdsii', order)
        self.assertEqual(result['current_stage'], 'failed')
        self.assertIn('LEC gate', result['last_error'])

    def test_starting_at_gdsii_cannot_skip_proof(self):
        result, order = self.execute(start='gdsii')
        self.assertEqual(order, ['check_lec_inputs'])
        self.assertEqual(result['current_stage'], 'failed')

    def test_app_passes_lec_receipt_and_config_into_backend_state(self):
        args = app._parse_args(['--mailbox', '/mailbox', '--lec-validation-result', '/validation.json',
                                '--lec-remote-config', '/remote.json'])
        with patch.object(app, '_load_service_exports'), patch.object(app, 'stage_mailbox', return_value={}), \
                patch.object(app, 'run_flow', side_effect=lambda **kw: kw['initial_state']):
            result = app.main(args)
        self.assertTrue(result['lec_enabled'])
        self.assertEqual(result['lec_validation_result'], '/validation.json')
        self.assertEqual(result['lec_remote_config'], '/remote.json')

    def test_legacy_explicit_backend_test_keeps_its_stage_sequence(self):
        seen = []
        def success(name):
            def run(state):
                seen.append(name)
                return {**state, 'stage_status': {**state['stage_status'], name: 'success'}}
            return run
        with ExitStack() as stack:
            stack.enter_context(patch.object(orchestrator, 'SSHExecutor', return_value=MagicMock()))
            for name, factory in (('rtl_prep', '_make_rtl_prep_runner'), ('netlist', '_make_netlist_runner'),
                                  ('mapped_netlist', '_make_mapped_runner'), ('gdsii', '_make_gdsii_runner')):
                stack.enter_context(patch.object(orchestrator, factory, return_value=success(name)))
            result = orchestrator.run_flow(dict(history=[], stage_status={}), {})
        self.assertEqual(seen, ['rtl_prep', 'netlist', 'mapped_netlist', 'gdsii'])
        self.assertEqual(result['current_stage'], 'done')


if __name__ == '__main__':
    unittest.main()
