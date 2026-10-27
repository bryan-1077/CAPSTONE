"""Adapters around existing workflows; future stages deliberately stop the run."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from .contracts import FailureReport, RunConfig, Stage, StageResult
from .remote import RemoteConfig, remote_check_node
from .validation import validation_node, verify_backend_handoff
from .display import TimestampedOutput, print_node_header
from .auth import cached_password
from .gdsii_validation import gdsii_validation_node

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
    """Pass the selected snapshot and frequency into backend."""
    from .frequency import positive_mhz
    if config.target_mhz is None:
        raise ValueError("Choose a target frequency before starting backend.")
    target = positive_mhz(str(config.target_mhz))
    command = [config.backend_python or sys.executable, "-u", str(REPO_ROOT / "local/backend/app.py"),
               "--mailbox", str(mailbox.expanduser().resolve()), "--target-mhz", str(target)]
    if config.ask_password:
        command.append("--ask-password")
    return command


def stream_command(command: list[str], log: Path, *, cwd: Path | None = None,
                   pass_fds: tuple[int, ...] = (), env_overrides: dict[str, str] | None = None,
                   timestamp_output: bool = True) -> int:
    """Inherit terminal input and tee output, including prompts without newlines."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.update(env_overrides or {})
    timestamps = TimestampedOutput() if timestamp_output else None
    with log.open("w", encoding="utf-8") as handle:
        with subprocess.Popen(command, cwd=cwd or FRONTEND, env=env,
                              pass_fds=pass_fds,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace") as process:
            assert process.stdout is not None
            while chunk := process.stdout.read(1):
                if timestamps is not None:
                    chunk = timestamps.format(chunk)
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


def backend_node(config: RunConfig, run_dir: Path) -> StageResult:
    """Run backend on the verified snapshot or an explicitly unvalidated test."""
    from .mailbox import verify

    if config.validation_result is None and not config.allow_unvalidated:
        return StageResult("backend", "needs_attention",
            "Backend requires accepted validation evidence. Explicit backend testing requires --allow-unvalidated.")
    if config.mailbox is None:
        return StageResult("backend", "needs_attention", "Backend requires an explicit mailbox snapshot.")
    mailbox = config.mailbox.resolve()
    metadata = verify(mailbox)
    validation_record = None
    if config.validation_result is not None:
        try:
            validation_record = verify_backend_handoff(config.validation_result, mailbox)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return StageResult("backend", "needs_attention", f"Backend validation handoff stopped: {exc}")
    result_path = run_dir / "backend_result.json"
    if result_path.exists():
        raise FileExistsError(result_path)
    log = run_dir / "backend.log"
    reports = run_dir / "backend_reports"
    command = backend_command(config, mailbox) + ["--result-json", str(result_path), "--log-dir", str(reports)]
    artifacts = {"log": str(log), "reports": str(reports), "mailbox": str(mailbox),
                 "validation": "passed" if validation_record is not None else "not_run"}
    if validation_record is not None:
        artifacts["validation_result"] = str(config.validation_result)
    try:
        env_overrides = None
        if validation_record is not None:
            remote = RemoteConfig.load(config.remote_config)
            host, username = remote.host, remote.username
            env_overrides = {"CAPSTONE_SSH_HOST": host, "CAPSTONE_SSH_USER": username,
                             "CAPSTONE_SSH_PORT": str(remote.port), "CAPSTONE_SSH_KEY": remote.key_filename or ""}
        else:
            host = os.getenv("CAPSTONE_SSH_HOST") or "olympus.ece.tamu.edu"
            username = os.getenv("CAPSTONE_SSH_USER") or "antgamez1203"
        password = cached_password(host, username)
        if password is None:
            code = stream_command(command, log, cwd=REPO_ROOT / "local/backend",
                                  env_overrides=env_overrides, timestamp_output=False)
        else:
            # An anonymous inherited descriptor keeps the secret out of argv,
            # environment variables, and persistent run artifacts.
            with tempfile.TemporaryFile() as credential:
                credential.write(password.encode("utf-8"))
                credential.seek(0)
                code = stream_command(command + ["--ssh-password-fd", str(credential.fileno())],
                                      log, cwd=REPO_ROOT / "local/backend",
                                      pass_fds=(credential.fileno(),), env_overrides=env_overrides,
                                      timestamp_output=False)
    except OSError as exc:
        artifacts["failure_category"] = "tool_environment"
        return StageResult("backend", "failed", f"Could not start backend: {exc}", artifacts)
    try:
        payload = json.loads(result_path.read_text())
        if not isinstance(payload, dict):
            raise ValueError("Backend result must be a JSON object")
    except (OSError, ValueError) as exc:
        artifacts["failure_category"] = "missing_result"
        return StageResult("backend", "failed", f"Backend produced no usable result: {exc}", artifacts, code)
    artifacts["result"] = str(result_path)
    stages = payload.get("stage_status", {})
    same_input = payload.get("mailbox_revision") == metadata["rtl_revision"]
    same_target = payload.get("timing_target_clock_period_ns") == 1000 / config.target_mhz
    passed = (code == 0 and payload.get("current_stage") == "done" and same_input and same_target
              and isinstance(stages, dict)
              and all(stages.get(stage) == "success" for stage in ("rtl_prep", "netlist", "mapped_netlist", "gdsii"))
              and payload.get("timing_closure_status") in (None, "passed")
              and (not payload.get("timing_closure_enabled") or payload.get("timing_closure_status") == "passed")
              and (not payload.get("physical_verification_enabled") or payload.get("physical_verification_status") == "passed"))
    message = ("Backend completed on the verified snapshot." if validation_record is not None else
               "Backend completed on an unvalidated snapshot.") if passed else str(
        payload.get("last_error") or "Backend incomplete, failed, or returned mismatched input/target evidence.")
    if not passed:
        artifacts["failure_category"] = str(payload.get("rtl_failure_category") or "unknown")
    # Preserve native triage in result JSON. PD intake normalization is separate work.
    return StageResult("backend", "passed" if passed else "failed", message, artifacts, code)


def refresh_controller_manifest(result: StageResult, run_dir: Path) -> StageResult:
    if result.status == "passed" and (FRONTEND / "rtl_output/ddr4_controller_top.sv").is_file():
        log = run_dir / "manifest_refresh.log"
        code = stream_command([sys.executable, "-u", str(FRONTEND / "generate_wrapper.py"), "--manifest-only"], log)
        result.artifacts["manifest_refresh_log"] = str(log)
        if code:
            result.status = "failed"
            result.returncode = code
            result.message = "Controller manifest refresh failed; inspect manifest_refresh.log."
    return result


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
    if config.no_bist:
        result.artifacts["bist"] = "not_run"
        result.message = "Frontend RTL lint completed; BIST skipped."
        return refresh_controller_manifest(result, run_dir)
    print_node_header("bist")
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
    return refresh_controller_manifest(result, run_dir)


def debug_node(config: RunConfig, run_dir: Path) -> StageResult:
    return run_frontend("debug", config, run_dir)


NODES = {"generate": generate_node, "debug": debug_node,
         "validation": validation_node, "backend": backend_node,
         "gdsii-validation": gdsii_validation_node, "remote-check": remote_check_node}
