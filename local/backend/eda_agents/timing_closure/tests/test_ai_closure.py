import json
import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from agents import openai_timing_closure as agent
from timing_closure import closure_runner as closure
from timing_closure.constraints import evaluate_limits, parse_total_power
from timing_closure.tests import test_closure_runner as fixtures
from probes.run_innovus_GDSII_universal import build_timing_eco_tcl
from workers.workers import _build_gdsii_timing_closure_args


def plan(size=4):
    return {"summary": "Resize the inverter to reduce critical-path delay.", "actions": [{
        "instance": "g189616", "from_cell": "sky130_fd_sc_hd__inv_2",
        "to_cell": f"sky130_fd_sc_hd__inv_{size}", "reason": "Critical path inverter drive strength",
    }]}


class EvidenceSSH(fixtures.FakeSSH):
    def __init__(self, slacks, *, ai_power=1.5, ai_area=3.5, hold_slack=0.02, missing_metric=None):
        super().__init__(slacks)
        self.ai_power, self.ai_area, self.hold_slack = ai_power, ai_area, hold_slack
        self.missing_metric = missing_metric

    def fetch_file(self, remote, local):
        if self.missing_metric and remote.endswith(self.missing_metric):
            return {"ok": False, "error": "missing", "remote_path": remote}
        result = super().fetch_file(remote, local)
        if "_ai_" in remote:
            if remote.endswith("power_postroute.rpt"):
                Path(local).write_text(f"Total Power: {self.ai_power} W\n")
            elif remote.endswith("die_area.rpt"):
                Path(local).write_text(f"die_area_mm2: {self.ai_area}\n")
            elif remote.endswith("hold_postroute.rpt"):
                text = Path(local).read_text().replace("0.020", f"{self.hold_slack:.3f}")
                Path(local).write_text(text)
        return result


