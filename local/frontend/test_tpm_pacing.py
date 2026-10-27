"""Offline pacing tests: fake clocks, real file locking, mocked HTTP calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests
import dual_llm_rtlGen as generator
import generate_microarch
import generate_specification
import rtlGen
import configure_from_text
import debug_agent
from token_usage import UsageRun, record_response
import tpm_pacing as pacing

ROOT = Path(__file__).resolve().parent
MESSAGES = [{'role': 'user', 'content': 'a hardware request'}]


class Clock:
    def __init__(self):
        self.value = 1000.0
        self.waits = []

    def now(self):
        return self.value

    def sleep(self, delay):
        self.waits.append(delay)
        self.value += delay


def response(reason='stop', status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({'choices': [{'finish_reason': reason, 'message': {'content': '{}'}}],
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}).encode()
    return result


class PacingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / 'reservations.json'
        env = patch.dict(os.environ, {'CAPSTONE_TPM_LIMIT': '80000', 'CAPSTONE_TPM_STATE_FILE': str(self.state),
                                     'TAMUS_AI_CHAT_API_KEY': 'mock-secret', 'DUAL_LLM_MAX_TOKENS': '8192',
                                     'DUAL_LLM_MAX_RETRY_TOKENS': '16384'})
        env.start()
        self.addCleanup(env.stop)
        self.output = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.clock = Clock()

    def reserve(self, max_tokens=1000):
        pacing.reserve_request(MESSAGES, max_tokens, now=self.clock.now, sleep=self.clock.sleep)

    def entries(self):
        return json.loads(self.state.read_text())

    def test_shared_budget_waits_only_until_capacity_expires(self):
        needed = pacing.estimate_input(MESSAGES) + 1000
        os.environ['CAPSTONE_TPM_LIMIT'] = str(2 * needed - 1)
        self.reserve()
        self.clock.value += 10
        self.reserve()
        self.assertEqual(self.clock.waits, [30, 20])
        self.assertEqual(self.entries(), [{'at': 1060.0, 'tokens': needed}])
        self.assertIn('next request needs', self.output.getvalue())

    def test_exact_boundary_admits_two_requests_without_waiting(self):
        needed = pacing.estimate_input(MESSAGES) + 1000
        os.environ['CAPSTONE_TPM_LIMIT'] = str(2 * needed)
        self.reserve()
        self.reserve()
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(sum(e['tokens'] for e in self.entries()), needed * 2)

    def test_request_larger_than_budget_fails_before_wait_or_state_write(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '100'
        with self.assertRaisesRegex(pacing.PacingError, 'exceeding.*Reduce context'):
            self.reserve()
        self.assertEqual(self.clock.waits, [])
        self.assertFalse(self.state.exists())

    def test_million_token_override_admits_large_requests_and_still_paces(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '1000000'
        for _ in range(9):
            self.reserve(max_tokens=100000)
        self.assertEqual(self.clock.waits, [])
        self.assertLessEqual(sum(e['tokens'] for e in self.entries()), 1000000)
        self.reserve(max_tokens=100000)
        self.assertEqual(self.clock.waits, [30, 30])
        self.assertEqual(len(self.entries()), 1)
        self.assertIn('/1,000,000 tokens', self.output.getvalue())

    def test_disabled_and_invalid_configuration(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '0'
        self.reserve()
        self.assertFalse(self.state.exists())
        for setting in ('-1', 'bad'):
            os.environ['CAPSTONE_TPM_LIMIT'] = setting
            with self.assertRaises(pacing.PacingError):
                self.reserve()

    def test_corrupt_state_is_not_silently_reset(self):
        for text in ('', '{broken', '{}', '[{"at": 0, "tokens": -1}]'):
            self.state.write_text(text)
            with self.assertRaisesRegex(pacing.PacingError, 'refusing to discard'):
                self.reserve()
            self.assertEqual(self.state.read_text(), text)

    def test_unicode_estimate_counts_utf8_bytes(self):
        self.assertGreater(pacing.estimate_input([{'content': '漢' * 100}]),
                           pacing.estimate_input([{'content': 'a' * 100}]))

    def test_subprocesses_cannot_reserve_the_same_remaining_capacity(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '1000'
        script = """from tpm_pacing import reserve_request
import sys
def no_wait(delay):
    sys.exit(9)
