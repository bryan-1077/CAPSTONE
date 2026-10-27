"""Strict payload contracts and rejection at real generation-loop boundaries."""
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dual_llm_rtlGen as generator
from llm_diagnostics import LlmFailure
from rtl_response_payload import PayloadValidationError, decode_json, json_input, validate_payload
from token_usage import ModuleUsage, UsageRun
from test_llm_diagnostics import response


RTL = 'module sample;\n  initial $display("quote, brace } { and \\\\ path");\nendmodule'
GOOD = {'status': 'VALID', 'issues': [], 'corrected_rtl': RTL}
VIOLATION = {'type': 'SPEC_VIOLATION', 'description': 'Fix the specified reset behavior.', 'location': 'sample.reset'}
AMBIGUITY = {'type': 'AMBIGUITY', 'description': 'Required response edge is unspecified.', 'location': 'response'}
NON_ISSUE = {'type': 'NON_ISSUE', 'description': 'Existing behavior matches context.', 'location': 'sample'}
STAGES = ('generator', 'merger', 'reviewer', 'compact_reviewer')


def valid(stage):
    return copy.deepcopy(GOOD if stage in ('generator', 'merger') else {'status': 'VALID', 'issues': []})


class RtlResponseSchemaTests(unittest.TestCase):
    def test_valid_payloads_are_copied_and_stage_shapes_are_explicit(self):
        for stage in STAGES:
            original = valid(stage)
            before = copy.deepcopy(original)
            result = validate_payload(original, stage)
            self.assertEqual(result, original)
            result['issues'].append(copy.deepcopy(NON_ISSUE))
            self.assertEqual(original, before)

    def test_status_alias_case_and_whitespace_compatibility_are_deliberate(self):
        for stage in STAGES:
            for status in ('VALID', 'valid', ' CLEAN ', 'clean'):
                with self.subTest(stage=stage, status=status):
                    self.assertEqual(validate_payload({**valid(stage), 'status': status}, stage)['status'], 'VALID')

    def test_single_outer_json_fence_and_whitespace_are_supported(self):
        raw = json.dumps(GOOD)
        for text in (raw, ' \n' + raw + '\t ', '```json\n' + raw + '\n```', '```\n' + raw + '\n```'):
            self.assertEqual(validate_payload(decode_json(json_input(text)), 'generator'), GOOD)

    def test_arbitrary_prose_multiple_objects_and_array_wrappers_are_not_extracted(self):
        raw = json.dumps(GOOD)
        for text in ('prefix\n' + raw, raw + '\nexplanation', raw + raw,
                     '```json\n' + raw + '\n```\nextra'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                decode_json(json_input(text))
        for value in ([GOOD], [], None, False, 123, 'text'):
            with self.subTest(value=value), self.assertRaises(PayloadValidationError):
                validate_payload(decode_json(json.dumps(value)), 'generator')

    def test_duplicate_keys_at_every_depth_are_rejected(self):
        cases = ['{"status":"INVALID","status":"VALID","issues":[],"corrected_rtl":"module sample; endmodule"}',
                 '{"status":"INVALID","issues":[{"type":"SPEC_VIOLATION","type":"NON_ISSUE",'
                 '"description":"x","location":"y"}]}',
                 '{"status":"VALID","issues":[],"corrected_rtl":"one","corrected_rtl":"two"}',
                 '{"status":"VALID","issues":[],"extra":{"x":1,"x":2}}']
        for text in cases:
            with self.subTest(text=text), self.assertRaisesRegex(PayloadValidationError, 'duplicate JSON member'):
                decode_json(text)

    def test_nonstandard_json_constants_are_rejected_but_strings_are_preserved(self):
        for literal in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(literal=literal), self.assertRaises(PayloadValidationError):
                decode_json('{"status":"VALID","issues":[],"corrected_rtl":' + literal + '}')
        data = {**GOOD, 'corrected_rtl': 'module sample; // NaN Infinity\nendmodule'}
        self.assertEqual(validate_payload(decode_json(json.dumps(data)), 'generator'), data)

    def test_missing_required_fields_unknown_fields_and_status_types_fail_all_stages(self):
        for stage in STAGES:
            candidates = []
            for key in ('status', 'issues'):
                data = valid(stage); del data[key]; candidates.append(data)
            for value in (None, [], {}, 1, True, '', 'unrecognized'):
                candidates.append({**valid(stage), 'status': value})
            candidates.append({**valid(stage), 'status_typo': 'VALID'})
            for data in candidates:
                with self.subTest(stage=stage, data=data), self.assertRaises(PayloadValidationError):
                    validate_payload(data, stage)

    def test_issues_must_be_arrays_of_complete_typed_nonempty_objects(self):
        bad = [None, {}, 'SPEC_VIOLATION', [None], ['some issue'], [{}],
               [{**VIOLATION, 'type': 'TYPO'}], [{**VIOLATION, 'type': 1}],
               [{**VIOLATION, 'description': ''}], [{**VIOLATION, 'description': None}],
               [{**VIOLATION, 'location': []}], [{**VIOLATION, 'location': ' '}],
               [{**VIOLATION, 'extra': 'ignored before'}]]
        missing = copy.deepcopy(VIOLATION); del missing['location']; bad.append([missing])
        for stage in STAGES:
            for issues in bad:
                with self.subTest(stage=stage, issues=issues), self.assertRaises(PayloadValidationError):
                    validate_payload({**valid(stage), 'status': 'INVALID', 'issues': issues}, stage)

    def test_status_issue_contradictions_are_payload_errors_not_spec_ambiguities(self):
        pairs = [('VALID', [VIOLATION]), ('VALID', [AMBIGUITY]), ('INVALID', []),
                 ('INVALID', [NON_ISSUE]), ('AMBIGUOUS', []), ('AMBIGUOUS', [VIOLATION])]
        for stage in STAGES:
            for status, issues in pairs:
                with self.subTest(stage=stage, status=status), self.assertRaises(PayloadValidationError):
                    validate_payload({**valid(stage), 'status': status, 'issues': issues}, stage)
        for stage in STAGES:
            self.assertEqual(validate_payload({**valid(stage), 'status': 'INVALID', 'issues': [VIOLATION]}, stage)['status'], 'INVALID')
            self.assertEqual(validate_payload({**valid(stage), 'status': 'AMBIGUOUS', 'issues': [AMBIGUITY]}, stage)['status'], 'AMBIGUOUS')

    def test_generator_and_merger_require_nonempty_text_rtl(self):
        for stage in ('generator', 'merger'):
            for value in ('', ' \n ', '```sv\n\n```'):
                with self.subTest(stage=stage, value=value), self.assertRaises(PayloadValidationError) as caught:
                    validate_payload({**GOOD, 'corrected_rtl': value}, stage)
                self.assertEqual(caught.exception.failure_kind, 'missing_rtl')
            with self.assertRaises(PayloadValidationError) as caught:
                validate_payload({'status': 'VALID', 'issues': []}, stage)
            self.assertEqual(caught.exception.failure_kind, 'missing_rtl')
            for value in (None, False, 0, [], {}):
                with self.subTest(value=value), self.assertRaises(PayloadValidationError) as caught:
                    validate_payload({**GOOD, 'corrected_rtl': value}, stage)
                self.assertEqual(caught.exception.failure_kind, 'invalid_payload')

    def test_reviewer_rtl_is_optional_but_wrong_types_and_compact_extras_fail(self):
        for data in ({'status': 'VALID', 'issues': []}, {'status': 'VALID', 'issues': [], 'corrected_rtl': ''}):
            validate_payload(data, 'reviewer')
        for value in (None, [], 1, True):
            with self.assertRaises(PayloadValidationError):
                validate_payload({'status': 'VALID', 'issues': [], 'corrected_rtl': value}, 'reviewer')
        for extra in ({'corrected_rtl': RTL}, {'invariants': []}):
            with self.assertRaises(PayloadValidationError):
                validate_payload({'status': 'VALID', 'issues': [], **extra}, 'compact_reviewer')

    def test_legacy_invariants_are_validated_even_if_the_compiler_supplies_its_own(self):
        for stage in ('generator', 'merger', 'reviewer'):
            validate_payload({**valid(stage), 'invariants': ['existing invariant']}, stage)
            for value in (None, 'one string', [1], [' '], {}):
                with self.subTest(value=value), self.assertRaises(PayloadValidationError):
                    validate_payload({**valid(stage), 'invariants': value}, stage)

    def test_escaped_rtl_and_internal_fences_are_not_modified_or_extracted(self):
        code = RTL + '\n// ```sv\n// this is a comment, not an outer wrapper\n// ```'
        for stage in ('generator', 'merger', 'reviewer'):
            result = validate_payload(decode_json(json.dumps({**GOOD, 'corrected_rtl': code})), stage)
            self.assertEqual(result['corrected_rtl'], code)
            self.assertEqual(validate_payload({**GOOD, 'corrected_rtl': '```sv\n' + RTL + '\n```'}, stage)['corrected_rtl'], RTL)


class RtlPayloadBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stdout = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(patch.dict(os.environ, {'CAPSTONE_TPM_LIMIT': '0', 'TAMUS_AI_CHAT_API_KEY': 'test-secret'}))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')))
        self.module = SimpleNamespace(design_name='sample')
        self.clean_lint = {'ok': True, 'blocking': [], 'ignored': [], 'nonblocking': []}

    def test_all_parsers_agree_on_status_issues_and_do_not_drop_sixth_issue(self):
        data = {'status': 'INVALID', 'issues': [copy.deepcopy(NON_ISSUE) for _ in range(5)] + [VIOLATION]}
        for parser, label in ((generator._parse_llm_json_payload, 'LLM1-Generate'),
                              (generator._parse_llm_json_payload, 'LLM1-Merge'),
                              (generator._parse_review_json_payload, 'LLM2-Review')):
            value = parser(json.dumps({**data, 'corrected_rtl': RTL}), RTL, label)
            self.assertFalse(value['parse_failed'])
            self.assertEqual(value['issues'], data['issues'])
        value = generator._parse_review_json_compact(json.dumps(data), 'LLM2-Review-Compact')
        self.assertFalse(value['parse_failed'])
        self.assertEqual(value['issues'], data['issues'])
        clean = generator._parse_review_json_compact(json.dumps({'status': 'CLEAN', 'issues': [NON_ISSUE]}), 'LLM2-Review-Compact')
        self.assertEqual(clean['status'], 'VALID')
        self.assertEqual(clean['issues'][0]['type'], 'NON_ISSUE')

    def test_parsers_reject_arrays_and_contradictory_status_instead_of_extracting_a_valid_object(self):
        for raw in (json.dumps([GOOD]), json.dumps({**GOOD, 'status': 'VALID', 'issues': [VIOLATION]})):
            for parser, label in ((generator._parse_llm_json_payload, 'LLM1-Generate'),
                                  (generator._parse_llm_json_payload, 'LLM1-Merge'),
                                  (generator._parse_review_json_payload, 'LLM2-Review')):
                payload = parser(raw, RTL, label)
                self.assertTrue(payload['parse_failed'])
                self.assertEqual(payload['failure']['kind'], 'invalid_payload')

    def test_generator_schema_failure_saves_field_path_and_stops_before_lint_review_or_cache(self):
        with UsageRun(self.root / 'usage') as usage:
            with ModuleUsage('sample', self.root):
                with patch.object(generator, '_design_context_bundle', return_value={'prompt_text': 'fixture', 'invariants': []}), \
                     patch('requests.post', return_value=response(json.dumps({**GOOD, 'issues': 'not an array'}))) as post, \
                     patch.object(generator, 'lint_candidate') as lint, patch.object(generator, '_llm2_review') as review, \
                     patch.object(generator, '_write_reviewed_cache') as cache:
                    with self.assertRaises(LlmFailure) as caught:
                        generator.run_dual_llm_rtlgen(self.module, '')
                self.assertEqual(caught.exception.kind, 'invalid_payload')
                self.assertEqual(caught.exception.failure['schema_path'], '/issues')
                post.assert_called_once()
                lint.assert_not_called(); review.assert_not_called(); cache.assert_not_called()
        artifact = json.loads(next(usage.directory.rglob('attempt_*.json')).read_text())
        self.assertEqual(artifact['failure']['schema_path'], '/issues')
        self.assertEqual(artifact['parser']['error_type'], 'PayloadValidationError')
        self.assertIsNone(artifact['parser']['location'])
        self.assertEqual(artifact['response']['content'], json.dumps({**GOOD, 'issues': 'not an array'}))

    def test_missing_rtl_cannot_reuse_fallback_as_new_generator_output(self):
        payload = generator._parse_llm_json_payload('{"status":"VALID","issues":[]}', RTL, 'LLM1-Generate')
        self.assertEqual(payload['corrected_rtl'], RTL)  # Available as diagnostic context only.
        with patch.object(generator, '_llm1_generate', return_value=payload), \
             patch.object(generator, 'lint_candidate') as lint, patch.object(generator, '_llm2_review') as review:
            with self.assertRaises(LlmFailure) as caught:
                generator.run_dual_llm_rtlgen(self.module, '')
        self.assertEqual(caught.exception.kind, 'missing_rtl')
        lint.assert_not_called(); review.assert_not_called()

    def test_invalid_lint_repair_payload_stops_before_relinter_or_reviewer(self):
        payload = generator._parse_llm_json_payload(json.dumps({**GOOD, 'corrected_rtl': None}), RTL, 'LLM1-Generate')
        with patch.object(generator, '_llm1_generate', side_effect=[GOOD, payload]) as generate, \
             patch.object(generator, 'lint_candidate', return_value={**self.clean_lint, 'ok': False, 'blocking': ['syntax']}) as lint, \
             patch.object(generator, '_llm2_review') as review, patch.object(generator, '_write_reviewed_cache') as cache:
            with self.assertRaises(LlmFailure) as caught:
                generator.run_dual_llm_rtlgen(self.module, '')
        self.assertEqual(caught.exception.kind, 'invalid_payload')
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(lint.call_count, 1)
        review.assert_not_called(); cache.assert_not_called()

    def test_invalid_merge_payload_stops_before_reusing_previous_rtl(self):
        rejected = generator._parse_llm_json_payload('{"status":"VALID","issues":[]}', RTL, 'LLM1-Merge')
        review_payload = {**GOOD, 'status': 'INVALID', 'issues': [VIOLATION]}
        with patch.object(generator, '_llm1_generate', return_value=GOOD), \
             patch.object(generator, 'lint_candidate', return_value=self.clean_lint), \
             patch.object(generator, '_llm2_review', return_value=review_payload), \
             patch.object(generator, '_llm1_merge', return_value=rejected), \
             patch.object(generator, 'lint_rtl_single_file') as lint, patch.object(generator, '_write_reviewed_cache') as cache:
            with self.assertRaises(LlmFailure) as caught:
                generator.run_dual_llm_rtlgen(self.module, '')
        self.assertEqual(caught.exception.kind, 'missing_rtl')
        lint.assert_not_called(); cache.assert_not_called()

    def test_reviewer_invalid_payload_still_uses_existing_compact_retry(self):
        bad = generator._parse_review_json_payload('{"issues":[]}', RTL, 'LLM2-Review')
        compact = generator._parse_review_json_compact('{"status":"CLEAN","issues":[]}', 'LLM2-Review-Compact')
        with patch.object(generator, '_llm1_generate', return_value=GOOD), \
             patch.object(generator, 'lint_candidate', return_value=self.clean_lint), \
             patch.object(generator, '_llm2_review', return_value=bad), \
             patch.object(generator, '_llm2_review_compact', return_value=compact) as retry, \
             patch.object(generator, 'lint_rtl_single_file', return_value=(True, [])), \
             patch.object(generator, '_write_reviewed_cache', return_value=RTL):
            self.assertEqual(generator.run_dual_llm_rtlgen(self.module, ''), RTL)
        retry.assert_called_once()

    def test_caller_invariants_and_rtl_internal_text_remain_authoritative(self):
        code = RTL + '\n// ```sv\n// retained comment\n// ```'
        data = {**GOOD, 'invariants': ['model echo'], 'corrected_rtl': code}
        for parser, label in ((generator._parse_llm_json_payload, 'LLM1-Generate'),
                              (generator._parse_review_json_payload, 'LLM2-Review')):
            result = parser(json.dumps(data), RTL, label, ['compiler invariant'])
            self.assertEqual(result['invariants'], ['compiler invariant'])
            self.assertEqual(result['corrected_rtl'], code)


if __name__ == '__main__':
    unittest.main()