class AIClosureTests(unittest.TestCase):
    implementation = fixtures.ClosureTests.implementation
    run_closure = fixtures.ClosureTests.run_closure

    def setUp(self):
        fixtures.ClosureTests.setUp(self)
        self.state.update(timing_closure_ai_enabled=True, timing_closure_ai_max_attempts=2)

    def test_uses_best_measured_preset_not_last_and_recovers(self):
        with patch.object(closure, "plan_timing_recovery", return_value=plan()) as ai:
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, 0.01]))
        self.assertEqual(result["timing_closure_status"], "passed")
        self.assertEqual(len(self.states), 4)
        source = self.states[3]["timing_closure_eco_plan"]["checkpoint"]
        self.assertTrue(source.endswith("build_GDSII_210MHz_02/db/06_final.enc"))
        self.assertEqual(self.states[3]["innovus_utilization"], 0.55)
        self.assertEqual(ai.call_args.args[0]["limits"], {"max_die_area_mm2": 4.0, "max_power_w": 2.0})
        self.assertIn("g189616", ai.call_args.args[0]["resize_choices"])
        self.assertEqual(result["timing_closure_best_attempt"]["attempt"], 4)
        self.assertTrue(result["timing_closure_limits"]["within_limits"])
        self.assertEqual(result["timing_closure_attempts"][-1]["hold_wns_ns"], 0.02)
        command = _build_gdsii_timing_closure_args(self.states[3])
        self.assertIn("--timing-eco-json", command)
        self.assertIn("--clock-period 4.762", command)

    def test_regression_restarts_from_best_and_feedback_reaches_ai(self):
        seen = []
        def decide(context):
            seen.append(json.loads(json.dumps(context)))
            return plan(4 if len(seen) == 1 else 8)
        with patch.object(closure, "plan_timing_recovery", side_effect=decide):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, -0.15, 0.01]))
        self.assertEqual(result["timing_closure_status"], "passed")
        self.assertEqual(self.states[3]["timing_closure_eco_plan"]["checkpoint"], self.states[4]["timing_closure_eco_plan"]["checkpoint"])
        self.assertEqual(seen[1]["attempt_history"][-1]["wns_ns"], -0.15)

    def test_improved_candidate_becomes_next_source(self):
        with patch.object(closure, "plan_timing_recovery", side_effect=[plan(4), plan(8)]):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, -0.05, 0.01]))
        self.assertEqual(result["timing_closure_status"], "passed")
        self.assertIn(self.states[3]["remote_gdsii_dir"], self.states[4]["timing_closure_eco_plan"]["checkpoint"])

    def test_passing_timing_over_budget_is_rejected_and_best_preserved(self):
        for overrides in ({"ai_power": 2.01}, {"ai_area": 4.01}, {"hold_slack": -0.03}):
            with self.subTest(overrides=overrides):
                self.state["timing_closure_ai_max_attempts"] = 1
                with patch.object(closure, "plan_timing_recovery", return_value=plan()):
                    result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, 0.01], **overrides))
                self.assertEqual(result["timing_closure_status"], "failed")
                self.assertEqual(result["timing_closure_best_attempt"]["attempt"], 2)
                self.assertNotEqual(result["timing_closure_attempts"][-1]["status"], "passed")

    def test_missing_measurement_cannot_pass(self):
        for report in ("power_postroute.rpt", "die_area.rpt"):
            with self.subTest(report=report):
                with patch.object(closure, "plan_timing_recovery") as ai:
                    result = self.run_closure(EvidenceSSH([0.01], missing_metric=report))
                self.assertEqual(result["timing_closure_status"], "failed")
                ai.assert_not_called()

    def test_invalid_or_repeated_ai_plan_stops_without_unbounded_runs(self):
        bad = plan()
        bad["actions"][0]["to_cell"] = "sky130_fd_sc_hd__nand2_1"
        with patch.object(closure, "plan_timing_recovery", return_value=bad):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2]))
        self.assertEqual(len(self.states), 3)
        self.assertIn("Unverified", result["last_error"])
        self.states.clear()
        with patch.object(closure, "plan_timing_recovery", return_value=plan()):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, -0.15]))
        self.assertEqual(len(self.states), 4)
        self.assertIn("repeated", result["last_error"])

    def test_ai_api_failure_does_not_claim_recovery(self):
        with patch.object(closure, "plan_timing_recovery", side_effect=RuntimeError("AI unavailable")):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2]))
        self.assertEqual(result["timing_closure_status"], "failed")
        self.assertIn("AI unavailable", result["last_error"])
        self.assertEqual(result["timing_closure_best_attempt"]["attempt"], 2)

    def test_missing_hold_report_and_ai_stop_cannot_pass(self):
        with patch.object(closure, "plan_timing_recovery", return_value=plan()):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2, 0.01], missing_metric="hold_postroute.rpt"))
        self.assertEqual(result["timing_closure_status"], "failed")
        self.assertIn("hold", result["last_error"])
        self.states.clear()
        with patch.object(closure, "plan_timing_recovery", return_value={"summary": "No useful legal change", "actions": []}):
            result = self.run_closure(EvidenceSSH([-0.3, -0.1, -0.2]))
        self.assertEqual(len(self.states), 3)
        self.assertIn("No useful legal change", result["last_error"])

    def test_recover_existing_best_remeasures_without_reserving_old_builds(self):
        self.state.update(timing_closure_recover_best=True, timing_closure_attempts=[{
            "attempt": i, "status": "violated", "wns_ns": slack, "target_period_ns": 4.762,
            "settings": closure.backend_settings({}, i), "outdir": f"old_{i}", "mapped_snapshot": "old_snapshot",
        } for i, slack in enumerate((-0.3, -0.1, -0.2), 1)])
        ssh = EvidenceSSH([-0.1, 0.01])
        with patch.object(closure, "plan_timing_recovery", return_value=plan()):
            result = self.run_closure(ssh)
        self.assertEqual(result["timing_closure_status"], "passed")
        self.assertEqual(len(self.states), 2)
        self.assertEqual(self.states[0]["timing_closure_eco_plan"], {"checkpoint": "/remote/project/old_2/db/06_final.enc", "actions": []})
        self.assertTrue(all(command.startswith("mkdir -- build_GDSII_recovery_") for command in ssh.reservations))
        self.assertIn("_recovery_", result["timing_closure_run_name"])


