"""Post-backend GDSII inspection over the shared SSH/Slurm transport."""

import hashlib
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from shared.remote.ssh_executor import SSHExecutor
from .auth import cached_password, run_with_password_retries
from .contracts import StageResult
from .display import TimestampedOutput
from .mailbox import verify
from .remote import RemoteConfig, slurm_command
from .validation import unpack_reports


def remote_path(root, value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Missing remote backend artifact directory")
    path = PurePosixPath(value)
    return str(path if path.is_absolute() else PurePosixPath(root) / path)


def backend_request(config, remote):
    if config.backend_result is None or config.mailbox is None:
        raise ValueError("GDSII validation requires the successful backend result and its mailbox snapshot")
    data = config.backend_result.read_bytes()
    payload = json.loads(data)
    metadata = verify(config.mailbox)
    for field, expected in (("ssh_host", remote.host), ("ssh_user", remote.username), ("ssh_port", remote.port)):
        if field in payload and payload[field] != expected:
            raise ValueError("GDSII SSH settings do not match the backend server")
    stages = payload.get("stage_status", {})
    if (payload.get("current_stage") != "done" or
            payload.get("mailbox_revision") != metadata["rtl_revision"] or
            config.target_mhz is None or
            payload.get("timing_target_clock_period_ns") != 1000 / config.target_mhz or
            not isinstance(stages, dict) or
            any(stages.get(s) != "success" for s in ("rtl_prep", "netlist", "mapped_netlist", "gdsii")) or
            payload.get("timing_closure_status") not in (None, "passed") or
            (payload.get("timing_closure_enabled") and payload.get("timing_closure_status") != "passed") or
            (payload.get("physical_verification_enabled") and payload.get("physical_verification_status") != "passed")):
        raise ValueError("Backend evidence is incomplete or does not match this snapshot and target")
    root = payload.get("remote_project_root")
    if not isinstance(root, str) or not PurePosixPath(root).is_absolute():
        raise ValueError("Backend must record an absolute remote_project_root")
    outputs = payload.get("outputs") or {}
    gds_dir = remote_path(root, outputs.get("gdsii_remote_dir") or payload.get("gdsii_candidate_path"))
    mapped = outputs.get("mapped_remote_dir") or payload.get("mapped_candidate_path")
    top = payload.get("top_module")
    if not isinstance(top, str) or not top.strip():
        raise ValueError("Backend did not record its top module")
    manifest = json.loads((config.mailbox / "rtl/manifest.json").read_text())
    if top != manifest.get("top_module"):
        raise ValueError("Backend top module does not match the mailbox manifest")
    return {"backend_digest": hashlib.sha256(data).hexdigest(),
            "pipeline": remote_path(remote.project_dir, remote.gdsii_validation_script),
            "gds_dir": gds_dir, "mapped_dir": remote_path(root, mapped) if mapped else gds_dir,
            "top_module": top, "gds_top": remote.gdsii_top,
            "gds_file": remote.gdsii_file, "netlist": remote.gdsii_netlist,
            "libraries": [remote_path(remote.project_dir, p) for p in remote.gdsii_libraries],
            "report_dirs": [remote_path(gds_dir, p) for p in remote.gdsii_report_dirs]}


def assess(payload, code):
    if payload.get("error"):
        return "needs_attention", payload["error"]
    report = payload.get("report")
    if not isinstance(report, dict) or not payload.get("input_unchanged"):
        raise ValueError("Missing GDSII report or unchanged-input evidence")
    native = report.get("status")
    if native in ("FAIL", "ERROR"):
        return "failed", f"GDSII validation reported {native}; inspect the saved report."
    if code != 0:
        return "failed", f"GDSII validation execution failed (exit {code})."
    if native == "PASS_WITH_GAPS":
        return "passed", "GDSII validation passed with gaps; limitations are recorded in the saved report."
    if native == "PASS" and (
            report.get("needs_review") or report.get("allow_signoff") is not True):
        return "needs_attention", "GDSII structural inspection completed with gaps; review is required."
    if native == "PASS" and report.get("structural_checks_passed") is True:
        return "passed", "GDSII validation passed."
    raise ValueError(f"Unsupported or inconsistent GDSII validation status: {native!r}")


def gdsii_validation_node(config, run_dir):
    log_path = run_dir / "gdsii_validation.log"
    result_path = run_dir / "gdsii_validation_result.json"
    artifacts = {"log": str(log_path), "result": str(result_path)}
    record = {"status": "running"}
    status, message, code = "needs_attention", "GDSII validation did not finish.", None
    ssh = None
    launched = finished = False
    with log_path.open("w", encoding="utf-8") as log:
        timestamps = {name: TimestampedOutput() for name in ("stdout", "stderr")}
        def output(name, text):
            text = timestamps["stderr" if name == "stderr" else "stdout"].format(text)
            log.write(text)
            log.flush()
            stream = sys.stderr if name == "stderr" else sys.stdout
            stream.write(text)
            stream.flush()
        try:
            remote = RemoteConfig.load(config.remote_config)
            if not remote.allow_validation_output_dirs:
                raise ValueError("Remote validation output directories are disabled")
            request = backend_request(config, remote)
            token = uuid4().hex
            request["invocation_id"] = token
            job_name = "capstone-gdsii-" + token
            from .allocation import workspace
            invocation = workspace(remote) / 'gdsii_validation_runs' / ('run_' + token)
            record.update(host=remote.host, username=remote.username, remote_directory=str(invocation),
                          backend_result=str(config.backend_result), request=request)
            request_path = run_dir / "gdsii_validation_request.json"
            request_path.write_text(json.dumps(request, indent=2) + "\n")
            artifacts["request"] = str(request_path)
            ssh = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
            ssh.password = cached_password(remote.host, remote.username)
            preparation = ("mkdir -p -- " + shlex.quote(str(invocation.parent)) +
                           " && mkdir -- " + shlex.quote(str(invocation)))
            prepared = run_with_password_retries(ssh, preparation,
                ask_password=config.ask_password or ssh.password is not None, timeout=60, on_output=output)
            if not prepared["ok"]:
                raise RuntimeError(prepared.get("stderr") or "Could not prepare remote GDSII workspace")
            ssh.upload_file(request_path, str(invocation / "request.json"), exclusive=True)
            ssh.upload_file(Path(__file__).with_name("gdsii_validation_runner.py"),
                            str(invocation / "runner.py"), exclusive=True)
            command = slurm_command(replace(remote, job_seconds=remote.gdsii_validation_job_seconds),
                [remote.python, "-B", "-u", str(invocation / "runner.py"), str(invocation)], job_name)
            record["command"] = command
            launched = True
            execution = ssh.run(command, timeout=remote.gdsii_validation_timeout_seconds, on_output=output)
            record["execution"] = execution
            code = execution["exit_code"]
            finished = code >= 0
            if not finished:
                raise RuntimeError(execution.get("stderr") or "SSH execution failed")
            for name, local in (("result.json", "remote_gdsii_validation_result.json"), ("reports.zip", "reports.zip")):
                fetched = ssh.fetch_file(str(invocation / name), run_dir / local)
                if not fetched["ok"]:
                    raise RuntimeError(f"Could not retrieve {name}: {fetched.get('error')}")
                artifacts["remote_result" if name == "result.json" else "archive"] = str(run_dir / local)
            payload = json.loads((run_dir / "remote_gdsii_validation_result.json").read_text())
            record["remote_result"] = payload
            if (payload.get("invocation_id") != token or
                    payload.get("backend_digest") != request["backend_digest"]):
                raise ValueError("GDSII result does not match this invocation and backend result")
            archive = run_dir / "reports.zip"
            if hashlib.sha256(archive.read_bytes()).hexdigest() != payload.get("reports_digest"):
                raise ValueError("GDSII report archive digest mismatch")
            unpack_reports(archive, run_dir / "reports")
            artifacts["reports"] = str(run_dir / "reports")
            if isinstance(payload.get("report"), dict):
                saved = run_dir / "reports/verification_reports/gdsii_validation_report.json"
                if json.loads(saved.read_text()) != payload["report"]:
                    raise ValueError("Archived GDSII report does not match the invocation result")
                for field in ("gds_file", "netlist_file"):
                    if payload["report"].get(field) != payload.get(field):
                        raise ValueError("GDSII report refers to different input artifacts")
            status, message = assess(payload, code)
            if status == "passed":
                artifacts["validation_status"] = payload["report"]["status"]
        except (Exception, KeyboardInterrupt) as exc:
            message = f"GDSII validation stopped: {type(exc).__name__}: {exc}"
            record["error"] = message
        finally:
            if ssh is not None:
                if launched and not finished:
                    try:
                        from .allocation import cancellation_command
                        record["cancellation"] = ssh.run(shlex.join(cancellation_command(remote, job_name)), timeout=15)
                    except Exception as exc:
                        record["cancellation_error"] = f"{type(exc).__name__}: {exc}"
                ssh.close()
                ssh.password = None
    record.update(status=status, message=message, returncode=code)
    result_path.write_text(json.dumps(record, indent=2) + "\n")
    return StageResult("gdsii-validation", status, message, artifacts, code)
