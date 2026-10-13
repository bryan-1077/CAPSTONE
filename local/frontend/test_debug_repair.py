"""Regression coverage for premature patching and failed-repair reassessment."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import debug_agent as agent
import debug_investigation as investigation


def assessment(actions=None, next_inspection=""):
    return json.dumps({
        "decision": "sufficient_evidence",
        "hypotheses": [{"summary": "Scheduler cannot issue", "cause": "Input may be undriven",
                        "uncertainty": "Need to check wiring", "next_inspection": next_inspection,
                        "supporting_evidence": ["E1", "E2", "E3"], "contradictory_evidence": []}],
        "next_actions": actions or [], "unresolved_questions": [],
    })


def record():
    return {"evidence": [{"id": f"E{i}", "kind": kind, "text": "source"}
                         for i, kind in enumerate(("rtl", "observation", "checker"), 1)],
            "actions": [], "pending": [], "decision": "insufficient_evidence"}


class EvidenceTests(unittest.TestCase):
    def test_requested_wiring_inspection_blocks_patching(self):
        data = record()
        action = {"tool": "search", "terms": ["timing_ok"], "paths": ["tb/top.sv"]}
        investigation.accept_assessment(data, assessment([action]))
        self.assertEqual(data["decision"], "insufficient_evidence")
        self.assertEqual(data["pending"][0], action)
        self.assertEqual(agent.route_after_assessment({"investigation": data, "repair": True}), "investigate")

    def test_primary_hypothesis_inspection_blocks_patching(self):
        data = record()
        investigation.accept_assessment(data, assessment(next_inspection="Check the input connection"))
        self.assertEqual(data["decision"], "insufficient_evidence")

    def test_independent_question_does_not_block_supported_repair(self):
        response = json.loads(assessment())
        response["unresolved_questions"] = ["A separate row-hit failure remains unexplained"]
        data = record()
        investigation.accept_assessment(data, json.dumps(response))
        self.assertEqual(data["decision"], "sufficient_evidence")

    def test_triage_includes_connection_and_all_failure_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            (root / "rtl_output/scheduler.sv").write_text("module scheduler; endmodule")
            (root / "tb").mkdir()
            lines = ["// padding"] * 170
            lines[114] = ".timing_ok(1'b1),"
            lines += ["task automatic checks;"]
            lines += [f'`CHECK(0, "failure {i}")' for i in range(4)]
            lines += ["endtask"]
            (root / "tb/top.sv").write_text("\n".join(lines))
            failure = root / "debug/failure"
            (failure / "intake").mkdir(parents=True)
            (failure / "intake/raw.log").write_text("\n".join(f"[ERROR] failure {i}" for i in range(4)))
            data = investigation.new_investigation(root, failure, [{"file": "rtl_output/scheduler.sv"}], [],
                8, 64000, {"testbench_errors": [{"message": f"failure {i}", "log_line": i + 1} for i in range(4)]})
            investigation.inspect(data, root)
            evidence = "\n".join(item["text"] for item in data["evidence"])
            self.assertIn(".timing_ok(1'b1)", evidence)
            for i in range(4):
                self.assertIn(f"failure {i}", evidence)


class RetryTests(unittest.TestCase):
    def test_failed_checks_and_empty_patch_route_to_reassessment(self):
        failed = {"stop_reason": "checks_failed", "status": {
            "status": "target_failed", "rtl_modified": False, "rollback": {"passed": True}}}
        self.assertEqual(agent.route_after_apply(failed), "reassess_repair")
        self.assertEqual(agent.route_after_validate({"validation": {"status": "patch_rejected"}}), "reassess_repair")
        failed["status"]["rollback"]["passed"] = False
        self.assertEqual(agent.route_after_apply(failed), "record_result")

    def test_previous_attempts_include_response_and_diagnostics_after_long_build(self):
        with tempfile.TemporaryDirectory() as directory:
            failure = Path(directory)
            attempt = failure / "attempts/attempt_01"
            attempt.mkdir(parents=True)
            (attempt / "patch.diff").write_text("")
            (attempt / "response.md").write_text("NO_PATCH: need timing_ok wiring")
            (attempt / "target.log").write_text("g++ compiling\n" * 2000 +
                "[ERROR] Scheduler should select row-hit req[1]\nTEST FAIL\nErrors: 1\n")
            context = agent.collect_previous_attempts_context(failure)
            self.assertIn("need timing_ok wiring", context[0]["response_excerpt"])
            self.assertIn("row-hit req[1]", context[0]["check_diagnostics"]["target"])
            self.assertNotIn("g++", context[0]["check_diagnostics"]["target"])

    def test_reassessment_passes_failed_patch_feedback_and_clears_readiness(self):
        data = record()
        data.update(decision="sufficient_evidence", stop_reason="old")
        feedback = [{"attempt": "attempt_01", "check_diagnostics": {"target": "TEST FAIL"}}]
        with patch.object(agent, "graph_failure_dir", return_value=Path("/tmp/failure")), \
             patch.object(agent, "investigation_record", return_value=data), \
             patch.object(agent, "collect_previous_attempts_context", return_value=feedback), \
             patch.object(agent, "numbered_attempt_dirs", return_value=[Path("attempt_01")]), \
             patch.object(agent, "save_investigation", return_value={"stop_reason": ""}), \
             patch.object(agent, "log_debug"), \
             patch.object(agent, "graph_assess_evidence_node", return_value={"assessed": True}) as assess:
            result = agent.graph_reassess_repair_node({"max_attempts": 3})
        self.assertEqual(data["repair_feedback"], feedback)
        self.assertEqual(data["decision"], "insufficient_evidence")
        self.assertNotIn("stop_reason", data)
        self.assertTrue(result["assessed"])
        assess.assert_called_once()

    def test_exhausted_repair_budget_stops_before_another_model_call(self):
        data = record()
        data["unresolved_questions"] = []
        with patch.object(agent, "graph_failure_dir", return_value=Path("/tmp/failure")), \
             patch.object(agent, "investigation_record", return_value=data), \
             patch.object(agent, "collect_previous_attempts_context", return_value=[]), \
             patch.object(agent, "numbered_attempt_dirs", return_value=[Path("attempt_01")]), \
             patch.object(agent, "save_investigation", return_value={"stop_reason": "repair_attempts_exhausted"}), \
             patch.object(agent, "graph_assess_evidence_node") as assess:
            result = agent.graph_reassess_repair_node({"max_attempts": 1})
        self.assertEqual(result["stop_reason"], "repair_attempts_exhausted")
        assess.assert_not_called()

    def test_langgraph_conditional_destinations_exist(self):
        # Validate graph wiring without installing LangGraph or calling a model.
        import sys
        import types
        from typing import get_args, get_type_hints
        class Graph:
            def __init__(self, schema):
                self.nodes = set()
                self.routes = []
            def add_node(self, name, function):
                self.nodes.add(name)
            def add_edge(self, source, destination):
                pass
            def add_conditional_edges(self, source, function):
                self.routes.append((source, function))
            def compile(self, **kwargs):
                for source, function in self.routes:
                    for destination in get_args(get_type_hints(function)["return"]):
                        if destination not in self.nodes:
                            raise AssertionError(f"Unknown destination {source} -> {destination}")
                return self
        modules = {name: types.ModuleType(name) for name in (
            "langgraph", "langgraph.checkpoint", "langgraph.checkpoint.memory", "langgraph.graph")}
        modules["langgraph.checkpoint.memory"].MemorySaver = lambda: None
        modules["langgraph.graph"].StateGraph = Graph
        modules["langgraph.graph"].START = "START"
        modules["langgraph.graph"].END = "END"
        with patch.dict(sys.modules, modules):
            self.assertTrue(callable(agent.build_langgraph_runner()))

    def test_fallback_reinvestigates_before_second_proposal(self):
        events = []
        def node(name, result):
            def run(state):
                events.append(name)
                return result
            return run
        initial = ["run_or_ingest", "parse_failure", "classify_failure", "localize_rtl", "write_patch_plan"]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in initial:
                stack.enter_context(patch.object(agent, f"graph_{name}_node", node(name, {})))
            stack.enter_context(patch.object(agent, "route_after_patch_plan", return_value="investigate"))
            stack.enter_context(patch.object(agent, "graph_investigate_node", node("inspect", {})))
            stack.enter_context(patch.object(agent, "graph_assess_evidence_node", node("assess", {
                "investigation": {"decision": "sufficient_evidence"}, "stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_build_patch_context_node", node("context", {"stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_propose_patch_node", node("propose", {"attempt_dir": "attempt", "stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_validate_patch_node", node("validate", {"validation": {"can_apply": True}})))
            stack.enter_context(patch.object(agent, "graph_apply_patch_node", side_effect=[{
                "stop_reason": "checks_failed", "status": {"status": "target_failed", "rtl_modified": False,
                "rollback": {"passed": True}}}, {"stop_reason": "", "status": {"status": "fixed"}}]))
            stack.enter_context(patch.object(agent, "graph_reassess_repair_node", node("reassess", {
                "stop_reason": "", "investigation": {"decision": "insufficient_evidence"}})))
            stack.enter_context(patch.object(agent, "graph_record_result_node", node("result", {})))
            agent.run_fallback_graph({"repair": True})
        first = events.index("propose")
        second = events.index("propose", first + 1)
        self.assertEqual(events[first + 1:second], ["validate", "reassess", "inspect", "assess", "context"])


if __name__ == "__main__":
    unittest.main()
