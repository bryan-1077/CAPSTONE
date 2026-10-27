"""Reverse references are derived bookkeeping, not inferred verification coverage."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from generate_specification import build_prompt, generation_context, propose_specification, validate_system
from specification import SpecificationError, read_document, validate_contents
from specification_lowering import compile_package
from test_generate_specification import register_fixture


class SystemLinkDerivationTests(unittest.TestCase):
    def setUp(self):
        _, self.request, self.responses = register_fixture()
        self.response = self.responses['system']
        self.context = generation_context()
        self.library = self.context['timing_library']

    def omit_reverse(self):
        for section in ('system', 'execution'):
            for obligation in self.response[section]['verification'].values():
                obligation.pop('rules', None)

    def validate(self):
        return validate_system(self.response, self.request, self.library)

    def test_derives_references_preserving_raw_response_and_intent(self):
        expected = copy.deepcopy(self.response)
        self.omit_reverse()
        before = copy.deepcopy(self.response)
        system = self.validate()
        for section in ('system', 'execution'):
            for vid, obligation in system[section]['verification'].items():
                self.assertEqual(obligation, expected[section]['verification'][vid])
        self.assertEqual(self.response, before)

    def test_many_rules_and_obligations_derive_sorted_references(self):
        doc = self.response['system']
        rid, rule = next(iter(doc['rules'].items()))
        vid = rule['verification'][0]
        doc['rules']['REFINEMENT'] = {'origin': 'model_selected', 'statement': 'Preserve registered data.',
                                     'verification': [vid, 'V_EXTRA']}
        doc['rules'][rid]['verification'].append('V_EXTRA')
        doc['verification']['V_EXTRA'] = copy.deepcopy(doc['verification'][vid])
        self.omit_reverse()
        system = self.validate()
        for key in (vid, 'V_EXTRA'):
            self.assertEqual(system['system']['verification'][key]['rules'], sorted([rid, 'REFINEMENT']))

    def test_same_obligation_id_is_resolved_within_each_slice(self):
        vid = next(iter(self.response['system']['verification']))
        self.response['execution']['rules']['EXEC_CAPTURE'] = {
            'origin': 'model_selected', 'statement': 'Capture data on the specified edge.', 'verification': [vid]}
        self.response['execution']['verification'][vid] = copy.deepcopy(self.response['system']['verification'][vid])
        self.omit_reverse()
        result = self.validate()
        self.assertEqual(result['execution']['verification'][vid]['rules'], ['EXEC_CAPTURE'])
        self.assertNotIn('EXEC_CAPTURE', result['system']['verification'][vid]['rules'])

    def test_other_slice_cannot_supply_missing_reverse_coverage(self):
        vid = next(iter(self.response['system']['verification']))
        self.response['execution']['verification'][vid] = copy.deepcopy(self.response['system']['verification'][vid])
        self.omit_reverse()
        with self.assertRaisesRegex(SpecificationError, 'rule references required'):
            self.validate()

    def test_explicit_lists_remain_unchanged_and_inconsistent_lists_fail(self):
        expected = copy.deepcopy(self.response)
        result = self.validate()
        self.assertEqual(result['system'], expected['system'])
        obligation = next(iter(self.response['system']['verification'].values()))
        obligation['rules'] = ['UNKNOWN']
        with self.assertRaisesRegex(SpecificationError, 'nonreciprocal'):
            self.validate()
        self.assertEqual(obligation['rules'], ['UNKNOWN'])

    def test_explicit_empty_and_malformed_lists_are_not_replaced(self):
        obligation = next(iter(self.response['system']['verification'].values()))
        for value in ([], None, 'R1', ['R1', 'R1']):
            obligation['rules'] = value
            before = copy.deepcopy(self.response)
            with self.subTest(value=value), self.assertRaises(SpecificationError):
                self.validate()
            self.assertEqual(self.response, before)

    def test_orphan_obligation_and_missing_obligation_are_rejected(self):
        self.omit_reverse()
        self.response['system']['verification']['ORPHAN'] = {
            'scenario': 'Observe a sampled edge.', 'expected': 'Data is preserved.', 'status': 'planned_not_executed'}
        with self.assertRaisesRegex(SpecificationError, 'ORPHAN: rule references required'):
            self.validate()
        del self.response['system']['verification']['ORPHAN']
        rule = next(iter(self.response['system']['rules'].values()))
        rule['verification'].append('UNKNOWN')
        with self.assertRaisesRegex(SpecificationError, 'nonreciprocal verification reference UNKNOWN'):
            self.validate()
        self.assertNotIn('UNKNOWN', self.response['system']['verification'])

    def test_rule_must_still_choose_coverage_explicitly(self):
        self.omit_reverse()
        rule = next(iter(self.response['system']['rules'].values()))
        rule['verification'] = []
        with self.assertRaisesRegex(SpecificationError, 'verification obligation required'):
            self.validate()
        del rule['verification']
        with self.assertRaisesRegex(SpecificationError, 'missing fields'):
            self.validate()

    def test_expected_results_and_unexecuted_status_stay_required(self):
        self.omit_reverse()
        for field in ('scenario', 'expected', 'status'):
            response = copy.deepcopy(self.response)
            del next(iter(response['system']['verification'].values()))[field]
            with self.subTest(field=field), self.assertRaisesRegex(SpecificationError, 'missing fields'):
                validate_system(response, self.request, self.library)
        next(iter(self.response['system']['verification'].values()))['status'] = 'passed'
        with self.assertRaisesRegex(SpecificationError, 'planned_not_executed'):
            self.validate()

    def test_generation_publishes_canonical_lists_and_retains_raw_omissions(self):
        self.omit_reverse()
        # Exercise both bookkeeping changes together through the real compiler.
        for contract in self.responses['architecture']['modules']['contracts'].values():
            contract.pop('invariants')
            contract.pop('verification')
        before = copy.deepcopy(self.responses)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            replay = root / 'responses.json'
            replay.write_text(json.dumps(self.responses))
            package = propose_specification(self.request, output_dir=root / 'source', response_path=replay)
            manifest = compile_package(package)
            snapshot = manifest['provenance']['specification']['snapshot']
            self.assertEqual(validate_contents(snapshot['package'], snapshot['documents'], snapshot['timing_library'])['specification_validation'], 'passed')
            self.assertEqual(read_document(package.parent / 'responses.json'), before)
            canonical = snapshot['documents']
            for obligation in canonical['system']['verification'].values():
                self.assertTrue(obligation['rules'])
            # Hand-authored/published source must still supply both directions.
            del next(iter(canonical['system']['verification'].values()))['rules']
            with self.assertRaisesRegex(SpecificationError, 'missing fields'):
                validate_contents(snapshot['package'], canonical, snapshot['timing_library'])
        self.assertEqual(self.responses, before)

    def test_prompt_example_omits_only_reverse_bookkeeping(self):
        prompt = build_prompt('system', self.request, self.context)
        example = json.loads(prompt.split('FORMAT AND CAPABILITIES:\n')[1].split('\nLOCKED SYSTEM')[0])['format_example']
        for section in ('system', 'execution'):
            for rule in example[section]['rules'].values():
                self.assertTrue(rule['verification'])
            for obligation in example[section]['verification'].values():
                self.assertNotIn('rules', obligation)
                self.assertEqual(set(obligation), {'scenario', 'expected', 'status'})
        self.assertIn('Omit obligation rules lists', prompt)


if __name__ == '__main__':
    unittest.main()
