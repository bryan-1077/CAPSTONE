"""Check stage boundaries without launching generation or repair tools."""

import json
import io
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.contracts import FailureReport, RunConfig, StageResult
from shared.top_orchestrator.nodes import debug_node, frontend_command, stream_command, generate_node, spec_command
from shared.top_orchestrator.orchestrator import run


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        # Stage tests start after selection; its real boundary is tested separately.
        preparation = patch("shared.top_orchestrator.orchestrator.prepare_generation",
            side_effect=lambda config, directory: replace(config,
                input_yaml=config.input_yaml or Path("/approved.yaml"), target_mhz=200))
        preparation.start()
        self.addCleanup(preparation.stop)
        publication = patch("shared.top_orchestrator.orchestrator.publish",
                            side_effect=lambda frontend, destination, specs: destination)
        publication.start()
        self.addCleanup(publication.stop)

    def test_lint_handoff_copies_diagnostic_for_both_entry_paths(self):
        for checks_only in (True, False):
            with self.subTest(checks_only=checks_only), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "run"
                output.mkdir()
                diagnostic = "%Error: rtl_output/ddr4_request_queue.sv:19:5: expecting ';'\n"
                def command(argv, log):
                    log.write_text("[FLOW] Full-system RTL lint FAILED. See flow_lint.log.\n")
                    (root / "flow_lint.log").write_text(diagnostic)
                    return 1
                with patch("shared.top_orchestrator.nodes.FRONTEND", root), patch(
                        "shared.top_orchestrator.nodes.stream_command", side_effect=command):
                    result = generate_node(RunConfig("generate", input_yaml=Path("/approved.yaml"),
                                                     recheck_only=checks_only), output)
                self.assertEqual(result.failure.log, output / "flow_lint.log")
                self.assertEqual(result.failure.log.read_text(), diagnostic)
                (root / "flow_lint.log").write_text("later run")
                self.assertEqual(result.failure.log.read_text(), diagnostic)

    def test_stale_lint_report_is_not_sent_to_debug(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "flow_lint.log").write_text("old error")
            with patch("shared.top_orchestrator.nodes.FRONTEND", root), patch(
                    "shared.top_orchestrator.nodes.stream_command", return_value=1):
                result = generate_node(RunConfig("generate", recheck_only=True), root)
            self.assertIsNone(result.failure)
            self.assertIn("No fresh lint", result.message)

    def test_auto_debug_preserves_needs_human_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = {"status": "needs_human", "reason": "evidence_unavailable",
                      "unresolved_questions": ["Where is the diagnostic?"]}
            def command(argv, log):
                name = argv[argv.index("--name") + 1]
                result_dir = root / "debug" / "failures" / ("0001_" + name) / "result"
                result_dir.mkdir(parents=True)
                (result_dir / "status.json").write_text(json.dumps(record))
                return 1
            with patch("shared.top_orchestrator.nodes.FRONTEND", root), patch(
                    "shared.top_orchestrator.nodes.stream_command", side_effect=command):
                result = debug_node(RunConfig("debug", failure=FailureReport("lint", root / "lint.log")), root)
            self.assertEqual(result.status, "needs_attention")
            self.assertEqual(result.returncode, 1)
            self.assertIn("needs_human", result.message)
            self.assertIn("evidence_unavailable", result.message)
            self.assertIn("Where is the diagnostic?", result.message)
            self.assertEqual(json.loads(Path(result.artifacts["debug_status_snapshot"]).read_text()), record)

    def test_failure_sources_route_to_debug_then_recheck(self):
        for source, entry in (("lint", "generate"), ("bist", "generate"),
                              ("verif", "validation"), ("pd", "backend")):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                calls = []
                def node(config, directory):
                    calls.append(config)
                    stage = entry if len(calls) == 1 else "generate"
                    if len(calls) == 1:
                        return StageResult(stage, "failed", "failure",
                                           failure=FailureReport(source, Path(temporary) / "failure.log"))
                    self.assertTrue(config.recheck_only)
                    return StageResult("generate", "passed", "rechecked")
                def debug(config, directory):
                    self.assertEqual(config.failure.source, source)
                    self.assertTrue(config.repair)
                    return StageResult("debug", "passed", "repaired")
                def validation(config, directory):
                    if not calls:
                        return node(config, directory)
                    return StageResult("validation", "not_implemented", "placeholder")
                nodes = {"generate": node, "debug": debug, "validation": validation,
                         "backend": node}
                with patch.dict("shared.top_orchestrator.orchestrator.NODES", nodes):
                    state = run(RunConfig(entry), Path(temporary) / "run")
                self.assertEqual([r.stage for r in state.results],
                                 [entry, "debug", "generate", "validation"])
                self.assertEqual(state.repair_cycles, 1)

    def test_repeated_failure_exhausts_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            def failure(config, directory):
                return StageResult("generate", "failed", "lint failed",
                    failure=FailureReport("lint", Path(temporary) / "lint.log"))
            def repair(config, directory):
                return StageResult("debug", "passed", "repaired")
            with patch.dict("shared.top_orchestrator.orchestrator.NODES",
                            {"generate": failure, "debug": repair}):
                state = run(RunConfig("generate", max_repair_cycles=1), Path(temporary) / "run")
            self.assertEqual(state.status, "needs_attention")
            self.assertEqual(len(state.results), 3)

    def test_default_generation_selects_specs(self):
        command = spec_command(Path("/approved.yaml"))
        self.assertTrue(any(part.endswith("configure_from_text.py") for part in command))
        self.assertNotIn("--run-flow", command)
        self.assertEqual(command[-2:], ["--output", "/approved.yaml"])
        command = frontend_command("generate", RunConfig("generate", Path("/input.yaml")))
        self.assertTrue(any(part.endswith("run_flow.py") for part in command))

    def test_prompt_is_visible_before_child_continues(self):
        # The child waits for evidence that its newline-free prompt was displayed.
        # A line-buffered reader would time out and make this test fail.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "visible"
            class Terminal(io.StringIO):
                def write(self, text):
                    result = super().write(text)
                    if self.getvalue().endswith("Choose specs: "):
                        marker.touch()
                    return result
            terminal = Terminal()
            script = (
                "import pathlib, sys, time\n"
                "print('Choose specs: ', end='', flush=True)\n"
                "marker = pathlib.Path(sys.argv[1])\n"
                "deadline = time.monotonic() + 5\n"
                "while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
                "sys.exit(0 if marker.exists() else 1)\n"
            )
            with patch("sys.stdout", terminal):
                code = stream_command([sys.executable, "-u", "-c", script, str(marker)], root / "log")
            self.assertEqual(code, 0)
            self.assertEqual(terminal.getvalue(), "Choose specs: ")
            self.assertEqual((root / "log").read_text(), terminal.getvalue())

    def test_generation_stops_at_placeholder_and_persists_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            def successful_command(command, log):
                log.write_text("=========== TEST PASS ===========\n")
                return 0
            with patch("shared.top_orchestrator.nodes.stream_command",
                       side_effect=successful_command) as command:
                state = run(RunConfig("generate", input_yaml=Path("/input.yaml")), run_dir)
            self.assertEqual(command.call_count, 2)
            self.assertTrue(command.call_args.args[0][1].endswith("run_sim.sh"))
            self.assertEqual([r.stage for r in state.results], ["generate", "validation"])
            self.assertEqual(state.status, "not_implemented")
            saved = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(saved["status"], "not_implemented")

    def test_bist_failure_stops_before_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("shared.top_orchestrator.nodes.stream_command", side_effect=[0, 1]):
                state = run(RunConfig("generate", max_repair_cycles=0), Path(temporary) / "run")
            self.assertEqual(state.status, "needs_attention")
            self.assertEqual(len(state.results), 1)
            self.assertIn("BIST failed", state.results[0].message)

    def test_bist_exit_zero_without_pass_is_failure(self):
        for evidence in ("TEST FAIL", "simulation ended"):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as temporary:
                def command(argv, log):
                    log.write_text(evidence)
                    return 0
                with patch("shared.top_orchestrator.nodes.stream_command", side_effect=command):
                    state = run(RunConfig("generate", max_repair_cycles=0), Path(temporary) / "run")
                self.assertEqual(state.status, "needs_attention")
                self.assertEqual(len(state.results), 1)

    def test_generation_failure_does_not_reach_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("shared.top_orchestrator.nodes.stream_command",
                       return_value=1):
                state = run(RunConfig("generate", input_yaml=Path("/input.yaml")),
                            Path(temporary) / "run")
            self.assertEqual(state.status, "failed")
            self.assertEqual(len(state.results), 1)

    def test_debug_exit_zero_is_not_enough(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "result").mkdir()
            status = root / "result" / "status.json"
            config = RunConfig("debug", failure_dir=root, repair=True)
            with patch("shared.top_orchestrator.nodes.stream_command",
                       return_value=0):
                for record in ({"status": "needs_human"}, [], {"status": "patch_applied"}):
                    status.write_text(json.dumps(record))
                    outcome = debug_node(config, root)
                    expected = "passed" if record == {"status": "patch_applied"} else "needs_attention"
                    self.assertEqual(outcome.status, expected)

    def test_future_success_route_and_existing_run_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            nodes = {stage: (lambda config, directory, stage=stage:
                            StageResult(stage, "passed", "test"))
                     for stage in ("validation", "backend")}
            with patch.dict("shared.top_orchestrator.orchestrator.NODES", nodes):
                state = run(RunConfig("validation"), root)
                self.assertEqual(state.status, "complete")
                self.assertEqual([r.stage for r in state.results], ["validation", "backend"])
                with self.assertRaises(FileExistsError):
                    run(RunConfig("validation"), root)


if __name__ == "__main__":
    unittest.main()
