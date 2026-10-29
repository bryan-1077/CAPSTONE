"""Password timing, reuse, cancellation, and artifact boundaries."""

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.auth import cached_password, password_session, prepare_password
from shared.top_orchestrator.contracts import RunConfig, StageResult
from shared.top_orchestrator.orchestrator import run


class FlowPasswordTests(unittest.TestCase):
    def test_prompt_follows_selection_precedes_generation_and_is_not_saved(self):
        events = []
        secret = "test-flow-secret"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote.json"
            remote.write_text(json.dumps(dict(host="host", username="user", project_dir="/project",
                                             partition="partition", qos="qos")))
            def selection(config, directory):
                events.append("selection")
                return replace(config, input_yaml=root / "approved.yaml", target_mhz=200)
            def password(prompt):
                self.assertIn("Enter SSH password for 'user' for Verification and Physical Design", prompt)
                events.append("password")
                return secret
            def generation(config, directory):
                events.append("generation")
                self.assertEqual(cached_password("host", "user"), secret)
                return StageResult("generate", "needs_attention", "fixture stopped")
            with patch("shared.top_orchestrator.orchestrator.prepare_generation", selection), patch(
                    "shared.top_orchestrator.auth.getpass.getpass", password), patch.dict(
                    "shared.top_orchestrator.orchestrator.NODES", generate=generation), patch("sys.stdout", io.StringIO()):
                run(RunConfig("generate", remote_config=remote), root / "run")
            self.assertEqual(events, ["selection", "password", "generation"])
            self.assertIsNone(cached_password("host", "user"))
            self.assertNotIn(secret, (root / "run/state.json").read_text())

    def test_backend_route_requires_password_without_flag_and_frontend_only_does_not(self):
        with password_session(), patch.dict("os.environ", {"CAPSTONE_SSH_HOST": "host", "CAPSTONE_SSH_USER": "user"}), patch(
                "shared.top_orchestrator.auth.getpass.getpass", return_value="secret") as prompt:
            prepare_password(RunConfig("generate", skip_verification=True, no_backend=True))
            prompt.assert_not_called()
            prepare_password(RunConfig("generate", skip_verification=True))
            prompt.assert_called_once()
            self.assertEqual(cached_password("host", "user"), "secret")

    def test_cancelled_or_empty_password_stops_before_any_stage(self):
        for error in (KeyboardInterrupt(), EOFError(), ""):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temporary, patch.dict(
                    "os.environ", {"CAPSTONE_SSH_HOST": "host", "CAPSTONE_SSH_USER": "user"}), patch(
                    "shared.top_orchestrator.auth.getpass.getpass",
                    side_effect=error if isinstance(error, BaseException) else None, return_value=""), patch(
                    "shared.top_orchestrator.orchestrator.NODES") as nodes, patch("sys.stdout", io.StringIO()):
                state = run(RunConfig("backend", allow_unvalidated=True), Path(temporary) / "run")
                self.assertEqual(state.status, "needs_attention")
                nodes.__getitem__.assert_not_called()
                self.assertIsNone(cached_password("host", "user"))
