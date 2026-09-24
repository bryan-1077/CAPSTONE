"""Bounded backend setup closure, gated on the current attempt's routed report."""
from __future__ import annotations

import json
from copy import deepcopy
import math
import re
import shlex
from pathlib import Path
from uuid import uuid4

from timing_closure.remote_analyze import fetch_remote_reports
from timing_closure.timing_analyzer import analyze_reports, parse_timing_report, render_markdown
from services.path_naming import resolve_prepare_dir, resolve_netlist_dir_choice, resolve_mapped_dir_choice
from services import mapped_snapshot
from services.build_names import frequency_label
from session_log_utils import SessionLogCapture
from agents.openai_timing_closure import plan_timing_recovery, resize_choices, validate_plan
from timing_closure.constraints import evaluate_limits, MAX_DIE_AREA_MM2, MAX_POWER_W


def evaluate_setup_report(report, target_period: float) -> tuple[str, str]:
    # Do not accept the analyzer's required-time fallback as proof of clock period.
    text = Path(report.source_file).read_text(encoding="utf-8", errors="replace")
    periods = re.findall(
        r"(?:\bPhase Shift\s+|\bclock period\s*[:=]\s*|\bcreate_clock[^\n;]*?-period\s+)([0-9]+(?:\.[0-9]+)?)",
        text, flags=re.IGNORECASE,
    )
    if not periods or any(abs(float(value) - target_period) > 0.00051 for value in periods):
        return "error", f"Routed report does not confirm the requested {target_period:.3f} ns period."
    if report.wns is None or not math.isfinite(report.wns) or not report.paths:
        return "error", "Routed report contains no usable setup paths/slack."
    if report.wns < 0 or (report.tns is not None and report.tns < 0) or report.violating_path_count:
        return "violated", f"Setup timing failed: WNS {report.wns:.3f} ns at {target_period:.3f} ns."
    return "passed", f"Setup timing passed: WNS {report.wns:.3f} ns at {target_period:.3f} ns."


def backend_settings(state: dict, attempt: int) -> dict:
    # Three reproducible experiments. Never relax the requested clock to claim closure.
    profiles = [(0.60, 0.25, 0.50), (0.55, 0.18, 0.35), (0.50, 0.12, 0.25)]
    base = profiles[0]
    profile = profiles[attempt - 1]
    keys = ("innovus_utilization", "innovus_ccopt_target_skew", "innovus_ccopt_target_max_transition")
    return {
        key: round(float(state.get(key) or initial) * value / initial, 4)
        for key, initial, value in zip(keys, base, profile)
    }


def run_setup_closure(state, ssh, implementation_runner, merge):
    log_path = Path(__file__).resolve().parents[1] / "logs" / "timing_closure" / "timing_closure.log"
    run_id = uuid4().hex
    run_name = f"target_{frequency_label(state.get('timing_target_clock_period_ns') or 4.762)}"
    if state.get("timing_closure_recover_best"):
        run_name += f"_recovery_{run_id[:8]}"
    with SessionLogCapture(log_path):
        with SessionLogCapture(log_path.parent / run_name / "timing_closure.log"):
            return _run_setup_closure(state, ssh, implementation_runner, merge, run_id=run_id)


