import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from timing_closure import closure_runner as closure
from timing_closure.timing_analyzer import parse_timing_report
from workers.workers import _build_gdsii_command


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "innovus_beginpoint_slack_time.rpt"


class FakeSSH:
    def __init__(self, slacks, *, missing=False, period="4.762"):
        self.slacks = iter(slacks)
        self.missing = missing
        self.period = period
        self.reservations = []
        self.uploads = []

    def upload_file(self, local, remote, *, exclusive=False):
        assert exclusive
        self.uploads.append(remote)

    def run(self, command, **kwargs):
        self.reservations.append(command)
        return {"ok": True}

    def fetch_file(self, remote, local):
        if self.missing:
            return {"ok": False, "error": "missing", "remote_path": remote}
        extra_reports = {
            "die_area.rpt": "die_area_mm2: 3.5\n",
            "power_postroute.rpt": "Total Power: 1.2 W\n",
            "eco_library.rpt": "sky130_fd_sc_hd__inv_2\nsky130_fd_sc_hd__inv_4\nsky130_fd_sc_hd__inv_8\nsky130_fd_sc_hd__nor2_1\nsky130_fd_sc_hd__nor2_2\n",
        }
        if Path(remote).name in extra_reports:
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_text(extra_reports[Path(remote).name])
            return {"ok": True, "remote_path": remote}
        text = FIXTURE.read_text().replace("5.000", self.period)
        slack = 0.02 if remote.endswith("hold_postroute.rpt") else next(self.slacks)
        text = text.replace("-0.119", f"{slack:.3f}")
        if slack >= 0:
            text = text.replace("VIOLATED", "MET")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_text(text)
        return {"ok": True, "remote_path": remote}


