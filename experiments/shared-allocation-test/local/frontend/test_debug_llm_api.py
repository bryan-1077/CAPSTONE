"""Debug API regressions with mocked provider responses."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
import debug_agent as agent


def response(content="NO_PATCH", reason="stop", status=200, data=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(data if data is not None else {
        "choices": [{"finish_reason": reason, "message": {"content": content}}],
        "usage": {"completion_tokens": 8192},
    }).encode()
    return result


class DebugApiTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {
            "TAMUS_AI_CHAT_API_KEY": "test-secret",
            "DEBUG_LLM_MAX_TOKENS": "8192",
            "DEBUG_LLM_MAX_RETRY_TOKENS": "16384",
        })
        env.start()
        self.addCleanup(env.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.diagnostics = Path(directory.name) / "api.json"
        output = patch("builtins.print")
        output.start()
        self.addCleanup(output.stop)

    def call(self, system_prompt=None):
        return agent.call_patch_llm("test prompt", "test-model", system_prompt=system_prompt,
                                    diagnostic_path=self.diagnostics)

    def test_complete_patch_preserves_metadata(self):
        with patch("requests.post", return_value=response(" NO_PATCH ")) as post:
            self.assertEqual(self.call(), "NO_PATCH")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8192)
        data = json.loads(self.diagnostics.read_text())
        self.assertEqual(data["finish_reasons"], ["stop"])
        self.assertEqual(data["usage"]["completion_tokens"], 8192)
        self.assertEqual(len(data["attempts"]), 1)
        self.assertNotIn("test-secret", self.diagnostics.read_text())

    def test_truncated_assessment_and_patch_retry_before_returning(self):
        for final, system_prompt in (("NO_PATCH", None), ('{"decision":"insufficient_evidence"}', "Return JSON")):
            for reason in ("length", "max_tokens"):
                with self.subTest(final=final, reason=reason):
                    with patch("requests.post", side_effect=[response("partial", reason), response(final)]) as post:
                        self.assertEqual(self.call(system_prompt), final)
                    payloads = [call.kwargs["json"] for call in post.call_args_list]
                    self.assertEqual([item["max_tokens"] for item in payloads], [8192, 16384])
                    self.assertEqual(payloads[0]["messages"], payloads[1]["messages"])
                    if system_prompt:
                        self.assertEqual(payloads[1]["messages"][0]["content"], system_prompt)
                    data = json.loads(self.diagnostics.read_text())
                    self.assertEqual(data["finish_reasons"], ["stop"])
                    self.assertEqual(data["attempts"][0]["error_type"], "truncated_response")
                    self.assertEqual(data["attempts"][0]["finish_reasons"], [reason])

    def test_valid_looking_truncated_text_is_never_returned(self):
        with patch("requests.post", return_value=response("NO_PATCH", "length")) as post:
            with self.assertRaisesRegex(RuntimeError, "incomplete assessment/patch output rejected"):
                self.call()
        self.assertEqual(post.call_count, 2)
        data = json.loads(self.diagnostics.read_text())
        self.assertEqual(len(data["attempts"]), 2)
        self.assertEqual(data["error_type"], "truncated_response")

    def test_empty_truncated_content_retries(self):
        with patch("requests.post", side_effect=[response(None, "length"), response()]) as post:
            self.assertEqual(self.call(), "NO_PATCH")
        self.assertEqual(post.call_count, 2)

    def test_budget_overrides_and_clamped_ceiling(self):
        with patch.dict("os.environ", {"DEBUG_LLM_MAX_TOKENS": "6000", "DEBUG_LLM_MAX_RETRY_TOKENS": "14000"}):
            with patch("requests.post", return_value=response("partial", "length")) as post:
                with self.assertRaisesRegex(RuntimeError, "max_tokens=14000"):
                    self.call()
        self.assertEqual([call.kwargs["json"]["max_tokens"] for call in post.call_args_list],
                         [6000, 12000, 14000])

    def test_equal_budget_and_ceiling_disable_retry(self):
        with patch.dict("os.environ", {"DEBUG_LLM_MAX_RETRY_TOKENS": "8192"}):
            with patch("requests.post", return_value=response("partial", "length")) as post:
                with self.assertRaisesRegex(RuntimeError, "truncated at token limit"):
                    self.call()
        self.assertEqual(post.call_count, 1)

    def test_exhausted_truncation_stops_investigation_before_assessment_parser(self):
        failure = self.diagnostics.parent
        (failure / "analysis").mkdir()
        record = {"decision": "insufficient_evidence", "assessments": 0, "max_actions": 1}
        with patch.object(agent, "investigation_record", return_value=record), \
             patch.object(agent, "graph_failure_dir", return_value=failure), \
             patch.object(agent, "read_json", return_value={}), \
             patch.object(agent, "compact_parsed_failure", return_value={}), \
             patch.object(agent, "log_debug"), \
             patch.object(agent, "save_investigation", side_effect=lambda state, data, step: data), \
             patch.object(agent.investigation, "evidence_unchanged", return_value=True), \
             patch.object(agent.investigation, "assessment_prompt", return_value="Return JSON"), \
             patch.object(agent.investigation, "accept_assessment") as parse, \
             patch("requests.post", return_value=response('{"decision":', "length")) as post:
            result = agent.graph_assess_evidence_node({"model": "test-model"})
        parse.assert_not_called()
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["assessments"], 1)
        self.assertEqual(result["stop_reason"], "assessment_failed")
        self.assertIn("truncated at token limit", result["unresolved_questions"][0])
        data = json.loads((failure / "analysis/assessment_001.api.json").read_text())
        self.assertEqual(len(data["attempts"]), 2)

    def test_invalid_budget_never_calls_provider(self):
        for initial, ceiling in (("bad", "16384"), ("0", "16384"), ("8192", "4096")):
            with self.subTest(initial=initial):
                with patch.dict("os.environ", {"DEBUG_LLM_MAX_TOKENS": initial, "DEBUG_LLM_MAX_RETRY_TOKENS": ceiling}):
                    with patch("requests.post") as post:
                        with self.assertRaisesRegex(RuntimeError, "Invalid DEBUG_LLM"):
                            self.call()
                        post.assert_not_called()

    def test_empty_content_stops_without_retry(self):
        with patch("requests.post", return_value=response(None)) as post:
            with self.assertRaisesRegex(RuntimeError, "no message content"):
                self.call()
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads(self.diagnostics.read_text())["error_type"], "empty_response")

    def test_content_filter_rejected_even_with_text(self):
        with patch("requests.post", return_value=response("NO_PATCH", "content_filter")):
            with self.assertRaisesRegex(RuntimeError, "finish_reason=content_filter"):
                self.call()

    def test_text_blocks_and_missing_finish_reason_still_supported(self):
        with patch("requests.post", return_value=response([{"type": "text", "text": "NO_PATCH"}], None)):
            self.assertEqual(self.call(), "NO_PATCH")

    def test_http_error_keeps_previous_retry_metadata_and_redacts_key(self):
        with patch("requests.post", side_effect=[response("partial", "length"),
                   response(status=400, data={"error": "test-secret unsupported budget"})]) as post:
            with self.assertRaisesRegex(RuntimeError, "HTTP 400") as caught:
                self.call()
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("test-secret", str(caught.exception))
        data = json.loads(self.diagnostics.read_text())
        self.assertEqual(data["attempts"][0]["error_type"], "truncated_response")
        self.assertEqual(data["error_type"], "http_error")

    def test_network_failure_is_reported_and_saved(self):
        with patch("requests.post", side_effect=requests.exceptions.Timeout("timed out")):
            with self.assertRaisesRegex(RuntimeError, "network error"):
                self.call()
        self.assertEqual(json.loads(self.diagnostics.read_text())["error_type"], "network_error")

    def test_invalid_envelope_is_reported_and_saved(self):
        for data in ({}, {"choices": []}, {"choices": None}, {"choices": [None]}, []):
            with self.subTest(data=data):
                with patch("requests.post", return_value=response(data=data)):
                    with self.assertRaisesRegex(RuntimeError, "unexpected response shape"):
                        self.call()
                self.assertEqual(json.loads(self.diagnostics.read_text())["error_type"], "invalid_response")

    def test_diagnostics_are_optional(self):
        with patch("requests.post", return_value=response()):
            self.assertEqual(agent.call_patch_llm("prompt", "model"), "NO_PATCH")


if __name__ == "__main__":
    unittest.main()
