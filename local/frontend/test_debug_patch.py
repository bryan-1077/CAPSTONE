import argparse
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import debug_agent as agent


class BareHunkTests(unittest.TestCase):
    def normalize(self, root, text):
        with patch.object(agent, "SCRIPT_DIR", root), patch.object(agent, "RTL_OUTPUT_DIR", root / "rtl_output"):
            return agent.normalize_patch_text(text)

    def test_multiple_hunks_with_line_offset_pass_git_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            source = root / "rtl_output/test.sv"
            original = "a\nb\nc\nd\ne\nf\ng\nh\ni\n"
            source.write_text(original)
            text = "--- a/rtl_output/test.sv\n+++ b/rtl_output/test.sv\n@@\n a\n-b\n+B\n+extra\n c\n@@\n g\n-h\n+H\n"
            normalized, warnings = self.normalize(root, text)
            self.assertIn("@@ -1,9 +1,10 @@", normalized)
            self.assertIn("\n i\n", normalized)
            checked = subprocess.run(["git", "apply", "--check", "-"], input=normalized,
                                     text=True, capture_output=True, cwd=root)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(source.read_text(), original)
            self.assertTrue(any("unique exact" in warning for warning in warnings))
            self.assertEqual(self.normalize(root, normalized)[0], normalized)
            applied = subprocess.run(["git", "apply", "-"], input=normalized,
                                     text=True, capture_output=True, cwd=root)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual(source.read_text(), "a\nB\nextra\nc\nd\ne\nf\ng\nH\ni\n")

    def test_unsafe_or_unmatched_context_is_not_reconstructed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            (root / "rtl_output/test.sv").write_text("repeat\nrepeat\n")
            for name, body in (("rtl_output/test.sv", "-repeat\n+new\n"),
                               ("rtl_output/test.sv", "-missing\n+new\n"),
                               ("rtl_output/test.sv", "+insertion\n"),
                               ("../outside.sv", "-old\n+new\n")):
                with self.subTest(name=name, body=body):
                    text = f"--- a/{name}\n+++ b/{name}\n@@\n{body}"
                    normalized, warnings = self.normalize(root, text)
                    self.assertIn("\n@@\n", normalized)
                    self.assertTrue(any("Cannot reconstruct" in warning for warning in warnings))


class GitPatchTests(unittest.TestCase):
    def test_successful_exit_without_changes_stops_before_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = root / "debug/failures/0001_test"
            for name in ("intake", "analysis", "attempts", "result"):
                (failure / name).mkdir(parents=True)
            attempt = failure / "attempts/attempt_01"
            attempt.mkdir()
            (attempt / "patch.diff").write_text("unused\n")
            source = root / "rtl_output/test.sv"
            source.parent.mkdir()
            source.write_text("module test; endmodule\n")
            validation = {"can_apply": True, "status": "patch_valid", "warnings": [],
                          "touched_files": ["rtl_output/test.sv"]}
            with patch.object(agent, "SCRIPT_DIR", root), \
                    patch.object(agent, "validate_patch_text", return_value=validation), \
                    patch.object(agent, "git_patch_command", return_value=["true"]), \
                    patch.object(agent, "run_named_check") as check:
                rc = agent.apply_attempt(argparse.Namespace(attempt_dir=str(attempt),
                    lint_command="true", target_command="true", keep_failed_patch=False, demo_mode=False))
            self.assertEqual(rc, 1)
            check.assert_not_called()
            status = json.loads((attempt / "apply.json").read_text())
            self.assertEqual(status["status"], "apply_failed")
            self.assertFalse(status["rtl_modified"])

    def test_nested_worktree_apply_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            root = repo / "local/frontend"
            source = root / "rtl_output/test.sv"
            source.parent.mkdir(parents=True)
            source.write_text("module test;\nwire a;\nendmodule\n")
            diff = root / "patch.diff"
            diff.write_text("--- a/rtl_output/test.sv\n+++ b/rtl_output/test.sv\n"
                            "@@ -1,3 +1,3 @@\n module test;\n-wire a;\n+wire b;\n endmodule\n")
            with patch.object(agent, "SCRIPT_DIR", root), \
                    patch.object(agent, "RTL_OUTPUT_DIR", source.parent):
                validation = agent.validate_patch_text(diff.read_text(), diff)
                self.assertTrue(validation["can_apply"], validation)
                before = agent.collect_file_hashes(["rtl_output/test.sv"])
                subprocess.run(agent.git_patch_command(diff), cwd=root, check=True, capture_output=True)
                self.assertIn("wire b;", source.read_text())
                rollback = agent.rollback_applied_patch(root, diff, ["rtl_output/test.sv"], before)
                self.assertTrue(rollback["passed"], rollback)
                self.assertIn("wire a;", source.read_text())


