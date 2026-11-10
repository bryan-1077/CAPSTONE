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
        self.edit_states = []
        self.review_requests = []
        real_review = flow._run_rtl_prep_review
        def review(state, ssh, *, attempt, log_label):
            events.append("review:" + log_label)
            passed = log_label == pass_label
            payload = {"pass": passed, "errors": [] if passed else ["Missing dependency " + log_label], "context": {}}
            stdout = "invalid JSON " + log_label if invalid_output else json.dumps(payload)
            ssh.run.return_value = {"ok": True, "exit_code": 0, "stdout": stdout, "stderr": "", "command": "review"}
            return real_review(state, ssh, attempt=attempt, log_label=log_label)
        def edit(state, *args, attempt, **kwargs):
            events.append(f"edit:{attempt}")
            self.edit_states.append(copy.deepcopy(state))
            return {}, fix_applied
        def ai_review(**kwargs):
            self.review_requests.append(copy.deepcopy(kwargs["local_review"]))
            return {}
        self.triage.side_effect = lambda **kw: events.append("triage") or self.diagnosis
        with (
            patch.object(flow, "_run_rtl_prep_generation", return_value=({}, True)),
            patch.object(flow, "_build_prepare_review_command", return_value="review"),
            patch.object(flow, "run_openai_prep_review", side_effect=ai_review),
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

    def backend_diagnosis(self):
        self.diagnosis.update({
            "handoff_owner": "backend", "failure_category": "sdc_constraint",
            "summary": "Top selection needs a prep repair.",
            "suggested_next_steps": ["Repair top selection in the prepared Tcl."],
        })

    def test_backend_triage_reaches_both_agents_and_recovers(self):
        self.backend_diagnosis()
        output, events = self.run_loop(fix_applied=True, pass_label="triage_1_post_fix")
        self.assertEqual(events[-4:], ["triage", "review:triage_1", "edit:4", "review:triage_1_post_fix"])
        self.assertEqual(self.edit_states[-1]["rtl_failure_triage_payload"], self.diagnosis)
        self.assertEqual(self.review_requests[-1]["context"]["rtl_failure_triage"], self.diagnosis)
        self.assertEqual(output["stage_status"]["rtl_prep"], "success")
        self.assertTrue(output["prep_review_passed"])
        self.assertIsNone(output["last_error"])
        self.assertIsNone(output["rtl_failure_handoff_owner"])
        self.assertEqual(output["rtl_failure_triage_payload"], {})
        self.assertIn("rtl_bundle_remote", output["outputs"])
        self.assertIn("rtl_prep_rtl_failure_triage_exhausted", output["logs"])
        self.triage.assert_called_once()

    def test_backend_recovery_is_bounded_and_keeps_latest_review(self):
        self.backend_diagnosis()
        output, events = self.run_loop(fix_applied=True)
        self.assertEqual(len([event for event in events if event.startswith("edit:")]), 6)
        self.assertEqual(events[-1], "review:triage_3_post_fix")
        self.assertEqual(output["stage_status"]["rtl_prep"], "failed")
        self.assertIn("3 triage-guided repair attempts", output["last_error"])
        self.assertEqual(output["validation"]["rtl_prep_review"]["errors"], ["Missing dependency triage_3_post_fix"])
        self.triage.assert_called_once()

    def test_backend_recovery_no_actions_still_stops(self):
        self.backend_diagnosis()
        output, events = self.run_loop(fix_applied=False)
        self.assertEqual(events[-2:], ["review:triage_3", "edit:6"])
        self.assertEqual(output["stage_status"]["rtl_prep"], "failed")
        self.triage.assert_called_once()

    def test_frontend_diagnosis_does_not_trigger_prep_recovery(self):
        self.diagnosis.update({"handoff_owner": "frontend", "failure_category": "poor_logic_design"})
        output, events = self.run_loop(fix_applied=True)
        self.assertEqual(events[-1], "triage")
        self.assertEqual(output["rtl_failure_handoff_owner"], "frontend")
        self.assertEqual(len(self.edit_states), 3)

    def test_editor_receives_triage_in_its_request_context(self):
        self.backend_diagnosis()
        state = {
            **self.state, "rtl_failure_triage_payload": self.diagnosis,
            "validation": {"rtl_prep_review": {"errors": ["bad top"], "context": {"key_files": {"top_module.txt": "top"}}}},
        }
        with patch.object(flow, "run_openai_prep_edit", return_value={"actions": []}) as editor:
            _, applied = flow._run_rtl_prep_fix(state, Mock(), attempt=4, log_label="triage_1")
        self.assertFalse(applied)
        context = editor.call_args.kwargs["review_payload"]["context"]
        self.assertEqual(context["rtl_failure_triage"], self.diagnosis)
        self.assertEqual(context["key_files"], {"top_module.txt": "top"})


if __name__ == "__main__":
    unittest.main()
