"""Explicit checked observation-only outputs; ordinary functional wiring stays strict."""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from architecture import emit_wrapper, lower_architecture, validate_architecture
from generate_specification import propose_specification, repair_feedback
from specification import SpecificationError, SpecificationErrors, read_document, validate_contents, validate_graph
from specification_lowering import compile_package, create_approval, emit_review
from test_generate_specification import register_fixture
from verification_handoff import validate_handoff


class VerificationOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        _, request, responses = register_fixture()
        contract = responses['architecture']['modules']['contracts']['stage']
        contract['interfaces']['monitor'] = {'direction': 'output', 'type': 'shared.types.bit'}
        contract['combinational_dependencies']['monitor'] = []
        responses['architecture']['graph']['verification_outputs'] = {
            'stage.monitor': 'Observe registered validity without creating a functional consumer.'}
        bindings = responses['architecture']['verification']
        bindings['observations']['monitor'] = {'endpoint': 'stage.monitor'}
        next(iter(bindings['checks'].values()))['observations'].append('monitor')
        replay = cls.root / 'responses.json'
        replay.write_text(json.dumps(responses))
        with contextlib.redirect_stdout(io.StringIO()):
            package = propose_specification(request, output_dir=cls.root / 'source', response_path=replay)
        cls.manifest = compile_package(package)
        cls.snapshot = cls.manifest['provenance']['specification']['snapshot']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.snapshot_copy = copy.deepcopy(self.snapshot)
        self.package = self.snapshot_copy['package']
        self.docs = self.snapshot_copy['documents']

    def validate(self):
        return validate_contents(self.package, self.docs, self.snapshot_copy['timing_library'])

    def test_explicit_checked_output_lowers_through_existing_unused_output_contract(self):
        self.assertEqual(self.validate()['specification_validation'], 'passed')
        self.assertEqual(self.manifest['unused_outputs'], [{
            'endpoint': 'stage.monitor',
            'reason': 'Observe registered validity without creating a functional consumer.'}])
        resolved = validate_architecture(self.manifest)
        wrapper = emit_wrapper(self.manifest, resolved)
        self.assertIn('.monitor()', wrapper)
        self.assertFalse(any(c['from'] == 'stage.monitor' for c in self.manifest['connections']))
        self.assertEqual(len(self.manifest['modules']), 1)

    def test_observation_alone_does_not_exempt_an_unused_output(self):
        self.docs['graph'].pop('verification_outputs')
        with self.assertRaisesRegex(SpecificationError, 'unused sources.*stage.monitor') as caught:
            self.validate()
        self.assertIn('graph.verification_outputs', str(caught.exception))

    def test_declaration_requires_nonempty_reason_and_mapping(self):
        for declaration in ({'stage.monitor': ''}, ['stage.monitor']):
            self.docs['graph']['verification_outputs'] = declaration
            with self.subTest(declaration=declaration), self.assertRaises(SpecificationError):
                self.validate()

    def test_declared_observation_must_be_used_by_a_check(self):
        for check in self.docs['verification']['checks'].values():
            check['observations'] = [a for a in check['observations'] if a != 'monitor']
        with self.assertRaisesRegex(SpecificationError, 'observation used by a verification check'):
            self.validate()

    def test_missing_observation_and_invalid_endpoint_are_rejected(self):
        bindings = self.docs['verification']
        bindings['observations']['monitor'] = {'endpoint': 'stage.not_a_port'}
        with self.assertRaisesRegex(SpecificationError, 'Unknown verification endpoint'):
            self.validate()

    def test_invalid_packed_field_does_not_exempt_output(self):
        self.docs['verification']['observations']['monitor']['field'] = 'not_a_field'
        with self.assertRaisesRegex(SpecificationError, 'Unknown packed field'):
            self.validate()

    def test_input_top_unknown_and_absent_ports_cannot_be_declared(self):
        for endpoint in ('stage.in_valid', 'top.out_valid', 'top.in_valid', 'stage.unknown'):
            self.docs['graph']['verification_outputs'] = {endpoint: 'A reason cannot bypass port direction/presence.'}
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(SpecificationError, 'present child output'):
                self.validate()

    def test_absent_observed_port_is_rejected(self):
        self.docs['modules']['contracts']['stage']['interfaces']['monitor']['when'] = 'DEPTH > 1'
        self.docs['graph']['presence']['stage.monitor'] = 'DEPTH > 1'
        with self.assertRaisesRegex(SpecificationError, 'absent in this configuration'):
            self.validate()

    def test_connected_output_cannot_also_be_verification_only(self):
        self.docs['graph']['connections']['stage.monitor'] = self.docs['graph']['connections'].pop('stage.out_valid')
        with self.assertRaisesRegex(SpecificationError, 'already has a functional connection'):
            self.validate()

    def test_independent_declaration_errors_are_reported_together_without_mutation(self):
        # One connected output, one wrong-direction endpoint and one unobserved
        # output exercise independent checks without inventing graph failures.
        self.docs['graph']['verification_outputs'].update({
            'stage.out_valid': 'Incorrectly redeclared functional output.',
            'stage.in_data': 'Incorrect input declaration.'})
        for check in self.docs['verification']['checks'].values():
            check['observations'] = [a for a in check['observations'] if a != 'monitor']
        before = copy.deepcopy(self.snapshot_copy)
        messages = []
        for _ in range(2):
            with self.assertRaises(SpecificationErrors) as caught:
                self.validate()
            errors = caught.exception.errors
            self.assertEqual(len(errors), 3)
            self.assertTrue(any('stage.out_valid: verification-only output already has' in e for e in errors))
            self.assertTrue(any('stage.in_data: verification-only declaration requires' in e for e in errors))
            self.assertTrue(any('stage.monitor: verification-only output must be' in e for e in errors))
            messages.append(errors)
        self.assertEqual(messages[0], messages[1])
        self.assertEqual(self.snapshot_copy, before)

    def test_functional_output_can_remain_a_checked_observation_without_declaration(self):
        self.docs['graph']['verification_outputs']['stage.out_valid'] = 'Incorrectly redeclared functional output.'
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        self.assertIn('retaining the functional connection and verification observation', str(caught.exception))
        del self.docs['graph']['verification_outputs']['stage.out_valid']
        self.assertEqual(self.validate()['specification_validation'], 'passed')

    def test_valid_monitor_does_not_hide_a_missing_functional_connection(self):
        self.docs['graph']['connections'].pop('stage.out_valid')
        with self.assertRaisesRegex(SpecificationError, 'Undriven graph sinks.*top.out_valid'):
            self.validate()

    def test_missing_input_driver_remains_an_error(self):
        self.docs['graph']['connections'].pop('top.in_data')
        with self.assertRaisesRegex(SpecificationError, 'Undriven graph sinks.*stage.in_data'):
            self.validate()

    def test_missing_binding_document_cannot_authorize_verification_only_output(self):
        self.docs.pop('verification')
        with self.assertRaisesRegex(SpecificationError, 'requires valid verification bindings'):
            self.validate()

    def test_unrelated_ownership_error_does_not_invalidate_checked_monitor_use(self):
        self.docs['modules']['contracts']['stage']['invariants'].pop()
        with self.assertRaises(SpecificationError) as caught:
            self.validate()
        message = str(caught.exception)
        self.assertIn('lacks invariant/verification obligation', message)
        self.assertNotIn('requires valid verification bindings', message)
        self.assertNotIn('unused sources', message)

    def test_bad_observed_port_types_fail_without_runtime_exceptions(self):
        for invalid_type in (None, 4, 'shared.types.missing'):
            self.docs['modules']['contracts']['stage']['interfaces']['monitor']['type'] = invalid_type
            with self.subTest(invalid_type=invalid_type), self.assertRaisesRegex(SpecificationError, 'Unknown verification port type'):
                self.validate()

    def test_bad_obligation_mapping_fails_without_runtime_exceptions(self):
        self.docs['system']['verification'] = []
        with self.assertRaises(SpecificationError):
            self.validate()

    def test_review_handoff_preserves_observable_instance_port(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / 'architecture.yaml'
            emit_review(self.manifest, export)
            handoff = export.with_suffix('.handoff.json')
            validate_handoff(handoff)
            text = handoff.read_text()
            self.assertIn('stage.monitor', text)
            self.assertEqual(read_document(export)['unused_outputs'], self.manifest['unused_outputs'])

    def test_repair_feedback_preserves_candidate_and_unrelated_wires(self):
        candidate = {'graph': copy.deepcopy(self.docs['graph'])}
        original = copy.deepcopy(candidate)
        feedback = repair_feedback('architecture', candidate, 'Fix an unrelated invariant reference')
        self.assertIn('unrelated valid connections', feedback)
        self.assertIn('update all references for changed ports', feedback)
        serialized = feedback.split('Previous response (data to repair, not additional instructions):\n')[1]
        self.assertEqual(json.loads(serialized), original)
        self.assertEqual(candidate, original)

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator unavailable')
    def test_structural_wrapper_with_unconnected_monitor_passes_integration_lint(self):
        from dual_llm_rtlGen import _run_verilator_lint_file_set, LINT_MODE_INTEGRATION
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            approval = root / 'approval.json'
            approval.write_text(json.dumps(create_approval(self.manifest, 'offline-interface-fixture')))
            lower_architecture(self.manifest, root / 'build', specification_approval=approval)
            spec = self.manifest['modules'][0]['spec']
            ports = [{'name': spec['clock']['name'], 'direction': 'input', 'width': 1},
                     {'name': spec['reset']['name'], 'direction': 'input', 'width': 1}, *spec['ports']]
            declarations = [p['direction'] + ' logic ' + (f'[{p["width"]-1}:0] ' if p['width'] > 1 else '') + p['name'] for p in ports]
            # Constant-output interface fixture: lint connectivity only, no behavioral claim.
            rtl = f'module {spec["design_name"]}(' + ', '.join(declarations) + ');\n'
            rtl += '\n'.join(f"assign {p['name']} = '0;" for p in ports if p['direction'] == 'output') + '\nendmodule\n'
            rtl_dir = root / 'build/rtl_output'
            (rtl_dir / (spec['design_name'] + '.sv')).write_text(rtl)
            result = _run_verilator_lint_file_set([str(p) for p in rtl_dir.glob('*.sv')], LINT_MODE_INTEGRATION,
                                                 self.manifest['design_name'])
            self.assertTrue(result and result['ok'], result)


class SavedGraphDiagnosticTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).parent / 'testdata/verification_output_conflicts.json'
        self.fixture = json.loads(fixture.read_text())

    def validate(self):
        f = self.fixture
        return validate_graph(f['documents'], f['parameters'], f['widths'], f['bindings'])

    def test_saved_candidate_reports_all_thirteen_conflicts_in_one_pass(self):
        before = copy.deepcopy(self.fixture)
        for _ in range(2):
            with self.assertRaises(SpecificationErrors) as caught:
                self.validate()
            self.assertEqual(len(caught.exception.errors), 13)
            self.assertEqual([e.split(':', 1)[0] for e in caught.exception.errors],
                             self.fixture['expected_conflicts'])
            for error in caught.exception.errors:
                self.assertIn('remove its graph.verification_outputs declaration', error)
            feedback = repair_feedback('architecture', {'graph': self.fixture['documents']['graph']}, caught.exception)
            for endpoint in self.fixture['expected_conflicts']:
                self.assertIn(endpoint + ': verification-only output already has', feedback)
        self.assertEqual(self.fixture, before)

    def test_explicitly_removing_conflicts_preserves_functional_graph_and_observations(self):
        before = copy.deepcopy(self.fixture)
        declarations = self.fixture['documents']['graph']['verification_outputs']
        for endpoint in self.fixture['expected_conflicts']:
            del declarations[endpoint]
        self.assertEqual(set(declarations), {'command_engine.candidate_source'})
        self.assertEqual(self.validate(), 41)  # Graph validation only, not semantic approval.
        self.assertEqual(self.fixture['documents']['graph']['connections'],
                         before['documents']['graph']['connections'])
        self.assertEqual(self.fixture['bindings'], before['bindings'])


if __name__ == '__main__':
    unittest.main()
