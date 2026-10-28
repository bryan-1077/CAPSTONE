"""Resume backend timing closure from an existing completed mapped-stage state."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import _load_service_exports
from session_log_utils import run_with_session_logging
from workers.orchestrator import run_flow
from services.build_names import frequency_label


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=ROOT / "logs" / "full_session.log.state.json")
    parser.add_argument("--target-period", type=float, help="Defaults to the saved state's clock period")
    parser.add_argument("--attempts", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--recover-best", action="store_true", help="Restore and re-measure the best saved attempt, then use AI resizing if needed")
    parser.add_argument("--ai-attempts", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    _load_service_exports()
    state = json.loads(args.state.read_text())
    if state.get("stage_status", {}).get("mapped_netlist") != "success":
        parser.error("The saved state must contain a successful mapped_netlist stage.")
    target_period = args.target_period if args.target_period is not None else state.get("timing_target_clock_period_ns") or 4.762
    saved_attempts = state.get("timing_closure_attempts", []) if args.recover_best else []
    state.update(
        current_stage="gdsii", stop_after_stage=None, route_back_to_stage=None,
        reserve_named_builds=False,
        timing_closure_enabled=True, timing_closure_max_attempts=args.attempts,
        timing_target_clock_period_ns=target_period, timing_use_overconstraint=False,
        timing_closure_analysis={}, timing_closure_attempts=saved_attempts,
        timing_closure_recover_best=args.recover_best,
        timing_closure_ai_enabled=True, timing_closure_ai_max_attempts=args.ai_attempts,
        timing_closure_eco_plan=None,
        history=[], logs={}, last_error=None,
    )
    ssh_config = {
        "host": state["ssh_host"], "username": state["ssh_user"],
        "port": state.get("ssh_port", 22), "key_filename": state.get("ssh_key_file"),
    }
    result = run_with_session_logging(
        lambda: run_flow(state, ssh_config, thread_id=f"target_{frequency_label(target_period)}"),
        log_path=ROOT / "logs" / "timing_closure" / "timing_closure_session.log",
        parser_script_path=ROOT / "parse_full_session_log.py",
        parser_outdir=ROOT / "logs" / "timing_closure",
    )
    print(result.get("last_error") or f"Setup timing closure: {result.get('timing_closure_status')}")
    return 0 if result.get("timing_closure_status") == "passed" and result.get("current_stage") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
