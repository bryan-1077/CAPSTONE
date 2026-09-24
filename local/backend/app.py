import argparse
import math
import os
import re
from pprint import pprint
from pathlib import Path
from services.build_names import full_flow_names

from workers.orchestrator import run_flow
from schemas.state import FlowState
from session_log_utils import run_with_session_logging
from timing_closure.max_clocking import run_max_clocking


DEFAULT_PREPARE_SCRIPT = "prepare_rtl_for_genus_universal.py"
DEFAULT_NETLIST_SCRIPT = "run_genus_netlist_universal.py"
DEFAULT_MAPPED_SCRIPT = "run_genus_mapped_universal.py"
DEFAULT_GDSII_SCRIPT = "run_innovus_GDSII_universal.py"


EXPORT_LINE_RE = re.compile(
    r'^\s*export\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>".*"|\'.*\'|[^\s#]+)\s*$'
)


def _load_service_exports() -> None:
    exports_path = Path(__file__).resolve().parent / "services" / "exports.txt"
    if not exports_path.exists():
        return

    for raw_line in exports_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXPORT_LINE_RE.match(line)
        if not match:
            continue

        name = match.group("name").strip()
        value = match.group("value").strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


def _env_float(name: str):
    raw_value = str(os.getenv(name, "")).strip()
    if not raw_value:
        return None
    try:
        parsed = float(raw_value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _env_int(name: str, default: int) -> int:
    raw_value = str(os.getenv(name, "")).strip()
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    return str(os.getenv(name, "1" if default else "0")).strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str):
    raw_value = str(os.getenv(name, "")).strip()
    if not raw_value:
        return []
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _positive_mhz(value: str) -> float:
    try:
        mhz = float(value)
        period = 1000 / mhz
    except (ValueError, ZeroDivisionError, OverflowError):
        raise argparse.ArgumentTypeError("frequency must be a finite positive number") from None
    if not math.isfinite(mhz) or mhz <= 0 or not math.isfinite(period) or period <= 0:
        raise argparse.ArgumentTypeError("frequency must be a finite positive number")
    return mhz


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run the EDA flow with a clock target in MHz.")
    parser.add_argument("command", nargs="*", metavar="COMMAND", help="max clocking: sweep from 230 MHz in 10 MHz steps until failure")
    parser.add_argument("--max-clocking", action="store_true", help="same as the 'max clocking' command")
    parser.add_argument(
        "--target-mhz", type=_positive_mhz, metavar="MHZ",
        help="target frequency, e.g. 230; overrides the environment period and disables inherited overconstraint",
    )
    overconstraint = parser.add_mutually_exclusive_group()
    overconstraint.add_argument(
        "--overconstraint-mhz", type=_positive_mhz, metavar="MHZ",
        help="explicitly optimize at a higher frequency than the nominal target",
    )
    overconstraint.add_argument(
        "--no-overconstraint", action="store_true",
        help="disable overconstraint, including any environment setting",
    )
    args = parser.parse_args(argv)
    if args.command and args.command != ["max", "clocking"]:
        parser.error("unknown command; use 'max clocking'")
    args.max_clocking = args.max_clocking or bool(args.command)
    if args.max_clocking and (args.target_mhz is not None or args.overconstraint_mhz is not None):
        parser.error("max clocking uses fixed targets starting at 230 MHz and cannot be combined with --target-mhz or --overconstraint-mhz")
    return args


def main(args=None):
    args = args if args is not None else _parse_args([])
    if args.max_clocking:
        def run_target(mhz):
            target_args = argparse.Namespace(**vars(args))
            target_args.target_mhz = mhz
            target_args.no_overconstraint = True
            return _run_single_target(target_args, require_timing_closure=True)

        return run_max_clocking(run_target)
    return _run_single_target(args)


