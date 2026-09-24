from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable

from workers.workers import (
    DEFAULT_PROJECT_ROOT,
    _build_gdsii_command,
    _build_gdsii_edit_command,
    _build_gdsii_review_command,
    _build_mapped_command,
    _build_mapped_edit_command,
    _build_mapped_review_command,
    _build_netlist_command,
    _build_netlist_edit_command,
    _build_netlist_review_command,
    _build_prepare_command,
    _build_prepare_fix_command,
    _build_prepare_review_command,
)
from agents.openai_gdsii_edit import run_openai_gdsii_edit
from agents.openai_gdsii_review import (
    merge_gdsii_review_results,
    run_openai_gdsii_review,
)
from agents.openai_mapped_edit import run_openai_mapped_edit
from agents.openai_mapped_review import (
    merge_mapped_review_results,
    run_openai_mapped_review,
)
from agents.openai_netlist_edit import run_openai_netlist_edit
from agents.openai_netlist_review import (
    merge_netlist_review_results,
    run_openai_netlist_review,
)
from agents.openai_prep_edit import run_openai_prep_edit
from agents.openai_rtl_failure_triage import run_openai_rtl_failure_triage
from schemas.state import FlowState
from timing_closure.remote_analyze import fetch_remote_reports
from timing_closure.timing_analyzer import analyze_reports, parse_timing_report
from timing_closure.closure_runner import run_setup_closure
from agents.openai_prep_review import (
    merge_prep_review_results,
    run_openai_prep_review,
)
from services.path_naming import (
    resolve_gdsii_dir_choice,
    resolve_mapped_dir_choice,
    resolve_netlist_dir_choice,
    resolve_prepare_dir,
)
from services.ssh_executor import SSHExecutor
from services.build_names import reserve_full_flow
from services.stage_routing import (
    extract_review_routing,
    should_run_gdsii_editor,
    should_run_mapped_editor,
    should_run_netlist_editor,
)


_EMITTED_HISTORY_PREFIX: list[str] = []


def _ordered_unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _summarize_action_history(actions: list[dict]) -> list[str]:
    summaries: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type", "")).strip()
        action_path = str(action.get("path", "")).strip()
        if not action_type and not action_path:
            continue
        summaries.append(" ".join(part for part in [action_type, action_path] if part))
    return _ordered_unique_strings(summaries)


def _build_prep_iteration_context(state: FlowState, current_errors: list[str]) -> dict:
    previous_errors = _ordered_unique_strings(state.get("prep_review_errors", []))
    current_ordered = _ordered_unique_strings(current_errors)
    previous_set = set(previous_errors)
    current_set = set(current_ordered)
    return {
        "previous_errors": previous_errors,
        "current_errors": current_ordered,
        "persistent_errors": [item for item in current_ordered if item in previous_set],
        "new_errors": [item for item in current_ordered if item not in previous_set],
        "resolved_errors": [item for item in previous_errors if item not in current_set],
        "prior_fix_history": _ordered_unique_strings(state.get("prep_fix_history", [])),
        "latest_applied_actions": _summarize_action_history(
            state.get("prep_fix_applied_actions", [])
        ),
    }


def _run_stage_with_retries(
    state: FlowState,
    *,
    stage_name: str,
    runner: Callable[[FlowState], dict],
    max_retries: int,
) -> FlowState:
    for attempt in range(1, max_retries + 1):
        state = _merge_state(
            state,
            {
                "current_stage": stage_name,
                "stage_status": {stage_name: f"running_attempt_{attempt}"},
                "history": state["history"] + [
                    f"Entering {stage_name} stage (attempt {attempt}/{max_retries})"
                ],
            },
        )

        updates = runner(state)
        state = _merge_state(state, updates)

        if state.get("route_back_to_stage") and state.get("route_back_to_stage") != stage_name:
            state = _merge_state(
                state,
                {
                    "retry_counts": {
                        **state.get("retry_counts", {}),
                        stage_name: attempt,
                    }
                },
            )
            return state

        if state["stage_status"].get(stage_name) == "success":
            state = _merge_state(
                state,
                {
                    "retry_counts": {
                        **state.get("retry_counts", {}),
                        stage_name: attempt - 1,
                    }
                },
            )
            return state

        if attempt < max_retries:
            state = _merge_state(
                state,
                {
                    "history": state["history"] + [
                        f"{stage_name} failed on attempt {attempt}; retrying"
                    ],
                    "retry_counts": {
                        **state.get("retry_counts", {}),
                        stage_name: attempt,
                    },
                    "inputs": {
                        **state.get("inputs", {}),
                        f"{stage_name}_feedback": state.get("last_error") or "",
                    },
                },
            )

    state = _merge_state(
        state,
        {
            "current_stage": "failed",
            "history": state["history"] + [
                f"{stage_name} failed after {max_retries} attempts",
                "Flow stopped due to failure",
            ],
            "retry_counts": {
                **state.get("retry_counts", {}),
                stage_name: max_retries,
            },
        },
    )
    return state


def _stage_retry_limit(state: FlowState, stage_name: str) -> int:
    default_limit = max(1, int(state.get("max_stage_retries", 3)))
    if stage_name in {"rtl_prep", "netlist", "mapped_netlist", "gdsii"}:
        return 1
    return default_limit


def _emit_live_progress(message: str) -> None:
    text = str(message).strip()
    if not text:
        return
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{timestamp}] {text}", file=sys.stdout, flush=True)


def _reset_live_history_prefix() -> None:
    global _EMITTED_HISTORY_PREFIX
    _EMITTED_HISTORY_PREFIX = []


def _common_history_prefix_length(history: list) -> int:
    count = 0
    for emitted, current in zip(_EMITTED_HISTORY_PREFIX, history):
        if str(emitted) != str(current):
            break
        count += 1
    return count


def _merge_state(state: FlowState, updates: dict) -> FlowState:
    global _EMITTED_HISTORY_PREFIX
    new_state = dict(state)
    for key, value in updates.items():
        if key == "rtl_failure_triage_payload":
            # A diagnosis describes one failure; never retain fields from an older one.
            new_state[key] = value
        elif isinstance(value, dict) and isinstance(new_state.get(key), dict):
            merged = dict(new_state[key])
            merged.update(value)
            new_state[key] = merged
        else:
            new_state[key] = value

    previous_history = state.get("history", [])
    current_history = new_state.get("history", [])
    if isinstance(previous_history, list) and isinstance(current_history, list):
        emit_start = max(
            len(previous_history),
            _common_history_prefix_length(current_history),
        )
        for entry in current_history[emit_start:]:
            _emit_live_progress(entry)
        _EMITTED_HISTORY_PREFIX = [str(entry) for entry in current_history]

    return new_state


def _attach_rtl_failure_triage(
    *,
    stage_name: str,
    label: str,
    merged_payload: dict,
    execution_context: dict,
    review_result: dict,
    logs: dict,
) -> dict | None:
    blocking_errors = merged_payload.get("blocking_errors", merged_payload.get("errors", []))
    if bool(merged_payload.get("pass")) and not blocking_errors and execution_context.get("ok"):
        return None

    triage = run_openai_rtl_failure_triage(
        local_review=merged_payload,
        execution_context=execution_context,
        logs={
            "stdout": review_result.get("stdout", ""),
            "stderr": review_result.get("stderr", ""),
        },
    )
    merged_payload["rtl_failure_triage"] = triage
    logs[f"{stage_name}_rtl_failure_triage_{label}"] = json.dumps(triage, indent=2)
    return triage


def _prep_triage_updates(payload: dict, result: dict, logs: dict, *, label: str, candidate_path: str) -> dict:
    failed = not payload.get("pass") or bool(payload.get("errors")) or not result.get("ok")
    if failed:
        _emit_live_progress(f"rtl_prep failure triage started ({label})")
    triage = _attach_rtl_failure_triage(
        stage_name="rtl_prep",
        label=label,
        merged_payload=payload,
        execution_context={
            "stage": "rtl_prep",
            "ok": bool(result.get("ok")),
            "exit_code": result.get("exit_code"),
            "candidate_path": candidate_path,
        },
        review_result=result,
        logs=logs,
    )
    if triage is not None:
        _emit_live_progress(
            f"rtl_prep failure triage completed ({label}): "
            f"{triage.get('handoff_owner', 'unknown')}/{triage.get('failure_category', 'unknown')}"
        )
    return {
        "rtl_failure_handoff_owner": triage.get("handoff_owner") if triage else None,
        "rtl_failure_category": triage.get("failure_category") if triage else None,
        "rtl_failure_triage_payload": triage or {},
    }


def _default_timing_report_paths(state: FlowState) -> list[str]:
    mapped_dir = str(state.get("mapped_candidate_path") or state.get("remote_mapped_dir") or "").strip()
    gdsii_dir = str(state.get("gdsii_candidate_path") or state.get("remote_gdsii_dir") or "").strip()
    top = (
        str(state.get("mapped_resolved_top") or "").strip()
        or str(state.get("mapped_top_module") or "").strip()
        or str(state.get("top_module") or "").strip()
    )
    paths: list[str] = []
    if mapped_dir:
        paths.append(f"{mapped_dir}/reports/timing_mapped.rpt")
    if gdsii_dir:
        paths.append(f"{gdsii_dir}/reports/timing_postroute.rpt")
        if top:
            paths.append(f"{gdsii_dir}/timingReports/{top}_postCTS_reg2reg.tarpt.gz")
    return paths


def _run_remote_timing_closure_analysis(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    label: str,
) -> dict:
    if not bool(state.get("timing_closure_enable_remote_analysis")):
        return {}

    project_root = str(state.get("remote_project_root", DEFAULT_PROJECT_ROOT)).strip()
    report_paths = state.get("timing_closure_report_paths") or _default_timing_report_paths(state)
    report_paths = [
        str(path).strip()
        for path in report_paths
        if str(path).strip()
    ]
    staging_dir = Path(
        str(
            state.get("timing_closure_local_staging_dir")
            or "/tmp/eda_timing_closure_remote"
        )
    )
    local_paths, fetch_results = fetch_remote_reports(
        ssh=ssh,
        remote_project_root=project_root,
        report_paths=report_paths,
        local_staging_dir=staging_dir,
    )
    parsed_reports = [
        parse_timing_report(path, max_paths=int(state.get("timing_closure_max_paths", 10)))
        for path in local_paths
    ]
    target_period = state.get("timing_target_clock_period_ns")
    if bool(state.get("timing_use_overconstraint")) and state.get("timing_overconstraint_clock_period_ns"):
        target_period = state.get("timing_overconstraint_clock_period_ns")
    analysis = analyze_reports(
        parsed_reports,
        target_period=float(target_period) if target_period not in (None, "") else None,
    )
    analysis["remote_fetch"] = fetch_results
    return {
        "logs": {
            **state["logs"],
            f"timing_closure_analysis_{label}": json.dumps(analysis, indent=2),
        },
        "validation": {
            **state.get("validation", {}),
            "timing_closure_analysis": analysis,
        },
        "timing_closure_analysis": analysis,
        "history": state["history"] + [
            "timing closure analysis completed"
            if parsed_reports
            else "timing closure analysis found no fetchable reports"
        ],
    }


def _run_simple_stage(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    stage_key: str,
    history_success: str,
    history_fail_prefix: str,
    error_prefix: str,
    command: str,
    cwd: str,
    log_prefix: str,
    timeout: int = 120,
    get_pty: bool = False,
) -> dict:
    result = ssh.run(command, cwd=cwd, timeout=timeout, get_pty=get_pty)

    logs = {
        **state["logs"],
        f"{log_prefix}_stdout": result["stdout"],
        f"{log_prefix}_stderr": result["stderr"],
        f"{log_prefix}_command": result["command"],
    }

    if not result["ok"]:
        return {
            "stage_status": {**state["stage_status"], stage_key: "failed"},
            "logs": logs,
            "history": state["history"] + [
                f"{history_fail_prefix} with exit code {result['exit_code']}"
            ],
            "last_error": (
                f"{error_prefix} with exit code {result['exit_code']}: "
                f"{result['stderr']}"
            ),
        }

    return {
        "stage_status": {**state["stage_status"], stage_key: "success"},
        "logs": logs,
        "history": state["history"] + [history_success],
        "last_error": None,
    }


