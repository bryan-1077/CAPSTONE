import json
import io
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import debug_agent as agent
import debug_investigation as investigation


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.failure = self.root / "debug" / "failures" / "0001_test"
        for name in ("intake", "analysis", "result", "attempts"):
            (self.failure / name).mkdir(parents=True)
        (self.root / "rtl_output").mkdir()
        (self.root / "tb").mkdir()
        (self.root / "rtl_output" / "counter.sv").write_text("assign count = 0;\n")
        (self.root / "tb" / "counter.sv").write_text("assert(count == 1);\n")
        (self.failure / "intake" / "raw.log").write_text("FAIL count expected 1 actual 0\n")
        (self.failure / "analysis" / "parsed_failure.json").write_text("{}")
        self.record = investigation.new_investigation(
            self.root, self.failure, [{"file": "rtl_output/counter.sv"}], ["count"], 8, 24000)

    def gather(self):
        while self.record["pending"] and investigation.can_inspect(self.record):
            investigation.inspect(self.record, self.root)

    def assessment(self):
        return {
            "decision": "sufficient_evidence",
            "hypotheses": [{
                "summary": "Counter output is constant zero",
                "cause": "The constant-zero assignment prevents the checker from observing count equal to one.",
                "supporting_evidence": [item["id"] for item in self.record["evidence"]],
                "contradictory_evidence": [],
                "uncertainty": "The intended update timing still requires a target rerun.",
                "next_inspection": "Check the input stimulus timing to distinguish an update delay from constant output.",
            }],
            "unresolved_questions": [], "next_actions": [],
        }

    def test_supported_assessment_and_changed_checker(self):
        self.gather()
        investigation.accept_assessment(self.record, json.dumps(self.assessment()))
        self.assertEqual(self.record["decision"], "sufficient_evidence")
        self.assertTrue(investigation.evidence_unchanged(self.record, self.root))
        (self.root / "tb" / "counter.sv").write_text("assert(count == 0);\n")
        self.assertFalse(investigation.evidence_unchanged(self.record, self.root))

    def test_invalid_or_missing_citations_cannot_authorize_patch(self):
        self.gather()
        for references in (["invented"], [], [self.record["evidence"][0]["id"]]):
            with self.subTest(references=references):
                data = self.assessment()
                data["hypotheses"][0]["supporting_evidence"] = references
                with self.assertRaises(ValueError):
                    investigation.accept_assessment(self.record, json.dumps(data))
                self.assertEqual(self.record["decision"], "insufficient_evidence")

    def test_missing_cause_is_rejected(self):
        self.gather()
        data = self.assessment()
        data["hypotheses"][0]["cause"] = ""
        with self.assertRaises(ValueError):
            investigation.accept_assessment(self.record, json.dumps(data))

    def test_terminal_assessment_allows_no_next_inspection(self):
        self.gather()
        for decision in ("sufficient_evidence", "evidence_unavailable"):
            for value in ("missing", None, "", "  "):
                with self.subTest(decision=decision, value=value):
                    data = self.assessment()
                    data["decision"] = decision
                    if value == "missing":
                        del data["hypotheses"][0]["next_inspection"]
                    else:
                        data["hypotheses"][0]["next_inspection"] = value
                    investigation.accept_assessment(self.record, json.dumps(data))
                    self.assertEqual(self.record["decision"], decision)
                    self.assertIsInstance(self.record["hypotheses"][0]["next_inspection"], str)

    def test_optional_next_inspection_does_not_relax_evidence_validation(self):
        self.gather()
        for invalid in ("cause", "uncertainty", "supporting_evidence"):
            data = self.assessment()
            del data["hypotheses"][0]["next_inspection"]
            data["hypotheses"][0][invalid] = [] if invalid == "supporting_evidence" else ""
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                investigation.accept_assessment(self.record, json.dumps(data))

    def test_continuing_assessment_requires_next_inspection(self):
        self.gather()
        for value in (None, "", [], 123):
            data = self.assessment()
            data["decision"] = "insufficient_evidence"
            data["hypotheses"][0]["next_inspection"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                investigation.accept_assessment(self.record, json.dumps(data))

    def test_graph_proceeds_when_sufficient_response_omits_next_inspection(self):
        self.gather()
        self.persist()
        state = self.node_environment()
        data = self.assessment()
        del data["hypotheses"][0]["next_inspection"]
        with patch.object(agent, "call_patch_llm", return_value=json.dumps(data)):
            state.update(agent.graph_assess_evidence_node(state))
        self.assertEqual(agent.route_after_assessment(state), "build_patch_context")

    def test_compiler_failure_requires_cited_diagnostic_but_not_checker(self):
        diagnostic = "%Error-PINNOTFOUND: rtl_output/counter.sv:1:10: Pin not found: 'act_pulse'"
        (self.failure / "intake" / "raw.log").write_text(diagnostic + "\n")
        self.gather()
        parsed = {"verilator_diagnostics": [
            {"severity": "error", "file": "rtl_output/counter.sv", "line": 1, "raw": diagnostic}
        ]}
        investigation.configure_evidence_policy(self.record, parsed)
        data = self.assessment()
        data["hypotheses"][0]["supporting_evidence"] = [
            item["id"] for item in self.record["evidence"] if item["kind"] != "checker"
        ]
        investigation.accept_assessment(self.record, json.dumps(data))
        self.assertEqual(self.record["decision"], "sufficient_evidence")

        for item in self.record["evidence"]:
            if item["kind"] == "observation":
                item["text"] = "Compilation started"
        with self.assertRaises(ValueError):
            investigation.accept_assessment(self.record, json.dumps(data))

    def test_compiler_warning_or_mixed_behavioral_failure_still_needs_checker(self):
        self.gather()
        data = self.assessment()
        data["hypotheses"][0]["supporting_evidence"] = [
            item["id"] for item in self.record["evidence"] if item["kind"] != "checker"
        ]
        for severity, test_fail in (("warning", False), ("error", True)):
            with self.subTest(severity=severity, test_fail=test_fail):
                investigation.configure_evidence_policy(self.record, {
                    "status_markers": {"test_fail": test_fail},
                    "verilator_diagnostics": [{"severity": severity, "file": "rtl_output/counter.sv",
                                               "line": 1, "raw": "FAIL count expected 1 actual 0"}],
                })
                with self.assertRaises(ValueError):
                    investigation.accept_assessment(self.record, json.dumps(data))

    def test_read_is_catalog_restricted_and_budgeted(self):
        self.record["pending"] = [{"tool": "read", "path": "../../secret"}]
        investigation.inspect(self.record, self.root)
        self.assertEqual(self.record["actions"][0]["status"], "unavailable")
        self.assertEqual(self.record["evidence"], [])
        self.record["max_actions"] = 1
        self.assertFalse(investigation.can_inspect(self.record))

    def test_context_budget_records_omissions(self):
        self.record["max_chars"] = 60
        (self.root / "rtl_output" / "counter.sv").write_text("assign count = 0;\n" * 50)
        self.record["pending"] = [{"tool": "read", "path": "rtl_output/counter.sv", "end_line": 50}]
        investigation.inspect(self.record, self.root)
        self.assertFalse(investigation.can_inspect(self.record))
        self.assertTrue(self.record["omitted_context"])
        self.assertTrue(self.record["evidence"])
        self.assertLessEqual(self.record["chars_used"], 60)
        self.assertEqual(self.record["chars_used"], sum(len(e["text"]) for e in self.record["evidence"]))

    def test_first_inspection_reaches_logic_beyond_declarations(self):
        (self.root / "rtl_output" / "counter.sv").write_text(
            "logic count;\n" * 300 + "assign count = saved_count;\n")
        (self.failure / "intake" / "raw.log").write_text("compiler noise\n" * 250 + "FAIL count differs\n")
        (self.root / "tb" / "counter.sv").write_text(
            "// setup\n" * 400 + 'assert(count == 1) else $error("count differs");\n')
        record = investigation.new_investigation(
            self.root, self.failure, [{"file": "rtl_output/counter.sv", "matched_terms": ["count"]}],
            ["count"], 8, 24000,
            parsed={"testbench_errors": [{"message": "count differs", "log_line": 251}]})
        investigation.inspect(record, self.root)
        self.assertEqual(len(record["actions"]), 1)
        self.assertEqual({e["kind"] for e in record["evidence"]}, {"rtl", "checker", "observation"})
        text = "\n".join(e["text"] for e in record["evidence"])
        self.assertIn("assign count = saved_count", text)
        self.assertIn("251: FAIL", text)
        self.assertIn("401: assert", text)
        self.assertLess(record["chars_used"], 8000)

    def test_overlapping_reads_do_not_consume_budget_twice(self):
        path = "rtl_output/counter.sv"
        (self.root / path).write_text("\n".join(f"assign signal_{i} = 0;" for i in range(30)))
        self.record["pending"] = [
            {"tool": "read", "path": path, "start_line": 1, "end_line": 20},
            {"tool": "read", "path": path, "start_line": 10, "end_line": 30},
            {"tool": "read", "path": path, "start_line": 1, "end_line": 30},
        ]
        self.gather()
        locations = [line for e in self.record["evidence"] for line in range(e["start_line"], e["end_line"] + 1)]
        self.assertEqual(locations, list(range(1, 31)))
        self.assertEqual(self.record["actions"][-1]["status"], "no_new_evidence")
        self.assertEqual(self.record["chars_used"], sum(len(e["text"]) for e in self.record["evidence"]))
        sources = investigation.source_context(self.record)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["text"].splitlines(), [
            f"{i + 1}: assign signal_{i} = 0;" for i in range(30)])

    def test_complete_primary_file_and_checker_task_on_first_assessment(self):
        rtl = "module counter;\n" + "logic count;\n" * 500 + "assign count = 0;\nendmodule\n"
        (self.root / "rtl_output/counter.sv").write_text(rtl)
        checker = ('function int expected();\n  return 1;\nendfunction\n'
                   'task automatic check_count();\n' + '// task endtask in comment\n' * 40 +
                   '  assert(count == expected()) else $error("wrong count");\nendtask\n'
                   'initial begin\n  check_count();\nend\n')
        (self.root / "tb/counter.sv").write_text(checker)
        record = investigation.new_investigation(
            self.root, self.failure, [{"file": "rtl_output/counter.sv"}], ["count"], 8, 64000,
            parsed={"testbench_errors": [{"message": "wrong count"}]})
        investigation.inspect(record, self.root)
        rtl_evidence = [e for e in record["evidence"] if e["kind"] == "rtl"]
        self.assertEqual(len(rtl_evidence), 1)
        self.assertEqual((rtl_evidence[0]["start_line"], rtl_evidence[0]["end_line"]), (1, 503))
        checker_text = next(s["text"] for s in investigation.source_context(record) if s["path"].startswith("tb/"))
        for expected in ("task automatic check_count", "endtask", "return 1", "check_count();"):
            self.assertIn(expected, checker_text)
        self.assertLessEqual(record["chars_used"], 64000)

    def test_oversized_primary_falls_back_to_logic_without_starving_checker(self):
        (self.root / "rtl_output/counter.sv").write_text("logic unused;\n" * 5000 + "assign count = 0;\n")
        investigation.inspect(self.record, self.root)
        self.assertEqual({e["kind"] for e in self.record["evidence"]}, {"rtl", "checker", "observation"})
        self.assertIn("assign count", "\n".join(e["text"] for e in self.record["evidence"]))
        self.assertTrue(any("exceeds allocation" in e["reason"] for e in self.record["omitted_context"]))

    def test_supported_partial_diagnosis_can_proceed_with_unresolved_symptoms(self):
        self.gather()
        data = self.assessment()
        data["unresolved_questions"] = ["A separate latency failure remains unexplained."]
        investigation.accept_assessment(self.record, json.dumps(data))
        self.assertEqual(self.record["decision"], "sufficient_evidence")
        prompt = investigation.assessment_prompt(self.record, "Multiple failures")
        self.assertIn("Do not require one cause to explain every symptom", prompt)
        self.assertIn("source_context", prompt)

    def test_checker_reads_leave_room_for_rtl(self):
        (self.root / "tb/counter.sv").write_text("assert(count == 1);\n" * 200)
        self.record["max_chars"] = 1000
        self.record["pending"] = [
            {"tool": "read", "path": "tb/counter.sv", "start_line": 1, "end_line": 200},
            {"tool": "read", "path": "rtl_output/counter.sv", "start_line": 1, "end_line": 10},
        ]
        self.gather()
        self.assertLessEqual(sum(len(e["text"]) for e in self.record["evidence"] if e["kind"] == "checker"), 400)
        self.assertTrue(any(e["kind"] == "rtl" for e in self.record["evidence"]))

    def test_scoped_search_does_not_read_checker_or_logs(self):
        self.record["pending"] = [{"tool": "search", "terms": ["count"], "paths": ["rtl_output/counter.sv"]}]
        investigation.inspect(self.record, self.root)
        self.assertEqual({e["kind"] for e in self.record["evidence"]}, {"rtl"})

    def test_search_reaches_unseen_assignments_and_repeated_search_progresses(self):
        path = "rtl_output/counter.sv"
        lines = ["logic target;" for _ in range(100)] + [
            "assign target = source;" if i % 10 == 0 else "// unrelated" for i in range(200)]
        (self.root / path).write_text("\n".join(lines))
        request = {"tool": "search", "paths": [path], "terms": ["target"]}
        self.record["pending"] = [request, request]
        investigation.inspect(self.record, self.root)
        first = self.record["chars_used"]
        self.assertIn("assign target = source", "\n".join(e["text"] for e in self.record["evidence"]))
        first_lines = self.record["actions"][0]["search_results"][0]["selected_lines"]
        self.assertTrue(all(number > 100 for number in first_lines))
        investigation.inspect(self.record, self.root)
        self.assertGreater(self.record["chars_used"], first)
        next_lines = self.record["actions"][1]["search_results"][0]["selected_lines"]
        self.assertFalse(set(first_lines) & set(next_lines))

    def test_development_log_identifies_added_context(self):
        investigation.inspect(self.record, self.root)
        state = self.node_environment()
        with patch.object(agent, "log_debug") as log:
            agent.save_investigation(state, self.record, "investigate")
        text = "\n".join(call.args[1] for call in log.call_args_list)
        self.assertIn("Inspection request:", text)
        self.assertIn("rtl_output/counter.sv:1-1", text)
        self.assertIn("Evidence E1", text)

    def test_section_headers_in_terminal_and_log(self):
        output = io.StringIO()
        with redirect_stdout(output):
            agent.log_debug(self.failure, "INVESTIGATION 2/8", section=True)
            agent.log_debug(self.failure, "Collected evidence")
            agent.log_debug(None, "DEBUG RESULT", section=True)
        text = output.getvalue()
        self.assertIn("\n===== INVESTIGATION 2/8 =====\n", text)
        self.assertIn("[debug:debug] Collected evidence", text)
        self.assertIn("\n===== DEBUG RESULT =====\n", text)
        saved = (self.failure / "debug.log").read_text()
        self.assertIn("===== INVESTIGATION 2/8 =====", saved)
        self.assertIn("[debug:debug] Collected evidence", saved)

    def test_empty_api_response_saves_metadata_without_credentials(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": ""}}],
                                      "usage": {"completion_tokens": 4096}}
        destination = self.failure / "analysis/assessment_001.api.json"
        with patch.dict(os.environ, {"TAMUS_AI_CHAT_API_KEY": "test-secret"}), \
                patch("requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "finish_reasons=.*length"):
                agent.call_patch_llm("test prompt", "test-model", diagnostic_path=destination)
        metadata = json.loads(destination.read_text())
        self.assertEqual(metadata["finish_reasons"], ["length"])
        self.assertEqual(metadata["content_characters"], 0)
        self.assertNotIn("test-secret", destination.read_text())

    def test_model_can_expand_inspection(self):
        self.gather()
        data = self.assessment()
        data.update(decision="insufficient_evidence", next_actions=[
            {"tool": "read", "path": "tb/counter.sv", "start_line": 1, "end_line": 5}])
        investigation.accept_assessment(self.record, json.dumps(data))
        investigation.inspect(self.record, self.root)
        self.assertEqual(self.record["evidence"][-1]["kind"], "checker")

    def node_environment(self):
        for name, value in (("SCRIPT_DIR", self.root), ("RTL_OUTPUT_DIR", self.root / "rtl_output")):
            context = patch.object(agent, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(agent, "graph_node_update", side_effect=lambda state, update, node: update)
        context.start()
        self.addCleanup(context.stop)
        return {"failure_dir": str(self.failure), "repair": True,
                "investigation": {"artifact": "analysis/investigation.json"}, "steps": [], "budgets": {}}

    def persist(self):
        (self.failure / "analysis" / "investigation.json").write_text(json.dumps(self.record))

    def test_assessment_routes_and_api_failure(self):
        self.gather()
        self.persist()
        state = self.node_environment()
        with patch.object(agent, "call_patch_llm", return_value=json.dumps(self.assessment())):
            state.update(agent.graph_assess_evidence_node(state))
        self.assertEqual(agent.route_after_assessment(state), "build_patch_context")
        state["repair"] = False
        self.assertEqual(agent.route_after_assessment(state), "record_result")
        self.record["decision"] = "insufficient_evidence"
        self.persist()
        with patch.object(agent, "call_patch_llm", side_effect=RuntimeError("API unavailable")):
            state.update(agent.graph_assess_evidence_node(state))
        self.assertEqual(state["status"]["status"], "needs_human")
        self.assertEqual(state["stop_reason"], "assessment_failed")

    def test_offline_and_stale_evidence_never_reach_proposal(self):
        self.record["max_actions"] = 1
        investigation.inspect(self.record, self.root)
        self.persist()
        state = self.node_environment()
        state["offline"] = True
        with patch.object(agent, "call_patch_llm") as llm:
            state.update(agent.graph_assess_evidence_node(state))
            llm.assert_not_called()
        self.assertEqual(state["stop_reason"], "investigation_steps_exhausted")
        self.assertEqual(agent.route_after_assessment(state), "record_result")
        (self.failure / "intake" / "raw.log").write_text("changed\n")
        state.update(agent.graph_investigate_node(state))
        self.assertEqual(state["stop_reason"], "stale_inputs")

    def test_changed_evidence_blocks_context_and_apply(self):
        self.gather()
        investigation.accept_assessment(self.record, json.dumps(self.assessment()))
        self.persist()
        state = self.node_environment()
        (self.root / "rtl_output" / "counter.sv").write_text("assign count = 1;\n")
        with patch.object(agent, "apply_attempt") as apply:
            state.update(agent.graph_apply_patch_node(state))
            apply.assert_not_called()
        self.assertEqual(state["status"]["status"], "needs_human")

    def test_real_graph_and_fallback_investigate_before_proposal(self):
        def assess(*args, **kwargs):
            self.record = json.loads((self.failure / "analysis" / "investigation.json").read_text())
            if {e["kind"] for e in self.record["evidence"]} >= {"rtl", "checker", "observation"}:
                return json.dumps(self.assessment())
            return json.dumps({"decision": "insufficient_evidence", "hypotheses": [],
                               "unresolved_questions": ["Need RTL and checker source"], "next_actions": []})

        with ExitStack() as stack:
            stack.enter_context(patch.object(agent, "SCRIPT_DIR", self.root))
            stack.enter_context(patch.object(agent, "resolve_failure_dir",
                                             side_effect=lambda name: self.root / name))
            stack.enter_context(patch.object(agent, "RTL_OUTPUT_DIR", self.root / "rtl_output"))
            stack.enter_context(patch.object(agent, "parsed_failure_terms", return_value=["count"]))
            stack.enter_context(patch.object(agent, "call_patch_llm", side_effect=assess))
            for name in ("run_or_ingest", "parse_failure", "classify_failure", "localize_rtl", "write_patch_plan"):
                stack.enter_context(patch.object(agent, f"graph_{name}_node", return_value={}))
            context = stack.enter_context(patch.object(agent, "graph_build_patch_context_node", return_value={}))
            proposal = stack.enter_context(patch.object(agent, "graph_propose_patch_node",
                                                        return_value={"stop_reason": "test_proposal_complete"}))
            runners = [agent.run_fallback_graph]
            try:
                runners.append(agent.build_langgraph_runner())
            except ImportError:
                pass
            for runner in runners:
                with self.subTest(runner=runner):
                    proposal.reset_mock()
                    context.reset_mock()
                    state = {"failure_dir": str(self.failure), "repair": True, "steps": [],
                             "investigation": {}, "suspects": [{"file": "rtl_output/counter.sv"}],
                             "budgets": {"investigation_steps_max": 8}}
                    result = runner(state)
                    self.assertEqual(result["investigation"]["decision"], "sufficient_evidence")
                    self.assertEqual(result["budgets"]["investigation_steps_used"], 1)
                    self.assertEqual(result["budgets"]["investigation_assessments_used"], 1)
                    self.assertEqual(result["hypotheses"][0]["summary"], "Counter output is constant zero")
                    context.assert_called_once()
                    proposal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
