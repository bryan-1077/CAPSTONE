import os
import re
from pathlib import Path
from pprint import pprint

from workers.orchestrator import run_flow
from schemas.state import FlowState
from session_log_utils import run_with_session_logging


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


def _validate_rtl_only_state(initial_state: FlowState) -> None:
    required_fields = {
        "remote_prepare_input": str(initial_state.get("remote_prepare_input", "")).strip(),
        "remote_prepare_script": str(initial_state.get("remote_prepare_script", "")).strip(),
        "remote_prepare_dir": str(initial_state.get("remote_prepare_dir", "")).strip(),
        "remote_project_root": str(initial_state.get("remote_project_root", "")).strip(),
        "top_module": str(initial_state.get("top_module", "")).strip(),
    }
    missing = [name for name, value in required_fields.items() if not value]
    if missing:
        raise SystemExit(
            "RTL-only runner is missing required state fields: " + ", ".join(missing)
        )

    repo_root = Path(__file__).resolve().parent
    local_input_probe = repo_root / required_fields["remote_prepare_input"]

    print("=== RTL-Only Run ===")
    print("This runner starts at rtl_prep and stops after rtl_prep completes.")
    print(f"Configured RTL input: {required_fields['remote_prepare_input']}")
    print(f"Configured prep output dir: {required_fields['remote_prepare_dir']}")
    print(f"Effective prepare script: {required_fields['remote_prepare_script']}")
    print(f"Top module: {required_fields['top_module']}")
    print(f"Remote project root: {required_fields['remote_project_root']}")

    if not local_input_probe.exists():
        print(
            "Warning: local RTL input probe does not exist at "
            f"{local_input_probe}. The remote run may still succeed if the input exists under "
            f"{required_fields['remote_project_root']}."
        )


def main():
    _load_service_exports()

    initial_state: FlowState = {
        "project_name": "eda-flow",
        "top_module": "MemoryController",
        "current_stage": "rtl_prep",
        "stop_after_stage": "rtl_prep",
        "history": ["RTL-only runner requested; stopping after rtl_prep."],
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
        "remote_prepare_input": "MemoryController",
        "remote_python_cmd": "python3.11",
        "remote_prepare_script": DEFAULT_PREPARE_SCRIPT,
        "remote_prepare_dir": "build_prep_mc",
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

    _validate_rtl_only_state(initial_state)

    ssh_config = {
        "host": initial_state["ssh_host"],
        "username": initial_state["ssh_user"],
        "port": initial_state["ssh_port"],
        "key_filename": initial_state["ssh_key_file"],
    }

    result = run_flow(
        initial_state=initial_state,
        ssh_config=ssh_config,
        thread_id="eda-flow-rtl-only",
    )
    return result


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent
    run_with_session_logging(
        main,
        log_path=repo_root / "logs" / "full_session.log",
        parser_script_path=repo_root / "parse_full_session_log.py",
        parser_outdir=repo_root / "logs",
        result_printer=pprint,
    )
