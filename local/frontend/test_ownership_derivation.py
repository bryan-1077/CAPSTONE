"""Compiler-owned reference bookkeeping; ownership and verification intent stay explicit."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from generate_specification import (assemble_package, build_prompt, generation_context,
                                    propose_specification, validate_system)
from specification import SpecificationError, read_document, validate_ownership
from specification_lowering import compile_package
from test_generate_specification import register_fixture


class OwnershipDerivationTests(unittest.TestCase):
    def setUp(self):
        _, self.request, self.responses = register_fixture()
        self.context = generation_context()
        self.system = validate_system(self.responses['system'], self.request, self.context['timing_library'])
        self.arch = self.responses['architecture']

    def omit_lists(self):
        for contract in self.arch['modules']['contracts'].values():
            contract.pop('invariants', None)
            contract.pop('verification', None)

    def assemble(self):
        return assemble_package(self.system, self.arch, self.request, 'offline', 'offline', [])

    def test_derives_missing_lists_without_mutating_choices_or_behavior(self):
        expected = copy.deepcopy(self.arch['modules']['contracts']['stage'])
        self.omit_lists()
        before = copy.deepcopy((self.system, self.arch))
        _, docs = self.assemble()
        contract = docs['modules']['contracts']['stage']
        for key in ('invariants', 'verification'):
            self.assertEqual(contract[key], sorted(expected[key]))
        for key in set(expected) - {'invariants', 'verification'}:
            self.assertEqual(contract[key], expected[key])
        self.assertEqual(docs['verification'], self.arch['verification'])
        self.assertEqual((self.system, self.arch), before)
        validate_ownership(docs)

    def test_each_owner_gets_deduplicated_slice_qualified_references(self):
        rid, rule = next(iter(self.system['system']['rules'].items()))
        vid = rule['verification'][0]
        self.system['execution']['rules'][rid] = copy.deepcopy(rule)
        self.system['execution']['verification'][vid] = copy.deepcopy(self.system['system']['verification'][vid])
        # Same obligation attached to a second rule tests de-duplication.
        extra = rid + '_REFINEMENT'
        self.system['system']['rules'][extra] = copy.deepcopy(rule)
        self.system['system']['verification'][vid]['rules'].append(extra)
        self.arch['modules']['contracts']['other'] = copy.deepcopy(self.arch['modules']['contracts']['stage'])
        self.arch['rule_owners']['execution.rules.' + rid] = ['stage', 'other']
        self.arch['rule_owners']['system.rules.' + extra] = ['stage', 'other']
        for owners in self.arch['rule_owners'].values():
            owners[:] = ['stage', 'other']
        self.omit_lists()
        _, docs = self.assemble()
        first = docs['modules']['contracts']['stage']
        second = docs['modules']['contracts']['other']
        self.assertEqual(first['invariants'], second['invariants'])
        self.assertEqual(first['verification'], second['verification'])
        self.assertIn('execution.verification.' + vid, first['verification'])
        self.assertIn('system.verification.' + vid, first['verification'])
        self.assertEqual(len(first['verification']), len(set(first['verification'])))
        validate_ownership(docs)  # Reference validity only; not integration proof.

    def test_explicit_legacy_lists_are_preserved_and_strictly_checked(self):
        _, docs = self.assemble()
        self.assertEqual(docs['modules'], self.arch['modules'])
        self.arch['modules']['contracts']['stage']['invariants'].pop()
        _, docs = self.assemble()
        with self.assertRaisesRegex(SpecificationError, 'lacks invariant/verification obligation'):
            validate_ownership(docs)

    def test_only_absent_fields_are_derived_not_empty_or_malformed_lists(self):
        self.arch['modules']['contracts']['stage'].pop('invariants')
        for supplied in ([], None, 'not_a_list'):
            self.arch['modules']['contracts']['stage']['verification'] = supplied
            _, docs = self.assemble()
            self.assertEqual(docs['modules']['contracts']['stage']['verification'], supplied)
            with self.subTest(supplied=supplied), self.assertRaises(SpecificationError):
                validate_ownership(docs)

    def test_unknown_empty_and_missing_owners_remain_errors(self):
        self.omit_lists()
        ref = next(iter(self.arch['rule_owners']))
        for supplied in ([], ['unknown']):
            self.arch['rule_owners'][ref] = supplied
            _, docs = self.assemble()
            self.assertNotIn('unknown', docs['modules']['contracts'])
            with self.subTest(supplied=supplied), self.assertRaisesRegex(SpecificationError, 'missing or unknown rule owners'):
                validate_ownership(docs)
        del self.arch['rule_owners'][ref]
        with self.assertRaisesRegex(SpecificationError, 'missing fields'):
            self.assemble()

    def test_reassembly_uses_current_owners_without_leaving_stale_references(self):
        self.arch['modules']['contracts']['other'] = copy.deepcopy(self.arch['modules']['contracts']['stage'])
        self.omit_lists()
        _, first = self.assemble()
        self.assertEqual(first['modules']['contracts']['other']['invariants'], [])
        for owners in self.arch['rule_owners'].values():
            owners[:] = ['other']
        _, second = self.assemble()
        self.assertEqual(second['modules']['contracts']['stage']['invariants'], [])
        self.assertEqual(second['modules']['contracts']['stage']['verification'], [])
        self.assertTrue(second['modules']['contracts']['other']['invariants'])
        validate_ownership(second)
        for contract in self.arch['modules']['contracts'].values():
            self.assertNotIn('invariants', contract)
            self.assertNotIn('verification', contract)

    def test_published_source_still_requires_explicit_reference_lists(self):
        self.omit_lists()
        _, docs = self.assemble()
        del docs['modules']['contracts']['stage']['invariants']
        with self.assertRaisesRegex(SpecificationError, 'missing fields'):
            validate_ownership(docs)

    def test_unowned_explicit_reference_is_not_overwritten(self):
        self.arch['modules']['contracts']['stage']['invariants'].append('system.rules.UNKNOWN')
        _, docs = self.assemble()
        with self.assertRaisesRegex(SpecificationError, 'unknown or unowned invariant'):
            validate_ownership(docs)

    def test_generated_omissions_pass_real_compiler_and_are_recorded_losslessly(self):
        self.omit_lists()
        before = copy.deepcopy(self.responses)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            replay = root / 'responses.json'
            replay.write_text(json.dumps(self.responses))
            package = propose_specification(self.request, output_dir=root / 'source', response_path=replay)
            manifest = compile_package(package)
            contract = manifest['provenance']['specification']['snapshot']['documents']['modules']['contracts']['stage']
            self.assertTrue(contract['invariants'])
            self.assertTrue(contract['verification'])
            self.assertEqual(read_document(package.parent / 'responses.json'), before)
            self.assertEqual(json.loads((package.parent / 'attempts.json').read_text())['attempts'][1]['response'], before['architecture'])
        self.assertEqual(self.responses, before)

    def test_reference_derivation_does_not_supply_missing_verification_checks(self):
        self.omit_lists()
        self.arch['verification']['checks'].clear()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            replay = root / 'responses.json'
            replay.write_text(json.dumps(self.responses))
            with self.assertRaisesRegex(SpecificationError, 'every obligation exactly once'):
                propose_specification(self.request, output_dir=root / 'source', response_path=replay)
            self.assertFalse((root / 'source').exists())

    def test_prompt_example_omits_lists_but_keeps_verification_intent(self):
        prompt = build_prompt('architecture', self.request, self.context, self.system)
        example = json.loads(prompt.split('FORMAT AND CAPABILITIES:\n')[1].split('\nLOCKED SYSTEM')[0])['format_example']
        for contract in example['modules']['contracts'].values():
            self.assertNotIn('invariants', contract)
            self.assertNotIn('verification', contract)
        self.assertTrue(example['rule_owners'])
        self.assertTrue(example['verification']['checks'])
        self.assertIn('compiler derives them', prompt)


if __name__ == '__main__':
    unittest.main()