def _run_single_target(args, *, require_timing_closure=False):
    _load_service_exports()
    target_period = (
        1000 / args.target_mhz if args.target_mhz is not None
        else _env_float("EDA_TIMING_TARGET_CLOCK_PERIOD_NS") or 4.762
    )
    overconstraint_period = _env_float("EDA_TIMING_OVERCONSTRAINT_CLOCK_PERIOD_NS")
    use_overconstraint = _env_bool("EDA_TIMING_USE_OVERCONSTRAINT")
    if args.target_mhz is not None or args.no_overconstraint:
        use_overconstraint = False
    if args.overconstraint_mhz is not None:
        overconstraint_period = 1000 / args.overconstraint_mhz
        if overconstraint_period >= target_period:
            raise ValueError("--overconstraint-mhz must be higher than the target frequency")
        use_overconstraint = True

    initial_state: FlowState = {
        "project_name": "eda-flow",
        "top_module": "ddr4_controller_top",
        "current_stage": "idle",
        "history": [],
        "last_error": None,
        "logs": {},
        "outputs": {},
        "inputs": {},
        "validation": {},
        "stage_status": {
            "rtl_prep": "pending",
            "netlist": "pending",
            "mapped_netlist": "pending",
            "gdsii": "pending",
        },
        "max_stage_retries": 3,
        "retry_counts": {},
        "remote_prepare_input": "MemoryController.zip",
        "remote_python_cmd": "python3.11",
        "remote_prepare_script": DEFAULT_PREPARE_SCRIPT,
        "remote_netlist_script": DEFAULT_NETLIST_SCRIPT,
        "remote_mapped_script": DEFAULT_MAPPED_SCRIPT,
        "gdsii_script": DEFAULT_GDSII_SCRIPT,
        "remote_genus_bin": "/opt/coe/cadence/DDI231/bin/genus",
        "netlist_run_timeout_sec": 14400,
        "netlist_heartbeat_sec": 60,
        "mapped_run_timeout_sec": 14400,
        "mapped_heartbeat_sec": 60,
        "gdsii_run_timeout_sec": 14400,
        "gdsii_heartbeat_sec": 60,
        "timing_target_clock_period_ns": target_period,
        "timing_closure_enabled": require_timing_closure or _env_bool("EDA_TIMING_CLOSURE_ENABLED", True),
        "timing_closure_max_attempts": _env_int("EDA_TIMING_CLOSURE_MAX_ATTEMPTS", 3),
        "timing_closure_ai_enabled": _env_bool("EDA_TIMING_CLOSURE_AI_ENABLED", True),
        "timing_closure_ai_max_attempts": _env_int("EDA_TIMING_CLOSURE_AI_MAX_ATTEMPTS", 3),
        "timing_overconstraint_clock_period_ns": overconstraint_period,
        "timing_use_overconstraint": use_overconstraint,
        "innovus_utilization": _env_float("EDA_INNOVUS_UTILIZATION"),
        "innovus_ccopt_target_skew": _env_float("EDA_INNOVUS_CCOPT_TARGET_SKEW"),
        "innovus_ccopt_target_max_transition": _env_float("EDA_INNOVUS_CCOPT_TARGET_MAX_TRANSITION"),
        "innovus_final_postroute_setup_opt": _env_bool("EDA_INNOVUS_FINAL_POSTROUTE_SETUP_OPT", True),
        "timing_closure_enable_remote_analysis": _env_bool("EDA_TIMING_CLOSURE_REMOTE_ANALYSIS", True),
        "timing_closure_report_paths": _env_csv("EDA_TIMING_CLOSURE_REPORT_PATHS"),
        "timing_closure_local_staging_dir": os.getenv("EDA_TIMING_CLOSURE_LOCAL_STAGING_DIR"),
        "timing_closure_max_paths": _env_int("EDA_TIMING_CLOSURE_MAX_PATHS", 10),
        "timing_closure_analysis": {},
        "prep_iteration": 0,
        "prep_max_iterations": 3,
        "prep_review_passed": False,
        "prep_review_errors": [],
        "prep_review_warnings": [],
        "prep_review_suggested_fixes": [],
        "prep_review_summary": None,
        "prep_dependency_errors": [],
        "prep_candidate_path": None,
        "prep_fix_applied_actions": [],
        "prep_fix_summary": None,
        "prep_fix_history": [],
        "prep_iteration_context": {},
        "prep_new_errors": [],
        "prep_persistent_errors": [],
        "prep_resolved_errors": [],
        "selected_top_module": None,
        "selected_top_file": None,
        "generic_interface_ports": [],
        "detected_patterns": [],
        "applicable_recipe_ids": [],
        "chosen_recipe_id": None,
        "inferred_modport_map": {},
        "generated_files": [],
        "generated_top_module": None,
        "generated_top_file": None,
        "adaptation_applied": False,
        "adaptation_reason": None,
        "adaptation_deterministic": None,
        "original_files_preserved": True,
        "ambiguity_reason": None,
        "top_selection_reason": None,
        "top_selection_candidates": [],
        "synthesis_top_checks": {},
        "netlist_iteration": 0,
        "netlist_max_iterations": 3,
        "netlist_review_passed": False,
        "netlist_review_errors": [],
        "netlist_review_warnings": [],
        "netlist_review_suggested_fixes": [],
        "netlist_review_summary": None,
        "netlist_candidate_path": None,
        "netlist_edit_applied_actions": [],
        "netlist_edit_summary": None,
        "netlist_output_files": [],
        "netlist_resolved_top": None,
        "mapped_iteration": 0,
        "mapped_max_iterations": 3,
        "mapped_review_passed": False,
        "mapped_review_errors": [],
        "mapped_review_warnings": [],
        "mapped_review_suggested_fixes": [],
        "mapped_review_summary": None,
        "mapped_candidate_path": None,
        "mapped_edit_applied_actions": [],
        "mapped_edit_summary": None,
        "mapped_output_files": [],
        "mapped_resolved_top": None,
        "mapped_review_payload": {},
        "gdsii_iteration": 0,
        "gdsii_max_iterations": 3,
        "gdsii_review_passed": False,
        "gdsii_review_errors": [],
        "gdsii_review_warnings": [],
        "gdsii_review_suggested_fixes": [],
        "gdsii_review_summary": None,
        "gdsii_candidate_path": None,
        "gdsii_edit_applied_actions": [],
        "gdsii_edit_summary": None,
        "gdsii_output_files": [],
        "gdsii_review_payload": {},
        "failure_stage_owner": None,
        "failure_class": None,
        "route_back_to_stage": None,
        "rtl_failure_handoff_owner": None,
        "rtl_failure_category": None,
        "rtl_failure_triage_payload": {},
        "netlist_review_payload": {},
        "universal_rules_path": str(
            Path(__file__).resolve().parent / "templates" / "universal genus prep instruction.md"
        ),
        "netlist_rules_path": str(
            Path(__file__).resolve().parent
            / "templates"
            / "universal_genus_netlist_instructions.md"
        ),
        "mapped_rules_path": str(
            Path(__file__).resolve().parent
            / "templates"
            / "universal_genus_mapped_instructions.md"
        ),
        "gdsii_rules_path": str(
            Path(__file__).resolve().parent
            / "templates"
            / "universal_gdsii_instructions.md"
        ),
        "ssh_host": "olympus.ece.tamu.edu",
        "ssh_user": "antgamez1203",
        "ssh_port": 22,
        "ssh_key_file": "~/.ssh/id_ed25519",
        "remote_project_root": "/home/ugrads/a/antgamez1203/capstone",
    }

    ssh_config = {
        "host": initial_state["ssh_host"],
        "username": initial_state["ssh_user"],
        "port": initial_state["ssh_port"],
        "key_filename": initial_state["ssh_key_file"],
    }

    initial_state.update(full_flow_names(target_period))

    result = run_flow(
        initial_state=initial_state,
        ssh_config=ssh_config,
        thread_id="eda-flow-run",
    )
    return result


if __name__ == "__main__":
    # Parse before opening session logs so --help and invalid arguments preserve them.
    args = _parse_args()
    repo_root = Path(__file__).resolve().parent
    result = run_with_session_logging(
        lambda: main(args),
        log_path=repo_root / "logs" / "full_session.log",
        parser_script_path=repo_root / "parse_full_session_log.py",
        parser_outdir=repo_root / "logs",
        result_printer=pprint,
    )
    raise SystemExit(0 if result and result.get("current_stage") == "done" else 1)