class GraphRetryTests(unittest.TestCase):
    def test_failed_checks_retry_in_both_backends(self):
        with ExitStack() as stack:
            for name in ("run_or_ingest", "parse_failure", "classify_failure", "localize_rtl",
                         "write_patch_plan", "investigate", "assess_evidence", "build_patch_context",
                         "record_result"):
                stack.enter_context(patch.object(agent, f"graph_{name}_node", return_value={}))
            stack.enter_context(patch.object(agent, "route_after_patch_plan", return_value="investigate"))
            stack.enter_context(patch.object(agent, "route_after_assessment", return_value="build_patch_context"))
            propose = stack.enter_context(patch.object(agent, "graph_propose_patch_node",
                return_value={"attempt_dir": "attempt", "stop_reason": ""}))
            stack.enter_context(patch.object(agent, "graph_validate_patch_node",
                return_value={"validation": {"can_apply": True}, "stop_reason": ""}))
            apply = stack.enter_context(patch.object(agent, "graph_apply_patch_node"))
            runners = [agent.run_fallback_graph]
            try:
                runners.append(agent.build_langgraph_runner())
            except ImportError:
                pass
            for runner in runners:
                propose.reset_mock()
                apply.side_effect = [
                    {"stop_reason": "checks_failed", "status": {"status": "target_failed",
                     "rtl_modified": False, "rollback": {"passed": True}}},
                    {"stop_reason": "", "status": {"status": "fixed"}},
                ]
                result = runner({"repair": True, "max_attempts": 2})
                self.assertEqual(result["status"]["status"], "fixed")
                self.assertEqual(propose.call_count, 2)

    def test_retry_requires_verified_rollback(self):
        for extra in ({}, {"rtl_modified": True}, {"rollback": {"passed": False}}):
            state = {"stop_reason": "checks_failed", "status": {"status": "target_failed", **extra}}
            self.assertEqual(agent.route_after_apply(state), "record_result")
        self.assertEqual(agent.route_after_validate({"validation": {"status": "no_patch"}}),
                         "record_result")


class RepairLoopTests(unittest.TestCase):
    def make_failure(self, root):
        failure = root / "debug" / "failures" / "0001_test"
        for name in ("intake", "analysis", "attempts", "result"):
            (failure / name).mkdir(parents=True)
        return failure

    def repair_args(self, failure, max_attempts=2):
        return argparse.Namespace(
            mode="repair",
            failure_dir=str(failure),
            model="test-model",
            offline=False,
            max_attempts=max_attempts,
            lint_command="true",
            target_command=None,
            keep_failed_patch=False,
            demo_mode=False,
        )

    def test_repair_retries_after_failed_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = self.make_failure(root)

            def propose(args):
                attempt = agent.next_attempt_dir(failure)
                (attempt / "patch.diff").write_text("diff\n")
                return 0

            def validate(args):
                attempt = Path(args.attempt_dir)
                validation = {"status": "patch_valid", "can_apply": True}
                (attempt / "validation.json").write_text(json.dumps(validation))
                return 0

            apply_statuses = ["lint_failed", "fixed"]

            def apply(args):
                attempt = Path(args.attempt_dir)
                status = apply_statuses.pop(0)
                record = {"status": status, "rtl_modified": status == "fixed"}
                (failure / "result" / "status.json").write_text(json.dumps(record))
                (attempt / "status.json").write_text(json.dumps(record))
                return 0 if status == "fixed" else 1

            with patch.object(agent, "propose_patch", side_effect=propose), \
                    patch.object(agent, "validate_attempt", side_effect=validate), \
                    patch.object(agent, "apply_attempt", side_effect=apply):
                rc = agent.repair_failure(self.repair_args(failure))

            self.assertEqual(rc, 0)
            attempts = agent.numbered_attempt_dirs(failure)
            self.assertEqual([path.name for path in attempts], ["attempt_01", "attempt_02"])
            repair = json.loads((failure / "result" / "repair.json").read_text())
            self.assertEqual(repair["status"], "fixed")
            self.assertEqual(repair["attempt"], "attempt_02")
            self.assertEqual([step["name"] for step in repair["steps"]],
                             ["propose", "validate", "apply", "propose", "validate", "apply"])

    def test_repair_marks_needs_human_after_invalid_patch_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            failure = self.make_failure(root)

            def propose(args):
                attempt = agent.next_attempt_dir(failure)
                (attempt / "patch.diff").write_text("invalid\n")
                return 0

            def validate(args):
                attempt = Path(args.attempt_dir)
                validation = {"status": "patch_rejected", "can_apply": False}
                (attempt / "validation.json").write_text(json.dumps(validation))
                (failure / "result" / "status.json").write_text(json.dumps(validation))
                return 1

            with patch.object(agent, "propose_patch", side_effect=propose), \
                    patch.object(agent, "validate_attempt", side_effect=validate), \
                    patch.object(agent, "apply_attempt") as apply:
                rc = agent.repair_failure(self.repair_args(failure))

            self.assertEqual(rc, 1)
            apply.assert_not_called()
            self.assertEqual([path.name for path in agent.numbered_attempt_dirs(failure)],
                             ["attempt_01", "attempt_02"])
            status = json.loads((failure / "result" / "status.json").read_text())
            self.assertEqual(status["status"], "needs_human")
            repair = json.loads((failure / "result" / "repair.json").read_text())
            self.assertEqual(repair["status"], "needs_human")
            self.assertEqual(repair["attempt"], "attempt_02")


if __name__ == "__main__":
    unittest.main()