def _run_rtl_prep_generation(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    candidate_path = state.get("remote_prepare_dir", "build_prep_mc_lang")
    state_for_attempt = _merge_state(
        state,
        {
            "prep_iteration": attempt,
            "history": state["history"] + [f"rtl_prep generate attempt {attempt}"],
        },
    )

    result = ssh.run(
        _build_prepare_command(state_for_attempt),
        cwd=project_root,
        timeout=120,
    )

    logs = {
        **state["logs"],
        f"rtl_prep_generate_{attempt}_stdout": result["stdout"],
        f"rtl_prep_generate_{attempt}_stderr": result["stderr"],
        f"rtl_prep_generate_{attempt}_command": result["command"],
    }

    if not result["ok"]:
        payload = {
            "pass": False,
            "errors": [f"RTL prep generation failed with exit code {result['exit_code']}: {result['stderr']}"],
            "attempt": attempt,
        }
        triage_updates = _prep_triage_updates(
            payload, result, logs, label=f"generate_{attempt}", candidate_path=candidate_path,
        )
        return (
            {
                **triage_updates,
                "validation": {**state.get("validation", {}), "rtl_prep_generation": payload},
                "logs": logs,
                "history": state_for_attempt["history"] + [
                    f"rtl_prep generate attempt {attempt} failed with exit code {result['exit_code']}"
                ],
                "last_error": (
                    f"RTL prep generate attempt {attempt} failed with exit code "
                    f"{result['exit_code']}: {result['stderr']}"
                ),
                "prep_iteration": attempt,
                "prep_candidate_path": candidate_path,
                "prep_review_passed": False,
            },
            False,
        )

    return (
        {
            "logs": logs,
            "history": state_for_attempt["history"] + [
                f"rtl_prep generate attempt {attempt} completed"
            ],
            "last_error": None,
            "prep_iteration": attempt,
            "prep_candidate_path": candidate_path,
        },
        True,
    )


def _parse_json_payload(stdout: str, *, label: str) -> dict:
    text = stdout.strip()
    if not text:
        raise ValueError(f"{label} returned empty stdout")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{label} returned no parseable output")

    return json.loads(lines[-1])


def _netlist_run_completion_status(result: dict) -> tuple[bool, str | None]:
    stdout = str(result.get("stdout", "") or "")
    marker_prefix = "EDA_NETLIST_RUN_COMPLETE:"

    for raw_line in reversed(stdout.splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return True, line[len(marker_prefix) :].strip()

    return False, None


def _netlist_run_environment_error(result: dict) -> str | None:
    stdout = str(result.get("stdout", "") or "")
    stderr = str(result.get("stderr", "") or "")
    marker_prefix = "EDA_NETLIST_ENV_ERROR:"

    for raw_line in reversed((stdout + "\n" + stderr).splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return line[len(marker_prefix) :].strip() or "unknown_environment_error"

    return None


def _mapped_run_completion_status(result: dict) -> tuple[bool, str | None]:
    stdout = str(result.get("stdout", "") or "")
    marker_prefix = "EDA_MAPPED_RUN_COMPLETE:"

    for raw_line in reversed(stdout.splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return True, line[len(marker_prefix) :].strip()

    return False, None


def _mapped_run_environment_error(result: dict) -> str | None:
    stdout = str(result.get("stdout", "") or "")
    stderr = str(result.get("stderr", "") or "")
    marker_prefix = "EDA_MAPPED_ENV_ERROR:"

    for raw_line in reversed((stdout + "\n" + stderr).splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return line[len(marker_prefix) :].strip() or "unknown_environment_error"

    return None


def _gdsii_run_completion_status(result: dict) -> tuple[bool, str | None]:
    stdout = str(result.get("stdout", "") or "")
    marker_prefix = "EDA_GDSII_RUN_COMPLETE:"

    for raw_line in reversed(stdout.splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return True, line[len(marker_prefix) :].strip()

    return False, None


def _gdsii_run_environment_error(result: dict) -> str | None:
    stdout = str(result.get("stdout", "") or "")
    stderr = str(result.get("stderr", "") or "")
    marker_prefix = "EDA_GDSII_ENV_ERROR:"

    for raw_line in reversed((stdout + "\n" + stderr).splitlines()):
        line = raw_line.strip()
        if line.startswith(marker_prefix):
            return line[len(marker_prefix) :].strip() or "unknown_environment_error"

    return None


def _run_rtl_prep_review(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    candidate_path = state.get("prep_candidate_path") or state.get(
        "remote_prepare_dir",
        "build_prep_mc_lang",
    )
    label = log_label or str(attempt)

    review_result = ssh.run(
        _build_prepare_review_command(state, candidate_path),
        cwd=project_root,
        timeout=300,
    )

    logs = {
        **state["logs"],
        f"rtl_prep_review_{label}_stdout": review_result["stdout"],
        f"rtl_prep_review_{label}_stderr": review_result["stderr"],
        f"rtl_prep_review_{label}_command": review_result["command"],
    }

    if not review_result["ok"]:
        errors = [
            (
                f"RTL prep review command failed on attempt {attempt} with exit code "
                f"{review_result['exit_code']}: {review_result['stderr']}"
            )
        ]
        payload = {"pass": False, "errors": errors, "attempt": attempt, "execution_context": {"ok": review_result["ok"], "exit_code": review_result["exit_code"]}}
        logs[f"rtl_prep_review_{label}_result"] = json.dumps(payload)
        return (
            {
                "logs": logs,
                "history": state["history"] + [
                    "rtl_prep review failed: 1 errors"
                ],
                "last_error": errors[0],
                "prep_review_passed": False,
                "prep_review_errors": errors,
                "validation": {
                    **state.get("validation", {}),
                    "rtl_prep_review": payload,
                },
            },
            False,
        )

    try:
        local_payload = _parse_json_payload(
            review_result["stdout"],
            label="rtl_prep review",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        errors = [f"Unable to parse rtl_prep review result on attempt {attempt}: {exc}"]
        payload = {"pass": False, "errors": errors, "attempt": attempt, "execution_context": {"ok": review_result["ok"], "exit_code": review_result["exit_code"]}}
        logs[f"rtl_prep_review_{label}_result"] = json.dumps(payload)
        return (
            {
                "logs": logs,
                "history": state["history"] + [
                    "rtl_prep review failed: 1 errors"
                ],
                "last_error": errors[0],
                "prep_review_passed": False,
                "prep_review_errors": errors,
                "validation": {
                    **state.get("validation", {}),
                    "rtl_prep_review": payload,
                },
            },
            False,
        )

    local_context = (
        dict(local_payload.get("context", {}))
        if isinstance(local_payload.get("context", {}), dict)
        else {}
    )
    current_local_errors = [str(item) for item in local_payload.get("errors", [])]
    iteration_context = _build_prep_iteration_context(state, current_local_errors)
    local_context["iteration_context"] = iteration_context
    local_payload["context"] = local_context

    ai_review = run_openai_prep_review(
        local_review=local_payload,
        universal_rules_path=state.get("universal_rules_path"),
    )
    merged_payload = merge_prep_review_results(
        local_review=local_payload,
        ai_review=ai_review,
    )

    errors = [str(item) for item in merged_payload.get("errors", [])]
    warnings = [str(item) for item in merged_payload.get("warnings", [])]
    suggested_fixes = [
        str(item) for item in merged_payload.get("suggested_fixes", [])
    ]
    passed = bool(merged_payload.get("pass")) and not errors
    synthesis_top_checks = merged_payload.get("context", {}).get("synthesis_top_checks", {})
    top_selection_candidates = synthesis_top_checks.get("top_selection_candidates", [])
    selected_top_module = synthesis_top_checks.get("selected_top_module")
    selected_top_file = synthesis_top_checks.get("selected_top_file")
    top_selection_reason = synthesis_top_checks.get("selection_source")
    generic_interface_ports = synthesis_top_checks.get("generic_interface_ports", [])
    detected_patterns = synthesis_top_checks.get("detected_patterns", [])
    applicable_recipe_ids = synthesis_top_checks.get("applicable_recipe_ids", [])
    chosen_recipe_id = synthesis_top_checks.get("chosen_recipe_id")
    inferred_modport_map = synthesis_top_checks.get("inferred_modport_map", {})
    generated_files = synthesis_top_checks.get("generated_files", [])
    generated_top_module = synthesis_top_checks.get("generated_top_module")
    generated_top_file = synthesis_top_checks.get("generated_top_file")
    adaptation_reason = synthesis_top_checks.get("adaptation_reason")
    adaptation_deterministic = synthesis_top_checks.get("adaptation_deterministic")
    original_files_preserved = synthesis_top_checks.get("original_files_preserved")
    ambiguity_reason = synthesis_top_checks.get("ambiguity_reason")

    logs[f"rtl_prep_review_{label}_local_result"] = json.dumps(local_payload, indent=2)
    logs[f"rtl_prep_review_{label}_ai_request_metadata"] = json.dumps(
        ai_review.get("request_metadata", {}),
        indent=2,
    )
    logs[f"rtl_prep_review_{label}_ai_raw_response"] = (
        ai_review.get("raw_response", "") or ""
    )
    logs[f"rtl_prep_review_{label}_iteration_context"] = json.dumps(
        iteration_context,
        indent=2,
    )
    if ai_review.get("api_error"):
        logs[f"rtl_prep_review_{label}_ai_error"] = str(ai_review["api_error"])
    logs[f"rtl_prep_review_{label}_result"] = json.dumps(merged_payload, indent=2)
    logs[f"stage_routing_decision_rtl_prep_{label}"] = json.dumps(
        {
            "stage": "rtl_prep",
            "attempt": attempt,
            "failure_stage_owner": merged_payload.get("failure_stage_owner"),
            "failure_class": merged_payload.get("failure_class"),
            "route_back_to_stage": merged_payload.get("route_back_to_stage"),
            "selected_top_module": selected_top_module,
            "selected_top_file": selected_top_file,
            "top_selection_reason": top_selection_reason,
            "generic_interface_ports": generic_interface_ports,
            "detected_patterns": detected_patterns,
            "applicable_recipe_ids": applicable_recipe_ids,
            "chosen_recipe_id": chosen_recipe_id,
            "generated_files": generated_files,
            "generated_top_module": generated_top_module,
            "generated_top_file": generated_top_file,
            "adaptation_inferable": synthesis_top_checks.get("adaptation_inferable"),
            "adaptation_applied": state.get("adaptation_applied", False),
        },
        indent=2,
    )

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                "rtl_prep review passed"
                if passed
                else f"rtl_prep review failed: {len(errors)} errors"
            ],
            "last_error": None if passed else "RTL prep review failed: " + "; ".join(errors),
            "prep_review_passed": passed,
            "prep_review_errors": errors,
            "prep_review_warnings": warnings,
            "prep_review_suggested_fixes": suggested_fixes,
            "prep_review_summary": merged_payload.get("summary"),
            "prep_dependency_errors": [
                str(item)
                for item in merged_payload.get("prep_dependency_errors", [])
            ],
            "selected_top_module": (
                str(selected_top_module).strip() if selected_top_module else None
            ),
            "selected_top_file": (
                str(selected_top_file).strip() if selected_top_file else None
            ),
            "generic_interface_ports": [
                str(item) for item in generic_interface_ports if str(item).strip()
            ],
            "detected_patterns": (
                detected_patterns if isinstance(detected_patterns, list) else []
            ),
            "applicable_recipe_ids": [
                str(item) for item in applicable_recipe_ids if str(item).strip()
            ]
            if isinstance(applicable_recipe_ids, list)
            else [],
            "chosen_recipe_id": (
                str(chosen_recipe_id).strip() if chosen_recipe_id else None
            ),
            "inferred_modport_map": (
                dict(inferred_modport_map)
                if isinstance(inferred_modport_map, dict)
                else {}
            ),
            "generated_files": [
                str(item) for item in generated_files if str(item).strip()
            ]
            if isinstance(generated_files, list)
            else [],
            "generated_top_module": (
                str(generated_top_module).strip() if generated_top_module else None
            ),
            "generated_top_file": (
                str(generated_top_file).strip() if generated_top_file else None
            ),
            "adaptation_reason": (
                str(adaptation_reason).strip() if adaptation_reason else None
            ),
            "adaptation_deterministic": (
                bool(adaptation_deterministic)
                if adaptation_deterministic is not None
                else None
            ),
            "original_files_preserved": (
                bool(original_files_preserved)
                if original_files_preserved is not None
                else True
            ),
            "ambiguity_reason": (
                str(ambiguity_reason).strip() if ambiguity_reason else None
            ),
            "top_selection_reason": (
                str(top_selection_reason).strip() if top_selection_reason else None
            ),
            "top_selection_candidates": (
                top_selection_candidates if isinstance(top_selection_candidates, list) else []
            ),
            "synthesis_top_checks": (
                synthesis_top_checks if isinstance(synthesis_top_checks, dict) else {}
            ),
            "prep_iteration_context": iteration_context,
            "prep_new_errors": iteration_context.get("new_errors", []),
            "prep_persistent_errors": iteration_context.get("persistent_errors", []),
            "prep_resolved_errors": iteration_context.get("resolved_errors", []),
            "failure_stage_owner": merged_payload.get("failure_stage_owner"),
            "failure_class": merged_payload.get("failure_class"),
            "route_back_to_stage": merged_payload.get("route_back_to_stage"),
            "validation": {
                **state.get("validation", {}),
                "rtl_prep_review": {
                    **merged_payload,
                    "attempt": attempt,
                    "execution_context": {"ok": review_result["ok"], "exit_code": review_result["exit_code"]},
                },
            },
        },
        passed,
    )


def _run_rtl_prep_fix(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    candidate_path = state.get("prep_candidate_path") or state.get(
        "remote_prepare_dir",
        "build_prep_mc_lang",
    )
    label = log_label or str(attempt)
    stored_review_payload = state.get("validation", {}).get("rtl_prep_review", {})
    review_payload = dict(stored_review_payload) if isinstance(stored_review_payload, dict) else {}
    review_context = (
        dict(review_payload.get("context", {}))
        if isinstance(review_payload.get("context", {}), dict)
        else {}
    )
    review_context["iteration_context"] = state.get("prep_iteration_context", {})
    review_context["prior_fix_history"] = _ordered_unique_strings(
        state.get("prep_fix_history", [])
    )
    review_payload["context"] = review_context

    fix_plan = run_openai_prep_edit(
        review_payload=review_payload,
        universal_rules_path=state.get("universal_rules_path"),
    )

    logs = {
        **state["logs"],
        f"rtl_prep_fix_{label}_ai_request_metadata": json.dumps(
            fix_plan.get("request_metadata", {}),
            indent=2,
        ),
        f"rtl_prep_fix_{label}_ai_raw_response": fix_plan.get("raw_response", "") or "",
        f"rtl_prep_fix_{label}_plan": json.dumps(
            {
                "actions": fix_plan.get("actions", []),
                "summary": fix_plan.get("summary", ""),
                "warnings": fix_plan.get("warnings", []),
            },
            indent=2,
        ),
    }

    if fix_plan.get("api_error"):
        logs[f"rtl_prep_fix_{label}_ai_error"] = str(fix_plan["api_error"])

    actions = [
        item for item in fix_plan.get("actions", [])
        if isinstance(item, dict) and item.get("type") and item.get("path")
    ]

    if not actions:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["rtl_prep fix skipped: no actions proposed"],
                "prep_fix_applied_actions": [],
                "prep_fix_summary": fix_plan.get("summary"),
                "prep_review_warnings": [
                    *state.get("prep_review_warnings", []),
                    *[str(item) for item in fix_plan.get("warnings", [])],
                ],
            },
            False,
        )

    apply_result = ssh.run(
        _build_prepare_fix_command(state, candidate_path, actions),
        cwd=project_root,
        timeout=300,
    )

    logs[f"rtl_prep_fix_{label}_stdout"] = apply_result["stdout"]
    logs[f"rtl_prep_fix_{label}_stderr"] = apply_result["stderr"]
    logs[f"rtl_prep_fix_{label}_command"] = apply_result["command"]

    if not apply_result["ok"]:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["rtl_prep fix failed to apply"],
                "prep_fix_applied_actions": [],
                "prep_fix_summary": fix_plan.get("summary"),
                "prep_review_warnings": [
                    *state.get("prep_review_warnings", []),
                    f"Prep fixer failed to apply actions: {apply_result['stderr']}",
                ],
            },
            False,
        )

    try:
        apply_payload = _parse_json_payload(
            apply_result["stdout"],
            label="rtl_prep fix output",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["rtl_prep fix produced unreadable output"],
                "prep_fix_applied_actions": [],
                "prep_fix_summary": fix_plan.get("summary"),
                "prep_review_warnings": [
                    *state.get("prep_review_warnings", []),
                    f"Prep fixer output could not be parsed: {exc}",
                ],
            },
            False,
        )

    applied_actions = [
        item for item in apply_payload.get("applied_actions", [])
        if isinstance(item, dict)
    ]
    updated_fix_history = _ordered_unique_strings(
        [
            *state.get("prep_fix_history", []),
            *state.get("prep_review_suggested_fixes", []),
            *_summarize_action_history(applied_actions),
        ]
    )

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                f"rtl_prep fix applied {len(applied_actions)} actions"
            ],
            "prep_fix_applied_actions": applied_actions,
            "prep_fix_summary": fix_plan.get("summary") or apply_payload.get("summary"),
            "prep_fix_history": updated_fix_history,
            "adaptation_applied": bool(apply_payload.get("adaptation_applied")),
            "chosen_recipe_id": (
                str(apply_payload.get("chosen_recipe_id", "")).strip()
                or state.get("chosen_recipe_id")
            ),
            "generated_files": (
                [
                    str(item) for item in apply_payload.get("generated_files", []) if str(item).strip()
                ]
                or state.get("generated_files", [])
            ),
            "generated_top_module": (
                str(apply_payload.get("generated_top_module", "")).strip() or state.get("generated_top_module")
            ),
            "generated_top_file": (
                str(apply_payload.get("generated_top_file", "")).strip() or state.get("generated_top_file")
            ),
            "adaptation_reason": (
                str(apply_payload.get("adaptation_reason", "")).strip()
                or state.get("adaptation_reason")
            ),
            "original_files_preserved": bool(apply_payload.get("original_files_preserved", True)),
            "prep_review_warnings": [
                *state.get("prep_review_warnings", []),
                *[str(item) for item in fix_plan.get("warnings", [])],
            ],
        },
        bool(applied_actions),
    )


