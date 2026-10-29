import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from agents import openai_rtl_failure_triage as triage


class RTLFailureTriageTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.report_path = Path(directory.name) / "reports" / "triage.md"

    def test_fallback_reports_identify_owner_and_suspected_issue(self):
        for diagnostic, owner in (
            ("Excessive combinational depth in the request selector.", "Frontend"),
            ("WNS -0.119 ns", "Backend"),
            ("Unrecognized failure", "Unknown (more evidence needed)"),
        ):
            with self.subTest(owner=owner), patch.dict(os.environ, {}, clear=True):
                result = triage.run_openai_rtl_failure_triage(
                    logs={"stderr": diagnostic}, report_path=self.report_path,
                )
                report = Path(result["report_path"]).read_text()
                self.assertIn(f"**Failure belongs to:** {owner}", report)
                if owner == "Frontend":
                    self.assertIn("## Suspected RTL issue", report)
                    self.assertIn(diagnostic, report)
                else:
                    self.assertNotIn("## Suspected RTL issue", report)

    def test_report_write_failure_preserves_classification(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(triage, "write_rtl_failure_report", side_effect=OSError("disk full")),
        ):
            result = triage.run_openai_rtl_failure_triage(logs={"stderr": "WNS -0.119"})
        self.assertEqual(result["handoff_owner"], "backend")
        self.assertIsNone(result["report_path"])
        self.assertTrue(any("disk full" in warning for warning in result["warnings"]))

    def classify(self, diagnostic):
        return triage.run_local_rtl_failure_triage(logs={"stderr": diagnostic})

    def test_design_quality_diagnostics_return_to_frontend(self):
        for diagnostic in (
            "Poor RTL logic design: a serial mux chain limits performance.",
            "Inefficient RTL architecture requires a datapath change.",
            "Excessive combinational depth in the request selector.",
            "Logic restructuring is required to meet the target.",
        ):
            with self.subTest(diagnostic=diagnostic):
                result = self.classify(diagnostic)
                self.assertEqual(result["handoff_owner"], "frontend")
                self.assertEqual(result["failure_category"], "poor_logic_design")
                self.assertIn(diagnostic, result["evidence"])

    def test_old_frontend_diagnostics_do_not_trigger_handoff(self):
        for diagnostic in (
            "syntax error: unexpected token near endmodule",
            "lint: unused signal, width mismatch, multiple drivers",
            "unsynthesizable CDFG-123: constant expression required",
            "FSM stuck: no exit transition; missing default state",
        ):
            with self.subTest(diagnostic=diagnostic):
                result = self.classify(diagnostic)
                self.assertEqual(result["handoff_owner"], "unknown")
                self.assertEqual(result["failure_category"], "unknown")

    def test_backend_diagnostics_stay_in_backend(self):
        for diagnostic, category in (
            ("negative slack: WNS -0.119 ns", "timing_violation"),
            ("global route congestion overflow", "congestion"),
            ("route error: open net", "routing_error"),
            ("SDC clock not found", "sdc_constraint"),
        ):
            with self.subTest(category=category):
                result = self.classify(diagnostic)
                self.assertEqual(result["handoff_owner"], "backend")
                self.assertEqual(result["failure_category"], category)

    def test_design_root_cause_takes_precedence_over_timing_symptom(self):
        result = self.classify("WNS -0.119 ns\nExcessive combinational depth in RTL.")
        self.assertEqual(result["handoff_owner"], "frontend")
        self.assertTrue(result["warnings"])

    def test_rtl_text_alone_does_not_trigger_design_handoff(self):
        result = triage.run_local_rtl_failure_triage(
            rtl_sources={"demo.v": "// poor logic design\nmodule demo; endmodule"}
        )
        self.assertEqual(result["handoff_owner"], "unknown")

    def test_retired_ai_categories_cannot_keep_frontend_ownership(self):
        for category in ("linting", "syntax_error", "code", "fsm_state_lockup", "unknown"):
            with self.subTest(category=category):
                result = triage._normalize_ai_result(
                    {"handoff_owner": "frontend", "failure_category": category}, model="test"
                )
                self.assertEqual(result["handoff_owner"], "unknown")

    def test_missing_api_key_keeps_local_design_diagnosis(self):
        with patch.dict(os.environ, {}, clear=True):
            result = triage.run_openai_rtl_failure_triage(
                logs={"stderr": "Poor logic design in the datapath."}, report_path=self.report_path,
            )
        self.assertEqual(result["handoff_owner"], "frontend")
        self.assertFalse(result["available"])

    def test_ai_request_uses_design_quality_policy_and_merges_result(self):
        ai_result = {
            "available": True, "enabled": True, "model": "test",
            "handoff_owner": "frontend", "failure_category": "poor_logic_design",
            "confidence": 0.98, "evidence": ["Serial mux chain requires RTL redesign."],
        }
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=object)}),
            patch.object(triage, "_build_client", return_value=object()),
            patch.object(triage, "_call_responses_api", return_value=(ai_result, "{}")) as call,
        ):
            result = triage.run_openai_rtl_failure_triage(
                logs={"stderr": "WNS -0.119"}, report_path=self.report_path,
            )
        report = self.report_path.read_text()
        self.assertIn("**Failure belongs to:** Frontend", report)
        self.assertIn("Serial mux chain requires RTL redesign.", report)
        self.assertEqual(result["handoff_owner"], "frontend")
        self.assertEqual(result["failure_category"], "poor_logic_design")
        self.assertIn("poor_logic_design", call.call_args.kwargs["system_prompt"])
        self.assertIn("Timing or congestion alone", call.call_args.kwargs["system_prompt"])
        categories = triage.RTL_FAILURE_TRIAGE_SCHEMA["properties"]["failure_category"]["enum"]
        self.assertIn("poor_logic_design", categories)
        self.assertNotIn("syntax_error", categories)


if __name__ == "__main__":
    unittest.main()