class LimitTests(unittest.TestCase):
    def test_exact_limits_and_power_units(self):
        for text in ("Total Power: 2 W", "Total Power: 2000 mW", "Total Power: 2e6 uW", "Total Power (mW) = 2000", "Power Units: W\nTotal Power: 2", "Total Power (W)  2 (100%)"):
            with self.subTest(text=text):
                self.assertTrue(evaluate_limits("die_area_mm2: 4", text)["within_limits"])
        self.assertFalse(evaluate_limits("die_area_mm2: 4.0001", "Total Power: 2 W")["within_limits"])
        self.assertFalse(evaluate_limits("die_area_mm2: 4", "Total Power: 2.0001 W")["within_limits"])

    def test_unknown_nonfinite_and_placeholder_measurements_rejected(self):
        for text in ("Legacy placeholder report", "Total Power: nan W", "Total Power: inf W", "Total Power: -1 W", "Total Power: 0 W", "Total Power: 2", "Total Power: 1e999 W"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_total_power(text)
        with self.assertRaises(ValueError):
            evaluate_limits("Total cell area: 3.5", "Total Power: 1 W")

    def test_worst_reported_view_is_used(self):
        self.assertEqual(parse_total_power("Total Power: 1 W\nTotal Power: 2.1 W"), 2.1)
        with self.assertRaises(ValueError):
            parse_total_power("Total Power: 1 W\nTotal Power: nan W")


class PlanTests(unittest.TestCase):
    def choices(self):
        return agent.resize_choices(fixtures.FIXTURE.read_text(), "sky130_fd_sc_hd__inv_2\nsky130_fd_sc_hd__inv_4\nsky130_fd_sc_hd__dfxtp_1\nsky130_fd_sc_hd__dfxtp_2")

    def test_only_observed_combinational_same_function_resizes_allowed(self):
        choices = self.choices()
        self.assertEqual(set(choices), {"g189616"})
        self.assertEqual(agent.validate_plan(plan(), choices), plan())
        unsafe = plan()
        unsafe["actions"][0]["instance"] = "other_gate"
        with self.assertRaises(ValueError):
            agent.validate_plan(unsafe, choices)

    def test_missing_key_is_explicit_failure(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            agent.plan_timing_recovery({})

    def test_api_uses_structured_schema_and_validates_output(self):
        client = Mock()
        response = Mock(output_text=json.dumps(plan()))
        client.responses.create.return_value = response
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}), patch.object(agent, "_build_client", return_value=client):
            result = agent.plan_timing_recovery({"resize_choices": self.choices()})
        self.assertEqual(result, plan())
        request = client.responses.create.call_args.kwargs
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertIn("4 mm^2", request["input"][0]["content"])
        self.assertIn("2 watts", request["input"][0]["content"])

    def test_chat_fallback_preserves_schema(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("Responses endpoint missing")
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
            patch.object(agent, "_build_client", return_value=client),
            patch.object(agent, "_is_not_found_error", return_value=True),
            patch.object(agent, "_extract_chat_output_text", return_value=json.dumps(plan())),
        ):
            self.assertEqual(agent.plan_timing_recovery({"resize_choices": self.choices()}), plan())
        self.assertTrue(client.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["strict"])

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
    def test_eco_tcl_applies_verified_resize_and_rejects_stale_master(self):
        script = "\n".join(build_timing_eco_tcl({"checkpoint": "/project/best/db/06_final.enc", "actions": plan()["actions"]}, "top"))
        stub = '''
proc restoreDesign {args} {puts RESTORED}
proc eda_die_area_mm2 {} {return 3.5}
proc deleteFiller {args} {}
proc dbGetInstByName {name} {return 0x1}
set master sky130_fd_sc_hd__inv_2
proc dbGet {name} {
    if {$name eq "head.libCells.name"} {return {sky130_fd_sc_hd__inv_2 sky130_fd_sc_hd__inv_4}}
    return $::master
}
proc ecoChangeCell {args} {set ::master [lindex $args 3]; puts RESIZED}
proc refinePlace {} {puts LEGALIZED}
proc ecoRoute {} {puts ROUTED}
'''
        completed = subprocess.run(["tclsh"], input=stub + script, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("RESIZED\nLEGALIZED\nROUTED", completed.stdout)
        failed = subprocess.run(["tclsh"], input=stub.replace("set master sky130_fd_sc_hd__inv_2", "set master sky130_fd_sc_hd__inv_8") + script, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 6)
        self.assertNotIn("RESIZED", failed.stdout)
        over_area = subprocess.run(["tclsh"], input=stub.replace("return 3.5", "return 4.01") + script, capture_output=True, text=True)
        self.assertEqual(over_area.returncode, 6)
        self.assertNotIn("RESIZED", over_area.stdout)

    def test_tcl_injection_and_function_changes_are_rejected(self):
        for field, value in (("instance", "foo}; exit 0; #"), ("to_cell", "sky130_fd_sc_hd__nand2_1")):
            bad = plan()["actions"]
            bad[0][field] = value
            with self.assertRaises(ValueError):
                build_timing_eco_tcl({"checkpoint": "/project/best.enc", "actions": bad}, "top")

    def test_setup_only_cli_generates_restore_resize_and_fresh_reports(self):
        root = Path(__file__).resolve().parents[2]
        fixture = root / "timing_closure" / "fixtures"
        with tempfile.TemporaryDirectory() as directory:
            command = [
                sys.executable, str(root / "probes" / "run_innovus_GDSII_universal.py"),
                "--mappeddir", str(fixture / "minimal_mapped"), "--outdir", directory,
                "--pdk-root", str(fixture / "minimal_pdk"), "--clock-period", "4.348",
                "--final-postroute-setup-opt", "--setup-only",
                "--timing-eco-json", json.dumps({"checkpoint": "/project/best/db/06_final.enc", "actions": plan()["actions"]}),
            ]
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            scripts = list((Path(directory) / "tcl").glob("*.tcl"))
            script = next(p.read_text() for p in scripts if "restoreDesign" in p.read_text())
            self.assertNotIn("\ninit_design\n", script)
            self.assertNotIn("\nfloorPlan ", script)
            self.assertIn("ecoChangeCell -inst {g189616}", script)
            self.assertIn("optDesign -postRoute -hold", script)
            self.assertIn("report_timing -early", script)
            self.assertIn("report_power -unit W", script)
            self.assertIn("die_area_mm2:", script)
            self.assertIn("saveDesign $DBS_DIR/06_final.enc", script)


if __name__ == "__main__":
    unittest.main()