reserve_request([{'role':'user','content':'hello'}],600,now=lambda:1000,sleep=no_wait)
"""
        processes = [subprocess.Popen([sys.executable, '-c', script], cwd=ROOT,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertFalse(stderr, stderr.decode())
        self.assertEqual(sorted(p.returncode for p in processes), [0, 9])
        self.assertEqual(len(self.entries()), 1)
        self.assertLessEqual(sum(e['tokens'] for e in self.entries()), 1000)

    def test_truncation_retry_and_other_model_share_reservations(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '20000'
        replies = iter([response('length'), response(), response()])
        sent_at = []
        def send(*args, **kwargs):
            sent_at.append(self.clock.value)
            self.assertLessEqual(sum(e['tokens'] for e in self.entries()), 20000)
            return next(replies)
        with patch.object(pacing.time, 'time', self.clock.now), patch.object(pacing.time, 'sleep', self.clock.sleep), \
             patch.object(pacing.requests, 'post', side_effect=send), UsageRun(self.root / 'usage') as usage:
            generator._call_llm('model_a', 'system', 'user', 'LLM1-Generate')
            generator._call_llm('model_b', 'system', 'user', 'LLM2-Review')
            usage.status = 'completed'
        self.assertEqual(sent_at, [1000, 1060, 1120])
        report = json.loads((usage.directory / 'usage.json').read_text())
        self.assertEqual(report['totals']['calls'], 3)
        self.assertEqual(report['totals']['total_tokens'], 36)

    def test_failed_http_attempt_keeps_reservation_without_automatic_retry(self):
        payload = {'model': 'model_a', 'messages': MESSAGES, 'max_tokens': 1000}
        needed = pacing.estimate_input(MESSAGES) + 1000
        os.environ['CAPSTONE_TPM_LIMIT'] = str(2 * needed - 1)
        with patch.object(pacing.time, 'time', self.clock.now), patch.object(pacing.time, 'sleep', self.clock.sleep), \
             patch.object(pacing.requests, 'post', side_effect=[requests.Timeout(), response()]) as post:
            with self.assertRaises(requests.Timeout):
                pacing.paced_post('https://example.invalid', json=payload)
            self.assertEqual(post.call_count, 1)
            self.assertEqual(len(self.entries()), 1)
            pacing.paced_post('https://example.invalid', json=payload)
        self.assertEqual(self.clock.value, 1060)

    def test_provider_headers_are_filtered_logged_and_saved_with_usage(self):
        reply = response(status=429)
        reply.headers.update({'X-RateLimit-Remaining-Tokens': '0', 'Retry-After': '12',
                              'Authorization': 'secret-header', 'Other': 'irrelevant'})
        with UsageRun(self.root / 'usage') as usage, patch.object(pacing.requests, 'post', return_value=reply) as post:
            result = pacing.paced_post('https://example.invalid', json={'model': 'm', 'messages': MESSAGES, 'max_tokens': 1000})
            record_response('m', 'call', result)
            post.assert_called_once()
        call = json.loads((usage.directory / 'calls.jsonl').read_text())
        self.assertEqual(call['rate_limit_headers'], {'X-RateLimit-Remaining-Tokens': '0', 'Retry-After': '12'})
        self.assertNotIn('secret-header', self.output.getvalue())
        self.assertEqual(call['outcome'], 'http_error')

    def test_other_frontend_api_entrypoints_use_the_same_reservation_file(self):
        with patch.object(pacing.time, 'time', self.clock.now), patch.object(pacing.time, 'sleep', self.clock.sleep), \
             patch.object(pacing.requests, 'post', return_value=response()) as post:
            generate_microarch.call_llm('prompt', 'model')
            generate_specification.call_llm('prompt', 'model', max_tokens=1000, label='Specification')
            rtlGen.call_llm('prompt')
            configure_from_text.call_llm('prompt', 'model')
            debug_agent.call_patch_llm('prompt', 'model')
        self.assertEqual(post.call_count, 5)
        self.assertEqual(len(self.entries()), 5)
        self.assertEqual(self.clock.waits, [])

    def test_architecture_oversize_error_preserves_actionable_diagnostic(self):
        os.environ['CAPSTONE_TPM_LIMIT'] = '100'
        with patch.object(pacing.requests, 'post') as post:
            with self.assertRaisesRegex(ValueError, 'Reduce context/output allowance'):
                generate_microarch.call_llm('prompt', 'model')
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
