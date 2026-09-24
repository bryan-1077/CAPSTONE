"""Sequential full-flow clock sweep, stopping at the first unsuccessful target."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from session_log_utils import SessionLogCapture


START_MHZ = 230
STEP_MHZ = 5


def run_max_clocking(run_target, *, output_dir=None):
    """Call run_target(MHz) synchronously; only completed, timing-gated runs pass."""
    root = Path(output_dir) if output_dir is not None else Path(__file__).resolve().parents[1] / "logs" / "max_clocking"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    summary = {
        "mode": "max_clocking",
        "current_stage": "running",
        "start_mhz": START_MHZ,
        "step_mhz": STEP_MHZ,
        "best_mhz": None,
        "best_state_path": None,
        "first_failed_mhz": None,
        "last_error": None,
        "attempts": [],
        "report_path": str(run_dir / "report.md"),
    }

    def save():
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        best = summary["best_mhz"]
        lines = [
            "# Max clocking", "",
            f"Highest passing frequency: {best} MHz" if best is not None else "No passing frequency found.",
            "",
            "| Target (MHz) | Result | WNS (ns) |",
            "| --- | --- | --- |",
        ]
        for attempt in summary["attempts"]:
            lines.append(f"| {attempt['mhz']} | {attempt['status']} | {attempt.get('wns_ns', '')} |")
        if summary["first_failed_mhz"] is not None:
            lines.extend(["", f"Stopped at {summary['first_failed_mhz']} MHz: {summary['last_error']}"])
        if summary["best_state_path"]:
            lines.extend(["", f"Best run state: `{summary['best_state_path']}`"])
        (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    mhz = START_MHZ
    while True:
        attempt = {"mhz": mhz, "period_ns": 1000 / mhz, "status": "running"}
        summary["attempts"].append(attempt)
        save()
        print(f"Max clocking: starting {mhz} MHz; waiting for the full flow to finish.", flush=True)
        state_path = run_dir / f"{mhz}MHz.state.json"
        with SessionLogCapture(run_dir / f"{mhz}MHz.log"):
            try:
                result = run_target(mhz)
                if not isinstance(result, dict):
                    raise ValueError("Flow returned no usable result state.")
            except Exception as exc:
                result = {"current_stage": "failed", "last_error": f"{type(exc).__name__}: {exc}"}
            state_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

        passed = (
            result.get("current_stage") == "done"
            and result.get("stage_status", {}).get("gdsii") == "success"
            and result.get("timing_closure_status") == "passed"
            and not result.get("last_error")
        )
        attempt.update(
            status="passed" if passed else "failed",
            state_path=str(state_path),
            gdsii_dir=result.get("remote_gdsii_dir"),
            wns_ns=(result.get("timing_closure_analysis") or {}).get("wns"),
        )
        if passed:
            summary.update(best_mhz=mhz, best_state_path=str(state_path))
            save()
            print(f"Max clocking: {mhz} MHz passed.", flush=True)
            mhz += STEP_MHZ
            continue

        reason = result.get("last_error") or "Full flow did not complete with passing post-route setup timing."
        attempt["error"] = reason
        summary.update(
            current_stage="done" if summary["best_mhz"] is not None else "failed",
            first_failed_mhz=mhz,
            last_error=reason,
        )
        save()
        print(f"Max clocking stopped at {mhz} MHz: {reason}", flush=True)
        if summary["best_mhz"] is None:
            print(f"No passing frequency found; the initial {START_MHZ} MHz run failed.", flush=True)
        else:
            print(f"Best passing frequency: {summary['best_mhz']} MHz.", flush=True)
        print(f"Max clocking report: {summary['report_path']}", flush=True)
        return summary
