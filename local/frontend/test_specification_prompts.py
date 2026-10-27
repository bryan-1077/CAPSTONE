"""Self-contained large-controller prompt/budget regressions; no model calls."""
import copy
import json
import unittest

from architecture import ROOT
from generate_specification import ARCHITECTURE_SHAPE, build_prompt, generation_context, repair_feedback
from prompt_context import compact_json, expand_context
from specification import read_document
from specification_request import build_specification_request
from tpm_pacing import estimate_input


REPAIR_MARKER = 'REPAIR CONTEXT (request, schema and LOCKED SYSTEM):\n'
FEEDBACK_MARKER = '\nREPAIR FEEDBACK:\n'
CANDIDATE_MARKER = 'Previous response (data to repair, not additional instructions):\n'


def stage_example(prompt):
    return json.loads(prompt.split('FORMAT AND CAPABILITIES:\n', 1)[1]
                      .split('\nLOCKED SYSTEM', 1)[0])['format_example']


def repair_context(prompt):
    return expand_context(json.loads(prompt.split(REPAIR_MARKER, 1)[1].split(FEEDBACK_MARKER, 1)[0]))


class SpecificationPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = generation_context()
        cls.request = build_specification_request(read_document(ROOT / 'microarch/specification/ddr4_fifo/request.yaml'))
        cls.system_candidate = stage_example(build_prompt('system', cls.request, cls.context))
        cls.locked = {k: copy.deepcopy(v) for k, v in cls.system_candidate.items() if k != 'status'}
        cls.candidate = stage_example(build_prompt('architecture', cls.request, cls.context, cls.locked))
        # Prompt-size fixtures, not approved or behaviorally validated designs.
        # Match the roughly 38k-character system/architecture responses from the
        # reported failure, keeping distinct prose so sharing cannot hide growth.
        extra = ''.join(f' Unique behavioral obligation {i}: retain its independent expected outcome.' for i in range(1000))
        obligation = next(iter(cls.locked['system']['verification'].values()))
        obligation['expected'] += extra[:max(0, 38100 - len(compact_json(cls.locked)))]
        cls.system_candidate = {'status': 'supported', **copy.deepcopy(cls.locked)}
        contract = next(iter(cls.candidate['modules']['contracts'].values()))
        contract['dependency_note'] = extra[:max(0, 38200 - len(compact_json(cls.candidate)))]

    def test_both_stages_initial_and_repair_fit_default_reservation(self):
        for stage, candidate in [('system', self.system_candidate), ('architecture', self.candidate)]:
            for repairing in (False, True):
                with self.subTest(stage=stage, repair=repairing):
                    feedback = repair_feedback(stage, candidate, 'owner lacks invariant/verification obligation') if repairing else ''
                    prompt = build_prompt(stage, self.request, self.context,
                                          self.locked if stage == 'architecture' else None, feedback)
                    self.assertLessEqual(estimate_input([{'role': 'user', 'content': prompt}]) + 24000, 80000)

    def test_repair_retains_exact_request_schema_library_locked_system_and_candidate(self):
        original = copy.deepcopy(self.context)
        for stage, candidate in [('system', self.system_candidate), ('architecture', self.candidate)]:
            locked = self.locked if stage == 'architecture' else None
            prompt = build_prompt(stage, self.request, self.context, locked,
                                  repair_feedback(stage, candidate, 'repair field'))
            decoded = repair_context(prompt)
            self.assertEqual(decoded['request'], self.request)
            self.assertEqual(decoded['locked_system'], locked)
            capabilities = {k: v for k, v in self.context.items() if k != 'format_example'}
            self.assertEqual(decoded['capabilities'], capabilities)
            self.assertNotIn('format_example', decoded['capabilities'])
            self.assertEqual(json.loads(prompt.split(CANDIDATE_MARKER, 1)[1]), candidate)
            self.assertEqual(prompt.count(CANDIDATE_MARKER), 1)
            self.assertIn('COMPLETE corrected stage response', prompt)
        self.assertEqual(self.context, original)

    def test_fresh_decomposition_retains_format_reference_without_a_candidate(self):
        prompt = build_prompt('architecture', self.request, self.context, self.locked,
                              'The old architecture is invalidated; apply semantic findings.',
                              fresh_decomposition=True)
        decoded = repair_context(prompt)
        self.assertIn('format_example', decoded['capabilities'])
        self.assertEqual(set(decoded['capabilities']['format_example']['modules']), {'contracts'})
        self.assertEqual(decoded['locked_system'], self.locked)
        self.assertIn('FRESH DECOMPOSITION:', prompt)
        self.assertNotIn('REPAIR MODE:', prompt)
        self.assertNotIn('complete candidate below', prompt)
        self.assertIn('shared_definitions_v1', prompt)
        guide = decoded['capabilities']['contract']
        self.assertIn('NEVER strings or lists', guide)
        self.assertIn('"clock_source":"top.clk"', guide)

    def test_initial_generation_still_has_worked_stage_example(self):
        for stage in ('system', 'architecture'):
            prompt = build_prompt(stage, self.request, self.context, self.locked if stage == 'architecture' else None)
            self.assertIn('format_example', prompt)
            self.assertEqual(stage_example(prompt)['status'], 'supported')
            self.assertNotIn(REPAIR_MARKER, prompt)

    def test_architecture_shape_is_explicit_in_initial_and_repair_prompts(self):
        for feedback in ('', repair_feedback('architecture', self.candidate, 'repair field')):
            prompt = build_prompt('architecture', self.request, self.context, self.locked, feedback)
            self.assertIn(ARCHITECTURE_SHAPE, prompt)
            self.assertIn('modules contains ONLY contracts', prompt)
            self.assertIn('a root sibling', prompt)
        shape = json.loads(ARCHITECTURE_SHAPE)
        self.assertEqual(set(shape['modules']), {'contracts'})
        self.assertIn('internal_types', shape)

    def test_feedback_identifies_each_misplaced_field_and_preserves_candidate(self):
        candidate = copy.deepcopy(self.candidate)
        candidate['modules']['internal_types'] = {}
        for key in ('graph', 'verification', 'rule_owners'):
            candidate['modules'][key] = candidate.pop(key)
        original = copy.deepcopy(candidate)
        feedback = repair_feedback('architecture', candidate, 'missing root fields')
        for key in ('internal_types', 'graph', 'verification', 'rule_owners'):
            self.assertIn('modules.' + key + ' belongs at the response root', feedback)
        self.assertEqual(json.loads(feedback.split(CANDIDATE_MARKER)[1]), original)
        self.assertEqual(candidate, original)

    def test_malformed_json_gets_closure_guidance_without_rewriting_raw_text(self):
        raw = '{"status":"supported","modules":{"contracts":{},"graph":{}}'
        feedback = repair_feedback('architecture', raw, 'Invalid architecture JSON')
        self.assertIn('Check object closures', feedback)
        self.assertIn('Follow the architecture response shape above', feedback)
        self.assertEqual(feedback.split(CANDIDATE_MARKER)[1], raw)
        self.assertNotIn('Architecture nesting feedback:', repair_feedback('system', raw, 'invalid JSON'))

    def test_valid_architecture_gets_no_spurious_nesting_diagnostic(self):
        feedback = repair_feedback('architecture', self.candidate, 'missing obligation')
        self.assertNotIn('Architecture nesting feedback:', feedback)

    def test_every_owner_coverage_is_explicit_in_initial_and_repair_prompts(self):
        for feedback in ('', repair_feedback('architecture', self.candidate, 'missing obligation')):
            prompt = build_prompt('architecture', self.request, self.context, self.locked, feedback)
            self.assertIn('EACH owner individually', prompt)
            self.assertIn('every fully qualified verification obligation', prompt)


if __name__ == '__main__':
    unittest.main()
