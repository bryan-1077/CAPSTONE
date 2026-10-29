"""Check final output against the specs, clock and stages actually recorded."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .contracts import RunConfig, RunState, StageResult
from .orchestrator import run
from .summary import final_summary


class FinalSummaryTests(unittest.TestCase):
    def test_summary_uses_published_specs_and_selected_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mailbox = root / "mailbox"
            mailbox.mkdir()
            (mailbox / "approved_specs.yaml").write_text(
                "design_name: controller\nmemory:\n  protocol: DDR4\n  speed: '3200'\n"
                "topology:\n  banks: 4\nfeatures:\n  scheduler: simple\n  refresh: true\n")
            mutable = root / "input.yaml"
            mutable.write_text("memory: {protocol: DDR5}\n")
            state = RunState(RunConfig("generate", input_yaml=mutable, mailbox=mailbox,
                                      target_mhz=200, target_mhz_source="command line"),
                             [StageResult(stage, "passed", "ok") for stage in
                              ("generate", "validation", "backend", "gdsii-validation")], status="complete")
            output = final_summary(state, root)
            for expected in ("protocol: DDR4", "speed: '3200'", "banks: 4", "scheduler: simple",
                             "refresh: true", "200 MHz", "5.00 ns", "command line",
                             str(mailbox / "approved_specs.yaml"), str(root)):
                self.assertIn(expected, output)
            self.assertNotIn("DDR5", output)
            self.assertNotIn("Agent 4", output)
            self.assertNotIn("LEC", output)
            self.assertEqual(output.count("PASSED"), 6)

    def test_skipped_stages_are_not_reported_as_passed(self):
        state = RunState(RunConfig("generate", no_bist=True, skip_verification=True),
                         [StageResult("generate", "passed", "lint only", {"bist": "not_run"})])
        output = final_summary(state, Path("/run"))
        self.assertIn("BIST simulation:      SKIPPED", output)
        self.assertIn("Agents 1–3:           SKIPPED", output)
        self.assertIn("Approved specifications: not recorded", output)
        self.assertIn("Frequency: not recorded", output)

    def test_resume_does_not_invent_frontend_results(self):
        state = RunState(RunConfig("backend", target_mhz=230, target_mhz_source="saved mapped build",
                                  validation_result=Path("/receipt.json")))
        output = final_summary(state, Path("/run"))
        self.assertIn("RTL lint:             NOT RECORDED IN THIS RUN", output)
        self.assertIn("BIST simulation:      NOT RECORDED IN THIS RUN", output)
        self.assertIn("PASSED (accepted verification receipt)", output)
        self.assertIn("230 MHz", output)

    def test_unreadable_specs_do_not_prevent_reporting_completed_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "approved_specs.yaml"
            for content in (None, "[invalid yaml", "- not a mapping"):
                if content is not None:
                    source.write_text(content)
                output = final_summary(RunState(RunConfig("backend", input_yaml=source)), Path(temporary))
                self.assertIn("Approved specifications unavailable", output)
                self.assertIn(str(source), output)

    def test_only_successful_gdsii_validation_prints_final_summary(self):
        for final_status in ("passed", "PASS_WITH_GAPS", "needs_attention", "failed"):
            with self.subTest(final_status=final_status), tempfile.TemporaryDirectory() as temporary:
                passed = final_status in ("passed", "PASS_WITH_GAPS")
                result = StageResult("gdsii-validation", "passed" if passed else final_status, "checked",
                                     {"validation_status": final_status})
                with patch("shared.top_orchestrator.orchestrator.prepare_password"), patch.dict(
                        "shared.top_orchestrator.orchestrator.NODES", {
                            "backend": lambda c, d: StageResult("backend", "passed", "done"),
                            "gdsii-validation": lambda c, d: result
                        }), patch("sys.stdout", io.StringIO()) as output:
                    state = run(RunConfig("backend", target_mhz=200), Path(temporary) / "run")
                self.assertEqual("SYSTEM RUN COMPLETE" in output.getvalue(), passed)
                self.assertEqual(state.status, "complete" if passed else final_status)
                if final_status == "PASS_WITH_GAPS":
                    self.assertIn("GDSII VALIDATION PASSED WITH GAPS", output.getvalue())
                    self.assertIn("GDSII validation:     PASS_WITH_GAPS", output.getvalue())
                    self.assertNotIn("needs_attention", output.getvalue())


if __name__ == "__main__":
    unittest.main()