def _make_rtl_prep_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        candidate_path = state.get("remote_prepare_dir", "build_prep_mc_lang")
        prep_max_iterations = state.get("prep_max_iterations", 3)

        working_state = _merge_state(
            state,
            {
                "prep_iteration": 0,
                "rtl_failure_handoff_owner": None,
                "rtl_failure_category": None,
                "rtl_failure_triage_payload": {},
                "prep_candidate_path": candidate_path,
                "prep_review_passed": False,
                "prep_review_errors": [],
                "prep_review_warnings": [],
                "prep_review_suggested_fixes": [],
                "prep_review_summary": None,
                "prep_dependency_errors": [],
                "prep_fix_applied_actions": [],
                "prep_fix_summary": None,
                "prep_fix_history": list(state.get("prep_fix_history", [])),
                "prep_iteration_context": {},
                "prep_new_errors": [],
                "prep_persistent_errors": [],
                "prep_resolved_errors": [],
                "failure_stage_owner": None,
                "failure_class": None,
                "route_back_to_stage": None,
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
            },
        )

        generate_updates, generated_ok = _run_rtl_prep_generation(
            working_state,
            ssh,
            attempt=1,
        )
        working_state = _merge_state(working_state, generate_updates)

        if not generated_ok:
            return _merge_state(
                working_state,
                {
                    "stage_status": {
                        **working_state["stage_status"],
                        "rtl_prep": "failed",
                    },
                },
            )

        for attempt in range(1, prep_max_iterations + 1):
            latest_review_label = str(attempt)
            review_updates, review_passed = _run_rtl_prep_review(
                working_state,
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, review_updates)

            if review_passed:
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "rtl_prep": "success",
                        },
                        "outputs": {
                            **working_state["outputs"],
                            "rtl_bundle_remote": f"{project_root}/{candidate_path}",
                        },
                        "last_error": None,
                    },
                )

            fix_updates, fix_applied = _run_rtl_prep_fix(
                _merge_state(
                    working_state,
                    {
                        "history": working_state["history"] + [
                            f"rtl_prep fix attempt {attempt}"
                        ]
                    },
                ),
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, fix_updates)

            if fix_applied:
                latest_review_label = f"{attempt}_post_fix"
                rereview_updates, rereview_passed = _run_rtl_prep_review(
                    working_state,
                    ssh,
                    attempt=attempt,
                    log_label=f"{attempt}_post_fix",
                )
                working_state = _merge_state(working_state, rereview_updates)

                if rereview_passed:
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "rtl_prep": "success",
                            },
                            "outputs": {
                                **working_state["outputs"],
                                "rtl_bundle_remote": f"{project_root}/{candidate_path}",
                            },
                            "last_error": None,
                        },
                    )

            if attempt < prep_max_iterations:
                retry_feedback_lines = [
                    *working_state.get("prep_review_errors", []),
                    *[
                        f"Suggested fix: {item}"
                        for item in working_state.get(
                            "prep_review_suggested_fixes",
                            [],
                        )
                    ],
                    *[
                        f"Applied fix: {item.get('type')} {item.get('path')}"
                        for item in working_state.get("prep_fix_applied_actions", [])
                        if isinstance(item, dict)
                    ],
                ]
                retry_feedback = "\n".join(line for line in retry_feedback_lines if line)
                working_state = _merge_state(
                    working_state,
                    {
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "rtl_prep_feedback": retry_feedback,
                        },
                    },
                )

        error_list = working_state.get("prep_review_errors", [])
        failure_message = (
            "RTL prep review failed after "
            f"{prep_max_iterations} iterations: {'; '.join(error_list)}"
            if error_list
            else f"RTL prep failed after {prep_max_iterations} iterations."
        )

        # Classify only after the entire review/edit budget has been exhausted.
        payload = dict(working_state.get("validation", {}).get("rtl_prep_review", {}))
        payload.update({"pass": False, "errors": error_list, "prep_attempts_exhausted": prep_max_iterations})
        logs = dict(working_state["logs"])
        execution = payload.get("execution_context", {})
        result = {
            "ok": execution.get("ok", True),
            "exit_code": execution.get("exit_code", 0),
            "stdout": logs.get(f"rtl_prep_review_{latest_review_label}_stdout", ""),
            "stderr": logs.get(f"rtl_prep_review_{latest_review_label}_stderr", ""),
        }
        triage_updates = _prep_triage_updates(
            payload, result, logs, label="exhausted", candidate_path=candidate_path,
        )
        return _merge_state(
            working_state,
            {
                **triage_updates,
                "logs": logs,
                "validation": {**working_state.get("validation", {}), "rtl_prep_review": payload},
                "stage_status": {
                    **working_state["stage_status"],
                    "rtl_prep": "failed",
                },
                "history": working_state["history"] + [
                    f"rtl_prep failed after {prep_max_iterations} prep iterations"
                ],
                "last_error": failure_message,
            },
        )

    return runner


