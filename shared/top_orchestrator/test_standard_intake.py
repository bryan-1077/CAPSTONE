"""Shared Standard intake preserves existing interpretation and confirmation."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from .contracts import RunConfig
from .frequency import prepare_generation
from .frontend_result import write_frontend_result


class StandardIntakeTests(unittest.TestCase):
    def test_saved_text_reaches_interpreter_and_clock_reaches_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "input.json"
            intake.write_text(json.dumps({"schema_version": 1, "status": "confirmed_request",
                "frontend_flow": "expand-spec", "original_text": "medium\n4 banks", "target_mhz": 180}))
            run = root / "run"
            run.mkdir()
            def interpret(command, log):
                self.assertIn("--text-file", command)
                self.assertEqual(Path(command[-1]).read_text(), "medium\n4 banks")
                (run / "approved_specs.yaml").write_text("memory: {banks: 4}\n")
                return 0
            with patch("shared.top_orchestrator.frequency.stream_command", side_effect=interpret):
                config = prepare_generation(RunConfig("generate", intake=intake), run)
            self.assertEqual(config.target_mhz, 180)
            self.assertEqual(yaml.safe_load(config.input_yaml.read_text())["controller_clock"], {"frequency_mhz": 180})
            from .timing_model import FUNCTIONAL_TIMING
            self.assertEqual(yaml.safe_load(config.input_yaml.read_text())["timing_model"], FUNCTIONAL_TIMING)

    def test_conflicting_interpreted_clock_blocks_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            intake = root / "input.json"
            intake.write_text(json.dumps({"schema_version": 1, "status": "confirmed_request",
                "frontend_flow": "expand-spec", "original_text": "medium", "target_mhz": 180}))
            def interpret(*args):
                (root / "approved_specs.yaml").write_text("controller_clock: {frequency_mhz: 200}")
                return 0
            with patch("shared.top_orchestrator.frequency.stream_command", side_effect=interpret):
                with self.assertRaisesRegex(ValueError, "conflicts"):
                    prepare_generation(RunConfig("generate", intake=intake), root)

    def test_result_hashes_files_without_claiming_backend_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rtl = root / "top.sv"
            rtl.write_text("module top; endmodule")
            output = write_frontend_result(root / "result.json", flow="specification", build=root,
                top="top", target_mhz=180, revision="revision", rtl_files=[rtl], contracts=[],
                requirements={}, checks={}, evidence={})
            record = json.loads(output.read_text())
            self.assertEqual(len(record["files"][0]["sha256"]), 64)
            self.assertFalse(record["backend_ready"])
