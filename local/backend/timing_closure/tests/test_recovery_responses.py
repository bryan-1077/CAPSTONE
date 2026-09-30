import json
from contextlib import ExitStack
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents import openai_timing_closure as agent

PLAN = {"summary": "No useful legal change", "actions": []}
TEXT = json.dumps(PLAN)


def chat_stream(text):
    return "\n\n".join("data: " + json.dumps({"choices": [{"delta": {"content": part}}]})
                         for part in (text[:10], text[10:])) + "\n\ndata: [DONE]\n"


class RecoveryResponseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.client = Mock()
        self.stack.enter_context(patch.dict("os.environ", {"OPENAI_API_KEY": "test"}))
        self.stack.enter_context(patch.object(agent, "_build_client", return_value=self.client))
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(self.temp) / "diagnostics.json"

    def run_plan(self):
        return agent.plan_timing_recovery({"resize_choices": {}}, diagnostics_path=self.path)

    def test_normal_responses_does_not_call_chat(self):
        self.client.responses.create.return_value = SimpleNamespace(output_text=TEXT)
        self.assertEqual(self.run_plan(), PLAN)
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "validated")

    def test_chat_stream_after_404(self):
        self.client.responses.create.side_effect = RuntimeError("404 Not Found")
        self.client.chat.completions.create.return_value = chat_stream(TEXT)
        self.assertEqual(self.run_plan(), PLAN)
        data = json.loads(self.path.read_text())
        self.assertEqual([r["api"] for r in data["requests"]], ["responses", "chat_completions"])
        self.assertEqual(data["requests"][1]["extracted_text"], TEXT)
        self.assertTrue(self.client.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["strict"])

    def test_streamed_chat_returned_by_responses(self):
        self.client.responses.create.return_value = chat_stream(TEXT)
        self.assertEqual(self.run_plan(), PLAN)
        self.client.chat.completions.create.assert_not_called()

    def test_responses_stream_deltas(self):
        self.client.responses.create.return_value = "data: " + json.dumps({
            "type": "response.output_text.delta", "delta": TEXT,
        }) + "\n\ndata: [DONE]"
        self.assertEqual(self.run_plan(), PLAN)

    def test_empty_responses_falls_back_once(self):
        self.client.responses.create.return_value = SimpleNamespace(output_text="", output=[])
        self.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=TEXT))])
        self.assertEqual(self.run_plan(), PLAN)
        self.client.chat.completions.create.assert_called_once()

    def test_both_empty_fail_with_saved_diagnostics(self):
        self.client.responses.create.return_value = ""
        self.client.chat.completions.create.return_value = ""
        with self.assertRaisesRegex(ValueError, "no text"):
            self.run_plan()
        data = json.loads(self.path.read_text())
        self.assertEqual(data["status"], "failed")
        self.assertEqual(len(data["requests"]), 2)
        self.assertIn("raw_response", data["requests"][1])

    def test_invalid_plan_is_not_retried_or_accepted(self):
        self.client.responses.create.return_value = SimpleNamespace(output_text='{"actions": []}')
        with self.assertRaises(ValueError):
            self.run_plan()
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "failed")

    def test_auth_error_does_not_fallback(self):
        self.client.responses.create.side_effect = RuntimeError("401 unauthorized")
        with self.assertRaisesRegex(RuntimeError, "401"):
            self.run_plan()
        self.client.chat.completions.create.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["status"], "failed")