def _run_netlist_generation(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool, dict]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    builddir, builddir_source = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    candidate_path, outdir_source = resolve_netlist_dir_choice(
        state.get("remote_netlist_dir"),
        builddir,
    )
    label = log_label or str(attempt)
    timeout = int(state.get("netlist_run_timeout_sec", 14400))
    state_for_attempt = _merge_state(
        state,
        {
            "netlist_iteration": attempt,
            "history": state["history"] + [f"netlist generate attempt {attempt}"],
        },
    )

    result = ssh.run(
        _build_netlist_command(state_for_attempt),
        cwd=project_root,
        timeout=timeout,
    )
    run_completed, completion_code = _netlist_run_completion_status(result)
    environment_error = _netlist_run_environment_error(result)

    logs = {
        **state["logs"],
        f"netlist_generate_{label}_stdout": result["stdout"],
        f"netlist_generate_{label}_stderr": result["stderr"],
        f"netlist_generate_{label}_command": result["command"],
        f"netlist_generate_{label}_completed": json.dumps(
            {
                "run_completed": run_completed,
                "completion_code": completion_code,
                "environment_error": environment_error,
                "timeout_sec": timeout,
            }
        ),
        f"netlist_generate_{label}_directory_resolution": json.dumps(
            {
                "builddir": builddir,
                "builddir_source": builddir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
            indent=2,
        ),
    }

    history_message = (
        f"netlist generate attempt {attempt} completed"
        if result["ok"]
        else f"netlist generate attempt {attempt} failed with exit code {result['exit_code']}"
    )
    if environment_error:
        history_message = (
            f"netlist generate attempt {attempt} failed environment precheck: {environment_error}"
        )
    if not run_completed:
        history_message = (
            f"netlist generate attempt {attempt} did not reach script completion; review skipped"
        )
    if environment_error:
        history_message = (
            f"netlist generate attempt {attempt} could not start netlist script: {environment_error}"
        )

    last_error = (
        None
        if result["ok"]
        else (
            f"Netlist generate attempt {attempt} failed with exit code "
            f"{result['exit_code']}: {result['stderr']}"
        )
    )
    if environment_error:
        last_error = (
            f"Netlist generate attempt {attempt} could not start because {environment_error}. "
            f"STDERR: {result['stderr']}"
        )
    if not run_completed:
        last_error = (
            f"Netlist generate attempt {attempt} did not finish the netlist script cleanly "
            f"before review. Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )
    if environment_error:
        last_error = (
            f"Netlist generate attempt {attempt} could not start because {environment_error}. "
            f"Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )

    updates = {
        "logs": logs,
        "history": state_for_attempt["history"] + [history_message],
        "last_error": last_error,
        "netlist_iteration": attempt,
        "netlist_candidate_path": candidate_path,
        "netlist_review_passed": False,
        "route_back_to_stage": None,
        "validation": {
            **state.get("validation", {}),
            "netlist_directory_resolution": {
                "builddir": builddir,
                "builddir_source": builddir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
        },
    }

    result_with_completion = {
        **result,
        "run_completed": run_completed,
        "completion_code": completion_code,
        "builddir": builddir,
        "builddir_source": builddir_source,
        "outdir": candidate_path,
        "outdir_source": outdir_source,
    }

    return updates, bool(result["ok"]), result_with_completion


def _run_netlist_review(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    builddir: str,
    candidate_path: str,
    generation_result: dict,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)

    review_result = ssh.run(
        _build_netlist_review_command(state, builddir, candidate_path),
        cwd=project_root,
        timeout=300,
    )

    logs = {
        **state["logs"],
        f"netlist_review_{label}_stdout": review_result["stdout"],
        f"netlist_review_{label}_stderr": review_result["stderr"],
        f"netlist_review_{label}_command": review_result["command"],
    }

    if not review_result["ok"]:
        errors = [
            (
                f"Netlist review command failed on attempt {attempt} with exit code "
                f"{review_result['exit_code']}: {review_result['stderr']}"
            )
        ]
        payload = {
            "pass": False,
            "failure_stage_owner": "netlist",
            "failure_class": "netlist_review_command_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "netlist",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["netlist review failed: 1 errors"],
                "last_error": errors[0],
                "netlist_review_passed": False,
                "netlist_review_errors": errors,
                "netlist_review_warnings": [],
                "netlist_review_suggested_fixes": [],
                "failure_stage_owner": "netlist",
                "failure_class": "netlist_review_command_failure",
                "route_back_to_stage": "netlist",
                "netlist_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "netlist_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    try:
        local_payload = _parse_json_payload(
            review_result["stdout"],
            label="netlist review",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        errors = [f"Unable to parse netlist review result on attempt {attempt}: {exc}"]
        payload = {
            "pass": False,
            "failure_stage_owner": "netlist",
            "failure_class": "netlist_review_parse_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "netlist",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["netlist review failed: 1 errors"],
                "last_error": errors[0],
                "netlist_review_passed": False,
                "netlist_review_errors": errors,
                "netlist_review_warnings": [],
                "netlist_review_suggested_fixes": [],
                "failure_stage_owner": "netlist",
                "failure_class": "netlist_review_parse_failure",
                "route_back_to_stage": "netlist",
                "netlist_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "netlist_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    command_error = None
    if not generation_result.get("ok"):
        command_error = (
            f"Netlist generation command failed with exit code "
            f"{generation_result.get('exit_code')}: "
            f"{generation_result.get('stderr', '').strip() or 'no stderr captured'}"
        )
        local_payload["errors"] = [
            *[str(item) for item in local_payload.get("errors", [])],
            command_error,
        ]
        local_payload["blocking_errors"] = [
            *[
                str(item)
                for item in local_payload.get(
                    "blocking_errors",
                    local_payload.get("errors", []),
                )
            ],
            command_error,
        ]
        local_payload["pass"] = False
        local_summary = str(local_payload.get("summary", "")).strip()
        local_payload["summary"] = (
            f"{local_summary} Netlist command execution failed."
            if local_summary
            else "Netlist command execution failed."
        )

    execution_context = {
        "ok": bool(generation_result.get("ok")),
        "exit_code": generation_result.get("exit_code"),
        "command": generation_result.get("command", ""),
        "stdout": generation_result.get("stdout", "")[:12000],
        "stderr": generation_result.get("stderr", "")[:12000],
        "builddir": builddir,
        "outdir": candidate_path,
    }
    ai_review = run_openai_netlist_review(
        local_review=local_payload,
        universal_rules_path=state.get("netlist_rules_path"),
        execution_context=execution_context,
    )
    merged_payload = merge_netlist_review_results(
        local_review=local_payload,
        ai_review=ai_review,
    )

    routing = extract_review_routing(merged_payload)
    errors = [str(item) for item in routing["blocking_errors"]]
    warnings = [str(item) for item in merged_payload.get("warnings", [])]
    suggested_fixes = [
        str(item) for item in merged_payload.get("suggested_fixes", [])
    ]
    review_context = merged_payload.get("context", {})
    detected_outputs = [
        str(item) for item in review_context.get("detected_outputs", [])
    ]
    resolved_top = review_context.get("resolved_top")
    passed = bool(merged_payload.get("pass")) and not errors and bool(generation_result.get("ok"))
    rtl_failure_triage = _attach_rtl_failure_triage(
        stage_name="netlist",
        label=label,
        merged_payload=merged_payload,
        execution_context=execution_context,
        review_result=review_result,
        logs=logs,
    )

    logs[f"netlist_review_{label}_local_result"] = json.dumps(local_payload, indent=2)
    logs[f"netlist_review_{label}_ai_request_metadata"] = json.dumps(
        ai_review.get("request_metadata", {}),
        indent=2,
    )
    logs[f"netlist_review_{label}_ai_raw_response"] = (
        ai_review.get("raw_response", "") or ""
    )
    if ai_review.get("api_error"):
        logs[f"netlist_review_{label}_ai_error"] = str(ai_review["api_error"])
    logs[f"netlist_review_{label}_result"] = json.dumps(merged_payload, indent=2)
    logs[f"netlist_review_{label}_routing"] = json.dumps(routing, indent=2)
    logs[f"stage_routing_decision_netlist_{label}"] = json.dumps(
        {
            "stage": "netlist",
            "attempt": attempt,
            **routing,
        },
        indent=2,
    )

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                "netlist review passed"
                if passed
                else f"netlist review failed: {len(errors)} errors"
            ],
            "last_error": None if passed else "Netlist review failed: " + "; ".join(errors),
            "netlist_review_passed": passed,
            "netlist_review_errors": errors,
            "netlist_review_warnings": warnings,
            "netlist_review_suggested_fixes": suggested_fixes,
            "netlist_review_summary": merged_payload.get("summary"),
            "netlist_output_files": detected_outputs,
            "netlist_resolved_top": (
                str(resolved_top).strip() if resolved_top is not None else None
            ),
            "failure_stage_owner": routing["failure_stage_owner"],
            "failure_class": routing["failure_class"],
            "route_back_to_stage": routing["route_back_to_stage"],
            "rtl_failure_handoff_owner": (
                rtl_failure_triage.get("handoff_owner")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_category": (
                rtl_failure_triage.get("failure_category")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_triage_payload": rtl_failure_triage or {},
            "netlist_review_payload": merged_payload,
            "validation": {
                **state.get("validation", {}),
                "netlist_review": {
                    **merged_payload,
                    "attempt": attempt,
                    "execution_context": {
                        "ok": execution_context["ok"],
                        "exit_code": execution_context["exit_code"],
                        "builddir": builddir,
                        "outdir": candidate_path,
                    },
                },
            },
        },
        passed,
    )


def _run_netlist_edit(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    builddir: str,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)
    review_payload = state.get("validation", {}).get("netlist_review", {})

    if not should_run_netlist_editor(review_payload):
        return (
            {
                "logs": {
                    **state["logs"],
                    f"netlist_edit_{label}_plan": json.dumps(
                        {
                            "actions": [],
                            "summary": (
                                "Netlist edit skipped because the reviewer did not assign ownership to the netlist stage."
                            ),
                            "warnings": [],
                        },
                        indent=2,
                    ),
                },
                "history": state["history"] + [
                    "netlist edit skipped: reviewer routed issue away from netlist"
                ],
                "netlist_edit_applied_actions": [],
                "netlist_edit_summary": (
                    "Skipped because the netlist reviewer routed the failure to a different stage."
                ),
            },
            False,
        )

    edit_plan = run_openai_netlist_edit(
        review_payload=review_payload,
        universal_rules_path=state.get("netlist_rules_path"),
    )

    logs = {
        **state["logs"],
        f"netlist_edit_{label}_ai_request_metadata": json.dumps(
            edit_plan.get("request_metadata", {}),
            indent=2,
        ),
        f"netlist_edit_{label}_ai_raw_response": edit_plan.get("raw_response", "") or "",
        f"netlist_edit_{label}_plan": json.dumps(
            {
                "actions": edit_plan.get("actions", []),
                "summary": edit_plan.get("summary", ""),
                "warnings": edit_plan.get("warnings", []),
            },
            indent=2,
        ),
    }

    if edit_plan.get("api_error"):
        logs[f"netlist_edit_{label}_ai_error"] = str(edit_plan["api_error"])

    actions = [
        item for item in edit_plan.get("actions", [])
        if isinstance(item, dict) and item.get("type") and item.get("path")
    ]

    if not actions:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["netlist edit skipped: no actions proposed"],
                "netlist_edit_applied_actions": [],
                "netlist_edit_summary": edit_plan.get("summary"),
                "netlist_review_warnings": [
                    *state.get("netlist_review_warnings", []),
                    *[str(item) for item in edit_plan.get("warnings", [])],
                ],
            },
            False,
        )

    apply_result = ssh.run(
        _build_netlist_edit_command(state, builddir, actions),
        cwd=project_root,
        timeout=300,
    )

    logs[f"netlist_edit_{label}_stdout"] = apply_result["stdout"]
    logs[f"netlist_edit_{label}_stderr"] = apply_result["stderr"]
    logs[f"netlist_edit_{label}_command"] = apply_result["command"]

    if not apply_result["ok"]:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["netlist edit failed to apply"],
                "netlist_edit_applied_actions": [],
                "netlist_edit_summary": edit_plan.get("summary"),
                "netlist_review_warnings": [
                    *state.get("netlist_review_warnings", []),
                    f"Netlist editor failed to apply actions: {apply_result['stderr']}",
                ],
            },
            False,
        )

    try:
        apply_payload = _parse_json_payload(
            apply_result["stdout"],
            label="netlist edit output",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["netlist edit produced unreadable output"],
                "netlist_edit_applied_actions": [],
                "netlist_edit_summary": edit_plan.get("summary"),
                "netlist_review_warnings": [
                    *state.get("netlist_review_warnings", []),
                    f"Netlist editor output could not be parsed: {exc}",
                ],
            },
            False,
        )

    applied_actions = [
        item for item in apply_payload.get("applied_actions", [])
        if isinstance(item, dict)
    ]

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                f"netlist edit applied {len(applied_actions)} actions"
            ],
            "netlist_edit_applied_actions": applied_actions,
            "netlist_edit_summary": edit_plan.get("summary") or apply_payload.get("summary"),
            "netlist_review_warnings": [
                *state.get("netlist_review_warnings", []),
                *[str(item) for item in edit_plan.get("warnings", [])],
            ],
        },
        bool(applied_actions),
    )


