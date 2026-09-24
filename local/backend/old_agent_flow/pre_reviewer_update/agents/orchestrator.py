from __future__ import annotations

import shlex
from typing import Callable

from langgraph.graph import END, StateGraph

from agents.workers import (
    _build_prepare_command,
    _build_netlist_command,
    _build_mapped_command,
    _build_gdsii_command,
    _build_inner_ssh_command,
    DEFAULT_PROJECT_ROOT,
)
from schemas.state import FlowState
from services.ssh_executor import SSHExecutor


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
                        f"{stage_name}_feedback": state.get("last_error"),
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

def _merge_state(state: FlowState, updates: dict) -> FlowState:
    new_state = dict(state)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(new_state.get(key), dict):
            merged = dict(new_state[key])
            merged.update(value)
            new_state[key] = merged
        else:
            new_state[key] = value
    return new_state


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


def _make_rtl_prep_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        outdir = state.get("remote_prepare_dir", "build_prep_mc_lang")
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)

        result = _run_simple_stage(
            state,
            ssh,
            stage_key="rtl_prep",
            history_success="rtl_prep stage completed",
            history_fail_prefix="rtl_prep stage failed",
            error_prefix="RTL prep stage failed",
            command=_build_prepare_command(state),
            cwd=project_root,
            log_prefix="rtl_prep",
        )

        if result["stage_status"]["rtl_prep"] != "success":
            return result

        verify_command = (
            f"test -d {shlex.quote(outdir)} && "
            f"find {shlex.quote(outdir)} -type f | sort"
        )
        verify = ssh.run(verify_command, cwd=project_root)

        logs = {
            **result["logs"],
            "rtl_prep_verify_stdout": verify["stdout"],
            "rtl_prep_verify_stderr": verify["stderr"],
            "rtl_prep_verify_command": verify["command"],
        }

        if (not verify["ok"]) or (not verify["stdout"].strip()):
            return {
                "stage_status": {**state["stage_status"], "rtl_prep": "failed"},
                "logs": logs,
                "history": state["history"] + ["rtl_prep verification failed"],
                "last_error": (
                    f"RTL prep ran, but verification failed for {outdir}. "
                    f"STDERR: {verify['stderr']}"
                ),
            }

        return {
            **result,
            "logs": logs,
            "outputs": {
                **state["outputs"],
                "rtl_bundle_remote": f"{project_root}/{outdir}",
            },
        }

    return runner


def _make_netlist_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        outdir = state.get("remote_netlist_dir", "build_netlist_mc_lang")
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        results_dir = f"{outdir}/results"

        result = _run_simple_stage(
            state,
            ssh,
            stage_key="netlist",
            history_success="netlist stage completed",
            history_fail_prefix="netlist stage failed",
            error_prefix="Netlist stage failed",
            command=_build_netlist_command(state),
            cwd=project_root,
            log_prefix="netlist",
            timeout=3600,
        )

        if result["stage_status"]["netlist"] != "success":
            return result

        verify_inner = (
            "set -e; "
            f"cd {shlex.quote(project_root)}; "
            f"test -d {shlex.quote(outdir)}; "
            f"find {shlex.quote(results_dir)} -type f "
            "\\( -name '*_generic.v' -o -name '*.db' -o -name '*_generic.sdc' \\) "
            "| sort"
        )

        if state.get("inner_compute_host"):
            verify_command = _build_inner_ssh_command(state, verify_inner)
        else:
            verify_command = verify_inner

        verify = ssh.run(verify_command, cwd=project_root, timeout=600)

        logs = {
            **result["logs"],
            "netlist_verify_stdout": verify["stdout"],
            "netlist_verify_stderr": verify["stderr"],
            "netlist_verify_command": verify["command"],
        }

        if (not verify["ok"]) or (not verify["stdout"].strip()):
            return {
                "stage_status": {**state["stage_status"], "netlist": "failed"},
                "logs": logs,
                "history": state["history"] + ["netlist verification failed"],
                "last_error": (
                    f"Netlist stage ran, but verification failed for {outdir}. "
                    f"STDERR: {verify['stderr']}"
                ),
            }

        return {
            **result,
            "logs": logs,
            "outputs": {
                **state["outputs"],
                "netlist_remote_dir": f"{project_root}/{outdir}",
            },
        }

    return runner


