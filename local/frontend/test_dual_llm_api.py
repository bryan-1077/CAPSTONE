"""API boundary regressions; all provider calls are mocked."""
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

import dual_llm_rtlGen as generator
from token_usage import ModuleUsage


def response(content="{}", finish_reason="stop", status=200, data=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data if data is not None else {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "usage": {"completion_tokens": 4096},
    }).encode()
    return result


class DualLlmApiTests(unittest.TestCase):
    def setUp(self):
        pacing = patch.dict("os.environ", {"CAPSTONE_TPM_LIMIT": "0"})
        pacing.start()
        self.addCleanup(pacing.stop)
        self.env = patch.dict("os.environ", {
            "TAMUS_AI_CHAT_API_KEY": "test-secret",
            "DUAL_LLM_MAX_TOKENS": "8192",
            "DUAL_LLM_MAX_RETRY_TOKENS": "16384",
            "DUAL_LLM_TIMEOUT_SECONDS": "180",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def call(self):
        return generator._call_llm("test-model", "system", "user", "LLM1-Generate")

    def test_complete_response_and_diagnostics(self):
        with patch.object(generator.requests, "post", return_value=response(" {} ")) as post:
            self.assertEqual(self.call(), "{}")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8192)
        self.assertIn("finish_reason=stop", self.output.getvalue())
        self.assertIn("completion_tokens=4096", self.output.getvalue())
        self.assertNotIn("test-secret", self.output.getvalue())

    def test_default_budgets_distinguish_code_generation_from_review(self):
        with patch.dict(os.environ):
            os.environ.pop('DUAL_LLM_MAX_TOKENS', None)
            for label, budget in [('LLM1-Generate', 16384), ('LLM1-Merge', 16384), ('LLM2-Review', 8192)]:
                with self.subTest(label=label), patch.object(generator.requests, 'post', return_value=response()) as post:
                    generator._call_llm('test-model', 'system', 'user', label)
                    self.assertEqual(post.call_args.kwargs['json']['max_tokens'], budget)

    def test_custom_ceiling_caps_default_generation_budget(self):
        with patch.dict(os.environ, {'DUAL_LLM_MAX_RETRY_TOKENS': '10000'}):
            os.environ.pop('DUAL_LLM_MAX_TOKENS', None)
            with patch.object(generator.requests, 'post', return_value=response()) as post:
                self.call()
            self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 10000)

    def test_truncation_saves_partial_response_and_never_accepts_it(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop('DUAL_LLM_MAX_TOKENS', None)
            with self.assertRaisesRegex(RuntimeError, 'Response truncated at token limit'):
                with ModuleUsage('large_module', directory):
                    with patch.object(generator.requests, 'post', return_value=response('partial RTL', 'length')) as post:
                        self.call()
            post.assert_called_once()
            report = json.loads(next(Path(directory).rglob('truncated_*.json')).read_text())
            self.assertEqual(report['content'], 'partial RTL')
            self.assertEqual(report['max_tokens'], 16384)
            self.assertEqual(report['usage']['completion_tokens'], 4096)
            self.assertFalse(report['accepted'])

    def test_truncation_retries_same_prompt_with_larger_budget(self):
        for reason in ("length", "max_tokens"):
            with self.subTest(reason=reason):
                with patch.object(generator.requests, "post", side_effect=[
                    response('{"corrected_rtl":"module', reason), response('{"corrected_rtl":"module m; endmodule"}')
                ]) as post:
                    self.assertIn("endmodule", self.call())
                payloads = [call.kwargs["json"] for call in post.call_args_list]
                self.assertEqual([item["max_tokens"] for item in payloads], [8192, 16384])
                self.assertEqual(payloads[0]["messages"], payloads[1]["messages"])

    def test_truncation_at_ceiling_stops_before_json_parser(self):
        with patch.object(generator.requests, "post", return_value=response("partial", "length")) as post:
            with self.assertRaisesRegex(RuntimeError, "Response truncated at token limit"):
                self.call()
        self.assertEqual(post.call_count, 2)

    def test_ceiling_can_disable_retry(self):
        with patch.dict("os.environ", {"DUAL_LLM_MAX_RETRY_TOKENS": "8192"}):
            with patch.object(generator.requests, "post", return_value=response("partial", "length")) as post:
                with self.assertRaisesRegex(RuntimeError, "Response truncated"):
                    self.call()
        self.assertEqual(post.call_count, 1)

    def test_provider_clamps_limit_retries_remain_bounded(self):
        with patch.dict("os.environ", {"DUAL_LLM_MAX_TOKENS": "6000", "DUAL_LLM_MAX_RETRY_TOKENS": "14000"}):
            with patch.object(generator.requests, "post", return_value=response("partial", "length")) as post:
                with self.assertRaisesRegex(RuntimeError, "max_tokens=14000"):
                    self.call()
        self.assertEqual([call.kwargs["json"]["max_tokens"] for call in post.call_args_list],
                         [6000, 12000, 14000])

    def test_invalid_budget_fails_before_network(self):
        for initial, ceiling in (("bad", "16384"), ("0", "16384"), ("8192", "4096")):
            with self.subTest(initial=initial, ceiling=ceiling):
                with patch.dict("os.environ", {"DUAL_LLM_MAX_TOKENS": initial, "DUAL_LLM_MAX_RETRY_TOKENS": ceiling}):
                    with patch.object(generator.requests, "post") as post:
                        with self.assertRaisesRegex(RuntimeError, "Invalid DUAL_LLM"):
                            self.call()
                        post.assert_not_called()

    def test_default_and_configured_timeouts_reach_http_client(self):
        for setting, expected in ((None, 180), ('600', 600), ('12.5', 12.5)):
            with self.subTest(setting=setting), patch.dict(os.environ):
                if setting is None:
                    os.environ.pop('DUAL_LLM_TIMEOUT_SECONDS', None)
                else:
                    os.environ['DUAL_LLM_TIMEOUT_SECONDS'] = setting
                with patch.object(generator.requests, 'post', return_value=response()) as post:
                    self.call()
                self.assertEqual(post.call_args.kwargs['timeout'], expected)

    def test_invalid_timeouts_fail_before_network(self):
        for value in ('0', '-1', 'bad', 'nan', 'inf'):
            with self.subTest(value=value), patch.dict(os.environ, {'DUAL_LLM_TIMEOUT_SECONDS': value}), \
                 patch.object(generator.requests, 'post') as post:
                with self.assertRaisesRegex(RuntimeError, 'positive finite'):
                    self.call()
                post.assert_not_called()

    def test_malformed_provider_envelope_is_runtime_error(self):
        for data in ({"choices": []}, {"choices": None}, {}, {"choices": [None]}):
            with self.subTest(data=data):
                with patch.object(generator.requests, "post", return_value=response(data=data)):
                    with self.assertRaisesRegex(RuntimeError, "Unexpected response shape"):
                        self.call()

    def test_empty_or_nontext_content_is_not_lint_failure(self):
        for content in (None, "", "  ", []):
            with self.subTest(content=content):
                with patch.object(generator.requests, "post", return_value=response(content)) as post:
                    with self.assertRaisesRegex(RuntimeError, "empty or non-text content"):
                        self.call()
                self.assertEqual(post.call_count, 1)

    def test_filtered_response_is_not_accepted(self):
        with patch.object(generator.requests, "post", return_value=response("partial", "content_filter")):
            with self.assertRaisesRegex(RuntimeError, "finish_reason=content_filter"):
                self.call()

    def test_legacy_response_without_finish_reason_is_supported(self):
        with patch.object(generator.requests, "post", return_value=response(finish_reason=None)):
            self.assertEqual(self.call(), "{}")

    def test_http_failure_is_distinct_and_redacts_credentials(self):
        with patch.object(generator.requests, "post", return_value=response(
                status=400, data={"error": "test-secret unsupported budget"})) as post:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400") as caught:
                self.call()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertEqual(post.call_count, 1)

    def test_network_failure_is_distinct(self):
        with patch.object(generator.requests, "post", side_effect=requests.exceptions.Timeout("timed out")):
            with self.assertRaisesRegex(RuntimeError, "Network error"):
                self.call()

    def test_empty_generated_rtl_never_reaches_lint_or_reviewer(self):
        payload = {"corrected_rtl": "", "status": "AMBIGUOUS", "issues": [], "parse_failed": True}
        for lint_enabled in (True, False):
            with self.subTest(lint_enabled=lint_enabled):
                with patch.object(generator, "_llm1_generate", return_value=payload), \
                     patch.object(generator, "lint_candidate") as lint, \
                     patch.object(generator, "_llm2_review") as review:
                    with self.assertRaisesRegex(RuntimeError, "failed before lint/review"):
                        generator.run_dual_llm_rtlgen(SimpleNamespace(design_name="scheduler"), "", lint_enabled)
                    lint.assert_not_called()
                    review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