def _make_netlist_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        builddir, builddir_source = resolve_prepare_dir(
            state.get("prep_candidate_path"),
            state.get("remote_prepare_dir"),
        )
        candidate_path, outdir_source = resolve_netlist_dir_choice(
            state.get("remote_netlist_dir"),
            builddir,
        )
        netlist_max_iterations = state.get("netlist_max_iterations", 3)

        working_state = _merge_state(
            state,
            {
                "netlist_iteration": 0,
                "netlist_candidate_path": candidate_path,
                "netlist_review_passed": False,
                "netlist_review_errors": [],
                "netlist_review_warnings": [],
                "netlist_review_suggested_fixes": [],
                "netlist_review_summary": None,
                "netlist_edit_applied_actions": [],
                "netlist_edit_summary": None,
                "netlist_output_files": [],
                "netlist_resolved_top": None,
                "failure_stage_owner": None,
                "failure_class": None,
                "route_back_to_stage": None,
                "netlist_review_payload": {},
                "logs": {
                    **state["logs"],
                    "netlist_directory_resolution": json.dumps(
                        {
                            "builddir": builddir,
                            "builddir_source": builddir_source,
                            "outdir": candidate_path,
                            "outdir_source": outdir_source,
                        },
                        indent=2,
                    ),
                },
            },
        )

        for attempt in range(1, netlist_max_iterations + 1):
            generate_updates, generated_ok, generation_result = _run_netlist_generation(
                working_state,
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, generate_updates)

            if not generation_result.get("run_completed"):
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "netlist": "failed",
                        },
                    },
                )

            review_updates, review_passed = _run_netlist_review(
                working_state,
                ssh,
                attempt=attempt,
                builddir=builddir,
                candidate_path=candidate_path,
                generation_result=generation_result,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, review_updates)
            routing = extract_review_routing(
                working_state.get("validation", {}).get("netlist_review", {})
            )

            if routing["is_prep_owned"]:
                reroute_feedback = "\n".join(
                    [
                        *routing["blocking_errors"],
                        *[
                            f"Suggested fix: {item}"
                            for item in working_state.get(
                                "netlist_review_suggested_fixes",
                                [],
                            )
                        ],
                    ]
                )
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "netlist": "routed_to_prep",
                        },
                        "history": working_state["history"] + [
                            "netlist review routed the failure back to rtl_prep"
                        ],
                        "last_error": (
                            working_state.get("netlist_review_summary")
                            or "Netlist review routed the failure back to rtl_prep."
                        ),
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "rtl_prep_feedback": reroute_feedback,
                        },
                    },
                )

            if generated_ok and review_passed:
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "netlist": "success",
                        },
                        "outputs": {
                            **working_state["outputs"],
                            "netlist_remote_dir": f"{project_root}/{candidate_path}",
                        },
                        "last_error": None,
                    },
                )

            edit_updates, edit_applied = _run_netlist_edit(
                _merge_state(
                    working_state,
                    {
                        "history": working_state["history"] + [
                            f"netlist edit attempt {attempt}"
                        ]
                    },
                ),
                ssh,
                attempt=attempt,
                builddir=builddir,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, edit_updates)

            if edit_applied:
                rerun_updates, rerun_ok, rerun_result = _run_netlist_generation(
                    _merge_state(
                        working_state,
                        {
                            "history": working_state["history"] + [
                                f"netlist rerun after edit attempt {attempt}"
                            ]
                        },
                    ),
                    ssh,
                    attempt=attempt,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rerun_updates)

                if not rerun_result.get("run_completed"):
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "netlist": "failed",
                            },
                        },
                    )

                rereview_updates, rereview_passed = _run_netlist_review(
                    working_state,
                    ssh,
                    attempt=attempt,
                    builddir=builddir,
                    candidate_path=candidate_path,
                    generation_result=rerun_result,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rereview_updates)
                reroute = extract_review_routing(
                    working_state.get("validation", {}).get("netlist_review", {})
                )

                if reroute["is_prep_owned"]:
                    reroute_feedback = "\n".join(
                        [
                            *reroute["blocking_errors"],
                            *[
                                f"Suggested fix: {item}"
                                for item in working_state.get(
                                    "netlist_review_suggested_fixes",
                                    [],
                                )
                            ],
                        ]
                    )
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "netlist": "routed_to_prep",
                            },
                            "history": working_state["history"] + [
                                "netlist rereview routed the failure back to rtl_prep"
                            ],
                            "last_error": (
                                working_state.get("netlist_review_summary")
                                or "Netlist review routed the failure back to rtl_prep."
                            ),
                            "inputs": {
                                **working_state.get("inputs", {}),
                                "rtl_prep_feedback": reroute_feedback,
                            },
                        },
                    )

                if rerun_ok and rereview_passed:
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "netlist": "success",
                            },
                            "outputs": {
                                **working_state["outputs"],
                                "netlist_remote_dir": f"{project_root}/{candidate_path}",
                            },
                            "last_error": None,
                        },
                    )

            if attempt < netlist_max_iterations:
                retry_feedback_lines = [
                    *working_state.get("netlist_review_errors", []),
                    *[
                        f"Suggested fix: {item}"
                        for item in working_state.get(
                            "netlist_review_suggested_fixes",
                            [],
                        )
                    ],
                    *[
                        f"Applied edit: {item.get('type')} {item.get('path')}"
                        for item in working_state.get("netlist_edit_applied_actions", [])
                        if isinstance(item, dict)
                    ],
                ]
                retry_feedback = "\n".join(line for line in retry_feedback_lines if line)
                working_state = _merge_state(
                    working_state,
                    {
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "netlist_feedback": retry_feedback,
                        },
                    },
                )

        error_list = working_state.get("netlist_review_errors", [])
        failure_message = (
            "Netlist review failed after "
            f"{netlist_max_iterations} iterations: {'; '.join(error_list)}"
            if error_list
            else f"Netlist stage failed after {netlist_max_iterations} iterations."
        )

        return _merge_state(
            working_state,
            {
                "stage_status": {
                    **working_state["stage_status"],
                    "netlist": "failed",
                },
                "history": working_state["history"] + [
                    f"netlist failed after {netlist_max_iterations} iterations"
                ],
                "last_error": failure_message,
            },
        )

    return runner


def _run_mapped_generation(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool, dict]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    prep_dir, prep_dir_source = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    builddir, builddir_source = resolve_netlist_dir_choice(
        state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
        prep_dir,
    )
    candidate_path, outdir_source = resolve_mapped_dir_choice(
        state.get("remote_mapped_dir"),
        builddir,
        prep_dir,
    )
    label = log_label or str(attempt)
    timeout = int(state.get("mapped_run_timeout_sec", 14400))
    state_for_attempt = _merge_state(
        state,
        {
            "mapped_iteration": attempt,
            "history": state["history"] + [f"mapped generate attempt {attempt}"],
        },
    )

    result = ssh.run(
        _build_mapped_command(state_for_attempt),
        cwd=project_root,
        timeout=timeout,
    )
    run_completed, completion_code = _mapped_run_completion_status(result)
    environment_error = _mapped_run_environment_error(result)

    logs = {
        **state["logs"],
        f"mapped_generate_{label}_stdout": result["stdout"],
        f"mapped_generate_{label}_stderr": result["stderr"],
        f"mapped_generate_{label}_command": result["command"],
        f"mapped_generate_{label}_completed": json.dumps(
            {
                "run_completed": run_completed,
                "completion_code": completion_code,
                "environment_error": environment_error,
                "timeout_sec": timeout,
            }
        ),
        f"mapped_generate_{label}_directory_resolution": json.dumps(
            {
                "prep_dir": prep_dir,
                "prep_dir_source": prep_dir_source,
                "builddir": builddir,
                "builddir_source": builddir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
            indent=2,
        ),
    }

    history_message = (
        f"mapped generate attempt {attempt} completed"
        if result["ok"]
        else f"mapped generate attempt {attempt} failed with exit code {result['exit_code']}"
    )
    if environment_error:
        history_message = (
            f"mapped generate attempt {attempt} failed environment precheck: {environment_error}"
        )
    if not run_completed:
        history_message = (
            f"mapped generate attempt {attempt} did not reach script completion; review skipped"
        )
    if environment_error:
        history_message = (
            f"mapped generate attempt {attempt} could not start mapped script: {environment_error}"
        )

    last_error = (
        None
        if result["ok"]
        else (
            f"Mapped generate attempt {attempt} failed with exit code "
            f"{result['exit_code']}: {result['stderr']}"
        )
    )
    if environment_error:
        last_error = (
            f"Mapped generate attempt {attempt} could not start because {environment_error}. "
            f"STDERR: {result['stderr']}"
        )
    if not run_completed:
        last_error = (
            f"Mapped generate attempt {attempt} did not finish the mapped script cleanly "
            f"before review. Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )
    if environment_error:
        last_error = (
            f"Mapped generate attempt {attempt} could not start because {environment_error}. "
            f"Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )

    updates = {
        "logs": logs,
        "history": state_for_attempt["history"] + [history_message],
        "last_error": last_error,
        "mapped_iteration": attempt,
        "mapped_candidate_path": candidate_path,
        "mapped_review_passed": False,
        "route_back_to_stage": None,
        "validation": {
            **state.get("validation", {}),
            "mapped_directory_resolution": {
                "prep_dir": prep_dir,
                "prep_dir_source": prep_dir_source,
                "builddir": builddir,
                "builddir_source": builddir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
        },
    }

    result_with_completion = {
        **result,
        "run_completed": run_completed,
        "completion_code": completion_code,
        "builddir": builddir,
        "builddir_source": builddir_source,
        "outdir": candidate_path,
        "outdir_source": outdir_source,
    }

    return updates, bool(result["ok"]), result_with_completion


def _run_mapped_review(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    builddir: str,
    candidate_path: str,
    generation_result: dict,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)

    review_result = ssh.run(
        _build_mapped_review_command(state, builddir, candidate_path),
        cwd=project_root,
        timeout=300,
    )

    logs = {
        **state["logs"],
        f"mapped_review_{label}_stdout": review_result["stdout"],
        f"mapped_review_{label}_stderr": review_result["stderr"],
        f"mapped_review_{label}_command": review_result["command"],
    }

    if not review_result["ok"]:
        errors = [
            (
                f"Mapped review command failed on attempt {attempt} with exit code "
                f"{review_result['exit_code']}: {review_result['stderr']}"
            )
        ]
        payload = {
            "pass": False,
            "failure_stage_owner": "mapped_netlist",
            "failure_class": "mapped_review_command_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "mapped_netlist",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["mapped review failed: 1 errors"],
                "last_error": errors[0],
                "mapped_review_passed": False,
                "mapped_review_errors": errors,
                "mapped_review_warnings": [],
                "mapped_review_suggested_fixes": [],
                "failure_stage_owner": "mapped_netlist",
                "failure_class": "mapped_review_command_failure",
                "route_back_to_stage": "mapped_netlist",
                "mapped_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "mapped_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    try:
        local_payload = _parse_json_payload(
            review_result["stdout"],
            label="mapped review",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        errors = [f"Unable to parse mapped review result on attempt {attempt}: {exc}"]
        payload = {
            "pass": False,
            "failure_stage_owner": "mapped_netlist",
            "failure_class": "mapped_review_parse_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "mapped_netlist",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["mapped review failed: 1 errors"],
                "last_error": errors[0],
                "mapped_review_passed": False,
                "mapped_review_errors": errors,
                "mapped_review_warnings": [],
                "mapped_review_suggested_fixes": [],
                "failure_stage_owner": "mapped_netlist",
                "failure_class": "mapped_review_parse_failure",
                "route_back_to_stage": "mapped_netlist",
                "mapped_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "mapped_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    command_error = None
    if not generation_result.get("ok"):
        command_error = (
            f"Mapped generation command failed with exit code "
            f"{generation_result.get('exit_code')}: "
            f"{generation_result.get('stderr', '').strip() or 'no stderr captured'}"
        )
        local_payload["errors"] = [
            *[str(item) for item in local_payload.get("errors", [])],
            command_error,
        ]
        local_payload["blocking_errors"] = [
            *[
                str(item)
                for item in local_payload.get(
                    "blocking_errors",
                    local_payload.get("errors", []),
                )
            ],
            command_error,
        ]
        local_payload["pass"] = False
        local_summary = str(local_payload.get("summary", "")).strip()
        local_payload["summary"] = (
            f"{local_summary} Mapped command execution failed."
            if local_summary
            else "Mapped command execution failed."
        )

    execution_context = {
        "ok": bool(generation_result.get("ok")),
        "exit_code": generation_result.get("exit_code"),
        "command": generation_result.get("command", ""),
        "stdout": generation_result.get("stdout", "")[:12000],
        "stderr": generation_result.get("stderr", "")[:12000],
        "builddir": builddir,
        "outdir": candidate_path,
    }
    ai_review = run_openai_mapped_review(
        local_review=local_payload,
        universal_rules_path=state.get("mapped_rules_path"),
        execution_context=execution_context,
    )
    merged_payload = merge_mapped_review_results(
        local_review=local_payload,
        ai_review=ai_review,
    )

    routing = extract_review_routing(merged_payload)
    errors = [str(item) for item in routing["blocking_errors"]]
    warnings = [str(item) for item in merged_payload.get("warnings", [])]
    suggested_fixes = [
        str(item) for item in merged_payload.get("suggested_fixes", [])
    ]
    review_context = merged_payload.get("context", {})
    detected_outputs = [
        str(item) for item in review_context.get("detected_outputs", [])
    ]
    resolved_top = review_context.get("resolved_top")
    passed = bool(merged_payload.get("pass")) and not errors and bool(generation_result.get("ok"))
    rtl_failure_triage = _attach_rtl_failure_triage(
        stage_name="mapped",
        label=label,
        merged_payload=merged_payload,
        execution_context=execution_context,
        review_result=review_result,
        logs=logs,
    )

    logs[f"mapped_review_{label}_local_result"] = json.dumps(local_payload, indent=2)
    logs[f"mapped_review_{label}_ai_request_metadata"] = json.dumps(
        ai_review.get("request_metadata", {}),
        indent=2,
    )
    logs[f"mapped_review_{label}_ai_raw_response"] = (
        ai_review.get("raw_response", "") or ""
    )
    if ai_review.get("api_error"):
        logs[f"mapped_review_{label}_ai_error"] = str(ai_review["api_error"])
    logs[f"mapped_review_{label}_result"] = json.dumps(merged_payload, indent=2)
    logs[f"mapped_review_{label}_routing"] = json.dumps(routing, indent=2)
    logs[f"stage_routing_decision_mapped_{label}"] = json.dumps(
        {
            "stage": "mapped_netlist",
            "attempt": attempt,
            **routing,
        },
        indent=2,
    )

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                "mapped review passed"
                if passed
                else f"mapped review failed: {len(errors)} errors"
            ],
            "last_error": None if passed else "Mapped review failed: " + "; ".join(errors),
            "mapped_review_passed": passed,
            "mapped_review_errors": errors,
            "mapped_review_warnings": warnings,
            "mapped_review_suggested_fixes": suggested_fixes,
            "mapped_review_summary": merged_payload.get("summary"),
            "mapped_output_files": detected_outputs,
            "mapped_resolved_top": (
                str(resolved_top).strip() if resolved_top is not None else None
            ),
            "failure_stage_owner": routing["failure_stage_owner"],
            "failure_class": routing["failure_class"],
            "route_back_to_stage": routing["route_back_to_stage"],
            "rtl_failure_handoff_owner": (
                rtl_failure_triage.get("handoff_owner")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_category": (
                rtl_failure_triage.get("failure_category")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_triage_payload": rtl_failure_triage or {},
            "mapped_review_payload": merged_payload,
            "validation": {
                **state.get("validation", {}),
                "mapped_review": {
                    **merged_payload,
                    "attempt": attempt,
                    "execution_context": {
                        "ok": execution_context["ok"],
                        "exit_code": execution_context["exit_code"],
                        "builddir": builddir,
                        "outdir": candidate_path,
                    },
                },
            },
        },
        passed,
    )


def _run_mapped_edit(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    builddir: str,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)
    review_payload = state.get("validation", {}).get("mapped_review", {})

    if not should_run_mapped_editor(review_payload):
        return (
            {
                "logs": {
                    **state["logs"],
                    f"mapped_edit_{label}_plan": json.dumps(
                        {
                            "actions": [],
                            "summary": (
                                "Mapped edit skipped because the reviewer did not assign ownership to the mapped stage."
                            ),
                            "warnings": [],
                        },
                        indent=2,
                    ),
                },
                "history": state["history"] + [
                    "mapped edit skipped: reviewer routed issue away from mapped"
                ],
                "mapped_edit_applied_actions": [],
                "mapped_edit_summary": (
                    "Skipped because the mapped reviewer routed the failure to a different stage."
                ),
            },
            False,
        )

    edit_plan = run_openai_mapped_edit(
        review_payload=review_payload,
        universal_rules_path=state.get("mapped_rules_path"),
    )

    logs = {
        **state["logs"],
        f"mapped_edit_{label}_ai_request_metadata": json.dumps(
            edit_plan.get("request_metadata", {}),
            indent=2,
        ),
        f"mapped_edit_{label}_ai_raw_response": edit_plan.get("raw_response", "") or "",
        f"mapped_edit_{label}_plan": json.dumps(
            {
                "actions": edit_plan.get("actions", []),
                "summary": edit_plan.get("summary", ""),
                "warnings": edit_plan.get("warnings", []),
            },
            indent=2,
        ),
    }

    if edit_plan.get("api_error"):
        logs[f"mapped_edit_{label}_ai_error"] = str(edit_plan["api_error"])

    actions = [
        item for item in edit_plan.get("actions", [])
        if isinstance(item, dict) and item.get("type") and item.get("path")
    ]

    if not actions:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["mapped edit skipped: no actions proposed"],
                "mapped_edit_applied_actions": [],
                "mapped_edit_summary": edit_plan.get("summary"),
                "mapped_review_warnings": [
                    *state.get("mapped_review_warnings", []),
                    *[str(item) for item in edit_plan.get("warnings", [])],
                ],
            },
            False,
        )

    apply_result = ssh.run(
        _build_mapped_edit_command(state, builddir, actions),
        cwd=project_root,
        timeout=300,
    )

    logs[f"mapped_edit_{label}_stdout"] = apply_result["stdout"]
    logs[f"mapped_edit_{label}_stderr"] = apply_result["stderr"]
    logs[f"mapped_edit_{label}_command"] = apply_result["command"]

    if not apply_result["ok"]:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["mapped edit failed to apply"],
                "mapped_edit_applied_actions": [],
                "mapped_edit_summary": edit_plan.get("summary"),
                "mapped_review_warnings": [
                    *state.get("mapped_review_warnings", []),
                    f"Mapped editor failed to apply actions: {apply_result['stderr']}",
                ],
            },
            False,
        )

    try:
        apply_payload = _parse_json_payload(
            apply_result["stdout"],
            label="mapped edit output",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["mapped edit produced unreadable output"],
                "mapped_edit_applied_actions": [],
                "mapped_edit_summary": edit_plan.get("summary"),
                "mapped_review_warnings": [
                    *state.get("mapped_review_warnings", []),
                    f"Mapped editor output could not be parsed: {exc}",
                ],
            },
            False,
        )

    applied_actions = [
        item for item in apply_payload.get("applied_actions", [])
        if isinstance(item, dict)
    ]

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                f"mapped edit applied {len(applied_actions)} actions"
            ],
            "mapped_edit_applied_actions": applied_actions,
            "mapped_edit_summary": edit_plan.get("summary") or apply_payload.get("summary"),
            "mapped_review_warnings": [
                *state.get("mapped_review_warnings", []),
                *[str(item) for item in edit_plan.get("warnings", [])],
            ],
        },
        bool(applied_actions),
    )


