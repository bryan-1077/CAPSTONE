import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agents.openai_physical_verification import available_recipes, validate_plan
from agents import openai_physical_verification as agent
from physical_verification import closure_runner as closure
from physical_verification.reports import analyze_physical_reports, violation_count
from probes.run_innovus_GDSII_universal import build_physical_repair_tcl
from workers.workers import _build_gdsii_timing_closure_args


ROOT = Path(__file__).resolve().parents[2]
TIMING = ROOT / "timing_closure/fixtures/innovus_beginpoint_slack_time.rpt"
DRC = "SPACING: Regular Wire of Net n_4433 & Blockage of Cell FILL_T_0_11862 ( li1 )\nTotal Violations : 2 Viols.\n"


def repair(recipe="refill_and_reroute"):
    return {"summary": "Repair reported physical violations.",
            "actions": [{"recipe": recipe, "reason": "Report evidence"}]}


class FakeSSH:
    def __init__(self, *, drc=2, antenna=0, repaired_drc=0, repaired_antenna=0, slack=0.02, initial_slack=0.02, missing=None):
        self.drc, self.antenna = drc, antenna
        self.repaired_drc, self.repaired_antenna = repaired_drc, repaired_antenna
        self.slack, self.missing = slack, missing
        self.initial_slack = initial_slack
        self.commands, self.uploads = [], []
        self.fetches = []

    def run(self, command, **kwargs):
        self.commands.append(command)
        return {"ok": True}

    def upload_file(self, local, remote, *, exclusive=False):
        assert exclusive
        self.uploads.append(remote)

    def fetch_file(self, remote, local):
        self.fetches.append(remote)
        name = Path(remote).name
        if self.missing and name.endswith(self.missing):
            return {"ok": False, "remote_path": remote}
        if name.endswith(".geom.rpt"):
            name = "drc.rpt"
        elif name.endswith(".antenna.rpt"):
            name = "antenna.rpt"
        repaired = "_physical_" in remote
        slack = (self.slack if repaired else self.initial_slack) if name == "timing_postroute.rpt" else 0.02
        timing = TIMING.read_text().replace("5.000", "3.846").replace("-0.119", f"{slack:.3f}")
        if slack >= 0:
            timing = timing.replace("VIOLATED", "MET")
        drc_count = self.repaired_drc if repaired else self.drc
        antenna_count = self.repaired_antenna if repaired else self.antenna
        text = {
            "drc.rpt": DRC.replace("2 Viols", f"{drc_count} Viols") if drc_count else "Total Violations : 0 Viols.\n",
            "antenna.rpt": f"#Total number of process antenna violations = {antenna_count}\n",
            "timing_postroute.rpt": timing, "hold_postroute.rpt": timing,
            "die_area.rpt": "die_area_mm2: 0.2\n", "power_postroute.rpt": "Total Power: 0.05 W\n",
        }[name]
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_text(text)
        return {"ok": True, "remote_path": remote}


class PhysicalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = {"remote_project_root": "/project", "backend_log_dir": self.temp.name,
            "top_module": "ddr4_controller_top",
            "gdsii_candidate_path": "build_GDSII_260MHz_01", "stage_status": {"gdsii": "success"},
            "history": [], "outputs": {"gdsii_remote_dir": "/project/build_GDSII_260MHz_01"},
            "timing_target_clock_period_ns": 1000 / 260, "timing_closure_status": "passed",
            "physical_verification_max_attempts": 2}
        self.launches = []

    def implementation(self, state):
        self.launches.append(state)
        return {**state, "gdsii_candidate_path": state["remote_gdsii_dir"],
                "stage_status": {"gdsii": "success"},
                "outputs": {"gdsii_remote_dir": "/project/" + state["remote_gdsii_dir"]}}

    def run_closure(self, ssh, plan=None, *, require_setup_timing=True):
        with patch.object(closure, "plan_physical_repair", return_value=plan or repair()) as planner:
            result = closure.run_physical_closure(self.state, ssh, self.implementation,
                                                 lambda state, updates: {**state, **updates},
                                                 require_setup_timing=require_setup_timing)
        return result, planner

    def test_user_spacing_report_and_both_antenna_formats(self):
        evidence = analyze_physical_reports(DRC, "Total number of process antenna violations: 3\n")
        self.assertEqual(evidence["drc_count"], 2)
        self.assertEqual(evidence["antenna_count"], 3)
        self.assertEqual(available_recipes(evidence), ["reroute", "refill_and_reroute", "antenna_cleanup"])
        self.assertEqual(violation_count("#Total number of process antenna violations = 0\n", "antenna"), 0)

    def test_missing_ambiguous_and_tool_error_reports_do_not_pass(self):
        for text in ("", "Verification complete", "Total Violations: 0\nTotal Violations: 2\n",
                     "**ERROR: check failed\nTotal Violations: 0\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                violation_count(text, "drc")

    def test_clean_reports_do_not_call_ai(self):
        result, planner = self.run_closure(FakeSSH(drc=0))
        self.assertEqual(result["physical_verification_status"], "passed")
        planner.assert_not_called()
        self.assertFalse(self.launches)

    def test_innovus_clean_messages_and_repeated_totals(self):
        self.assertEqual(violation_count("No DRC violations were found\n", "drc"), 0)
        self.assertEqual(violation_count("No Violations Found\n", "antenna"), 0)
        for kind, total in (("drc", "Total Violations: 2\n"),
                            ("antenna", "Total number of process antenna violations = 2\n")):
            with self.subTest(kind=kind):
                self.assertEqual(violation_count(total * 2, kind), 2)
        with self.assertRaises(ValueError):
            violation_count("No DRC violations were found\nTotal Violations: 2\n", "drc")

    def test_innovus_clean_reports_skip_planner_and_implementation(self):
        ssh = FakeSSH(drc=0)
        fetch = ssh.fetch_file

        def fetch_clean(remote, local):
            result = fetch(remote, local)
            if remote.endswith(".geom.rpt"):
                Path(local).write_text("No DRC violations were found\n")
            elif remote.endswith(".antenna.rpt"):
                Path(local).write_text("No Violations Found\n")
            return result

        ssh.fetch_file = fetch_clean
        result, planner = self.run_closure(ssh)
        self.assertEqual(result["physical_verification_status"], "passed")
        planner.assert_not_called()
        self.assertFalse(self.launches)
        self.assertFalse(ssh.uploads)
        self.assertFalse(ssh.commands)

    def test_timing_pass_with_two_drc_violations_triggers_separate_agent(self):
        ssh = FakeSSH()
        result, planner = self.run_closure(ssh)
        self.assertEqual(result["physical_verification_status"], "passed")
        planner.assert_called_once()
        self.assertEqual(planner.call_args.args[0]["evidence"]["drc_count"], 2)
        launched = self.launches[0]
        self.assertIsNone(launched["timing_closure_eco_plan"])
        self.assertEqual(launched["physical_verification_plan"]["checkpoint"], "/project/build_GDSII_260MHz_01/db/06_final.enc")
        self.assertIn("_physical_", result["outputs"]["gdsii_remote_dir"])
        self.assertEqual(result["physical_verification_analysis"]["drc_count"], 0)
        self.assertEqual(len(ssh.uploads), 1)
        self.assertIn("--physical-repair-json", _build_gdsii_timing_closure_args(launched))

    def test_antenna_only_violation_does_not_launch_agent(self):
        result, planner = self.run_closure(FakeSSH(drc=0, antenna=2), repair("antenna_cleanup"))
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertIn("agent skipped", result["last_error"])
        planner.assert_not_called()
        self.assertFalse(self.launches)

    def test_missing_drc_or_hold_report_fails_closed(self):
        for name in (".geom.rpt", ".antenna.rpt", "hold_postroute.rpt"):
            with self.subTest(name=name):
                result, planner = self.run_closure(FakeSSH(missing=name))
                self.assertEqual(result["stage_status"]["gdsii"], "failed")
                planner.assert_not_called()

    def test_build_report_paths_use_top_module_and_resolved_top_takes_precedence(self):
        for resolved in (None, "different_controller_top"):
            with self.subTest(resolved=resolved):
                self.state["mapped_resolved_top"] = resolved
                ssh = FakeSSH(drc=0)
                result, _ = self.run_closure(ssh)
                self.assertEqual(result["physical_verification_status"], "passed")
                top = resolved or "ddr4_controller_top"
                self.assertEqual(ssh.fetches[:2], [
                    f"/project/build_GDSII_260MHz_01/reports/{top}.geom.rpt",
                    f"/project/build_GDSII_260MHz_01/reports/{top}.antenna.rpt"])

    def test_missing_or_invalid_top_cannot_read_unrelated_reports(self):
        for top in (None, "../../other_top"):
            with self.subTest(top=top):
                self.state["top_module"] = top
                ssh = FakeSSH(drc=0)
                result, _ = self.run_closure(ssh)
                self.assertEqual(result["physical_verification_status"], "failed")
                self.assertFalse(ssh.fetches)

    def test_repair_that_regresses_timing_retains_source_checkpoint(self):
        result, _ = self.run_closure(FakeSSH(slack=-0.03))
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertEqual(result["outputs"], self.state["outputs"])
        self.assertIsNone(result["route_back_to_stage"])
        self.assertIn("rejected_reason", result["physical_verification_attempts"][1])
        self.assertEqual(len(self.launches), 1)  # Repeated rejected plan is blocked.

    def test_non_improving_repair_preserves_source_and_blocks_repeat(self):
        result, _ = self.run_closure(FakeSSH(repaired_drc=2))
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(result["outputs"], self.state["outputs"])

    def test_one_attempt_budget_stops_with_remaining_violations(self):
        self.state["physical_verification_max_attempts"] = 1
        result, _ = self.run_closure(FakeSSH(drc=2, repaired_drc=1))
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertEqual(len(self.launches), 1)

    def test_backend_runs_physical_gate_inside_each_template_attempt(self):
        from workers import orchestrator
        def templates(state, ssh, implementation, merge):
            implementation(state)
            return implementation(state)
        with patch.object(orchestrator, "_make_gdsii_implementation_runner", return_value=self.implementation), \
             patch.object(orchestrator, "run_setup_closure", side_effect=templates) as timing, \
             patch.object(orchestrator, "run_physical_closure", return_value={"physical_verification_status": "passed"}) as physical:
            self.state["remote_gdsii_dir"] = self.state["gdsii_candidate_path"]
            result = orchestrator._make_gdsii_runner(object())(self.state)
        timing.assert_called_once()
        self.assertEqual(physical.call_count, 2)
        self.assertEqual(len(self.launches), 2)  # Repair runner is raw, not recursive.
        for call in physical.call_args_list:
            self.assertFalse(call.kwargs["require_setup_timing"])
            self.assertEqual(call.args[0]["stage_status"]["gdsii"], "success")
        self.assertEqual(result["physical_verification_status"], "passed")

    def test_clean_template_with_negative_setup_is_left_for_timing_agent(self):
        result, planner = self.run_closure(FakeSSH(drc=0, initial_slack=-0.03), require_setup_timing=False)
        self.assertEqual(result["physical_verification_status"], "passed")
        self.assertFalse(result["physical_verification_analysis"]["setup_passed"])
        planner.assert_not_called()

    def test_dirty_template_can_be_repaired_while_setup_is_still_negative(self):
        result, planner = self.run_closure(FakeSSH(initial_slack=-0.03, slack=-0.02), require_setup_timing=False)
        self.assertEqual(result["physical_verification_status"], "passed")
        self.assertEqual(result["physical_verification_analysis"]["drc_count"], 0)
        self.assertFalse(result["physical_verification_analysis"]["timing_passed"])
        planner.assert_called_once()

    def test_template_repair_cannot_worsen_existing_negative_setup(self):
        result, _ = self.run_closure(FakeSSH(initial_slack=-0.03, slack=-0.04), require_setup_timing=False)
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertEqual(result["outputs"], self.state["outputs"])

    def test_ai_response_fallback_and_diagnostics_keep_structured_recipe_schema(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("404 Not Found")
        response = Mock()
        context = {"available_recipes": ["refill_and_reroute"]}
        client.chat.completions.create.return_value = response
        path = Path(self.temp.name) / "ai_response.json"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
             patch.object(agent, "_build_client", return_value=client), \
             patch.object(agent, "_is_not_found_error", return_value=True), \
             patch.object(agent, "_recovery_response_text", return_value=(json.dumps(repair()), "response")):
            plan = agent.plan_physical_repair(context, diagnostics_path=path)
        self.assertEqual(plan, repair())
        self.assertEqual(json.loads(path.read_text())["status"], "validated")
        schema = client.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["schema"]
        self.assertIs(schema, agent.PLAN_SCHEMA)

    def test_selection_api_uses_physical_agent_schema_and_saves_diagnostics(self):
        client = Mock()
        selection = {"selected_attempt": 2, "summary": "Best setup paths with adequate hold margin."}
        client.responses.create.side_effect = RuntimeError("404 Not Found")
        context = {"eligible_attempts": [1, 2, 3], "presets": []}
        path = Path(self.temp.name) / "selection_response.json"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), \
             patch.object(agent, "_build_client", return_value=client), \
             patch.object(agent, "_is_not_found_error", return_value=True), \
             patch.object(agent, "_recovery_response_text", return_value=(json.dumps(selection), "response")):
            self.assertEqual(agent.select_resize_source(context, diagnostics_path=path), selection)
        self.assertEqual(json.loads(path.read_text())["status"], "validated")
        request = client.chat.completions.create.call_args.kwargs
        self.assertIs(request["response_format"]["json_schema"]["schema"], agent.SELECTION_SCHEMA)
        self.assertIn("hold_postroute.rpt", request["messages"][0]["content"])

    def test_selection_rejects_ineligible_ids_and_invalid_output(self):
        for selected in (2, True, "1", None):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                agent.validate_selection({"selected_attempt": selected, "summary": "Choice"}, [1, 3])
        with self.assertRaises(ValueError):
            agent.validate_selection({"selected_attempt": 1, "summary": ""}, [1])

    def test_unavailable_ai_cannot_waive_violations(self):
        with patch.object(closure, "plan_physical_repair", side_effect=RuntimeError("API unavailable")):
            result = closure.run_physical_closure(self.state, FakeSSH(), self.implementation,
                                                  lambda state, updates: {**state, **updates})
        self.assertEqual(result["physical_verification_status"], "failed")
        self.assertIn("API unavailable", result["last_error"])

    def test_ai_disabled_still_requires_clean_reports(self):
        self.state["physical_verification_ai_enabled"] = False
        result, planner = self.run_closure(FakeSSH())
        self.assertEqual(result["physical_verification_status"], "failed")
        planner.assert_not_called()

    def test_plan_rejects_script_injection_and_unsupported_recipes(self):
        for recipe in ("deleteInst logic", "antenna_cleanup", "reroute; exit 0"):
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                validate_plan(repair(recipe), ["refill_and_reroute"])
        with self.assertRaises(ValueError):
            build_physical_repair_tcl({"checkpoint": "/project/best;exec bad", "actions": repair()["actions"]}, "top", "diode")

    def test_generated_physical_script_restores_and_rechecks_everything(self):
        fixture = ROOT / "timing_closure/fixtures"
        outdir = Path(self.temp.name) / "generated"
        result = subprocess.run([sys.executable, str(ROOT / "probes/run_innovus_GDSII_universal.py"),
            "--mappeddir", str(fixture / "minimal_mapped"), "--pdk-root", str(fixture / "minimal_pdk"),
            "--outdir", str(outdir), "--setup-only", "--physical-repair-json",
            json.dumps({"checkpoint": "/project/best/db/06_final.enc", "actions": repair("antenna_cleanup")["actions"]})],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        script = (outdir / "tcl/run_innovus.tcl").read_text()
        self.assertIn("source $eco_checkpoint", script)
        self.assertIn("set antfix_max_passes 5", script)
        for check in ("verify_drc -report $REPORTS_DIR/${TOP}.geom.rpt", "verifyProcessAntenna -report $REPORTS_DIR/${TOP}.antenna.rpt",
                      "report_timing -max_paths", "report_timing -early", "report_power -power_unit W"):
            self.assertIn(check, script)
        self.assertLess(script.index("deleteFiller -prefix FILL"), script.index("addFiller -cell"))
        self.assertNotIn("ecoChangeCell", script)
        if shutil.which("tclsh"):
            # Run the actual generated Tcl parsers, including the legacy '#'/'='
            # antenna format that previously prevented targeted diode cleanup.
            report = Path(self.temp.name) / "antenna.rpt"
            report.write_text("net1 (1)\n  gate1 (cell) A\n#Total number of process antenna violations = 2\n")
            start = script.index("proc parse_antenna_targets")
            end = script.index("proc insert_targeted_antenna_diodes", start)
            parser_script = script[start:end] + f'puts "COUNT=[parse_antenna_violation_count {{{report}}}]"\nputs "TARGETS=[parse_antenna_targets {{{report}}}]"\n'
            parsed = subprocess.run(["tclsh"], input=parser_script, text=True, capture_output=True)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            self.assertIn("COUNT=2", parsed.stdout)
            self.assertIn("net1 gate1 A", parsed.stdout)


if __name__ == "__main__":
    unittest.main()
