"""Complete response evidence and classification; all requests are mocked."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

import dual_llm_rtlGen as generator
from llm_diagnostics import FailureKind, LlmFailure
from token_usage import ModuleUsage, UsageRun, summarize, _read


def response(content='{}', *, reason='stop', status=200, data=None, body=None):
    result = requests.Response()
    result.status_code = status
    result.encoding = 'utf-8'
    if body is None:
        data = data if data is not None else {'choices': [{'finish_reason': reason, 'message': {'content': content}}],
                                              'usage': {'prompt_tokens': 17, 'completion_tokens': 9, 'total_tokens': 26}}
        body = json.dumps(data, ensure_ascii=False)
    result._content = body.encode('utf-8')
    return result


def malformed_rtl():
    code = 'module sample;\n' + '// independent retained RTL text\n' * 220 + 'initial $display("hello");\nendmodule\nTAIL_SENTINEL'
    return json.dumps({'status': 'VALID', 'issues': [], 'corrected_rtl': code}, indent=2).replace(
        '\\"hello\\"', '"hello"')


class LlmDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {'TAMUS_AI_CHAT_API_KEY': 'test-secret',
            'CAPSTONE_TPM_LIMIT': '0', 'DUAL_LLM_MAX_TOKENS': '8192', 'DUAL_LLM_MAX_RETRY_TOKENS': '16384',
            'DUAL_LLM_TIMEOUT_SECONDS': '180'}))
        self.stdout = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.stdout))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')))
        self.usage = self.stack.enter_context(UsageRun(self.root / 'usage'))
        self.module = self.stack.enter_context(ModuleUsage('sample', self.root))
        self.module_dir = self.usage.directory / 'modules' / self.module.scope
        self.model = 'protected.gpt-5.6-terra'

    def call(self, label='LLM1-Generate'):
        return generator._call_llm(self.model, 'system instructions', 'user request', label)

    def artifacts(self):
        return sorted(self.module_dir.glob('llm_calls/*/attempt_*.json'))

    def records(self):
        return [json.loads(p.read_text()) for p in self.artifacts()]

    def test_full_response_saved_before_parser_and_valid_string_api_preserved(self):
        raw = ' \n' + json.dumps({'status': 'VALID', 'issues': [], 'corrected_rtl': 'module sample; endmodule'}) + '\n '
        with patch('requests.post', return_value=response(raw)) as post:
            text = self.call()
        self.assertIsInstance(text, str)
        self.assertEqual(text, raw.strip())
        record = self.records()[0]
        self.assertEqual(record['response']['content'], raw)
        self.assertEqual(json.loads(record['response']['body'])['choices'][0]['message']['content'], raw)
        self.assertIsNone(record['parser'])
        self.assertEqual(record['request']['model'], self.model)
        self.assertEqual(record['request']['stage'], 'LLM1-Generate')
        self.assertEqual(record['request']['max_tokens'], 8192)
        self.assertEqual(record['request']['timeout_seconds'], 180)
        self.assertEqual(len(record['request']['messages_sha256']), 64)
        self.assertNotIn('headers', record['request'])
        self.assertEqual(self.artifacts()[0].stat().st_mode & 0o777, 0o600)
        payload = generator._parse_llm_json_payload(text, '', 'LLM1-Generate')
        self.assertFalse(payload['parse_failed'])
        self.assertEqual(self.records()[0]['parser']['status'], 'parsed')
        post.assert_called_once()

    def test_long_malformed_json_preserves_exact_failure_beyond_console_preview(self):
        bad = malformed_rtl()
        raw = '\n```json\n' + bad + '\n```\n'
        with patch('requests.post', return_value=response(raw)) as post:
            text = self.call()
        payload = generator._parse_llm_json_payload(text, '', 'LLM1-Generate')
        record = self.records()[0]
        self.assertEqual(record['response']['content'], raw)
        self.assertEqual(record['parser']['input'], bad)
        self.assertEqual(record['failure']['kind'], 'malformed_json')
        self.assertEqual(payload['failure']['diagnostic'], str(self.artifacts()[0]))
        self.assertEqual(record['parser']['coordinate_space'], 'parser.input')
        with self.assertRaises(json.JSONDecodeError) as caught:
            json.loads(record['parser']['input'])
        error = caught.exception
        self.assertEqual(record['parser']['location'], {'line': error.lineno, 'column': error.colno, 'position': error.pos})
        self.assertGreater(error.pos, generator.RAW_RESPONSE_PREVIEW_CHARS)
        self.assertIn('TAIL_SENTINEL', self.artifacts()[0].read_text())
        self.assertNotIn('TAIL_SENTINEL', self.stdout.getvalue())
        self.assertTrue(payload['parse_failed'])
        post.assert_called_once()

    def test_parser_evidence_covers_generator_merger_reviewer_and_compact_review(self):
        for label, parser in [('LLM1-Generate', generator._parse_llm_json_payload),
                              ('LLM1-Merge', generator._parse_llm_json_payload),
                              ('LLM2-Review', generator._parse_review_json_payload)]:
            with self.subTest(label=label), patch('requests.post', return_value=response('{bad')):
                payload = parser(self.call(label), 'module previous; endmodule', label)
            self.assertEqual(payload['failure']['kind'], 'malformed_json')
            self.assertEqual(payload['corrected_rtl'], 'module previous; endmodule')
        with patch('requests.post', return_value=response('{bad')):
            payload = generator._parse_review_json_compact(self.call('LLM2-Review-Compact'), 'LLM2-Review-Compact')
        self.assertEqual(payload['failure']['kind'], 'malformed_json')
        self.assertEqual(len(self.artifacts()), 4)
        self.assertTrue(all(r['parser']['status'] == 'failed' for r in self.records()))

    def test_missing_rtl_invalid_payload_and_syntax_are_distinct(self):
        for raw, expected in [('{"status":"VALID","issues":[]}', 'missing_rtl'),
                              ('{}', 'invalid_payload'), ('[]', 'invalid_payload'), ('{broken', 'malformed_json')]:
            with self.subTest(raw=raw), patch('requests.post', return_value=response(raw)):
                parsed = generator._parse_llm_json_payload(self.call(), '', 'LLM1-Generate')
            self.assertEqual(parsed['failure']['kind'], expected)
        # Reviewer corrected_rtl remains optional; checkpoint 1 does not impose a new schema.
        with patch('requests.post', return_value=response('{"status":"VALID","issues":[]}')):
            parsed = generator._parse_review_json_payload(self.call('LLM2-Review'), 'module sample; endmodule', 'LLM2-Review')
        self.assertFalse(parsed['parse_failed'])
        self.assertNotIn('failure', parsed)

    def test_network_categories_record_request_without_inventing_usage_or_retrying(self):
        for error, kind in [(requests.Timeout('timeout test-secret'), 'network_timeout'),
                            (requests.ConnectionError('connection test-secret'), 'network_connection'),
                            (requests.RequestException('request test-secret'), 'network_error')]:
            with self.subTest(kind=kind), patch('requests.post', side_effect=error) as post:
                with self.assertRaises(LlmFailure) as caught:
                    self.call()
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn('test-secret', ''.join(traceback.format_exception(caught.exception)))
                post.assert_called_once()
        records = self.records()
        self.assertTrue(all(r['response'] is None for r in records))
        totals = summarize(_read(self.usage.directory / 'calls.jsonl'))
        self.assertEqual(totals['calls'], 3)
        self.assertEqual(totals['calls_missing_input_tokens'], 3)
        self.assertIsNone(totals['cost']['estimated_total_usd'])

    def test_http_errors_keep_complete_redacted_body_and_exact_status_without_retry(self):
        for status in (400, 401, 403, 429, 500, 524):
            body = 'test-secret provider error ' + 'long message ' * 250 + 'HTTP_BODY_TAIL'
            with self.subTest(status=status), patch('requests.post', return_value=response(status=status, body=body)) as post:
                with self.assertRaises(LlmFailure) as caught:
                    self.call()
                self.assertEqual(caught.exception.kind, 'http_error')
                self.assertEqual(caught.exception.failure['http_status'], status)
                self.assertNotIn('test-secret', str(caught.exception))
                post.assert_called_once()
        self.assertEqual({r['response']['http_status'] for r in self.records()}, {400, 401, 403, 429, 500, 524})
        for record in self.records():
            self.assertIn('HTTP_BODY_TAIL', record['response']['body'])
            self.assertNotIn('test-secret', record['response']['body'])

    def test_provider_envelope_errors_are_distinct_from_inner_json_errors(self):
        for data in ({'choices': []}, {'choices': None}, {}, {'choices': [None]}, {'choices': [{'message': []}]}):
            with self.subTest(data=data), patch('requests.post', return_value=response(data=data)) as post:
                with self.assertRaises(LlmFailure) as caught:
                    self.call()
                self.assertEqual(caught.exception.kind, 'provider_response')
                post.assert_called_once()
        with patch('requests.post', return_value=response(body='invalid HTTP envelope')):
            with self.assertRaises(LlmFailure) as caught:
                self.call()
        self.assertEqual(caught.exception.kind, 'provider_response')
        self.assertTrue(all(r['parser'] is None for r in self.records()))

    def test_empty_nontext_filtered_and_unfinished_content_have_separate_categories(self):
        cases = [(None, 'stop', 'empty_response'), ('  ', 'stop', 'empty_response'),
                 ([], 'stop', 'non_text_response'), ({}, 'stop', 'non_text_response'),
                 ('partial', 'content_filter', 'filtered_response'), ('partial', 'tool_calls', 'unfinished_response')]
        for content, reason, expected in cases:
            with self.subTest(expected=expected), patch('requests.post', return_value=response(content, reason=reason)) as post:
                with self.assertRaises(LlmFailure) as caught:
                    self.call()
                self.assertEqual(caught.exception.kind, expected)
                post.assert_called_once()

    def test_existing_truncation_retry_saves_separate_attempts_and_counts_usage_once(self):
        first, second = '{partial response one', '{"status":"VALID","issues":[],"corrected_rtl":"module sample; endmodule"}'
        with patch('requests.post', side_effect=[response(first, reason='length'), response(second)]) as post:
            text = self.call()
        generator._parse_llm_json_payload(text, '', 'LLM1-Generate')
        paths = self.artifacts()
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0].parent, paths[1].parent)
        records = self.records()
        self.assertEqual([r['request']['max_tokens'] for r in records], [8192, 16384])
        self.assertEqual([r['response']['content'] for r in records], [first, second])
        self.assertEqual(records[0]['failure']['kind'], 'truncated_response')
        self.assertIsNone(records[1]['failure'])
        totals = summarize(_read(self.usage.directory / 'calls.jsonl'))
        self.assertEqual((totals['calls'], totals['input_tokens'], totals['output_tokens']), (2, 34, 18))
        self.assertEqual(post.call_count, 2)

    def test_truncation_at_ceiling_keeps_full_evidence_and_legacy_partial_artifact(self):
        content = 'partial ' * 1000
        with patch.dict(os.environ, {'DUAL_LLM_MAX_RETRY_TOKENS': '8192'}), patch('requests.post', return_value=response(content, reason='length')) as post:
            with self.assertRaises(LlmFailure) as caught:
                self.call()
        self.assertEqual(caught.exception.kind, 'truncated_response')
        self.assertEqual(self.records()[0]['response']['content'], content)
        self.assertEqual(len(list(self.module_dir.glob('truncated_*.json'))), 1)
        post.assert_called_once()

    def test_missing_usage_stays_unknown_while_content_and_parser_evidence_survive(self):
        data = {'choices': [{'finish_reason': 'stop', 'message': {'content': malformed_rtl()}}]}
        with patch('requests.post', return_value=response(data=data)):
            generator._parse_llm_json_payload(self.call(), '', 'LLM1-Generate')
        record = self.records()[0]
        self.assertIsNone(record['response']['usage'])
        self.assertEqual(record['failure']['kind'], 'malformed_json')
        totals = summarize(_read(self.usage.directory / 'calls.jsonl'))
        self.assertEqual(totals['calls_missing_output_tokens'], 1)
        self.assertIsNone(totals['total_tokens'])

    def test_credentials_redacted_in_encoded_body_content_parser_and_logs(self):
        secret = 'token"slash\\\nλ'
        with patch.dict(os.environ, {'TAMUS_AI_CHAT_API_KEY': secret}):
            raw = json.dumps({'note': secret, 'corrected_rtl': 'module sample; endmodule'}) + ', BAD'
            # Make the error occur inside the extracted JSON, after the secret.
            raw = raw[:-5] + '"unterminated}'
            with patch('requests.post', return_value=response(raw)):
                parsed = generator._parse_llm_json_payload(self.call(), '', 'LLM1-Generate')
            record = self.records()[0]
            variants = (secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1])
            def strings(value):
                if isinstance(value, str):
                    yield value
                elif isinstance(value, dict):
                    for k, v in value.items():
                        yield k
                        yield from strings(v)
                elif isinstance(value, list):
                    for v in value:
                        yield from strings(v)
            for text in [*strings(record), self.stdout.getvalue(), str(parsed['failure'])]:
                for variant in variants:
                    self.assertNotIn(variant, text)
            self.assertTrue(record['credentials_redacted'])
            self.assertEqual(record['parser']['coordinate_space'], 'parser.input_before_credential_redaction')

    def test_atomic_write_failure_does_not_mask_original_parse_failure(self):
        with patch('requests.post', return_value=response(malformed_rtl())):
            text = self.call()
        before = self.artifacts()[0].read_bytes()
        with patch('llm_diagnostics.os.replace', side_effect=PermissionError('test-secret permission denied')):
            parsed = generator._parse_llm_json_payload(text, '', 'LLM1-Generate')
        self.assertEqual(parsed['failure']['kind'], 'malformed_json')
        self.assertEqual(self.artifacts()[0].read_bytes(), before)
        self.assertEqual(list(self.artifacts()[0].parent.glob('.*.json.*')), [])
        self.assertIn('Diagnostic write failed', self.stdout.getvalue())
        self.assertNotIn('test-secret', self.stdout.getvalue())

    def test_configuration_fails_before_http_and_does_not_create_attempt_artifacts(self):
        for setting in ({'TAMUS_AI_CHAT_API_KEY': ''}, {'DUAL_LLM_MAX_TOKENS': 'bad'}, {'DUAL_LLM_TIMEOUT_SECONDS': 'nan'}):
            with self.subTest(setting=setting), patch.dict(os.environ, setting), patch('requests.post') as post:
                with self.assertRaises(LlmFailure) as caught:
                    self.call()
                self.assertEqual(caught.exception.kind, 'configuration')
                post.assert_not_called()
        self.assertEqual(self.artifacts(), [])

    def test_module_failure_links_original_parser_error_without_new_retry_lint_or_review(self):
        with patch.object(generator, '_design_context_bundle', return_value={'prompt_text': 'context', 'invariants': []}), \
             patch('requests.post', return_value=response(malformed_rtl())) as post, \
             patch.object(generator, 'lint_candidate') as lint, patch.object(generator, '_llm2_review') as review, \
             patch.object(generator, '_write_reviewed_cache') as cache:
            with self.assertRaises(LlmFailure) as caught:
                generator.run_dual_llm_rtlgen(SimpleNamespace(design_name='sample'), '')
        self.assertEqual(caught.exception.kind, 'malformed_json')
        post.assert_called_once()
        lint.assert_not_called()
        review.assert_not_called()
        cache.assert_not_called()
        report = json.loads((self.module_dir / 'rtl_review.json').read_text())
        self.assertEqual(report['failure']['kind'], 'malformed_json')
        self.assertEqual([e['kind'] for e in report['failure_events']], ['malformed_json'])
        self.assertTrue(Path(report['failure']['diagnostic']).exists())
        self.assertEqual(len(report['rounds']), 1)
        self.assertEqual(report['outcome'], 'failed')

    def test_real_spec_ambiguity_is_classified_separately(self):
        code = 'module sample; endmodule'
        good = {'status': 'VALID', 'issues': [], 'corrected_rtl': code}
        ambiguity = {'status': 'AMBIGUOUS', 'issues': [
            {'type': 'AMBIGUITY', 'description': 'The specification leaves the response edge undefined.', 'location': 'response'}],
            'corrected_rtl': code}
        with patch.object(generator, '_llm1_generate', return_value=good), \
             patch.object(generator, 'lint_candidate', return_value={'ok': True, 'blocking': [], 'ignored': [], 'nonblocking': []}), \
             patch.object(generator, '_llm2_review', return_value=ambiguity):
            with self.assertRaises(LlmFailure) as caught:
                generator.run_dual_llm_rtlgen(SimpleNamespace(design_name='sample'), '')
        self.assertEqual(caught.exception.kind, 'spec_clarification')
        report = json.loads((self.module_dir / 'rtl_review.json').read_text())
        self.assertEqual(report['outcome'], 'needs_spec_clarification')
        self.assertEqual(report['failure']['kind'], 'spec_clarification')

    def test_private_unscoped_calls_remain_in_memory_without_global_artifacts(self):
        with patch('token_usage.current_module_directory', return_value=None), patch('requests.post', return_value=response('{bad')):
            text = self.call()
            parsed = generator._parse_llm_json_payload(text, '', 'LLM1-Generate')
        self.assertEqual(self.artifacts(), [])
        self.assertIsNone(parsed['failure']['diagnostic'])
        self.assertEqual(parsed['failure']['kind'], 'malformed_json')

    def test_evidence_survives_module_and_usage_failure_cleanup(self):
        with UsageRun(self.root / 'failed-entry-point') as usage:
            with self.assertRaises(LlmFailure):
                with ModuleUsage('sample', self.root):
                    with patch.object(generator, '_design_context_bundle', return_value={'prompt_text': 'context', 'invariants': []}), \
                         patch('requests.post', return_value=response(malformed_rtl())):
                        generator.run_dual_llm_rtlgen(SimpleNamespace(design_name='sample'), '')
        report = json.loads((usage.directory / 'usage.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['totals']['calls'], 1)
        self.assertEqual(report['modules'][0]['status'], 'failed')
        directory = usage.directory / 'modules' / report['modules'][0]['scope']
        trace = json.loads((directory / 'rtl_review.json').read_text())
        artifact = json.loads(Path(trace['failure']['diagnostic']).read_text())
        self.assertEqual(artifact['response']['content'], malformed_rtl())
        self.assertEqual(artifact['failure']['kind'], 'malformed_json')


if __name__ == '__main__':
    unittest.main()