class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        patcher = patch.object(closure, "__file__", str(self.repo / "timing_closure" / "closure_runner.py"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.states = []
        self.state = {
            "remote_project_root": "/remote/project", "gdsii_candidate_path": "build_GDSII_original",
            "timing_target_clock_period_ns": 4.762, "history": [], "logs": {},
            "stage_status": {"mapped_netlist": "success"},
            "timing_closure_ai_enabled": False,
        }

    def implementation(self, state):
        self.states.append(state)
        return {**state, "stage_status": {"gdsii": "success"}, "gdsii_review_passed": True}

    def run_closure(self, ssh):
        return closure.run_setup_closure(self.state, ssh, self.implementation, lambda state, updates: {**state, **updates})

    def test_negative_then_positive_retries_with_distinct_outputs_and_settings(self):
        ssh = FakeSSH([-0.119, 0.012])
        result = self.run_closure(ssh)
        self.assertEqual(result["timing_closure_status"], "passed")
        self.assertEqual(len(self.states), 2)
        self.assertEqual(result["timing_closure_analysis"]["wns"], 0.012)
        self.assertNotEqual(self.states[0]["remote_gdsii_dir"], self.states[1]["remote_gdsii_dir"])
        self.assertEqual(self.states[0]["remote_gdsii_dir"], "build_GDSII_210MHz_01")
        self.assertEqual(self.states[1]["remote_gdsii_dir"], "build_GDSII_210MHz_02")
        self.assertNotEqual(self.states[0]["innovus_utilization"], self.states[1]["innovus_utilization"])
        for state in self.states:
            self.assertEqual(state["mapped_candidate_path"], state["remote_mapped_dir"])
            self.assertEqual(state["mapped_candidate_path"], "build_mapped_snapshot_210MHz_01")
            command = _build_gdsii_command(state)
            self.assertIn("--clock-period 4.762", command)
            self.assertIn("--final-postroute-setup-opt", command)
            self.assertNotIn("rm -rf", command)
        self.assertEqual(len(ssh.uploads), 2)
        self.assertTrue(ssh.uploads[0].endswith(".timing_closure_runner_210MHz_01.py"))
        self.assertTrue(ssh.uploads[1].endswith(".timing_closure_runner_210MHz_02.py"))
        status = json.loads((self.repo / "logs" / "timing_closure" / "timing_closure_status.json").read_text())
        self.assertEqual(status["run_name"], "target_210MHz")
        self.assertEqual(result["timing_closure_run_name"], "target_210MHz")
        self.assertTrue((self.repo / "logs" / "timing_closure" / "target_210MHz" / "attempt_2_state.json").exists())
        self.assertEqual([a["wns_ns"] for a in status["attempts"]], [-0.119, 0.012])
        log_dir = self.repo / "logs" / "timing_closure"
        self.assertIn("passed", (log_dir / "timing_closure_report.md").read_text())
        self.assertEqual(
            (log_dir / "timing_closure_report.md").read_text(),
            (log_dir / "target_210MHz" / "timing_closure_report.md").read_text(),
        )
        self.assertTrue(list((log_dir / "remote").rglob("*.rpt")))
        self.assertFalse((self.repo / ".timing_closure_remote").exists())
        self.assertFalse((self.repo / "timing_closure" / "timing_closure_report.md").exists())

    def test_exhaustion_does_not_report_success(self):
        result = self.run_closure(FakeSSH([-0.119, -0.1, -0.08]))
        self.assertEqual(len(self.states), 3)
        self.assertEqual(result["stage_status"]["gdsii"], "failed")
        self.assertIn("after 3 backend attempts", result["last_error"])

    def test_missing_report_fails_without_accepting_old_analysis(self):
        self.state["timing_closure_analysis"] = {"wns": 1.0}
        result = self.run_closure(FakeSSH([], missing=True))
        self.assertEqual(len(self.states), 1)
        self.assertEqual(result["timing_closure_analysis"], {})
        self.assertEqual(result["stage_status"]["gdsii"], "failed")

    def test_wrong_period_cannot_pass_even_with_positive_slack(self):
        result = self.run_closure(FakeSSH([0.1], period="5.000"))
        self.assertEqual(result["timing_closure_status"], "failed")
        self.assertIn("does not confirm", result["last_error"])

    def test_zero_slack_passes(self):
        result = self.run_closure(FakeSSH([0.0]))
        self.assertEqual(result["timing_closure_status"], "passed")

    def test_required_time_alone_is_not_clock_evidence(self):
        path = self.repo / "report.rpt"
        path.write_text("Required time: 4.762\nWNS: 0.2\n")
        status, _ = closure.evaluate_setup_report(parse_timing_report(path), 4.762)
        self.assertEqual(status, "error")

    def test_backend_failure_stops_without_fetching_or_claiming_closure(self):
        def failed(state):
            return {**state, "stage_status": {"gdsii": "failed"}, "last_error": "optimization command failed"}
        result = closure.run_setup_closure(self.state, FakeSSH([]), failed, lambda state, updates: {**state, **updates})
        self.assertEqual(result["timing_closure_status"], "failed")
        self.assertEqual(result["last_error"], "optimization command failed")

    def test_backend_failure_does_not_reroute_to_shared_mapping(self):
        def failed(state):
            return {**state, "stage_status": {"gdsii": "failed"}, "route_back_to_stage": "mapped_netlist"}
        result = closure.run_setup_closure(self.state, FakeSSH([]), failed, lambda state, updates: {**state, **updates})
        self.assertIsNone(result["route_back_to_stage"])

    def test_failed_snapshot_prevents_backend_launch(self):
        ssh = FakeSSH([])
        with patch.object(ssh, "run", return_value={"ok": False, "stderr": "Mapped inputs changed"}):
            result = self.run_closure(ssh)
        self.assertFalse(self.states)
        self.assertEqual(result["timing_closure_status"], "failed")
        self.assertIn("Cannot isolate mapped inputs", result["last_error"])

    def test_app_enables_closure_with_default_target(self):
        import app
        with patch.dict("os.environ", {}, clear=True), patch.object(app, "_load_service_exports"), patch.object(app, "run_flow", return_value={}) as run:
            app.main()
        initial = run.call_args.kwargs["initial_state"]
        self.assertTrue(initial["timing_closure_enabled"])
        self.assertTrue(initial["innovus_final_postroute_setup_opt"])
        self.assertEqual(initial["timing_target_clock_period_ns"], 4.762)

    def test_app_requests_exclusive_reservation_of_named_directories(self):
        import app
        with patch.object(app, "_load_service_exports"), patch.object(app, "run_flow", return_value={}) as run:
            app.main()
            app.main()
        first, second = [call.kwargs["initial_state"] for call in run.call_args_list]
        for state in (first, second):
            self.assertTrue(state["reserve_named_builds"])
            self.assertEqual(state["remote_prepare_dir"], "build_prep_210MHz_01")
            self.assertEqual(state["remote_netlist_dir"], "build_netlist_210MHz_01")
            self.assertEqual(state["remote_mapped_dir"], "build_mapped_210MHz_01")
            self.assertEqual(state["remote_gdsii_dir"], "build_GDSII_210MHz_01")

    def test_orchestrator_wraps_success_in_timing_gate_by_default(self):
        from workers import orchestrator
        with patch.object(orchestrator, "_make_gdsii_implementation_runner", return_value=self.implementation), patch.object(orchestrator, "run_setup_closure", return_value={"gated": True}) as gate:
            result = orchestrator._make_gdsii_runner(object())(self.state)
        self.assertTrue(result["gated"])
        gate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