def _make_mapped_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        prep_dir, prep_dir_source = resolve_prepare_dir(
            state.get("prep_candidate_path"),
            state.get("remote_prepare_dir"),
        )
        builddir, builddir_source = resolve_netlist_dir_choice(
            state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
            prep_dir,
        )
        candidate_path, outdir_source = resolve_mapped_dir_choice(
            state.get("remote_mapped_dir"),
            builddir,
            prep_dir,
        )
        mapped_max_iterations = state.get("mapped_max_iterations", 3)

        working_state = _merge_state(
            state,
            {
                "mapped_iteration": 0,
                "mapped_candidate_path": candidate_path,
                "mapped_review_passed": False,
                "mapped_review_errors": [],
                "mapped_review_warnings": [],
                "mapped_review_suggested_fixes": [],
                "mapped_review_summary": None,
                "mapped_edit_applied_actions": [],
                "mapped_edit_summary": None,
                "mapped_output_files": [],
                "mapped_resolved_top": None,
                "failure_stage_owner": None,
                "failure_class": None,
                "route_back_to_stage": None,
                "mapped_review_payload": {},
                "logs": {
                    **state["logs"],
                    "mapped_directory_resolution": json.dumps(
                        {
                            "prep_dir": prep_dir,
                            "prep_dir_source": prep_dir_source,
                            "builddir": builddir,
                            "builddir_source": builddir_source,
                            "outdir": candidate_path,
                            "outdir_source": outdir_source,
                        },
                        indent=2,
                    ),
                },
            },
        )

        for attempt in range(1, mapped_max_iterations + 1):
            generate_updates, generated_ok, generation_result = _run_mapped_generation(
                working_state,
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, generate_updates)

            if not generation_result.get("run_completed"):
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "mapped_netlist": "failed",
                        },
                    },
                )

            review_updates, review_passed = _run_mapped_review(
                working_state,
                ssh,
                attempt=attempt,
                builddir=builddir,
                candidate_path=candidate_path,
                generation_result=generation_result,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, review_updates)
            routing = extract_review_routing(
                working_state.get("validation", {}).get("mapped_review", {})
            )

            if routing["is_netlist_owned"]:
                reroute_feedback = "\n".join(
                    [
                        *routing["blocking_errors"],
                        *[
                            f"Suggested fix: {item}"
                            for item in working_state.get(
                                "mapped_review_suggested_fixes",
                                [],
                            )
                        ],
                    ]
                )
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "mapped_netlist": "routed_to_netlist",
                        },
                        "history": working_state["history"] + [
                            "mapped review routed the failure back to netlist"
                        ],
                        "last_error": (
                            working_state.get("mapped_review_summary")
                            or "Mapped review routed the failure back to netlist."
                        ),
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "netlist_feedback": reroute_feedback,
                        },
                    },
                )

            if generated_ok and review_passed:
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "mapped_netlist": "success",
                        },
                        "outputs": {
                            **working_state["outputs"],
                            "mapped_remote_dir": f"{project_root}/{candidate_path}",
                        },
                        "last_error": None,
                    },
                )

            edit_updates, edit_applied = _run_mapped_edit(
                _merge_state(
                    working_state,
                    {
                        "history": working_state["history"] + [
                            f"mapped edit attempt {attempt}"
                        ]
                    },
                ),
                ssh,
                attempt=attempt,
                builddir=builddir,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, edit_updates)

            if edit_applied:
                rerun_updates, rerun_ok, rerun_result = _run_mapped_generation(
                    _merge_state(
                        working_state,
                        {
                            "history": working_state["history"] + [
                                f"mapped rerun after edit attempt {attempt}"
                            ]
                        },
                    ),
                    ssh,
                    attempt=attempt,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rerun_updates)

                if not rerun_result.get("run_completed"):
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "mapped_netlist": "failed",
                            },
                        },
                    )

                rereview_updates, rereview_passed = _run_mapped_review(
                    working_state,
                    ssh,
                    attempt=attempt,
                    builddir=builddir,
                    candidate_path=candidate_path,
                    generation_result=rerun_result,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rereview_updates)
                reroute = extract_review_routing(
                    working_state.get("validation", {}).get("mapped_review", {})
                )

                if reroute["is_netlist_owned"]:
                    reroute_feedback = "\n".join(
                        [
                            *reroute["blocking_errors"],
                            *[
                                f"Suggested fix: {item}"
                                for item in working_state.get(
                                    "mapped_review_suggested_fixes",
                                    [],
                                )
                            ],
                        ]
                    )
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "mapped_netlist": "routed_to_netlist",
                            },
                            "history": working_state["history"] + [
                                "mapped rereview routed the failure back to netlist"
                            ],
                            "last_error": (
                                working_state.get("mapped_review_summary")
                                or "Mapped review routed the failure back to netlist."
                            ),
                            "inputs": {
                                **working_state.get("inputs", {}),
                                "netlist_feedback": reroute_feedback,
                            },
                        },
                    )

                if rerun_ok and rereview_passed:
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "mapped_netlist": "success",
                            },
                            "outputs": {
                                **working_state["outputs"],
                                "mapped_remote_dir": f"{project_root}/{candidate_path}",
                            },
                            "last_error": None,
                        },
                    )

            if attempt < mapped_max_iterations:
                retry_feedback_lines = [
                    *working_state.get("mapped_review_errors", []),
                    *[
                        f"Suggested fix: {item}"
                        for item in working_state.get(
                            "mapped_review_suggested_fixes",
                            [],
                        )
                    ],
                    *[
                        f"Applied edit: {item.get('type')} {item.get('path')}"
                        for item in working_state.get("mapped_edit_applied_actions", [])
                        if isinstance(item, dict)
                    ],
                ]
                retry_feedback = "\n".join(line for line in retry_feedback_lines if line)
                working_state = _merge_state(
                    working_state,
                    {
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "mapped_feedback": retry_feedback,
                        },
                    },
                )

        error_list = working_state.get("mapped_review_errors", [])
        failure_message = (
            "Mapped review failed after "
            f"{mapped_max_iterations} iterations: {'; '.join(error_list)}"
            if error_list
            else f"Mapped stage failed after {mapped_max_iterations} iterations."
        )

        return _merge_state(
            working_state,
            {
                "stage_status": {
                    **working_state["stage_status"],
                    "mapped_netlist": "failed",
                },
                "history": working_state["history"] + [
                    f"mapped failed after {mapped_max_iterations} iterations"
                ],
                "last_error": failure_message,
            },
        )

    return runner


