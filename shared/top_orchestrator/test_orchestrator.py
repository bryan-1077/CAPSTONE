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
    def test_skip_lec_cli_aliases_and_plan(self):
        from shared.top_orchestrator.__main__ import main
        for flag in ("--skip-lec", "--no-lec"):
            with self.subTest(flag=flag), patch("sys.argv", ["orchestrator", flag, "--plan"]), patch(
                    "sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertIn("mapped netlist -> GDSII generation", output.getvalue())
                self.assertIn("LEC: not_run", output.getvalue())
            with patch("sys.argv", ["orchestrator", flag]), patch(
                    "shared.top_orchestrator.remote.RemoteConfig.load"), patch(
                    "shared.top_orchestrator.__main__.run") as execute:
                execute.return_value.status = "complete"
                self.assertEqual(main(), 0)
                self.assertTrue(execute.call_args.args[0].skip_lec)
                self.assertFalse(execute.call_args.args[0].skip_verification)

    def test_backend_skip_lec_requires_explicit_not_run_evidence(self):
        from types import SimpleNamespace
        from shared.top_orchestrator.nodes import backend_node
        for skip, recorded_skip, expected in ((True, True, "passed"), (False, True, "failed"),
                                              (True, False, "failed"), (False, False, "passed")):
            with self.subTest(skip=skip, recorded_skip=recorded_skip), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = RunConfig("generate", mailbox=root / "mailbox", target_mhz=200,
                                   validation_result=root / "validation.json", skip_lec=skip)
                payload = dict(current_stage="done", mailbox_revision="revision",
                               timing_target_clock_period_ns=5.0, lec_enabled=not recorded_skip,
                               lec_status="not_run" if recorded_skip else "passed",
                               stage_status={**dict.fromkeys(("rtl_prep", "netlist", "mapped_netlist", "gdsii"),
                                                            "success"), "lec": "not_run" if recorded_skip else "success"})
                def execute(command, *args, **kwargs):
                    self.assertEqual("--skip-lec" in command, skip)
                    self.assertIn("--lec-validation-result", command)
                    (root / "backend_result.json").write_text(json.dumps(payload))
                    return 0
                with patch("shared.top_orchestrator.mailbox.verify", return_value={"rtl_revision": "revision"}), patch(
                        "shared.top_orchestrator.nodes.verify_backend_handoff", return_value={}), patch(
                        "shared.top_orchestrator.nodes.RemoteConfig.load",
                        return_value=SimpleNamespace(host="fake", username="fake", port=22, key_filename=None)), patch(
                        "shared.top_orchestrator.allocation.backend_environment", return_value={}), patch(
                        "shared.top_orchestrator.nodes.cached_password", return_value=None), patch(
                        "shared.top_orchestrator.nodes.stream_command", side_effect=execute):
                    result = backend_node(config, root)
                self.assertEqual(result.status, expected, result.message)
                self.assertEqual(result.artifacts["validation"], "passed")

    def test_frontend_only_stops_after_repair_and_recheck(self):
        seen = []
        def generate(config, directory):
            seen.append(("generate", config.no_bist, config.recheck_only))
            if not config.recheck_only:
                return StageResult("generate", "failed", "lint failure", failure=FailureReport("lint", directory / "lint.log"))
            return StageResult("generate", "passed", "checked")
        def debug(config, directory):
            seen.append(("debug", config.no_bist, False))
            return StageResult("debug", "passed", "repaired")
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                "shared.top_orchestrator.orchestrator.NODES", {
                    "generate": generate, "debug": debug,
                    "validation": lambda *args: self.fail("Verification must not run"),
                    "backend": lambda *args: self.fail("Backend must not run")}):
            state = run(RunConfig("generate", skip_verification=True, no_backend=True, no_bist=True),
                        Path(temporary) / "run")
        self.assertEqual(state.status, "complete")
        self.assertEqual(seen, [("generate", True, False), ("debug", True, False), ("generate", True, True)])
        self.assertEqual(state.results[-1].artifacts["backend"], "not_run")
        self.assertEqual(state.results[-1].artifacts["validation"], "not_run")

    def test_no_bist_runs_lint_without_simulation(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "shared.top_orchestrator.nodes.stream_command", return_value=0) as command:
            result = generate_node(RunConfig("generate", recheck_only=True, no_bist=True), Path(temporary))
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.artifacts["bist"], "not_run")
        self.assertEqual(command.call_count, 2)
        self.assertIn("run_full_system_lint", command.call_args_list[0].args[0][-1])
        self.assertIn("--manifest-only", command.call_args.args[0])

    def test_stage_skip_cli_aliases_do_not_load_remote_configuration(self):
        from shared.top_orchestrator.__main__ import main
        for flag in ("--no-verif", "--skip-verification"):
            argv = ["orchestrator", flag, "--no-backend", "--no-rtlgen", "--no-bist"]
            with self.subTest(flag=flag), patch("sys.argv", argv), patch(
                    "shared.top_orchestrator.__main__.run") as execute, patch(
                    "shared.top_orchestrator.remote.RemoteConfig.load", side_effect=AssertionError("Remote setup attempted")):
                execute.return_value.status = "complete"
                self.assertEqual(main(), 0)
                config = execute.call_args.args[0]
                self.assertTrue(config.skip_verification and config.no_backend and config.no_bist and config.recheck_only)
            with patch("sys.argv", argv + ["--plan"]), patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(), 0)
                self.assertIn("verification and backend skipped", output.getvalue())
                self.assertNotIn("Prompt for target", output.getvalue())
                self.assertNotIn("Then: bash", output.getvalue())

    def test_frontend_only_generation_does_not_prompt_for_frequency(self):
        from shared.top_orchestrator.frequency import prepare_generation
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "config.yaml"
            source.write_text("banks: 4\n")
            with patch("shared.top_orchestrator.frequency.select_frequency",
                       side_effect=AssertionError("Frontend-only frequency prompt")):
                config = prepare_generation(RunConfig("generate", input_yaml=source,
                    skip_verification=True, no_backend=True), root)
            self.assertEqual(config.input_yaml.read_text(), source.read_text())

    def setUp(self):
        credentials = patch("shared.top_orchestrator.orchestrator.prepare_password")
        credentials.start()
        self.addCleanup(credentials.stop)
        frequency = patch("shared.top_orchestrator.orchestrator.select_frequency",
                          side_effect=lambda config, directory: replace(config, target_mhz=200))
        frequency.start()
        self.addCleanup(frequency.stop)
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

    def test_skip_verification_preserves_frontend_repair_and_rechecks_before_backend(self):
        attempts = []
        def generate(config, directory):
            attempts.append(config)
            if len(attempts) == 1:
                return StageResult("generate", "failed", "BIST failed",
                    failure=FailureReport("bist", directory / "bist.log"))
            self.assertTrue(config.recheck_only)
            return StageResult("generate", "passed", "rechecked")
        def backend(config, directory):
            self.assertTrue(config.allow_unvalidated)
            self.assertEqual(config.target_mhz, 200)
            self.assertIsNotNone(config.mailbox)
            return StageResult("backend", "passed", "done")
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                "shared.top_orchestrator.orchestrator.NODES", {
                    "generate": generate,
                    "debug": lambda c, d: StageResult("debug", "passed", "repaired"),
                    "backend": backend,
                    "gdsii-validation": lambda c, d: StageResult("gdsii-validation", "needs_attention", "review required"),
                    "validation": lambda c, d: self.fail("Verification must be skipped"),
                }):
            state = run(RunConfig("generate", skip_verification=True), Path(temporary) / "run")
        self.assertEqual(state.status, "needs_attention")
        self.assertEqual([r.stage for r in state.results], ["generate", "debug", "generate", "backend", "gdsii-validation"])

    def test_skip_verification_stops_on_frontend_failure_or_publication_failure(self):
        for failure in ("frontend", "publication"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary, patch.dict(
                    "shared.top_orchestrator.orchestrator.NODES", {
                        "generate": lambda c, d: StageResult("generate",
                            "failed" if failure == "frontend" else "passed", "test"),
                        "backend": lambda c, d: self.fail("Backend must not run on failed input"),
                    }), patch("shared.top_orchestrator.orchestrator.publish", side_effect=OSError("publication failed")):
                state = run(RunConfig("generate", skip_verification=True), Path(temporary) / "run")
            self.assertEqual(state.status, "failed")
            self.assertEqual(len(state.results), 1)

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
            self.assertRegex(terminal.getvalue(),
                             r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [^\]]+\] Choose specs: $")
            self.assertEqual((root / "log").read_text(), terminal.getvalue())

    def test_generation_hands_off_to_validation_and_persists_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            def successful_command(command, log):
                log.write_text("=========== TEST PASS ===========\n")
                return 0
            with patch("shared.top_orchestrator.nodes.stream_command",
                       side_effect=successful_command) as command:
                state = run(RunConfig("generate", input_yaml=Path("/input.yaml")), run_dir)
            self.assertEqual(command.call_count, 3)
            self.assertTrue(command.call_args_list[1].args[0][1].endswith("run_sim.sh"))
            self.assertIn("--manifest-only", command.call_args.args[0])
            self.assertEqual([r.stage for r in state.results], ["generate", "validation"])
            self.assertEqual(state.status, "needs_attention")
            saved = json.loads((run_dir / "state.json").read_text())
            self.assertEqual(saved["status"], "needs_attention")

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

    def test_validation_success_stops_without_backend_and_protects_existing_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            nodes = {stage: (lambda config, directory, stage=stage:
                            StageResult(stage, "passed", "test"))
                     for stage in ("validation", "backend")}
            with patch.dict("shared.top_orchestrator.orchestrator.NODES", nodes):
                state = run(RunConfig("validation"), root)
                self.assertEqual(state.status, "complete")
                self.assertEqual([r.stage for r in state.results], ["validation"])
                with self.assertRaises(FileExistsError):
                    run(RunConfig("validation"), root)

    def test_default_plan_includes_backend_and_honors_no_backend(self):
        from shared.top_orchestrator.__main__ import main
        for flags, route in ((["--backend-python", "/backend/python"], "remote validation -> backend"),
                             (["--no-backend"], "remote validation -> stop (no backend)")):
            with self.subTest(flags=flags), patch("sys.argv", ["orchestrator", "--plan", *flags]), patch(
                    "sys.stdout", io.StringIO()) as output, patch("getpass.getpass") as password:
                self.assertEqual(main(), 0)
                self.assertIn(route, output.getvalue())
                password.assert_not_called()


if __name__ == "__main__":
    unittest.main()