def _run_setup_closure(state, ssh, implementation_runner, merge, *, run_id=None):
    repo = Path(__file__).resolve().parents[1]
    log_dir = repo / "logs" / "timing_closure"
    log_dir.mkdir(parents=True, exist_ok=True)
    report_dir = repo / "timing_closure"
    report_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or uuid4().hex
    run_name = f"target_{frequency_label(state.get('timing_target_clock_period_ns') or 4.762)}"
    if state.get("timing_closure_recover_best"):
        run_name += f"_recovery_{run_id[:8]}"
    run_log_dir = log_dir / run_name
    run_log_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(state.get("timing_closure_local_staging_dir") or repo / ".timing_closure_remote") / run_id
    staging.mkdir(parents=True, exist_ok=False)
    attempts = []
    candidates = []
    tried_plans = set()
    working = merge(state, {
        "timing_closure_run_name": run_name,
        "timing_closure_status": "running",
        "timing_closure_attempts": [],
        "timing_closure_analysis": {},
        "timing_closure_best_attempt": None,
        "timing_closure_limits": {"max_die_area_mm2": MAX_DIE_AREA_MM2, "max_power_w": MAX_POWER_W},
        "history": state["history"] + ["Timing closure enabled: fresh routed setup report required for success"],
    })

    def record(status, message, analysis=None):
        nonlocal working
        updates = {
            "timing_closure_status": status,
            "timing_closure_attempts": list(attempts),
            "history": working["history"] + [message],
        }
        if analysis is not None:
            updates["timing_closure_analysis"] = analysis
            updates["validation"] = {**working.get("validation", {}), "timing_closure_analysis": analysis}
        working = merge(working, updates)
        summary = {"run_name": run_name, "run_id": run_id, "status": status, "message": message, "attempts": attempts,
                   "limits": working["timing_closure_limits"], "best_attempt": working.get("timing_closure_best_attempt")}
        (log_dir / "timing_closure_status.json").write_text(json.dumps(summary, indent=2) + "\n")
        (run_log_dir / "timing_closure_status.json").write_text(json.dumps(summary, indent=2) + "\n")
        report_text = f"# Setup timing closure: {status}\n\n{message}\n\nRun: `{run_name}`\n\n"
        if analysis is not None:
            report_text += render_markdown(analysis)
        report_text += "\nLimits: die footprint <= 4 mm²; reported total power <= 2 W.\n"
        for entry in attempts:
            report_text += f"\nAttempt {entry['attempt']} ({entry.get('kind', 'preset')}): {entry['status']}; WNS {entry.get('wns_ns')}; measurements {entry.get('limits', {})}\n"
            if entry.get("ai_plan"):
                report_text += f"AI rationale: {entry['ai_plan']['summary']}\n"
        (report_dir / "timing_closure_report.md").write_text(report_text)

    def fail(message):
        record("failed", message, working.get("timing_closure_analysis") or None)
        return merge(working, {
            "stage_status": {**working.get("stage_status", {}), "gdsii": "failed"},
            "last_error": message,
            # A backend-only closure failure must not rebuild shared upstream inputs.
            "route_back_to_stage": None,
        })

    record("running", f"Preparing backend setup closure: {run_name}.")
    try:
        target = float(state.get("timing_target_clock_period_ns") or 4.762)
        if state.get("timing_use_overconstraint"):
            target = float(state.get("timing_overconstraint_clock_period_ns") or target)
        if not math.isfinite(target) or target <= 0:
            return fail("Timing closure requires a finite positive target period.")
        limit = int(state.get("timing_closure_max_attempts", 3))
        if limit not in (1, 2, 3):
            return fail("Timing closure supports 1 to 3 backend attempts.")
        ai_limit = int(state.get("timing_closure_ai_max_attempts", 3)) if state.get("timing_closure_ai_enabled", True) else 0
        if not 0 <= ai_limit <= 10:
            return fail("AI timing closure supports 0 to 10 recovery attempts.")
        recovery_source = None
        if state.get("timing_closure_recover_best"):
            eligible = [entry for entry in state.get("timing_closure_attempts", [])
                        if entry.get("status") in {"violated", "passed"}
                        and isinstance(entry.get("wns_ns"), (int, float)) and math.isfinite(entry["wns_ns"])
                        and abs(float(entry.get("target_period_ns", 0)) - target) <= 0.00051
                        and entry.get("outdir") and entry.get("mapped_snapshot")]
            if not eligible:
                return fail("Saved state has no measured checkpoint at the requested clock period to recover.")
            recovery_source = max(eligible, key=lambda entry: entry["wns_ns"])
            limit = 1  # Re-measure the saved best checkpoint before asking AI for edits.
        project_root = state["remote_project_root"]
        prep_dir, _ = resolve_prepare_dir(state.get("prep_candidate_path"), state.get("remote_prepare_dir"))
        netlist_dir, _ = resolve_netlist_dir_choice(state.get("netlist_candidate_path") or state.get("remote_netlist_dir"), prep_dir)
        source_dir, _ = resolve_mapped_dir_choice(state.get("mapped_candidate_path") or state.get("remote_mapped_dir"), netlist_dir, prep_dir)
        naming_period = float(state.get("timing_target_clock_period_ns") or target)
        frequency = frequency_label(naming_period)
        snapshot_dir = recovery_source["mapped_snapshot"] if recovery_source else f"build_mapped_snapshot_{frequency}_01"
        snapshot_code = Path(mapped_snapshot.__file__).read_text()
        snapshot_command = " ".join(shlex.quote(value) for value in (
            state.get("remote_python_cmd") or "python3", "-c", snapshot_code, source_dir, snapshot_dir,
        ))
        if not recovery_source:
            snapshot_result = ssh.run(snapshot_command, cwd=project_root, timeout=120)
            if not snapshot_result["ok"]:
                return fail(f"Cannot isolate mapped inputs: {snapshot_result.get('stderr', '')}")
        working = merge(working, {
            "mapped_candidate_path": snapshot_dir,
            "remote_mapped_dir": snapshot_dir,
            "timing_closure_source_mapped_dir": source_dir,
            "timing_closure_mapped_snapshot_dir": snapshot_dir,
            "history": working["history"] + [f"Timing closure mapped inputs isolated: {source_dir} -> {snapshot_dir}"],
        })
        base_dir = state.get("timing_closure_base_outdir") or "build_GDSII"
        if recovery_source:
            base_dir = f"{base_dir}_recovery_{run_id[:8]}"
        # Label by the nominal internal-clock target, even when optimizing with
        # a tighter overconstraint. Round MHz to absorb period precision (4.762 -> 210).
        for attempt in range(1, limit + ai_limit + 1):
            is_ai = attempt > limit
            plan = None
            eco_plan = None
            source_entry = None
            input_state = working
            if is_ai:
                if not candidates:
                    return fail("No measured checkpoint satisfies the 4 mm² / 2 W limits; AI resizing cannot start.")
                best = max(candidates, key=lambda item: item["entry"]["wns_ns"])
                source_entry = best["entry"]
                library_paths, _ = fetch_remote_reports(
                    ssh=ssh, remote_project_root=project_root,
                    report_paths=[f"{source_entry['outdir']}/reports/eco_library.rpt"], local_staging_dir=staging,
                )
                if len(library_paths) != 1:
                    return fail("The best checkpoint has no available-cell inventory; AI cannot guess cell sizes.")
                timing_text = Path(source_entry["report"]).read_text()
                choices = resize_choices(timing_text, library_paths[0].read_text())
                if not choices:
                    return fail("No supported gate/buffer resize choices were found on the reported critical paths.")
                context = {
                    "target_period_ns": target, "source_attempt": source_entry,
                    "limits": {"max_die_area_mm2": MAX_DIE_AREA_MM2, "max_power_w": MAX_POWER_W},
                    "timing_report": timing_text, "resize_choices": choices,
                    "attempt_history": deepcopy(attempts),
                }
                record("planning", f"AI recovery {attempt - limit}/{ai_limit}: inspect best attempt {source_entry['attempt']} with WNS {source_entry['wns_ns']:.3f} ns.")
                plan = validate_plan(plan_timing_recovery(context), choices)
                (run_log_dir / f"ai_plan_{attempt}.json").write_text(json.dumps({"context": context, "plan": plan}, indent=2) + "\n")
                if not plan["actions"]:
                    return fail(f"AI timing recovery stopped: {plan['summary']}")
                signature = json.dumps([source_entry["outdir"], sorted(
                    [(a["instance"], a["from_cell"], a["to_cell"]) for a in plan["actions"]])])
                if signature in tried_plans:
                    return fail("AI repeated an already measured resize plan; stopping without rerunning it.")
                tried_plans.add(signature)
                settings = source_entry["settings"]
                input_state = best["state"]
                eco_plan = {"checkpoint": f"{project_root}/{source_entry['outdir']}/db/06_final.enc", "actions": plan["actions"]}
            elif recovery_source:
                settings = recovery_source["settings"]
                source_entry = recovery_source
                eco_plan = {"checkpoint": f"{project_root}/{recovery_source['outdir']}/db/06_final.enc", "actions": []}
            else:
                settings = backend_settings(state, attempt)
            outdir = (f"{base_dir}_{frequency}_ai_{run_id[:8]}_{attempt - limit:02d}" if is_ai
                      else f"{base_dir}_{frequency}_{attempt:02d}")
            reservation = ssh.run(f"mkdir -- {shlex.quote(outdir)}", cwd=project_root)
            if not reservation["ok"]:
                return fail(f"Cannot reserve fresh timing output directory {outdir}; existing builds are never overwritten: {reservation['stderr']}")
            script = (f".timing_closure_runner_{frequency}_{run_id[:8]}_{attempt:02d}.py" if is_ai or recovery_source
                      else f".timing_closure_runner_{frequency}_{attempt:02d}.py")
            ssh.upload_file(repo / "probes" / "run_innovus_GDSII_universal.py", f"{project_root}/{script}", exclusive=True)
            entry = {"attempt": attempt, "outdir": outdir, "mapped_snapshot": snapshot_dir, "target_period_ns": target, "settings": settings, "status": "running",
                     "kind": "ai_resize" if is_ai else "restore_best" if recovery_source else "preset",
                     "source_outdir": source_entry["outdir"] if source_entry else None}
            if plan:
                entry["ai_plan"] = plan
            attempts.append(entry)
            record("running", f"Timing closure {entry['kind']} attempt {attempt}: target {target:.3f} ns, output {outdir}")
            working = implementation_runner(merge({**input_state, "timing_closure_analysis": {}}, {
                **settings,
                "history": working["history"],
                "timing_closure_eco_plan": eco_plan,
                "timing_closure_limits": working["timing_closure_limits"],
                "timing_closure_best_attempt": working.get("timing_closure_best_attempt"),
                "remote_gdsii_dir": outdir,
                "gdsii_script": script,
                "gdsii_max_iterations": 1,
                "timing_target_clock_period_ns": target,
                "innovus_final_postroute_setup_opt": True,
                # Closure fetches only this run's post-route report, never old mapped/CTS reports.
                "timing_closure_enable_remote_analysis": False,
                "stage_status": {**working.get("stage_status", {}), "gdsii": "running"},
            }))
            attempt_log_dir = run_log_dir
            attempt_log_dir.mkdir(parents=True, exist_ok=True)
            (attempt_log_dir / f"attempt_{attempt}_state.json").write_text(json.dumps(working, indent=2) + "\n")
            if working.get("stage_status", {}).get("gdsii") != "success":
                entry["status"] = "implementation_failed"
                return fail(working.get("last_error") or "Backend implementation failed before timing validation.")
            working = merge(working, {"stage_status": {**working["stage_status"], "gdsii": "checking_timing"}})
            paths, fetched = fetch_remote_reports(
                ssh=ssh, remote_project_root=project_root,
                report_paths=[f"{outdir}/reports/timing_postroute.rpt"],
                local_staging_dir=staging,
            )
            if len(paths) != 1:
                entry["status"] = "report_missing"
                entry["remote_fetch"] = fetched
                return fail("Fresh post-route timing report could not be fetched; timing closure cannot pass.")
            report = parse_timing_report(paths[0])
            analysis = analyze_reports([report], target_period=target)
            analysis["remote_fetch"] = fetched
            status, message = evaluate_setup_report(report, target)
            entry.update(status=status, wns_ns=report.wns, report=str(paths[0]))
            if status == "error":
                record(status, message, analysis)
                return fail(message)
            metric_paths, metric_fetch = fetch_remote_reports(
                ssh=ssh, remote_project_root=project_root,
                report_paths=[f"{outdir}/reports/die_area.rpt", f"{outdir}/reports/power_postroute.rpt"],
                local_staging_dir=staging,
            )
            entry["metrics_fetch"] = metric_fetch
            if len(metric_paths) != 2:
                entry["status"] = "metrics_missing"
                return fail("Fresh die-area and total-power reports are required to enforce 4 mm² / 2 W.")
            try:
                entry["limits"] = evaluate_limits(metric_paths[0].read_text(), metric_paths[1].read_text())
            except ValueError as exc:
                entry["status"] = "metrics_invalid"
                return fail(str(exc))
            working["timing_closure_limits"] = entry["limits"]
            if not entry["limits"]["within_limits"]:
                status = "limits_exceeded"
                message = f"Candidate rejected: die {entry['limits']['die_area_mm2']:.6f} mm², power {entry['limits']['total_power_w']:.6f} W; limits 4 mm² / 2 W."
            if is_ai:
                hold_paths, _ = fetch_remote_reports(
                    ssh=ssh, remote_project_root=project_root,
                    report_paths=[f"{outdir}/reports/hold_postroute.rpt"], local_staging_dir=staging,
                )
                if len(hold_paths) != 1:
                    entry["status"] = "hold_missing"
                    return fail("AI candidate has no fresh hold-timing report.")
                hold = parse_timing_report(hold_paths[0])
                if hold.wns is None or not math.isfinite(hold.wns) or not hold.paths:
                    entry["status"] = "hold_missing"
                    return fail("AI candidate hold timing cannot be verified.")
                entry["hold_wns_ns"] = hold.wns
                if hold.wns < 0 or (hold.tns is not None and hold.tns < 0) or hold.violating_path_count:
                    status = "hold_violated"
                    message = f"AI resize candidate rejected: hold slack {hold.wns:.3f} ns."
            entry["status"] = status
            if status in {"passed", "violated"} and entry["limits"]["within_limits"]:
                candidates.append({"entry": dict(entry), "state": dict(working)})
                working["timing_closure_best_attempt"] = max(candidates, key=lambda item: item["entry"]["wns_ns"])["entry"]
            record(status, message, analysis)
            (attempt_log_dir / f"attempt_{attempt}_state.json").write_text(json.dumps(working, indent=2) + "\n")
            if status == "passed":
                return merge(working, {
                    "stage_status": {**working["stage_status"], "gdsii": "success"},
                    "last_error": None,
                })
        return fail(f"Timing/area/power closure failed after {limit} backend attempts and {ai_limit} AI recovery attempts. Best eligible attempt: {working.get('timing_closure_best_attempt', {}).get('outdir') if working.get('timing_closure_best_attempt') else 'none'}. See timing_closure/timing_closure_report.md.")
    except Exception as exc:
        return fail(f"Timing closure failed: {exc}")
