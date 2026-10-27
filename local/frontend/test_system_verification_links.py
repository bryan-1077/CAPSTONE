"""Collect independent system-stage link errors before paying for a repair."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from generate_specification import generation_context, propose_specification, validate_system
from specification import SpecificationError, SpecificationErrors, read_document
from test_generate_specification import register_fixture


class SystemVerificationLinkTests(unittest.TestCase):
    def setUp(self):
        _, self.request, self.responses = register_fixture()
        self.library = generation_context()['timing_library']

    def broken(self):
        response = copy.deepcopy(self.responses['system'])
        rid = next(iter(response['system']['rules']))
        response['system']['rules'][rid]['verification'].append('MISSING_OBLIGATION')
        response['system']['verification']['V_' + rid]['rules'].append('MISSING_RULE')
        response['execution']['verification']['V_EXEC'] = {
            'rules': ['MISSING_EXEC_RULE'], 'scenario': 'Observe a sampled edge.',
            'expected': 'The declared requirement holds.', 'status': 'planned_not_executed'}
        return response

    def diagnostics(self, response):
        with self.assertRaises(SpecificationErrors) as caught:
            validate_system(response, self.request, self.library)
        return caught.exception.errors

    def test_all_three_errors_across_both_slices_are_stable_and_input_is_unchanged(self):
        response = self.broken()
        original = copy.deepcopy(response)
        errors = self.diagnostics(response)
        self.assertEqual(len(errors), 3)
        self.assertIn('nonreciprocal verification reference MISSING_OBLIGATION', errors[0])
        self.assertIn('nonreciprocal rule reference MISSING_RULE', errors[1])
        self.assertIn('execution.verification.V_EXEC', errors[2])
        self.assertEqual(errors, self.diagnostics(response))
        self.assertEqual(len(errors), len(set(errors)))
        self.assertEqual(response, original)

    def test_other_slice_does_not_satisfy_a_local_verification_reference(self):
        response = self.broken()
        response['execution']['verification']['MISSING_OBLIGATION'] = {
            'rules': ['MISSING_EXEC_RULE'], 'scenario': 'Observe an edge.',
            'expected': 'The requirement holds.', 'status': 'planned_not_executed'}
        errors = self.diagnostics(response)
        self.assertTrue(any('system.rules.' in e and 'MISSING_OBLIGATION' in e for e in errors))

    def test_malformed_obligations_stop_before_relationship_checks(self):
        for malformed in ([], {'rules': 'NOT_A_LIST', 'scenario': 'Observe.',
                               'expected': 'Holds.', 'status': 'planned_not_executed'}):
            response = self.broken()
            response['execution']['verification']['V_EXEC'] = malformed
            with self.subTest(malformed=malformed), self.assertRaises(SpecificationError) as caught:
                validate_system(response, self.request, self.library)
            self.assertNotIsInstance(caught.exception, SpecificationErrors)

    def test_valid_links_are_preserved(self):
        response = self.responses['system']
        system = validate_system(response, self.request, self.library)
        for section in ('system', 'execution'):
            self.assertEqual(system[section], response[section])

    def cross_phase_broken(self):
        response = self.broken()
        for name in ('D_BANKS', 'D_CAPACITY'):
            response['package']['decisions'][name] = {
                'origin': 'model_selected', 'status': 'recorded',
                'value': 4, 'reason': 'Fixture value.'}
        for name in ('BANK_COUNT', 'WAITING_CAPACITY'):
            response['shared']['parameters'][name] = {'value': 4, 'configurable': False}
        response['package']['verification_strategy']['configurations']['parameters'] = {
            'BANK_COUNT': [4], 'WAITING_CAPACITY': [4]}
        return response

    def test_metadata_matrix_and_links_are_reported_together_without_mutation(self):
        response = self.cross_phase_broken()
        original = copy.deepcopy(response)
        errors = self.diagnostics(response)
        self.assertEqual(len(errors), 7)  # Two values, two axes, three links.
        for name in ('D_BANKS', 'D_CAPACITY'):
            self.assertTrue(any('decisions.' + name + '.value' in e for e in errors))
        for name in ('BANK_COUNT', 'WAITING_CAPACITY'):
            self.assertTrue(any('configurations.parameters.' + name in e for e in errors))
        self.assertEqual(errors, self.diagnostics(response))
        self.assertEqual(response, original)

    def test_bad_shared_shape_skips_matrix_but_keeps_metadata_and_links(self):
        response = self.cross_phase_broken()
        response['shared'] = []
        errors = self.diagnostics(response)
        self.assertEqual(len(errors), 6)
        self.assertFalse(any('configurations.parameters.' in e for e in errors))
        self.assertTrue(any('shared' in e for e in errors))

    def test_bad_strategy_shape_reports_validation_error_without_crashing(self):
        response = self.responses['system']
        response['package']['verification_strategy'] = []
        errors = self.diagnostics(response)
        self.assertEqual(len(errors), 1)
        self.assertIn('verification_strategy', errors[0])

    def test_implicit_configurable_allowed_parameter_still_passes(self):
        response = self.responses['system']
        response['shared']['parameters']['DEPTH'].pop('configurable')
        response['package']['verification_strategy']['configurations']['parameters'] = {'DEPTH': [1]}
        validate_system(response, self.request, self.library)

    def test_first_repair_and_journal_receive_every_error(self):
        broken = self.cross_phase_broken()
        errors = self.diagnostics(broken)
        raw = json.dumps(broken)
        replies = iter([raw, json.dumps(self.responses['system']), json.dumps(self.responses['architecture'])])
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return next(replies)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            package = propose_specification(self.request, output_dir=root / 'source', caller=caller)
            self.assertEqual(len(prompts), 3)
            for error in errors:
                self.assertIn(error, prompts[1])
            journal = json.loads((package.parent / 'attempts.json').read_text())
            rejected = journal['attempts'][0]
            self.assertEqual(rejected['response'], raw)
            self.assertEqual(rejected['diagnostics'], errors)
            self.assertEqual([a['status'] for a in journal['attempts']], ['rejected', 'accepted', 'accepted'])
            repairs = read_document(package)['generation']['validation_repairs']
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]['diagnostics'], errors)
            self.assertFalse(list(root.rglob('*.sv')))
            self.assertFalse(list(root.rglob('*approval*')))

    def test_three_rejections_still_stop_before_architecture_or_publication(self):
        raw = json.dumps(self.broken())
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return raw
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            with self.assertRaisesRegex(SpecificationError, 'system specification failed validation'):
                propose_specification(self.request, output_dir=root / 'source', caller=caller)
            self.assertEqual(len(prompts), 3)
            report = json.loads((root / 'source.failure.json').read_text())
            self.assertEqual(report['accepted_stages'], [])
            self.assertTrue(all(len(r['diagnostics']) == 3 for r in report['validation_repairs']))
            self.assertFalse((root / 'source').exists())


if __name__ == '__main__':
    unittest.main()
