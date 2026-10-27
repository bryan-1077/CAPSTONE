"""Offline tests for a bounded, non-emitting live generation diagnostic."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

import probe_generation as probe
from token_usage import UsageRun


class GenerationProbeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.spec = self.root / 'module.yaml'
        self.spec.write_text('A fixture for prompt capture')
        self.output = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        env = patch.dict(os.environ, {'TAMUS_AI_CHAT_API_KEY': 'mock-secret', 'CAPSTONE_TPM_LIMIT': '0'})
        env.start()
        self.addCleanup(env.stop)
        usage = patch.object(probe, 'UsageRun', side_effect=lambda *a, **k: UsageRun(self.root, **k))
        usage.start()
        self.addCleanup(usage.stop)
        self.request = {'model': probe.generator.LLM1_MODEL,
                        'messages': [{'role': 'system', 'content': 'Original system instructions'},
                                     {'role': 'user', 'content': 'Original complete module request'}]}

    def response(self, status=200, reason='length'):
        result = requests.Response()
        result.status_code = status
        result._content = json.dumps({'choices': [{'finish_reason': reason, 'message': {'content': 'partial'}}],
                                     'usage': {'prompt_tokens': 100, 'completion_tokens': 512}}).encode()
        result._content_consumed = True
        return result

    def test_capture_uses_production_prompt_without_calling_provider_and_restores_client(self):
        original = probe.generator._call_llm
        with patch.object(probe, 'validate_spec', return_value=SimpleNamespace(design_name='fixture')), \
             patch.object(probe.generator, '_design_context_bundle', return_value={'prompt_text': 'EXACT CONTEXT', 'invariants': []}), \
             patch.object(probe.generator.requests, 'post') as post:
            request = probe.capture_request(self.spec)
        self.assertIs(probe.generator._call_llm, original)
        self.assertEqual(request['model'], probe.generator.LLM1_MODEL)
        self.assertEqual(request['messages'][0]['content'], probe.generator._SYS_GENERATOR)
        self.assertIn('EXACT CONTEXT', request['messages'][1]['content'])
        self.assertIn('PREVIOUS ATTEMPTS', request['messages'][1]['content'])
        post.assert_not_called()

    def test_dry_run_has_no_api_call_or_usage_artifacts(self):
        with patch.object(probe, 'capture_request', return_value=self.request), patch.object(probe, 'paced_post') as post:
            probe.run_probe(self.spec, dry_run=True)
        post.assert_not_called()
        self.assertFalse(list(self.root.rglob('usage.json')))

    def test_one_bounded_call_preserves_messages_and_counts_truncation_as_transport_success(self):
        for reason in ('length', 'stop'):
            with self.subTest(reason=reason), patch.object(probe, 'capture_request', return_value=self.request), \
                 patch.object(probe, 'paced_post', return_value=self.response(reason=reason)) as post:
                probe.run_probe(self.spec)
                post.assert_called_once()
                payload = post.call_args.kwargs['json']
                self.assertEqual(payload['messages'], self.request['messages'])
                self.assertEqual(payload['max_tokens'], 512)
                self.assertFalse(payload['stream'])
        reports = [json.loads(p.read_text()) for p in self.root.rglob('usage.json')]
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(r['status'] == 'completed' and r['totals']['calls'] == 1 for r in reports))
        self.assertFalse(list(self.root.rglob('*.sv')))
        self.assertNotIn('mock-secret', self.output.getvalue())

    def test_http_timeout_is_reported_without_retry_and_preserves_headers(self):
        response = self.response(status=524)
        response.headers['Retry-After'] = '120'
        with patch.object(probe, 'capture_request', return_value=self.request), \
             patch.object(probe, 'paced_post', return_value=response) as post:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 524'):
                probe.run_probe(self.spec)
        post.assert_called_once()
        report = json.loads(next(self.root.rglob('usage.json')).read_text())
        self.assertEqual(report['status'], 'failed')
        ledger = json.loads(next(self.root.rglob('calls.jsonl')).read_text())
        self.assertEqual(ledger['rate_limit_headers'], {'Retry-After': '120'})

    def test_network_failure_is_counted_once_without_exposing_exception_details(self):
        with patch.object(probe, 'capture_request', return_value=self.request), \
             patch.object(probe, 'paced_post', side_effect=requests.ConnectionError('mock-secret')) as post:
            with self.assertRaisesRegex(RuntimeError, 'network request failed'):
                probe.run_probe(self.spec)
        post.assert_called_once()
        report = json.loads(next(self.root.rglob('usage.json')).read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['totals']['calls'], 1)
        self.assertNotIn('mock-secret', self.output.getvalue())

    def test_invalid_caps_fail_before_prompt_capture_or_network(self):
        with patch.object(probe, 'capture_request') as capture, patch.object(probe, 'paced_post') as post:
            for cap in (0, -1, 2049):
                with self.assertRaises(ValueError):
                    probe.run_probe(self.spec, cap)
        capture.assert_not_called()
        post.assert_not_called()

    def streaming_response(self, events):
        response = self.response()
        response.headers['Content-Type'] = 'text/event-stream; charset=utf-8'
        lines = []
        for event in events:
            if event == 'heartbeat':
                lines.extend([': keepalive', ''])
            else:
                data = event if isinstance(event, str) else json.dumps(event)
                lines.extend(['data: ' + data, ''])
        response.iter_lines = lambda **kwargs: iter(lines)
        return response

    def test_buffered_reasoning_fields_and_detailed_usage_are_summarized_without_text(self):
        response = self.response()
        data = response.json()
        data['choices'][0]['message'] = {'content': '', 'reasoning_content': 'PRIVATE THINKING'}
        data['usage']['completion_tokens_details'] = {'reasoning_tokens': 512}
        response._content = json.dumps(data).encode()
        with patch.object(probe, 'capture_request', return_value=self.request), \
             patch.object(probe, 'paced_post', return_value=response):
            probe.run_probe(self.spec, 2048)
        details = json.loads(next(self.root.rglob('probe.json')).read_text())
        self.assertEqual(details['message_fields']['reasoning_content']['characters'], 16)
        self.assertEqual(details['provider_usage']['completion_tokens_details']['reasoning_tokens'], 512)
        self.assertNotIn('PRIVATE THINKING', self.output.getvalue())
        self.assertNotIn('PRIVATE THINKING', next(self.root.rglob('probe.json')).read_text())

    def test_streaming_records_heartbeat_reasoning_content_and_final_usage_once(self):
        response = self.streaming_response([
            'heartbeat', {'choices': [{'delta': {'reasoning_content': 'PRIVATE'}}]},
            {'choices': [{'delta': {'content': 'module '}}]},
            {'choices': [{'delta': {'content': 'test'}, 'finish_reason': 'length'}]},
            {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 2048,
                                     'completion_tokens_details': {'reasoning_tokens': 1800}}}, '[DONE]'])
        with patch.object(probe, 'capture_request', return_value=self.request), \
             patch.object(probe, 'paced_post', return_value=response) as post:
            probe.run_probe(self.spec, 2048, stream=True)
        post.assert_called_once()
        self.assertTrue(post.call_args.kwargs['stream'])
        self.assertTrue(post.call_args.kwargs['json']['stream'])
        self.assertEqual(post.call_args.kwargs['json']['stream_options'], {'include_usage': True})
        self.assertEqual(post.call_args.kwargs['json']['messages'], self.request['messages'])
        details = json.loads(next(self.root.rglob('probe.json')).read_text())
        self.assertEqual(details['message_fields']['content']['characters'], 11)
        self.assertIsNotNone(details['first_body_seconds'])
        self.assertIsNotNone(details['first_reasoning_seconds'])
        self.assertIsNotNone(details['first_content_seconds'])
        self.assertEqual(details['status'], 'completed')
        usage = json.loads(next(self.root.rglob('usage.json')).read_text())
        self.assertEqual(usage['totals']['calls'], 1)
        self.assertEqual(usage['totals']['output_tokens'], 2048)
        self.assertNotIn('PRIVATE', next(self.root.rglob('probe.json')).read_text())

    def test_stream_without_usage_is_unknown_and_premature_end_fails(self):
        for done in (False, True):
            events = [{'choices': [{'delta': {'content': 'partial'}, 'finish_reason': 'length'}]}]
            if done:
                events.append('[DONE]')
            response = self.streaming_response(events)
            with self.subTest(done=done), patch.object(probe, 'capture_request', return_value=self.request), \
                 patch.object(probe, 'paced_post', return_value=response):
                if done:
                    probe.run_probe(self.spec, stream=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Incomplete SSE stream'):
                        probe.run_probe(self.spec, stream=True)
        reports = [json.loads(p.read_text()) for p in self.root.rglob('usage.json')]
        self.assertEqual(sorted(r['status'] for r in reports), ['completed', 'failed'])
        self.assertTrue(all(r['totals']['output_tokens'] is None and r['totals']['calls'] == 1 for r in reports))

    def test_gateway_ignoring_stream_or_returning_malformed_events_fails_without_retry(self):
        for response in (self.response(), self.streaming_response(['{bad', '[DONE]']),
                         self.streaming_response(['null', '[DONE]']),
                         self.streaming_response([{'error': {'message': 'PRIVATE ERROR'}}, '[DONE]'])):
            with patch.object(probe, 'capture_request', return_value=self.request), \
                 patch.object(probe, 'paced_post', return_value=response) as post:
                with self.assertRaises((RuntimeError, ValueError)):
                    probe.run_probe(self.spec, stream=True)
                post.assert_called_once()
        self.assertNotIn('PRIVATE ERROR', self.output.getvalue())

    def test_timeout_uses_environment_and_explicit_override(self):
        for explicit, expected in ((None, 600), (300, 300)):
            with self.subTest(explicit=explicit), patch.dict(os.environ, {'DUAL_LLM_TIMEOUT_SECONDS': '600'}), \
                 patch.object(probe, 'capture_request', return_value=self.request), \
                 patch.object(probe, 'paced_post', return_value=self.response()) as post:
                probe.run_probe(self.spec, timeout=explicit)
                self.assertEqual(post.call_args.kwargs['timeout'], expected)
        reports = [json.loads(p.read_text()) for p in self.root.rglob('probe.json')]
        self.assertEqual(sorted(r['client_timeout_seconds'] for r in reports), [300, 600])

    def test_invalid_timeout_fails_before_capture_or_request(self):
        with patch.object(probe, 'capture_request') as capture, patch.object(probe, 'paced_post') as post:
            for value in (0, -1, float('inf'), float('nan')):
                with self.assertRaisesRegex(RuntimeError, 'positive finite'):
                    probe.run_probe(self.spec, timeout=value)
        capture.assert_not_called()
        post.assert_not_called()

    def test_streaming_diagnostic_window_uses_the_configured_timeout(self):
        response = self.streaming_response([
            {'choices': [{'delta': {'content': 'text'}, 'finish_reason': 'stop'}]}, '[DONE]'])
        with patch.object(probe, 'capture_request', return_value=self.request), \
             patch.object(probe, 'paced_post', return_value=response) as post, \
             patch.object(probe.time, 'perf_counter', side_effect=[0] + [200] * 30):
            probe.run_probe(self.spec, stream=True, timeout=600)
        self.assertEqual(post.call_args.kwargs['timeout'], 600)
        details = json.loads(next(self.root.rglob('probe.json')).read_text())
        self.assertEqual(details['status'], 'completed')
        self.assertEqual(details['elapsed_seconds'], 200)


if __name__ == '__main__':
    unittest.main()
