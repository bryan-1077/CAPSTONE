"""Compare attributed preset reports before handing a checkpoint to the resizer."""
import math
from pathlib import Path

from physical_verification.reports import analyze_physical_reports
from timing_closure.remote_analyze import fetch_remote_reports
from timing_closure.recovery_context import excerpt
from timing_closure.timing_analyzer import parse_timing_report
from timing_closure.constraints import MAX_DIE_AREA_MM2, MAX_POWER_W


def build_selection_context(state, ssh, staging, attempts, candidates, target):
    # Imported at call time because setup closure also uses this report collector.
    from physical_verification.closure_runner import report_names
    from timing_closure.closure_runner import evaluate_setup_report

    eligible_ids = {item["entry"]["attempt"] for item in candidates}
    geom, antenna, setup, hold, _, _ = report_names(state)
    context = {"target_period_ns": target, "eligible_attempts": [], "presets": [],
               "limits": {"max_die_area_mm2": MAX_DIE_AREA_MM2, "max_power_w": MAX_POWER_W}}
    for entry in attempts:
        item = {"attempt": entry["attempt"], "outdir": entry["outdir"],
                "status": entry["status"], "limits": entry.get("limits", {}),
                "eligible": False, "reports": {}}
        if entry.get("error"):
            item["prior_error"] = entry["error"]
        context["presets"].append(item)
        paths = {}
        for kind, name in (("geometry", geom), ("antenna", antenna), ("setup", setup), ("hold", hold)):
            remote = f"{entry['outdir']}/reports/{name}"
            fetched, results = fetch_remote_reports(ssh=ssh,
                remote_project_root=state["remote_project_root"], report_paths=[remote],
                local_staging_dir=staging)
            record = {"source_build": entry["outdir"], "remote_path": remote}
            item["reports"][kind] = record
            if not fetched:
                record.update(status="missing", error=results[0].get("error", "Fetch failed"))
                continue
            path = paths[kind] = fetched[0]
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            record.update(status="included", content=excerpt(text, 16000),
                          original_chars=len(text), truncated=len(text) > 16000)
        try:
            if len(paths) != 4:
                raise ValueError("Geometry, antenna, setup, and hold reports are required for selection.")
            counts = analyze_physical_reports(paths["geometry"].read_text(), paths["antenna"].read_text())
            setup_report = parse_timing_report(paths["setup"])
            setup_status, setup_message = evaluate_setup_report(setup_report, target)
            hold_report = parse_timing_report(paths["hold"])
            hold_passed = (hold_report.wns is not None and math.isfinite(hold_report.wns)
                           and bool(hold_report.paths) and hold_report.wns >= 0
                           and not hold_report.violating_path_count
                           and (hold_report.tns is None or hold_report.tns >= 0))
            item.update(drc_count=counts["drc_count"], antenna_count=counts["antenna_count"],
                        setup_wns_ns=setup_report.wns, setup_tns_ns=setup_report.tns,
                        hold_wns_ns=hold_report.wns, hold_passed=hold_passed)
            if setup_status == "error":
                raise ValueError(setup_message)
            item["eligible"] = (entry["attempt"] in eligible_ids
                and counts["drc_count"] == counts["antenna_count"] == 0
                and hold_passed and item["limits"].get("within_limits", False))
            if item["eligible"]:
                context["eligible_attempts"].append(entry["attempt"])
            else:
                item["rejection_reason"] = "Preset failed physical verification, hold timing, or area/power limits."
        except ValueError as exc:
            item["rejection_reason"] = str(exc)
    return context
