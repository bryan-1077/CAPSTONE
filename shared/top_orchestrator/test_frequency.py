"""Frequency choices and handoff ordering without model or EDA calls."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.contracts import RunConfig, StageResult, FailureReport
from shared.top_orchestrator.frequency import positive_mhz, select_frequency
from shared.top_orchestrator.nodes import backend_command
from shared.top_orchestrator.orchestrator import run


class FrequencyTests(unittest.TestCase):
    def setUp(self):
        publication = patch("shared.top_orchestrator.orchestrator.publish",
                            side_effect=lambda frontend, destination, specs: destination)
        publication.start()
        self.addCleanup(publication.stop)

    def test_invalid_numbers(self):
        for value in ("0", "-1", "nan", "inf", "1e-320", "word"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                positive_mhz(value)

    def choose(self, text, answers, target=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.yaml"
            spec.write_text(text)
            with patch("builtins.input", side_effect=answers), patch(
                    "shared.top_orchestrator.frequency.ask_advisor", return_value="Consider 200 MHz.") as advisor:
                selected = select_frequency(RunConfig("generate", input_yaml=spec, target_mhz=target), root)
            return selected, advisor.call_count, json.loads((root / "frequency_selection.json").read_text())

    def test_default_and_numeric_never_call_advisor(self):
        for answers, target, expected in (([""], None, 200), (["nan", "0", "230"], None, 230),
                                          ([], 240, 240)):
            selected, calls, record = self.choose("memory: {speed: '3200'}", answers, target)
            self.assertEqual(selected.target_mhz, expected)
            self.assertEqual(calls, 0)
            self.assertEqual(record["suggestion_source"], "configured default")

    def test_clock_period_default_and_mismatch_warning(self):
        selected, calls, record = self.choose("controller_clock: {period_ns: 5}", [""])
        self.assertEqual(selected.target_mhz, 200)
        selected, calls, record = self.choose("controller_clock_mhz: 200", ["180"])
        self.assertIn("below", record["warnings"][0])

    def test_discussion_followup_and_explicit_acceptance(self):
        selected, calls, record = self.choose("memory: {speed: '3200'}",
            ["discuss", "I care more about power", "nan", "200"])
        self.assertEqual(calls, 2)
        self.assertEqual(selected.target_mhz, 200)
        self.assertEqual(len(record["conversation"]), 4)
        command = backend_command(selected, Path("/mailbox/run/revision_001"))
        self.assertEqual(command[-2:], ["--target-mhz", "200.0"])
        self.assertIn("/mailbox/run/revision_001", command)

    def test_advisor_failure_allows_manual_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.yaml"
            spec.write_text("memory: {speed: '3200'}")
            with patch("builtins.input", side_effect=["discuss", "220"]), patch(
                    "shared.top_orchestrator.frequency.ask_advisor", side_effect=ValueError("unavailable")):
                selected = select_frequency(RunConfig("generate", input_yaml=spec), root)
            self.assertEqual(selected.target_mhz, 220)

    def test_selection_precedes_generation_and_survives_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            events = []
            def specs(command, log):
                self.assertNotIn("--run-flow", command)
                Path(command[-1]).write_text("memory: {speed: '3200'}")
                events.append("specs")
                return 0
            def prompt(message):
                events.append("frequency")
                return "230"
            def generate(config, directory):
                events.append("recheck" if config.recheck_only else "generate")
                self.assertEqual(config.target_mhz, 230)
                saved = json.loads((root / "state.json").read_text())
                self.assertEqual(saved["config"]["target_mhz"], 230)
                if not config.recheck_only:
                    return StageResult("generate", "failed", "BIST", failure=FailureReport("bist", root / "log"))
                return StageResult("generate", "passed", "checked")
            def debug(config, directory):
                self.assertEqual(config.target_mhz, 230)
                return StageResult("debug", "passed", "fixed")
            with patch("shared.top_orchestrator.frequency.stream_command", side_effect=specs), patch(
                    "builtins.input", side_effect=prompt), patch.dict(
                    "shared.top_orchestrator.orchestrator.NODES", {"generate": generate, "debug": debug}):
                state = run(RunConfig("generate"), root)
            self.assertEqual(events, ["specs", "frequency", "generate", "recheck"])
            self.assertEqual(state.config.target_mhz, 230)

    def test_input_snapshot_and_noninteractive_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.yaml"
            source.write_text("memory: {speed: '3200'}")
            def generate(config, directory):
                self.assertEqual(config.input_yaml, root / "run/approved_specs.yaml")
                self.assertEqual(config.input_yaml.read_text(), source.read_text())
                self.assertEqual(config.target_mhz, 220)
                return StageResult("generate", "passed", "ok")
            with patch("builtins.input", side_effect=AssertionError("unexpected prompt")), patch.dict(
                    "shared.top_orchestrator.orchestrator.NODES", {"generate": generate}):
                run(RunConfig("generate", input_yaml=source, target_mhz=220), root / "run")

    def test_eof_stops_before_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.yaml"
            source.write_text("memory: {speed: '3200'}")
            with patch("builtins.input", side_effect=EOFError), patch.dict(
                    "shared.top_orchestrator.orchestrator.NODES", {"generate": lambda *_: self.fail("generated")}):
                state = run(RunConfig("generate", input_yaml=source), root / "run")
            self.assertEqual(state.status, "needs_attention")


if __name__ == "__main__":
    unittest.main()