def _make_mapped_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        outdir = state.get("remote_mapped_dir", "build_mapped_mc_lang")
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)
        results_dir = f"{outdir}/results"

        result = _run_simple_stage(
            state,
            ssh,
            stage_key="mapped_netlist",
            history_success="mapped_netlist stage completed",
            history_fail_prefix="mapped_netlist stage failed",
            error_prefix="Mapped stage failed",
            command=_build_mapped_command(state),
            cwd=project_root,
            log_prefix="mapped",
            timeout=3600,
        )

        if result["stage_status"]["mapped_netlist"] != "success":
            return result

        verify_inner = (
            "set -e; "
            f"cd {shlex.quote(project_root)}; "
            f"test -d {shlex.quote(outdir)}; "
            f"find {shlex.quote(results_dir)} -type f "
            "\\( -name '*_mapped.v' -o -name '*_mapped.sdc' -o -name '*_mapped.db' \\) "
            "| sort"
        )
        if state.get("inner_compute_host"):
            verify_command = _build_inner_ssh_command(state, verify_inner)
        else:
            verify_command = verify_inner
        verify = ssh.run(verify_command, cwd=project_root, timeout=600)

        logs = {
            **result["logs"],
            "mapped_verify_stdout": verify["stdout"],
            "mapped_verify_stderr": verify["stderr"],
            "mapped_verify_command": verify["command"],
        }

        if (not verify["ok"]) or (not verify["stdout"].strip()):
            return {
                "stage_status": {**state["stage_status"], "mapped_netlist": "failed"},
                "logs": logs,
                "history": state["history"] + ["mapped verification failed"],
                "last_error": (
                    f"Mapped stage ran, but verification failed for {outdir}. "
                    f"STDERR: {verify['stderr']}"
                ),
            }

        return {
            **result,
            "logs": logs,
            "outputs": {
                **state["outputs"],
                "mapped_remote_dir": f"{project_root}/{outdir}",
            },
        }

    return runner


def _make_gdsii_runner(ssh: SSHExecutor) -> Callable[[FlowState], dict]:
    def runner(state: FlowState) -> dict:
        outdir = state.get("remote_gdsii_dir", "build_GDSII_mc_lang")
        project_root = state.get("remote_project_root", DEFAULT_PROJECT_ROOT)

        result = _run_simple_stage(
            state,
            ssh,
            stage_key="gdsii",
            history_success="gdsii stage completed",
            history_fail_prefix="gdsii stage failed",
            error_prefix="GDSII stage failed",
            command=_build_gdsii_command(state),
            cwd=project_root,
            log_prefix="gdsii",
            timeout=7200,
        )

        if result["stage_status"]["gdsii"] != "success":
            return result

        verify_inner = (
            "set -e; "
            f"cd {shlex.quote(project_root)}; "
            f"test -d {shlex.quote(outdir)}; "
            f"find {shlex.quote(outdir)} -type f "
            "\\( -name '*.gds' -o -name '*.gdsii' \\) | sort"
        )
        if state.get("inner_compute_host"):
            verify_command = _build_inner_ssh_command(state, verify_inner)
        else:
            verify_command = verify_inner
        verify = ssh.run(verify_command, cwd=project_root, timeout=600)

        logs = {
            **result["logs"],
            "gdsii_verify_stdout": verify["stdout"],
            "gdsii_verify_stderr": verify["stderr"],
            "gdsii_verify_command": verify["command"],
        }

        if (not verify["ok"]) or (not verify["stdout"].strip()):
            return {
                "stage_status": {**state["stage_status"], "gdsii": "failed"},
                "logs": logs,
                "history": state["history"] + ["gdsii verification failed"],
                "last_error": (
                    f"GDSII stage ran, but verification failed for {outdir}. "
                    f"STDERR: {verify['stderr']}"
                ),
            }

        return {
            **result,
            "logs": logs,
            "outputs": {
                **state["outputs"],
                "gdsii_remote_dir": f"{project_root}/{outdir}",
            },
        }

    return runner


def run_flow(initial_state: FlowState, ssh_config: dict, thread_id: str = "eda-flow-run"):
    with SSHExecutor(**ssh_config) as ssh:
        rtl_prep_runner = _make_rtl_prep_runner(ssh)
        netlist_runner = _make_netlist_runner(ssh)
        mapped_runner = _make_mapped_runner(ssh)
        gdsii_runner = _make_gdsii_runner(ssh)

        state = dict(initial_state)
        max_retries = state.get("max_stage_retries", 3)

        stages = [
            ("rtl_prep", rtl_prep_runner),
            ("netlist", netlist_runner),
            ("mapped_netlist", mapped_runner),
            ("gdsii", gdsii_runner),
        ]

        for stage_name, runner in stages:
            state = _run_stage_with_retries(
                state,
                stage_name=stage_name,
                runner=runner,
                max_retries=max_retries,
            )

            if state["stage_status"].get(stage_name) != "success":
                state["current_stage"] = "failed"
                return state

        state = _merge_state(
            state,
            {
                "current_stage": "done",
                "history": state["history"] + ["Flow completed"],
            },
        )
        return state
