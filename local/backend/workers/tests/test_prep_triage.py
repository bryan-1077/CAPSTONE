import copy
import json
import unittest
from unittest.mock import Mock, patch

from workers import orchestrator as flow


class PrepTriageTests(unittest.TestCase):
    def setUp(self):
        self.state = {"logs": {}, "history": [], "validation": {}, "remote_prepare_dir": "prep"}
        self.diagnosis = {
            "handoff_owner": "unknown", "failure_category": "unknown",
            "summary": "Missing dependency requires investigation.",
        }
        patcher = patch.object(flow, "run_openai_rtl_failure_triage", return_value=self.diagnosis)
        self.triage = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(flow, "_emit_live_progress")
        self.progress = patcher.start()
        self.addCleanup(patcher.stop)

    def review(self, payload, *, ok=True, raw=None, label="1", merged=None):
        result = {
            "ok": ok, "exit_code": 0 if ok else 1, "command": "review",
            "stdout": json.dumps(payload) if raw is None else raw,
            "stderr": "" if ok else "review process failed",
        }
        with (
            patch.object(flow, "_build_prepare_review_command", return_value="review"),
            patch.object(flow, "run_openai_prep_review", return_value={}),
            patch.object(flow, "merge_prep_review_results", side_effect=lambda **kw: copy.deepcopy(merged if merged is not None else kw['local_review'])),
        ):
            return flow._run_rtl_prep_review(self.state, Mock(run=Mock(return_value=result)), attempt=1, log_label=label)

    def test_individual_review_failures_defer_triage(self):
        for options in (
            {}, {"ok": False}, {"raw": "not JSON"}, {"label": "1_post_fix"},
            {"merged": {"pass": False, "errors": ["AI-only error"], "context": {}}},
        ):
            with self.subTest(options=options):
                updates, passed = self.review({"pass": False, "errors": ["Missing module"]}, **options)
                self.assertFalse(passed)
                self.assertIn("rtl_prep_review", updates["validation"])
                self.triage.assert_not_called()

    def test_passing_review_skips_triage(self):
        updates, passed = self.review({"pass": True, "errors": [], "warnings": ["Non-blocking"]})
        self.assertTrue(passed)
        self.triage.assert_not_called()

    def test_generation_failure_triggers(self):
        result = {"ok": False, "exit_code": 2, "stdout": "", "stderr": "missing input ZIP", "command": "generate"}
        with patch.object(flow, "_build_prepare_command", return_value="generate"):
            updates, passed = flow._run_rtl_prep_generation(self.state, Mock(run=Mock(return_value=result)), attempt=1)
        self.assertFalse(passed)
        self.triage.assert_called_once()
        self.assertEqual(updates["rtl_failure_triage_payload"], self.diagnosis)
        self.assertIn("rtl_prep_rtl_failure_triage_generate_1", updates["logs"])
        self.assertEqual(updates["validation"]["rtl_prep_generation"]["rtl_failure_triage"], self.diagnosis)

    def run_loop(self, *, fix_applied, pass_label=None, invalid_output=False):
        state = {
            **self.state, "stage_status": {"rtl_prep": "running"}, "outputs": {},
            "prep_max_iterations": 3, "rtl_failure_triage_payload": {"stale": True},
            "rtl_failure_handoff_owner": "frontend", "rtl_failure_category": "poor_logic_design",
        }
        events = []
        real_review = flow._run_rtl_prep_review
        def review(state, ssh, *, attempt, log_label):
            events.append("review:" + log_label)
            passed = log_label == pass_label
            payload = {"pass": passed, "errors": [] if passed else ["Missing dependency " + log_label], "context": {}}
            stdout = "invalid JSON " + log_label if invalid_output else json.dumps(payload)
            ssh.run.return_value = {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": "", "command": "review"}
            return real_review(state, ssh, attempt=attempt, log_label=log_label)
        def edit(*args, attempt, **kwargs):
            events.append(f"edit:{attempt}")
            return {}, fix_applied
        self.triage.side_effect = lambda **kw: events.append("triage") or self.diagnosis
        with (
            patch.object(flow, "_run_rtl_prep_generation", return_value=({}, True)),
            patch.object(flow, "_build_prepare_review_command", return_value="review"),
            patch.object(flow, "run_openai_prep_review", return_value={}),
            patch.object(flow, "merge_prep_review_results", side_effect=lambda **kw: copy.deepcopy(kw["local_review"])),
            patch.object(flow, "_run_rtl_prep_review", side_effect=review),
            patch.object(flow, "_run_rtl_prep_fix", side_effect=edit),
        ):
            output = flow._make_rtl_prep_runner(Mock())(state)
        return output, events

    def test_three_edits_and_post_fix_reviews_before_one_triage(self):
        output, events = self.run_loop(fix_applied=True)
        self.assertEqual(events, [
            "review:1", "edit:1", "review:1_post_fix",
            "review:2", "edit:2", "review:2_post_fix",
            "review:3", "edit:3", "review:3_post_fix", "triage",
        ])
        self.triage.assert_called_once()
        self.assertEqual(output["stage_status"]["rtl_prep"], "failed")
        self.assertEqual(output["rtl_failure_triage_payload"], self.diagnosis)
        self.assertEqual(output["validation"]["rtl_prep_review"]["rtl_failure_triage"], self.diagnosis)
        self.assertIn("rtl_prep_rtl_failure_triage_exhausted", output["logs"])
        request = self.triage.call_args.kwargs
        self.assertIn("Missing dependency 3_post_fix", request["logs"]["stdout"])
        self.assertEqual(request["local_review"]["prep_attempts_exhausted"], 3)

    def test_no_edits_still_uses_three_attempts(self):
        output, events = self.run_loop(fix_applied=False)
        self.assertEqual(events, ["review:1", "edit:1", "review:2", "edit:2", "review:3", "edit:3", "triage"])
        self.triage.assert_called_once()
        self.assertIn("3 iterations", output["last_error"])

    def test_third_post_fix_success_skips_triage(self):
        output, events = self.run_loop(fix_applied=True, pass_label="3_post_fix")
        self.assertEqual(events[-2:], ["edit:3", "review:3_post_fix"])
        self.triage.assert_not_called()
        self.assertEqual(output["stage_status"]["rtl_prep"], "success")
        self.assertEqual(output["rtl_failure_triage_payload"], {})
        self.assertIsNone(output["rtl_failure_handoff_owner"])

    def test_first_review_success_skips_edits_and_triage(self):
        output, events = self.run_loop(fix_applied=True, pass_label="1")
        self.assertEqual(events, ["review:1"])
        self.triage.assert_not_called()
        self.assertEqual(output["stage_status"]["rtl_prep"], "success")

    def test_unreadable_output_triaged_only_after_attempts(self):
        output, events = self.run_loop(fix_applied=False, invalid_output=True)
        self.assertEqual(events[-2:], ["edit:3", "triage"])
        self.assertEqual(len(events), 7)
        self.triage.assert_called_once()
        self.assertEqual(self.triage.call_args.kwargs["logs"]["stdout"], "invalid JSON 3")


if __name__ == "__main__":
    unittest.main()
