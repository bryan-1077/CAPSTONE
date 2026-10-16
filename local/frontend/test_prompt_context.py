"""Prompt-size regressions with exact reconstruction; no model calls."""
import contextlib
import copy
import io
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dual_llm_rtlGen as generator
from ir_to_llm_context import build_llm_context, llm_context_to_string
from prompt_context import compact_json, encode_context, expand_context, serialize_context
from specification_lowering import compile_package, emit_review
from validator import validate_spec

ROOT = Path(__file__).resolve().parent


def legacy_prompt(design):
    """Pre-optimization context formatting, retained only as a size baseline."""
    ctx = build_llm_context(design)
    lines = [f'Design name:  {design.design_name}', f'Design type:  {design.design_type}']
    if design.clock:
        lines.append(f'Clock: {design.clock.name} @ {design.clock.frequency_mhz or "unspecified"} MHz')
    if design.reset:
        lines.append('Reset: {} | active_{} | {}'.format(design.reset.name,
                     'low' if design.reset.active_low else 'high',
                     'synchronous' if design.reset.synchronous else 'asynchronous'))
    lines.append('The complete public interface below includes clock/reset inputs declared separately in YAML. '
                 'Unused clock/reset ports do not require dummy logic in stateless modules.')
    lines.extend(['\nSTRUCTURED LLM CONTEXT (authoritative):', llm_context_to_string(ctx), ''])
    for title, values in [('AUTHORITATIVE INVARIANTS:', ctx['invariants']),
                          ('IMPLEMENTATION CONSTRAINTS:', ctx['implementation_constraints']),
                          ('FORBIDDEN PATTERNS:', ctx['forbidden_patterns']),
                          ('MUST NOT ASSUME:', ctx['validator_alignment']['must_not_assume'])]:
        lines.extend([generator._format_string_block(title, values), ''])
    return '\n'.join(lines).rstrip('\n')


class ContextSharingTests(unittest.TestCase):
    def test_new_design_metadata_and_fsm_survive_the_real_prompt_builder(self):
        design = SimpleNamespace(
            design_name='novel_link', design_type='fsm', ports=[],
            clock=SimpleNamespace(name='clock_i', frequency_mhz=None),
            reset=SimpleNamespace(name='reset_i', active_low=False, synchronous=False),
            fsm=SimpleNamespace(states=['WAIT', 'SEND'], reset_state='WAIT',
                                transitions=[SimpleNamespace(src='WAIT', dst='SEND', raw_condition='credit')]),
            metadata={'behavior': {'new_protocol': {'credit_rule': 'A future rule ' * 100}},
                      'future_metadata': {'preserve': [0, False, None, 'new mechanism']}})
        bundle = generator._design_context_bundle(design)
        decoded = expand_context(json.loads(bundle['ctx_str']))
        self.assertEqual(decoded, build_llm_context(design))
        self.assertEqual(decoded['metadata'], design.metadata)
        self.assertEqual(decoded['fsm']['transitions'][0]['condition'], 'credit')

    def test_nested_sharing_preserves_all_values_without_mutation(self):
        rule = 'Arbitrary new behavior with spaces, Unicode λ and quotes " exactly preserved. ' * 8
        subtree = {'not_a_known_schema': [rule, False, 0, None, {'ordered': ['z', 'a', 'z']}],
                   'other': [1, 1.5, True]}
        context = {'one': subtree, 'two': copy.deepcopy(subtree), 'other_rule': rule,
                   'unique_behavior': 'Only present once; this requirement must survive.'}
        original = copy.deepcopy(context)
        text = serialize_context(context)
        self.assertEqual(expand_context(json.loads(text)), original)
        self.assertEqual(context, original)
        self.assertEqual(text, serialize_context(dict(reversed(list(context.items())))))
        self.assertLess(len(text), len(compact_json(context)))
        self.assertEqual(text.count('Arbitrary new behavior'), 8)

    def test_small_values_and_existing_reference_metadata_are_preserved(self):
        for context in ({'width': 1, 'edges': ['rising']},
                        {'custom': {'$ref': '#/some_external_schema'}, 'repeat': ['x' * 300] * 3}):
            text = serialize_context(context)
            self.assertEqual(json.loads(text), context)
            self.assertEqual(expand_context(json.loads(text)), context)

    def test_generated_contexts_roundtrip_and_never_grow(self):
        rng = random.Random(17)
        for _ in range(40):
            facts = [('unfamiliar_' + str(rng.randrange(10)) + ' ') * rng.randrange(1, 60) for _ in range(8)]
            context = {'rules': facts, 'contract': {'rules': facts, 'unique': 'Preserve this field'},
                       'variants': [rng.choice(facts) for _ in range(10)]}
            text = serialize_context(context)
            self.assertEqual(expand_context(json.loads(text)), context)
            self.assertLessEqual(len(text), len(compact_json(context)))

    def test_decoder_rejects_dangling_and_cyclic_references(self):
        document = {'context_format': 'shared_definitions_v1', 'definitions': {},
                    'context': {'$ref': '#/definitions/C001'}}
        with self.assertRaises(KeyError):
            expand_context(document)
        document['definitions']['C001'] = {'$ref': '#/definitions/C001'}
        with self.assertRaisesRegex(ValueError, 'Cyclic'):
            expand_context(document)

    def test_history_retains_diagnostics_and_one_latest_candidate(self):
        history = [{'round': n, 'llm1_code': f'GENERATOR_{n}', 'llm2_code': f'REVIEWER_{n}',
                    'merged_code': f'MERGED_{n}', 'llm2_status': 'INVALID',
                    'llm2_report': [{'type': 'SPEC_VIOLATION', 'description': f'ISSUE_{n}'}]}
                   for n in range(1, 4)]
        report = generator._history_block(history, 4)
        self.assertIn('ISSUE_2', report)
        self.assertIn('ISSUE_3', report)
        self.assertNotIn('ISSUE_1', report)
        self.assertNotIn('MERGED_', report)
        with_candidate = generator._history_block(history, 4, include_latest_rtl=True)
        self.assertEqual(with_candidate.count('MERGED_3'), 1)
        self.assertNotIn('GENERATOR_', with_candidate)
        self.assertNotIn('REVIEWER_', with_candidate)


class ContractPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        paths = emit_review(compile_package(ROOT / 'microarch/specification/ddr4_fifo/package.yaml'),
                            Path(cls.temp.name) / 'architecture.yaml')
        with contextlib.redirect_stdout(io.StringIO()):
            cls.designs = [validate_spec(str(path)) for path in paths]

    def test_all_module_contexts_preserve_every_fact_and_reduce_size(self):
        for design in self.designs:
            with self.subTest(module=design.design_name):
                context = build_llm_context(design)
                bundle = generator._design_context_bundle(design)
                self.assertEqual(expand_context(json.loads(bundle['ctx_str'])), context)
                self.assertEqual(bundle['prompt_text'].count(bundle['ctx_str']), 1)
                self.assertLess(len(bundle['prompt_text']), len(legacy_prompt(design)) * 0.8)
                self.assertEqual(bundle['ctx']['metadata']['behavior'], design.metadata['behavior'])
                self.assertEqual(generator._compact_review_context(design), bundle['prompt_text'])

    def test_generator_and_merger_do_not_echo_invariants_but_keep_compiler_copy(self):
        design = self.designs[0]
        reply = json.dumps({'status': 'VALID', 'issues': [], 'corrected_rtl': 'module m; endmodule'})
        for system in (generator._SYS_GENERATOR, generator._SYS_MERGER):
            self.assertNotIn('"invariants":', system)
            self.assertIn('Do not echo invariants', system)
        with patch.object(generator, '_call_llm', return_value=reply):
            result = generator._llm1_generate(design, '', [])
        self.assertEqual(result['invariants'], build_llm_context(design)['invariants'])
        # Older/model-supplied echo fields cannot replace the compiler's facts.
        old_reply = json.dumps({'status': 'VALID', 'issues': [], 'invariants': ['invented'],
                                'corrected_rtl': 'module m; endmodule'})
        with patch.object(generator, '_call_llm', return_value=old_reply):
            result = generator._llm1_generate(design, '', [])
        self.assertEqual(result['invariants'], build_llm_context(design)['invariants'])

    def test_all_prompt_paths_use_complete_context_and_send_candidate_once(self):
        design = self.designs[0]
        original = 'module m; /* UNIQUE_ORIGINAL_RTL */ endmodule'
        corrected = 'module m; /* UNIQUE_CORRECTED_RTL */ endmodule'
        reply = json.dumps({'status': 'VALID', 'issues': [], 'corrected_rtl': original})
        history = [{'round': 1, 'llm1_code': original, 'llm2_code': original,
                    'merged_code': original, 'llm2_status': 'INVALID', 'llm2_report': []}]
        expected_context = generator._design_context_bundle(design)['ctx_str']
        calls = [lambda: generator._llm1_generate(design, '', history, lint_issues=['lint issue'], previous_rtl=original),
                 lambda: generator._llm1_generate(design, '', history),
                 lambda: generator._llm2_review(design, '', original, history),
                 lambda: generator._llm2_review_compact(design, original),
                 lambda: generator._llm1_merge(design, '', original, original, {'issues': []}, history)]
        for call in calls:
            with patch.object(generator, '_call_llm', return_value=reply) as api:
                call()
            message = api.call_args.args[2]
            self.assertEqual(message.count(expected_context), 1)
            self.assertEqual(message.count(original), 1)
        with patch.object(generator, '_call_llm', return_value=reply) as api:
            generator._llm1_merge(design, '', original, corrected, {'issues': []}, history)
        self.assertEqual(api.call_args.args[2].count(original), 1)
        self.assertEqual(api.call_args.args[2].count(corrected), 1)


if __name__ == '__main__':
    unittest.main()
