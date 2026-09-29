"""Adapters around existing workflows; future stages deliberately stop the run."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from .contracts import FailureReport, RunConfig, Stage, StageResult

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "local" / "frontend"


def frontend_command(stage: Stage, config: RunConfig) -> list[str]:
    if stage == "generate":
        if config.input_yaml is None:
            raise ValueError("Select specs before starting generation.")
        else:
            command = [sys.executable, "-u", str(FRONTEND / "run_flow.py"), str(config.input_yaml)]
        if config.cache:
            command.append("--cache")
        return command
    if stage == "debug":
        if config.failure is not None:
            command = [sys.executable, "-u", str(FRONTEND / "debug_agent.py"),
                       "auto", config.failure.source, "--log", str(config.failure.log),
                       "--repair", "--max-attempts", str(config.max_attempts)]
            if config.failure.target_command:
                command.extend(["--target-command", config.failure.target_command])
            if config.offline:
                command.append("--offline")
            return command
        if config.failure_dir is None:
            raise ValueError("Debug requires an existing failure workspace.")
        command = [sys.executable, "-u", str(FRONTEND / "debug_agent.py"), "graph",
                   "--failure-dir", str(config.failure_dir),
                   "--max-attempts", str(config.max_attempts)]
        if config.repair:
            command.append("--repair")
        if config.offline:
            command.append("--offline")
        return command
    raise ValueError(f"No frontend command for {stage}")


def spec_command(output: Path) -> list[str]:
    return [sys.executable, "-u", str(FRONTEND / "configure_from_text.py"),
            "--output", str(output)]


def backend_command(config: RunConfig, mailbox: Path) -> list[str]:
    """Command for the future validated backend handoff; never resolve defaults late."""
    from .frequency import positive_mhz
    if config.target_mhz is None:
        raise ValueError("Choose a target frequency before starting backend.")
    target = positive_mhz(str(config.target_mhz))
    return [sys.executable, "-u", str(REPO_ROOT / "local/backend/app.py"),
            "--mailbox", str(mailbox.expanduser().resolve()), "--target-mhz", str(target)]


def stream_command(command: list[str], log: Path) -> int:
    """Inherit terminal input and tee output, including prompts without newlines."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with log.open("w", encoding="utf-8") as handle:
        with subprocess.Popen(command, cwd=FRONTEND, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace") as process:
            assert process.stdout is not None
            while chunk := process.stdout.read(1):
                sys.stdout.write(chunk)
                sys.stdout.flush()
                handle.write(chunk)
                handle.flush()
            return process.wait()


def run_frontend(stage: Stage, config: RunConfig, run_dir: Path) -> StageResult:
    log = run_dir / f"{stage}.log"
    artifacts = {"log": str(log)}
    command = frontend_command(stage, config)
    workspace_name = None
    if stage == "debug" and config.failure is not None:
        workspace_name = "orchestrator_" + uuid4().hex
        command.extend(["--name", workspace_name])
    try:
        returncode = stream_command(command, log)
    except OSError as exc:
        return StageResult(stage, "failed", str(exc), artifacts)
    if returncode and stage != "debug":
        return StageResult(stage, "failed", "Frontend command failed; inspect the log.",
                           artifacts, returncode)
    if stage == "generate":
        artifacts["rtl_dir"] = str(FRONTEND / "rtl_output")
        artifacts["manifest"] = str(FRONTEND / "rtl_output" / "manifest.json")
        return StageResult(stage, "passed", "Generation completed; validation is still required.",
                           artifacts, 0)

    failure_dir = config.failure_dir
    if workspace_name:
        matches = list((FRONTEND / "debug" / "failures").glob(f"*_{workspace_name}"))
        failure_dir = matches[0] if len(matches) == 1 else None
    record = {}
    if failure_dir is not None:
        status_path = failure_dir / "result" / "status.json"
        artifacts["failure_dir"] = str(failure_dir)
        artifacts["debug_status"] = str(status_path)
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record = loaded
                snapshot = run_dir / "debug_status.json"
                snapshot.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                artifacts["debug_status_snapshot"] = str(snapshot)
        except (OSError, ValueError):
            pass
    status = record.get("status", "unknown")
    accepted = {"fixed", "patch_applied", "patch_applied_lint_passed",
                "patch_applied_target_passed"}
    if not returncode and (config.repair or config.failure) and status in accepted:
        return StageResult(stage, "passed", "Repair completed; rerunning frontend checks.", artifacts, 0)
    reason = record.get("reason") or record.get("error")
    message = f"Debug stopped with status: {status}."
    if reason:
        message += f" {reason}"
    questions = record.get("unresolved_questions")
    if isinstance(questions, list):
        message += " " + " ".join(str(question) for question in questions)
    outcome = "needs_attention" if status == "needs_human" or not returncode else "failed"
    return StageResult(stage, outcome, message, artifacts, returncode)


def lint_report_version():
    """Identify the current report so an old diagnostic cannot be reused."""
    try:
        stat = (FRONTEND / "flow_lint.log").stat()
        return stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size
    except FileNotFoundError:
        return None


def attach_lint_report(result: StageResult, run_dir: Path, command: list[str], previous) -> None:
    current = lint_report_version()
    if current is None or current == previous:
        result.message += " No fresh lint diagnostic report available; debug handoff stopped."
        return
    snapshot = run_dir / "flow_lint.log"
    shutil.copyfile(FRONTEND / "flow_lint.log", snapshot)
    result.artifacts["lint_report"] = str(snapshot)
    result.failure = FailureReport("lint", snapshot, shlex.join(command))


def validation_node(config: RunConfig, run_dir: Path) -> StageResult:
    """TODO: consume a versioned design bundle and emit checks plus failure evidence."""
    return StageResult("validation", "not_implemented", "Validation adapter is not connected.")


def backend_node(config: RunConfig, run_dir: Path) -> StageResult:
    """TODO: consume the validated revision and constraints; emit reports and outputs."""
    return StageResult("backend", "not_implemented", "Backend adapter is not connected.")


def generate_node(config: RunConfig, run_dir: Path) -> StageResult:
    lint_command = [sys.executable, "-u", "-c",
                    "from run_flow import run_full_system_lint; run_full_system_lint()"]
    previous_report = lint_report_version()
    if config.recheck_only:
        log = run_dir / "system_lint.log"
        code = stream_command(lint_command, log)
        result = StageResult("generate", "passed" if code == 0 else "failed",
                             "System lint recheck", {"log": str(log)}, code)
        if code:
            attach_lint_report(result, run_dir, lint_command, previous_report)
    else:
        result = run_frontend("generate", config, run_dir)
        log = run_dir / "generate.log"
        evidence = log.read_text(errors="replace") if log.exists() else ""
        if result.status == "failed" and "[FLOW] Full-system RTL lint FAILED" in evidence:
            attach_lint_report(result, run_dir, lint_command, previous_report)
    if result.status != "passed":
        return result
    print("[ORCHESTRATOR] Generation finished; running BIST...", flush=True)
    log = run_dir / "bist.log"
    result.artifacts["bist_log"] = str(log)
    try:
        code = stream_command(["bash", str(FRONTEND / "run_sim.sh"), "--no-wave-prompt"], log)
    except OSError as exc:
        result.status = "failed"
        result.message = f"Could not run BIST: {exc}"
        result.returncode = None
        return result
    result.returncode = code
    # The current testbench prints TEST FAIL and calls $finish, which may exit 0.
    evidence = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    if code or "TEST FAIL" in evidence or "TEST PASS" not in evidence:
        result.status = "failed"
        result.message = "Generation completed but BIST failed or lacked a pass marker; inspect bist.log."
        result.failure = FailureReport("bist", log,
            shlex.join(["bash", str(FRONTEND / "run_sim.sh"), "--no-wave-prompt"]))
    else:
        result.message = "Generation and BIST completed; validation is still required."
    return result


def debug_node(config: RunConfig, run_dir: Path) -> StageResult:
    return run_frontend("debug", config, run_dir)


NODES = {"generate": generate_node, "debug": debug_node,
         "validation": validation_node, "backend": backend_node}
