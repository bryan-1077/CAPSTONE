"""Require clean physical reports and remeasure timing after every repair."""
import json
import math
import re
import shlex
from pathlib import Path
from uuid import uuid4

from agents.openai_physical_verification import available_recipes, plan_physical_repair, validate_plan
from physical_verification.reports import analyze_physical_reports
from timing_closure.closure_runner import evaluate_setup_report
from timing_closure.constraints import evaluate_limits
from timing_closure.remote_analyze import fetch_remote_reports
from timing_closure.timing_analyzer import parse_timing_report
from session_log_utils import SessionLogCapture


def report_names(state):
    top = next((str(state.get(key) or "").strip()
                for key in ("mapped_resolved_top", "mapped_top_module", "top_module")
                if str(state.get(key) or "").strip()), "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top):
        raise ValueError("Physical verification requires a valid resolved top module for report filenames.")
    return (f"{top}.geom.rpt", f"{top}.antenna.rpt", "timing_postroute.rpt",
            "hold_postroute.rpt", "die_area.rpt", "power_postroute.rpt")


def measure(state, ssh, outdir, staging, *, require_setup_timing=True):
    names = report_names(state)
    paths, fetched = fetch_remote_reports(ssh=ssh, remote_project_root=state["remote_project_root"],
        report_paths=[f"{outdir}/reports/{name}" for name in names], local_staging_dir=staging)
    if len(paths) != len(names):
        missing = [item.get("remote_path") for item in fetched if not item["ok"]]
        raise ValueError(f"Fresh physical/timing reports are required; missing: {missing}")
    drc, antenna, setup_path, hold_path, area, power = paths
    evidence = analyze_physical_reports(drc.read_text(), antenna.read_text())
    target = float(state.get("timing_overconstraint_clock_period_ns")
                   if state.get("timing_use_overconstraint") else state["timing_target_clock_period_ns"])
    setup = parse_timing_report(setup_path)
    setup_status, message = evaluate_setup_report(setup, target)
    if setup_status == "error":
        raise ValueError(message)
    hold = parse_timing_report(hold_path)
    if hold.wns is None or not math.isfinite(hold.wns) or not hold.paths:
        raise ValueError("Fresh hold report contains no usable slack/paths.")
    limits = evaluate_limits(area.read_text(), power.read_text())
    hold_passed = (hold.wns >= 0 and not hold.violating_path_count
                   and (hold.tns is None or hold.tns >= 0))
    evidence.update(outdir=outdir, report_paths=[str(path) for path in paths], remote_fetch=fetched,
        setup_wns_ns=setup.wns, hold_wns_ns=hold.wns, target_period_ns=target,
        setup_passed=setup_status == "passed", hold_passed=hold_passed,
        timing_passed=setup_status == "passed" and hold_passed,
        require_setup_timing=require_setup_timing,
        limits=limits)
    evidence["passed"] = (evidence["drc_count"] == 0 and evidence["antenna_count"] == 0
                          and hold_passed and (not require_setup_timing or evidence["setup_passed"])
                          and limits["within_limits"])
    return evidence


def run_physical_closure(state, ssh, implementation_runner, merge, *, require_setup_timing=True):
    if not state.get("physical_verification_enabled", True):
        return merge(state, {"physical_verification_status": "disabled"})
    if state.get("stage_status", {}).get("gdsii") != "success":
        return state
    repo = Path(__file__).resolve().parents[1]
    log_root = Path(state.get("backend_log_dir") or repo / "logs") / "physical_verification"
    log_dir = log_root / uuid4().hex
    log_dir.mkdir(parents=True, exist_ok=False)
    with SessionLogCapture(log_dir / "physical_verification.log"):
        return _run(state, ssh, implementation_runner, merge, repo, log_root, log_dir,
                    require_setup_timing=require_setup_timing)


def _run(state, ssh, implementation_runner, merge, repo, log_root, log_dir, *, require_setup_timing):
    attempts = []
    working = merge(state, {"physical_verification_status": "running", "physical_verification_attempts": []})

    def record(status, message):
        nonlocal working
        print(f"Physical verification: {message}", flush=True)
        working = merge(working, {
            "physical_verification_status": status, "physical_verification_attempts": list(attempts),
            "physical_verification_report_dir": str(log_dir),
            "history": working.get("history", []) + [message],
        })
        payload = {"status": status, "message": message, "attempts": attempts, "report_dir": str(log_dir)}
        for destination in (log_root, log_dir):
            (destination / "physical_verification_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        (log_dir / "state.json").write_text(json.dumps(working, indent=2) + "\n")

    def fail(message):
        nonlocal working
        working = merge(working, {"stage_status": {**working["stage_status"], "gdsii": "failed"},
            "last_error": message, "failure_stage_owner": "physical_verification",
            "failure_class": "physical_verification_failure", "route_back_to_stage": None})
        record("failed", message)
        return working

    try:
        limit = int(state.get("physical_verification_max_attempts", 3))
        if not 0 <= limit <= 10:
            return fail("Physical verification supports 0 to 10 repair attempts.")
        source_dir = state.get("gdsii_candidate_path") or state.get("remote_gdsii_dir")
        if not source_dir:
            return fail("Physical verification requires the current GDSII output directory.")
        source_state = state
        evidence = measure(state, ssh, source_dir, log_dir / "initial", require_setup_timing=require_setup_timing)
        attempts.append({"attempt": 0, "kind": "verification", **evidence})
        tried = set()
        for attempt in range(limit + 1):
            if evidence["passed"]:
                working = merge(working, {
                    "physical_verification_analysis": evidence,
                    "validation": {**working.get("validation", {}), "physical_verification": evidence},
                    "stage_status": {**working["stage_status"], "gdsii": "success"}, "last_error": None,
                    "failure_stage_owner": None, "failure_class": None, "route_back_to_stage": None,
                })
                record("passed", f"DRC=0, antenna=0; setup WNS {evidence['setup_wns_ns']:.3f} ns, hold WNS {evidence['hold_wns_ns']:.3f} ns.")
                return working
            record("violated", f"{source_dir}: DRC={evidence['drc_count']}, antenna={evidence['antenna_count']}.")
            if not evidence["drc_count"] and not evidence["antenna_count"]:
                return fail("Physical reports are clean but final timing or area/power checks failed.")
            if not evidence["drc_count"]:
                return fail("DRC=0; physical repair agent skipped. Process antenna violations remain.")
            if attempt == limit or not state.get("physical_verification_ai_enabled", True):
                return fail("Physical verification could not reach zero DRC and antenna violations within its repair budget.")
            context = {"evidence": evidence, "available_recipes": available_recipes(evidence),
                       "attempt_history": attempts}
            (log_dir / f"context_{attempt + 1}.json").write_text(json.dumps(context, indent=2) + "\n")
            plan = validate_plan(plan_physical_repair(context,
                diagnostics_path=log_dir / f"ai_response_{attempt + 1}.json"), context["available_recipes"])
            if not plan["actions"]:
                return fail("Physical repair agent stopped: " + plan["summary"])
            signature = (source_dir, tuple(sorted(a["recipe"] for a in plan["actions"])))
            if signature in tried:
                return fail("Physical repair agent repeated a previously measured repair; stopping.")
            tried.add(signature)
            project = state["remote_project_root"]
            outdir = f"{source_dir}_physical_{log_dir.name[:8]}_{attempt + 1:02d}"
            reservation = ssh.run(f"mkdir -- {shlex.quote(outdir)}", cwd=project)
            if not reservation["ok"]:
                return fail("Cannot reserve a fresh physical repair output directory.")
            script = f".physical_verification_runner_{log_dir.name}_{attempt + 1}.py"
            ssh.upload_file(repo / "probes/run_innovus_GDSII_universal.py", f"{project}/{script}", exclusive=True)
            candidate = implementation_runner(merge(source_state, {
                "history": working.get("history", []), "remote_gdsii_dir": outdir, "gdsii_script": script,
                "gdsii_max_iterations": 1, "timing_closure_eco_plan": None,
                "physical_verification_plan": {"checkpoint": f"{project}/{source_dir}/db/06_final.enc", "actions": plan["actions"]},
                "innovus_final_postroute_setup_opt": False, "timing_closure_enable_remote_analysis": False,
                "stage_status": {**source_state["stage_status"], "gdsii": "running"},
            }))
            entry = {"attempt": attempt + 1, "kind": "ai_physical_repair", "plan": plan,
                     "source_outdir": source_dir, "outdir": outdir}
            attempts.append(entry)
            (log_dir / f"candidate_{attempt + 1}_state.json").write_text(json.dumps(candidate, indent=2) + "\n")
            if candidate.get("stage_status", {}).get("gdsii") != "success":
                entry["rejected_reason"] = candidate.get("last_error") or "Implementation failed."
                return fail("Physical repair execution failed: " + entry["rejected_reason"])
            measured = measure(candidate, ssh, outdir, log_dir / f"attempt_{attempt + 1}",
                               require_setup_timing=require_setup_timing)
            entry.update(measured)
            setup_regressed = (not measured["setup_passed"] and
                               (require_setup_timing or measured["setup_wns_ns"] < evidence["setup_wns_ns"]))
            if setup_regressed or not measured["hold_passed"] or not measured["limits"]["within_limits"]:
                entry["rejected_reason"] = "Repair regressed timing or exceeded area/power limits; source checkpoint retained."
                record("rejected", entry["rejected_reason"])
                continue
            if measured["drc_count"] + measured["antenna_count"] >= evidence["drc_count"] + evidence["antenna_count"]:
                entry["rejected_reason"] = "Repair did not reduce physical violations; source checkpoint retained."
                record("rejected", entry["rejected_reason"])
                continue
            working = candidate
            source_state, source_dir, evidence = candidate, outdir, measured
        return fail("Physical verification repair budget exhausted.")
    except Exception as exc:
        return fail(f"Physical verification failed: {exc}")