def _run_gdsii_generation(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool, dict]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    prep_dir, prep_dir_source = resolve_prepare_dir(
        state.get("prep_candidate_path"),
        state.get("remote_prepare_dir"),
    )
    netlist_dir, netlist_dir_source = resolve_netlist_dir_choice(
        state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
        prep_dir,
    )
    mapped_dir, mapped_dir_source = resolve_mapped_dir_choice(
        state.get("mapped_candidate_path") or state.get("remote_mapped_dir"),
        netlist_dir,
        prep_dir,
    )
    candidate_path, outdir_source = resolve_gdsii_dir_choice(
        state.get("remote_gdsii_dir"),
        mapped_dir,
        netlist_dir,
        prep_dir,
    )
    label = log_label or str(attempt)
    timeout = int(state.get("gdsii_run_timeout_sec", 14400))
    state_for_attempt = _merge_state(
        state,
        {
            "gdsii_iteration": attempt,
            "history": state["history"] + [f"gdsii generate attempt {attempt}"],
        },
    )

    result = ssh.run(
        _build_gdsii_command(state_for_attempt),
        cwd=project_root,
        timeout=timeout,
    )
    run_completed, completion_code = _gdsii_run_completion_status(result)
    environment_error = _gdsii_run_environment_error(result)

    logs = {
        **state["logs"],
        f"gdsii_generate_{label}_stdout": result["stdout"],
        f"gdsii_generate_{label}_stderr": result["stderr"],
        f"gdsii_generate_{label}_command": result["command"],
        f"gdsii_generate_{label}_completed": json.dumps(
            {
                "run_completed": run_completed,
                "completion_code": completion_code,
                "environment_error": environment_error,
                "timeout_sec": timeout,
            }
        ),
        f"gdsii_generate_{label}_directory_resolution": json.dumps(
            {
                "prep_dir": prep_dir,
                "prep_dir_source": prep_dir_source,
                "netlist_dir": netlist_dir,
                "netlist_dir_source": netlist_dir_source,
                "mappeddir": mapped_dir,
                "mappeddir_source": mapped_dir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
            indent=2,
        ),
    }

    history_message = (
        f"gdsii generate attempt {attempt} completed"
        if result["ok"]
        else f"gdsii generate attempt {attempt} failed with exit code {result['exit_code']}"
    )
    if environment_error:
        history_message = (
            f"gdsii generate attempt {attempt} failed environment precheck: {environment_error}"
        )
    if not run_completed:
        history_message = (
            f"gdsii generate attempt {attempt} did not reach script completion; review skipped"
        )
    if environment_error:
        history_message = (
            f"gdsii generate attempt {attempt} could not start gdsii script: {environment_error}"
        )

    last_error = (
        None
        if result["ok"]
        else (
            f"GDSII generate attempt {attempt} failed with exit code "
            f"{result['exit_code']}: {result['stderr']}"
        )
    )
    if environment_error:
        last_error = (
            f"GDSII generate attempt {attempt} could not start because {environment_error}. "
            f"STDERR: {result['stderr']}"
        )
    if not run_completed:
        last_error = (
            f"GDSII generate attempt {attempt} did not finish the GDSII script cleanly "
            f"before review. Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )
    if environment_error:
        last_error = (
            f"GDSII generate attempt {attempt} could not start because {environment_error}. "
            f"Configured timeout was {timeout} seconds. STDERR: {result['stderr']}"
        )

    updates = {
        "logs": logs,
        "history": state_for_attempt["history"] + [history_message],
        "last_error": last_error,
        "gdsii_iteration": attempt,
        "gdsii_candidate_path": candidate_path,
        "gdsii_review_passed": False,
        "route_back_to_stage": None,
        "validation": {
            **state.get("validation", {}),
            "gdsii_directory_resolution": {
                "prep_dir": prep_dir,
                "prep_dir_source": prep_dir_source,
                "netlist_dir": netlist_dir,
                "netlist_dir_source": netlist_dir_source,
                "mappeddir": mapped_dir,
                "mappeddir_source": mapped_dir_source,
                "outdir": candidate_path,
                "outdir_source": outdir_source,
            },
        },
    }

    result_with_completion = {
        **result,
        "run_completed": run_completed,
        "completion_code": completion_code,
        "mappeddir": mapped_dir,
        "mappeddir_source": mapped_dir_source,
        "outdir": candidate_path,
        "outdir_source": outdir_source,
    }

    return updates, bool(result["ok"]), result_with_completion


def _run_gdsii_review(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    mappeddir: str,
    candidate_path: str,
    generation_result: dict,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)

    review_result = ssh.run(
        _build_gdsii_review_command(state, mappeddir, candidate_path),
        cwd=project_root,
        timeout=300,
    )

    logs = {
        **state["logs"],
        f"gdsii_review_{label}_stdout": review_result["stdout"],
        f"gdsii_review_{label}_stderr": review_result["stderr"],
        f"gdsii_review_{label}_command": review_result["command"],
    }

    if not review_result["ok"]:
        errors = [
            (
                f"GDSII review command failed on attempt {attempt} with exit code "
                f"{review_result['exit_code']}: {review_result['stderr']}"
            )
        ]
        payload = {
            "pass": False,
            "failure_stage_owner": "gdsii",
            "failure_class": "gdsii_review_command_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "gdsii",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["gdsii review failed: 1 errors"],
                "last_error": errors[0],
                "gdsii_review_passed": False,
                "gdsii_review_errors": errors,
                "gdsii_review_warnings": [],
                "gdsii_review_suggested_fixes": [],
                "failure_stage_owner": "gdsii",
                "failure_class": "gdsii_review_command_failure",
                "route_back_to_stage": "gdsii",
                "gdsii_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "gdsii_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    try:
        local_payload = _parse_json_payload(
            review_result["stdout"],
            label="gdsii review",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        errors = [f"Unable to parse GDSII review result on attempt {attempt}: {exc}"]
        payload = {
            "pass": False,
            "failure_stage_owner": "gdsii",
            "failure_class": "gdsii_review_parse_failure",
            "blocking_errors": errors,
            "errors": errors,
            "warnings": [],
            "suggested_fixes": [],
            "route_back_to_stage": "gdsii",
            "summary": errors[0],
        }
        return (
            {
                "logs": logs,
                "history": state["history"] + ["gdsii review failed: 1 errors"],
                "last_error": errors[0],
                "gdsii_review_passed": False,
                "gdsii_review_errors": errors,
                "gdsii_review_warnings": [],
                "gdsii_review_suggested_fixes": [],
                "failure_stage_owner": "gdsii",
                "failure_class": "gdsii_review_parse_failure",
                "route_back_to_stage": "gdsii",
                "gdsii_review_payload": payload,
                "validation": {
                    **state.get("validation", {}),
                    "gdsii_review": {
                        **payload,
                        "attempt": attempt,
                    },
                },
            },
            False,
        )

    command_error = None
    if not generation_result.get("ok"):
        command_error = (
            f"GDSII generation command failed with exit code "
            f"{generation_result.get('exit_code')}: "
            f"{generation_result.get('stderr', '').strip() or 'no stderr captured'}"
        )
        local_payload["errors"] = [
            *[str(item) for item in local_payload.get("errors", [])],
            command_error,
        ]
        local_payload["blocking_errors"] = [
            *[
                str(item)
                for item in local_payload.get(
                    "blocking_errors",
                    local_payload.get("errors", []),
                )
            ],
            command_error,
        ]
        local_payload["pass"] = False
        local_summary = str(local_payload.get("summary", "")).strip()
        local_payload["summary"] = (
            f"{local_summary} GDSII command execution failed."
            if local_summary
            else "GDSII command execution failed."
        )

    execution_context = {
        "ok": bool(generation_result.get("ok")),
        "exit_code": generation_result.get("exit_code"),
        "command": generation_result.get("command", ""),
        "stdout": generation_result.get("stdout", "")[:12000],
        "stderr": generation_result.get("stderr", "")[:12000],
        "mappeddir": mappeddir,
        "outdir": candidate_path,
    }
    ai_review = run_openai_gdsii_review(
        local_review=local_payload,
        universal_rules_path=state.get("gdsii_rules_path"),
        execution_context=execution_context,
    )
    merged_payload = merge_gdsii_review_results(
        local_review=local_payload,
        ai_review=ai_review,
    )

    routing = extract_review_routing(merged_payload)
    errors = [str(item) for item in routing["blocking_errors"]]
    warnings = [str(item) for item in merged_payload.get("warnings", [])]
    suggested_fixes = [
        str(item) for item in merged_payload.get("suggested_fixes", [])
    ]
    review_context = merged_payload.get("context", {})
    detected_outputs = [
        str(item) for item in review_context.get("detected_outputs", [])
    ]
    passed = bool(merged_payload.get("pass")) and not errors and bool(generation_result.get("ok"))
    rtl_failure_triage = _attach_rtl_failure_triage(
        stage_name="gdsii",
        label=label,
        merged_payload=merged_payload,
        execution_context=execution_context,
        review_result=review_result,
        logs=logs,
    )

    logs[f"gdsii_review_{label}_local_result"] = json.dumps(local_payload, indent=2)
    logs[f"gdsii_review_{label}_ai_request_metadata"] = json.dumps(
        ai_review.get("request_metadata", {}),
        indent=2,
    )
    logs[f"gdsii_review_{label}_ai_raw_response"] = (
        ai_review.get("raw_response", "") or ""
    )
    if ai_review.get("api_error"):
        logs[f"gdsii_review_{label}_ai_error"] = str(ai_review["api_error"])
    logs[f"gdsii_review_{label}_result"] = json.dumps(merged_payload, indent=2)
    logs[f"gdsii_review_{label}_routing"] = json.dumps(routing, indent=2)
    logs[f"stage_routing_decision_gdsii_{label}"] = json.dumps(
        {
            "stage": "gdsii",
            "attempt": attempt,
            **routing,
        },
        indent=2,
    )

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                "gdsii review passed"
                if passed
                else f"gdsii review failed: {len(errors)} errors"
            ],
            "last_error": None if passed else "GDSII review failed: " + "; ".join(errors),
            "gdsii_review_passed": passed,
            "gdsii_review_errors": errors,
            "gdsii_review_warnings": warnings,
            "gdsii_review_suggested_fixes": suggested_fixes,
            "gdsii_review_summary": merged_payload.get("summary"),
            "gdsii_output_files": detected_outputs,
            "failure_stage_owner": routing["failure_stage_owner"],
            "failure_class": routing["failure_class"],
            "route_back_to_stage": routing["route_back_to_stage"],
            "rtl_failure_handoff_owner": (
                rtl_failure_triage.get("handoff_owner")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_category": (
                rtl_failure_triage.get("failure_category")
                if rtl_failure_triage
                else None
            ),
            "rtl_failure_triage_payload": rtl_failure_triage or {},
            "gdsii_review_payload": merged_payload,
            "validation": {
                **state.get("validation", {}),
                "gdsii_review": {
                    **merged_payload,
                    "attempt": attempt,
                    "execution_context": {
                        "ok": execution_context["ok"],
                        "exit_code": execution_context["exit_code"],
                        "mappeddir": mappeddir,
                        "outdir": candidate_path,
                    },
                },
            },
        },
        passed,
    )


def _run_gdsii_edit(
    state: FlowState,
    ssh: SSHExecutor,
    *,
    attempt: int,
    log_label: str | None = None,
) -> tuple[dict, bool]:
    project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
    label = log_label or str(attempt)
    review_payload = state.get("validation", {}).get("gdsii_review", {})

    if not should_run_gdsii_editor(review_payload):
        return (
            {
                "logs": {
                    **state["logs"],
                    f"gdsii_edit_{label}_plan": json.dumps(
                        {
                            "actions": [],
                            "summary": (
                                "GDSII edit skipped because the reviewer did not assign ownership to the GDSII stage."
                            ),
                            "warnings": [],
                        },
                        indent=2,
                    ),
                },
                "history": state["history"] + [
                    "gdsii edit skipped: reviewer routed issue away from gdsii"
                ],
                "gdsii_edit_applied_actions": [],
                "gdsii_edit_summary": (
                    "Skipped because the GDSII reviewer routed the failure to a different stage."
                ),
            },
            False,
        )

    edit_plan = run_openai_gdsii_edit(
        review_payload=review_payload,
        universal_rules_path=state.get("gdsii_rules_path"),
    )

    logs = {
        **state["logs"],
        f"gdsii_edit_{label}_ai_request_metadata": json.dumps(
            edit_plan.get("request_metadata", {}),
            indent=2,
        ),
        f"gdsii_edit_{label}_ai_raw_response": edit_plan.get("raw_response", "") or "",
        f"gdsii_edit_{label}_plan": json.dumps(
            {
                "actions": edit_plan.get("actions", []),
                "summary": edit_plan.get("summary", ""),
                "warnings": edit_plan.get("warnings", []),
            },
            indent=2,
        ),
    }

    if edit_plan.get("api_error"):
        logs[f"gdsii_edit_{label}_ai_error"] = str(edit_plan["api_error"])

    actions = [
        item for item in edit_plan.get("actions", [])
        if isinstance(item, dict) and item.get("type") and item.get("path")
    ]

    if not actions:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["gdsii edit skipped: no actions proposed"],
                "gdsii_edit_applied_actions": [],
                "gdsii_edit_summary": edit_plan.get("summary"),
                "gdsii_review_warnings": [
                    *state.get("gdsii_review_warnings", []),
                    *[str(item) for item in edit_plan.get("warnings", [])],
                ],
            },
            False,
        )

    apply_result = ssh.run(
        _build_gdsii_edit_command(state, actions),
        cwd=project_root,
        timeout=300,
    )

    logs[f"gdsii_edit_{label}_stdout"] = apply_result["stdout"]
    logs[f"gdsii_edit_{label}_stderr"] = apply_result["stderr"]
    logs[f"gdsii_edit_{label}_command"] = apply_result["command"]

    if not apply_result["ok"]:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["gdsii edit failed to apply"],
                "gdsii_edit_applied_actions": [],
                "gdsii_edit_summary": edit_plan.get("summary"),
                "gdsii_review_warnings": [
                    *state.get("gdsii_review_warnings", []),
                    f"GDSII editor failed to apply actions: {apply_result['stderr']}",
                ],
            },
            False,
        )

    try:
        apply_payload = _parse_json_payload(
            apply_result["stdout"],
            label="gdsii edit output",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return (
            {
                "logs": logs,
                "history": state["history"] + ["gdsii edit produced unreadable output"],
                "gdsii_edit_applied_actions": [],
                "gdsii_edit_summary": edit_plan.get("summary"),
                "gdsii_review_warnings": [
                    *state.get("gdsii_review_warnings", []),
                    f"GDSII editor output could not be parsed: {exc}",
                ],
            },
            False,
        )

    applied_actions = [
        item for item in apply_payload.get("applied_actions", [])
        if isinstance(item, dict)
    ]

    return (
        {
            "logs": logs,
            "history": state["history"] + [
                f"gdsii edit applied {len(applied_actions)} actions"
            ],
            "gdsii_edit_applied_actions": applied_actions,
            "gdsii_edit_summary": edit_plan.get("summary") or apply_payload.get("summary"),
            "gdsii_review_warnings": [
                *state.get("gdsii_review_warnings", []),
                *[str(item) for item in edit_plan.get("warnings", [])],
            ],
        },
        bool(applied_actions),
    )


def _make_gdsii_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    implementation_runner = _make_gdsii_implementation_runner(ssh)

    def runner(state: FlowState) -> dict:
        if not state.get("timing_closure_enabled", True):
            return implementation_runner(_merge_state(state, {
                "timing_closure_status": "disabled",
                "history": state["history"] + ["Timing closure explicitly disabled; GDS generation only"],
            }))
        return run_setup_closure(state, ssh, implementation_runner, _merge_state)

    return runner


def _make_gdsii_implementation_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        prep_dir, prep_dir_source = resolve_prepare_dir(
            state.get("prep_candidate_path"),
            state.get("remote_prepare_dir"),
        )
        netlist_dir, netlist_dir_source = resolve_netlist_dir_choice(
            state.get("netlist_candidate_path") or state.get("remote_netlist_dir"),
            prep_dir,
        )
        mapped_dir, mapped_dir_source = resolve_mapped_dir_choice(
            state.get("mapped_candidate_path") or state.get("remote_mapped_dir"),
            netlist_dir,
            prep_dir,
        )
        candidate_path, outdir_source = resolve_gdsii_dir_choice(
            state.get("remote_gdsii_dir"),
            mapped_dir,
            netlist_dir,
            prep_dir,
        )
        gdsii_max_iterations = state.get("gdsii_max_iterations", 3)

        working_state = _merge_state(
            state,
            {
                "gdsii_iteration": 0,
                "gdsii_candidate_path": candidate_path,
                "gdsii_review_passed": False,
                "gdsii_review_errors": [],
                "gdsii_review_warnings": [],
                "gdsii_review_suggested_fixes": [],
                "gdsii_review_summary": None,
                "gdsii_edit_applied_actions": [],
                "gdsii_edit_summary": None,
                "gdsii_output_files": [],
                "failure_stage_owner": None,
                "failure_class": None,
                "route_back_to_stage": None,
                "gdsii_review_payload": {},
                "logs": {
                    **state["logs"],
                    "gdsii_directory_resolution": json.dumps(
                        {
                            "prep_dir": prep_dir,
                            "prep_dir_source": prep_dir_source,
                            "netlist_dir": netlist_dir,
                            "netlist_dir_source": netlist_dir_source,
                            "mappeddir": mapped_dir,
                            "mappeddir_source": mapped_dir_source,
                            "outdir": candidate_path,
                            "outdir_source": outdir_source,
                        },
                        indent=2,
                    ),
                },
            },
        )

        for attempt in range(1, gdsii_max_iterations + 1):
            generate_updates, generated_ok, generation_result = _run_gdsii_generation(
                working_state,
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, generate_updates)

            if not generation_result.get("run_completed"):
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "gdsii": "failed",
                        },
                    },
                )

            review_updates, review_passed = _run_gdsii_review(
                working_state,
                ssh,
                attempt=attempt,
                mappeddir=mapped_dir,
                candidate_path=candidate_path,
                generation_result=generation_result,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, review_updates)
            routing = extract_review_routing(
                working_state.get("validation", {}).get("gdsii_review", {})
            )

            if routing["is_mapped_owned"]:
                reroute_feedback = "\n".join(
                    [
                        *routing["blocking_errors"],
                        *[
                            f"Suggested fix: {item}"
                            for item in working_state.get(
                                "gdsii_review_suggested_fixes",
                                [],
                            )
                        ],
                    ]
                )
                return _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "gdsii": "routed_to_mapped",
                        },
                        "history": working_state["history"] + [
                            "gdsii review routed the failure back to mapped_netlist"
                        ],
                        "last_error": (
                            working_state.get("gdsii_review_summary")
                            or "GDSII review routed the failure back to mapped_netlist."
                        ),
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "mapped_feedback": reroute_feedback,
                        },
                    },
                )

            if generated_ok and review_passed:
                success_state = _merge_state(
                    working_state,
                    {
                        "stage_status": {
                            **working_state["stage_status"],
                            "gdsii": "success",
                        },
                        "outputs": {
                            **working_state["outputs"],
                            "gdsii_remote_dir": f"{project_root}/{candidate_path}",
                        },
                        "last_error": None,
                    },
                )
                timing_updates = _run_remote_timing_closure_analysis(
                    success_state,
                    ssh,
                    label=f"{attempt}",
                )
                return _merge_state(success_state, timing_updates) if timing_updates else success_state

            edit_updates, edit_applied = _run_gdsii_edit(
                _merge_state(
                    working_state,
                    {
                        "history": working_state["history"] + [
                            f"gdsii edit attempt {attempt}"
                        ]
                    },
                ),
                ssh,
                attempt=attempt,
                log_label=str(attempt),
            )
            working_state = _merge_state(working_state, edit_updates)

            if edit_applied:
                rerun_updates, rerun_ok, rerun_result = _run_gdsii_generation(
                    _merge_state(
                        working_state,
                        {
                            "history": working_state["history"] + [
                                f"gdsii rerun after edit attempt {attempt}"
                            ]
                        },
                    ),
                    ssh,
                    attempt=attempt,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rerun_updates)

                if not rerun_result.get("run_completed"):
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "gdsii": "failed",
                            },
                        },
                    )

                rereview_updates, rereview_passed = _run_gdsii_review(
                    working_state,
                    ssh,
                    attempt=attempt,
                    mappeddir=mapped_dir,
                    candidate_path=candidate_path,
                    generation_result=rerun_result,
                    log_label=f"{attempt}_post_edit",
                )
                working_state = _merge_state(working_state, rereview_updates)
                reroute = extract_review_routing(
                    working_state.get("validation", {}).get("gdsii_review", {})
                )

                if reroute["is_mapped_owned"]:
                    reroute_feedback = "\n".join(
                        [
                            *reroute["blocking_errors"],
                            *[
                                f"Suggested fix: {item}"
                                for item in working_state.get(
                                    "gdsii_review_suggested_fixes",
                                    [],
                                )
                            ],
                        ]
                    )
                    return _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "gdsii": "routed_to_mapped",
                            },
                            "history": working_state["history"] + [
                                "gdsii rereview routed the failure back to mapped_netlist"
                            ],
                            "last_error": (
                                working_state.get("gdsii_review_summary")
                                or "GDSII review routed the failure back to mapped_netlist."
                            ),
                            "inputs": {
                                **working_state.get("inputs", {}),
                                "mapped_feedback": reroute_feedback,
                            },
                        },
                    )

                if rerun_ok and rereview_passed:
                    success_state = _merge_state(
                        working_state,
                        {
                            "stage_status": {
                                **working_state["stage_status"],
                                "gdsii": "success",
                            },
                            "outputs": {
                                **working_state["outputs"],
                                "gdsii_remote_dir": f"{project_root}/{candidate_path}",
                            },
                            "last_error": None,
                        },
                    )
                    timing_updates = _run_remote_timing_closure_analysis(
                        success_state,
                        ssh,
                        label=f"{attempt}_post_edit",
                    )
                    return _merge_state(success_state, timing_updates) if timing_updates else success_state

            if attempt < gdsii_max_iterations:
                retry_feedback_lines = [
                    *working_state.get("gdsii_review_errors", []),
                    *[
                        f"Suggested fix: {item}"
                        for item in working_state.get(
                            "gdsii_review_suggested_fixes",
                            [],
                        )
                    ],
                    *[
                        f"Applied edit: {item.get('type')} {item.get('path')}"
                        for item in working_state.get("gdsii_edit_applied_actions", [])
                        if isinstance(item, dict)
                    ],
                ]
                retry_feedback = "\n".join(line for line in retry_feedback_lines if line)
                working_state = _merge_state(
                    working_state,
                    {
                        "inputs": {
                            **working_state.get("inputs", {}),
                            "gdsii_feedback": retry_feedback,
                        },
                    },
                )

        error_list = working_state.get("gdsii_review_errors", [])
        failure_message = (
            "GDSII review failed after "
            f"{gdsii_max_iterations} iterations: {'; '.join(error_list)}"
            if error_list
            else f"GDSII stage failed after {gdsii_max_iterations} iterations."
        )

        return _merge_state(
            working_state,
            {
                "stage_status": {
                    **working_state["stage_status"],
                    "gdsii": "failed",
                },
                "history": working_state["history"] + [
                    f"gdsii failed after {gdsii_max_iterations} iterations"
                ],
                "last_error": failure_message,
            },
        )

    return runner


