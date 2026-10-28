"""Offline tests of shared intake and route isolation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .intake import collect_intake
from .contracts import RunConfig
from .nodes import frontend_command
from .orchestrator import run


class IntakeTests(unittest.TestCase):
    def test_both_routes_preserve_text_and_frequency_without_approval(self):
        text = "Build an interface.\nKeep ${literal} and all requirements.\n"
        for flow in ("expand-spec", "specification"):
            with self.subTest(flow=flow), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "description.txt"
                source.write_text(text)
                output = Path(tmp) / "intake.json"
                with patch("builtins.input", return_value="yes"):
                    collect_intake(flow, source, 180, output)
                record = json.loads(output.read_text())
                self.assertEqual(record["original_text"], text)
                self.assertEqual(record["target_mhz"], 180)
                self.assertFalse(record["design_approved"])
                self.assertEqual(record["source_files"], [str(source)])

    def test_interactive_experimental_can_leave_choices_open(self):
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.input", side_effect=[
                "2", "Build a queue; choose the implementation.", ".", "invalid", "", "y"]):
            output = collect_intake(None, None, None, Path(tmp) / "request.json")
            record = json.loads(output.read_text())
            self.assertEqual(record["frontend_flow"], "specification")
            self.assertEqual(record["target_mhz"], 200)

    def test_rejection_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, patch("builtins.input", side_effect=["queue", ".", "n"]):
            output = Path(tmp) / "request.json"
            with self.assertRaises(ValueError):
                collect_intake("specification", None, 100, output)
            self.assertFalse(output.exists())

    def test_experimental_cannot_enter_legacy_execution(self):
        config = RunConfig("generate", frontend_flow="specification")
        with self.assertRaises(ValueError):
            frontend_command("generate", config)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            with self.assertRaises(ValueError):
                run(config, output)
            self.assertFalse(output.exists())

    def test_cli_preparation_plan_is_side_effect_free(self):
        from .__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "request.json"
            with patch("sys.argv", ["orchestrator", "--frontend-flow", "specification",
                                    "--prepare-intake", str(output), "--plan"]), patch(
                    "builtins.input", side_effect=AssertionError("Unexpected prompt")):
                self.assertEqual(main(), 0)
            self.assertFalse(output.exists())
