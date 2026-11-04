import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agents import ai_config, openai_prep_review as prep
from agents import openai_physical_verification as physical
from agents import openai_timing_closure as timing


class TAMUBackendTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TAMUS_AI_CHAT_API_KEY": "test-tamu-key",
                                     "TAMU_MODEL": "protected.test-model"}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_client_uses_tamu_key_endpoint_and_model(self):
        # Even a separate OpenAI configuration must not redirect a TAMU key.
        with patch.dict(os.environ, {"OPENAI_API_KEY": "other-key", "OPENAI_BASE_URL": "https://other.invalid"}), patch(
                "openai.OpenAI") as client:
            prep._build_client()
        self.assertEqual(client.call_args.kwargs["api_key"], "test-tamu-key")
        self.assertEqual(client.call_args.kwargs["base_url"], "https://chat-api.tamu.ai/openai")
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertEqual(ai_config.model_name("gpt-5.2"), "protected.test-model")

    def test_existing_openai_configuration_still_works(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-test", "OPENAI_MODEL": "test-model"}, clear=True), patch(
                "openai.OpenAI") as client:
            prep._build_client()
            self.assertEqual(ai_config.model_name("default"), "test-model")
        self.assertEqual(client.call_args.kwargs["api_key"], "openai-test")
        self.assertNotIn("base_url", client.call_args.kwargs)

    def client_for(self, result):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
        return client

    def test_tamu_physical_recovery_calls_chat_and_keeps_key_out_of_diagnostics(self):
        plan = {"summary": "No repairs needed", "actions": []}
        client = self.client_for(plan)
        with tempfile.TemporaryDirectory() as temporary, patch.object(physical, "_build_client", return_value=client):
            path = Path(temporary) / "diagnostics.json"
            result = physical.plan_physical_repair({"available_recipes": []}, diagnostics_path=path)
            self.assertNotIn("test-tamu-key", path.read_text())
        self.assertEqual(result, plan)
        client.responses.create.assert_not_called()
        self.assertEqual(client.chat.completions.create.call_args.kwargs["model"], "protected.test-model")

    def test_tamu_timing_recovery_calls_chat(self):
        plan = {"summary": "No supported repair", "actions": []}
        client = self.client_for(plan)
        with patch.object(timing, "_build_client", return_value=client):
            result = timing.plan_timing_recovery({"resize_choices": []})
        self.assertEqual(result, plan)
        client.responses.create.assert_not_called()
        client.chat.completions.create.assert_called_once()

    def test_tamu_prep_review_is_enabled_and_uses_chat(self):
        result = {"pass": True, "errors": [], "warnings": [], "false_positive_dependencies": [],
                  "suggested_fixes": [], "summary": "Reviewed"}
        client = self.client_for(result)
        with patch.object(prep, "_build_client", return_value=client):
            reviewed = prep.run_openai_prep_review(local_review={}, universal_rules_path=None)
        self.assertTrue(reviewed["enabled"])
        self.assertTrue(reviewed["available"])
        self.assertIsNone(reviewed["api_error"])
        self.assertEqual(reviewed["request_metadata"]["base_url"], ai_config.TAMU_BASE_URL)
        client.responses.create.assert_not_called()
        client.chat.completions.create.assert_called_once()