def run_flow(initial_state: FlowState, ssh_config: dict, thread_id: str = "eda-flow-run"):
    _reset_live_history_prefix()
    with SSHExecutor(**ssh_config) as ssh:
        if initial_state.get("reserve_named_builds"):
            reserve_full_flow(ssh, initial_state)
        rtl_prep_runner = _make_rtl_prep_runner(ssh)
        netlist_runner = _make_netlist_runner(ssh)
        mapped_runner = _make_mapped_runner(ssh)
        gdsii_runner = _make_gdsii_runner(ssh)

        state = dict(initial_state)

        stages = [
            ("rtl_prep", rtl_prep_runner),
            ("netlist", netlist_runner),
            ("mapped_netlist", mapped_runner),
            ("gdsii", gdsii_runner),
        ]

        stage_index_by_name = {
            stage_name: index for index, (stage_name, _) in enumerate(stages)
        }
        requested_stage = str(state.get("current_stage", "")).strip()
        stop_after_stage = str(state.get("stop_after_stage", "")).strip()
        stage_index = stage_index_by_name.get(requested_stage, 0)

        while stage_index < len(stages):
            stage_name, runner = stages[stage_index]
            stage_max_retries = _stage_retry_limit(state, stage_name)
            state = _run_stage_with_retries(
                state,
                stage_name=stage_name,
                runner=runner,
                max_retries=stage_max_retries,
            )

            route_back_to_stage = state.get("route_back_to_stage")
            if route_back_to_stage and route_back_to_stage != stage_name:
                route_key = f"route:{stage_name}->{route_back_to_stage}"
                route_count = int(state.get("retry_counts", {}).get(route_key, 0)) + 1
                route_retry_limit = min(
                    _stage_retry_limit(state, stage_name),
                    _stage_retry_limit(state, route_back_to_stage),
                )
                if route_count > route_retry_limit:
                    return _merge_state(
                        state,
                        {
                            "current_stage": "failed",
                            "history": state["history"] + [
                                f"Routing loop {stage_name} -> {route_back_to_stage} exceeded {route_retry_limit} attempts",
                                "Flow stopped due to failure",
                            ],
                            "retry_counts": {
                                **state.get("retry_counts", {}),
                                route_key: route_count,
                            },
                            "last_error": (
                                state.get("last_error")
                                or f"Routing loop {stage_name} -> {route_back_to_stage} exceeded the retry limit."
                            ),
                        },
                    )

                state = _merge_state(
                    state,
                    {
                        "history": state["history"] + [
                            f"Routing from {stage_name} back to {route_back_to_stage}"
                        ],
                        "retry_counts": {
                            **state.get("retry_counts", {}),
                            route_key: route_count,
                        },
                        "route_back_to_stage": None,
                        "stage_status": {
                            **state.get("stage_status", {}),
                            route_back_to_stage: "pending",
                            "netlist": "pending" if route_back_to_stage == "rtl_prep" else state.get("stage_status", {}).get("netlist", "pending"),
                            "mapped_netlist": "pending",
                            "gdsii": "pending",
                        },
                    },
                )
                stage_index = stage_index_by_name[route_back_to_stage]
                continue

            if state["stage_status"].get(stage_name) != "success":
                state["current_stage"] = "failed"
                return state
            if stop_after_stage == stage_name:
                state = _merge_state(
                    state,
                    {
                        "current_stage": "done",
                        "history": state["history"]
                        + [f"Flow stopped after requested stage {stage_name}"],
                    },
                )
                return state
            stage_index += 1

        state = _merge_state(
            state,
            {
                "current_stage": "done",
                "history": state["history"] + ["Flow completed"],
            },
        )
        return state
