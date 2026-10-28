"""Offline Experimental dispatch and owned-output tests; no provider calls."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from .contracts import RunConfig
from .orchestrator import run
from .specification_frontend import read_intake


class SpecificationFrontendTests(unittest.TestCase):
    def fixture(self, root):
        path = root / "intake.json"
        path.write_text(json.dumps({"schema_version": 1, "status": "confirmed_request",
            "frontend_flow": "specification", "original_text": "Build a queue.\nChoose the details.",
            "target_mhz": 180, "target_mhz_source": "user"}))
        return path

    def test_conflicting_overrides_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.fixture(Path(tmp))
            for flow, target in [("expand-spec", None), (None, 200)]:
                with self.assertRaises(ValueError):
                    read_intake(path, flow, target)

    def test_success_uses_owned_build_and_never_legacy_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = self.fixture(root)
            commands = []
            def execute(command, log):
                commands.append(command)
                build = log.parent / "build"
                if len(commands) == 1:
                    build.mkdir()
                    (build / "build_status.json").write_text(json.dumps({
                        "rtl_generation": "passed", "integration_lint": "passed",
                        "specification_approval": "approved", "verification_handoff": "exported"}))
                    (build / "top.sv").write_text("module top; endmodule")
                    (build / "verification_handoff.json").write_text(json.dumps({
                        "phase": "rtl", "design": {"top_module": "top"},
                        "identity": {"rtl_revision": "test-revision"},
                        "rtl_files": ["top.sv"], "inputs": [], "verification_intent": {}}))
                return 0
            config = RunConfig("generate", frontend_flow="specification", intake=intake,
                               skip_verification=True, no_backend=True)
            with patch("shared.top_orchestrator.specification_frontend.stream_command", side_effect=execute), patch(
                    "shared.top_orchestrator.specification_mailbox.publish_specification", return_value=root / "mailbox"), patch(
                    "shared.top_orchestrator.orchestrator.publish", side_effect=AssertionError("Legacy publication")):
                state = run(config, root / "run")
            self.assertEqual(state.status, "complete")
            self.assertEqual(len(commands), 2)
            self.assertIn("--specification", commands[0])
            self.assertNotIn("--microarch", commands[0])
            self.assertIn(str(root / "run/frontend/build"), commands[0])
            request = yaml.safe_load((root / "run/frontend/input/request.yaml").read_text())
            self.assertEqual(request["requirements"], ["Build a queue.\nChoose the details."])
            self.assertEqual(request["controller_clock"]["frequency_mhz"], 180)
            self.assertEqual(state.config.target_mhz, 180)
            self.assertEqual(state.repair_cycles, 0)

    def test_failure_has_no_outer_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = RunConfig("generate", frontend_flow="specification", intake=self.fixture(root),
                               skip_verification=True, no_backend=True)
            with patch("shared.top_orchestrator.specification_frontend.stream_command", return_value=1) as execute:
                state = run(config, root / "run")
            self.assertEqual(state.status, "failed")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(state.repair_cycles, 0)

    def test_cli_plan_has_no_writes(self):
        from .__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = self.fixture(root)
            argv = ["orchestrator", "--intake", str(intake), "--no-verif", "--no-backend",
                    "--run-dir", str(root / "run"), "--plan"]
            with patch("sys.argv", argv), patch("shared.top_orchestrator.__main__.run", side_effect=AssertionError("Execution")):
                self.assertEqual(main(), 0)
            self.assertFalse((root / "run").exists())
