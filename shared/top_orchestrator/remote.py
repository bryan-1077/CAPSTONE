"""Configurable SSH -> Slurm preflight, independent of validation verdicts."""

import json
import getpass
import os
import shlex
import sys
import warnings
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from uuid import uuid4

from shared.remote.ssh_executor import SSHExecutor
from .auth import run_with_password_retries
from .contracts import StageResult


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    username: str
    project_dir: str
    partition: str
    qos: str
    port: int = 22
    key_filename: str | None = None
    python: str = "python3.11"
    # load-ecen-454 is an allocation alias; srun is already added by slurm_command.
    setup_commands: tuple[str, ...] = ("source ~/.bashrc",)
    required_tools: tuple[str, ...] = ()
    cpus: int = 1
    allocation_wait_seconds: int = 30
    job_seconds: int = 120
    timeout_seconds: int = 210
    validation_script: str = "server/validation/run_pipeline.py"
    validation_job_seconds: int = 14400
    validation_timeout_seconds: int = 14490
    validation_jobs: int = 1
    validation_workspace: str | None = None
    allow_validation_output_dirs: bool = True

    @classmethod
    def load(cls, path: Path | None = None):
        data = json.loads(path.expanduser().read_text()) if path is not None else {}
        if not isinstance(data, dict):
            raise ValueError("Remote configuration must be a JSON object")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown remote settings: {', '.join(sorted(unknown))}")
        environment = {
            "host": "CAPSTONE_SSH_HOST", "username": "CAPSTONE_SSH_USER",
            "port": "CAPSTONE_SSH_PORT", "key_filename": "CAPSTONE_SSH_KEY",
            "project_dir": "CAPSTONE_REMOTE_PROJECT_DIR", "python": "CAPSTONE_REMOTE_PYTHON",
            "partition": "CAPSTONE_SLURM_PARTITION", "qos": "CAPSTONE_SLURM_QOS",
            "cpus": "CAPSTONE_SLURM_CPUS", "allocation_wait_seconds": "CAPSTONE_SLURM_WAIT_SECONDS",
            "job_seconds": "CAPSTONE_SLURM_JOB_SECONDS", "timeout_seconds": "CAPSTONE_REMOTE_TIMEOUT_SECONDS",
            "validation_script": "CAPSTONE_VALIDATION_SCRIPT",
            "validation_job_seconds": "CAPSTONE_VALIDATION_JOB_SECONDS",
            "validation_timeout_seconds": "CAPSTONE_VALIDATION_TIMEOUT_SECONDS",
            "validation_jobs": "CAPSTONE_VALIDATION_JOBS",
            "validation_workspace": "CAPSTONE_VALIDATION_WORKSPACE",
        }
        integer_fields = {"port", "cpus", "allocation_wait_seconds", "job_seconds", "timeout_seconds",
                          "validation_job_seconds", "validation_timeout_seconds", "validation_jobs"}
        for name, variable in environment.items():
            value = os.environ.get(variable)
            if name not in data and value:
                try:
                    data[name] = int(value) if name in integer_fields else value
                except ValueError:
                    raise ValueError(f"{variable} must be an integer") from None
        if "setup_commands" not in data and os.environ.get("CAPSTONE_REMOTE_SETUP"):
            data["setup_commands"] = [os.environ["CAPSTONE_REMOTE_SETUP"]]
        for name in ("host", "username", "project_dir", "partition", "qos"):
            if name not in data:
                raise ValueError(f"Set {environment[name]} in the local environment or {name} in --remote-config")
        for name in ("setup_commands", "required_tools"):
            if name in data:
                if not isinstance(data[name], list) or any(not isinstance(x, str) or not x.strip() for x in data[name]):
                    raise ValueError(f"{name} must be a list of nonempty strings")
                data[name] = tuple(data[name])
        config = cls(**data)
        if not isinstance(config.allow_validation_output_dirs, bool):
            raise ValueError("allow_validation_output_dirs must be a boolean")
        for name in ("host", "username", "project_dir", "partition", "qos", "python", "validation_script"):
            value = getattr(config, name)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{name} must be a nonempty string")
        if config.validation_workspace is not None and (
                not isinstance(config.validation_workspace, str) or
                not config.validation_workspace.strip() or "\x00" in config.validation_workspace):
            raise ValueError("validation_workspace must be a nonempty string or null")
        if not PurePosixPath(config.project_dir).is_absolute():
            raise ValueError("project_dir must be an absolute path on the SSH server")
        for name in integer_fields:
            value = getattr(config, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if config.port > 65535:
            raise ValueError("port must be at most 65535")
        if config.timeout_seconds < config.allocation_wait_seconds + config.job_seconds + 30:
            raise ValueError("timeout_seconds must allow allocation wait + job time + 30 seconds")
        if config.validation_timeout_seconds < config.allocation_wait_seconds + config.validation_job_seconds + 30:
            raise ValueError("validation_timeout_seconds must allow allocation wait + validation job time + 30 seconds")
        if config.key_filename is not None and not isinstance(config.key_filename, str):
            raise ValueError("key_filename must be a local path string")
        return config


def slurm_command(config: RemoteConfig, argv: list[str], job_name: str) -> str:
    """Setup is trusted configuration; paths and command arguments are shell quoted."""
    hours, remainder = divmod(config.job_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    script = "\n".join([
        "set -e",
        "shopt -s expand_aliases",
        *config.setup_commands,
        "cd -- " + shlex.quote(config.project_dir),
        *("command -v -- " + shlex.quote(tool) + " >/dev/null" for tool in config.required_tools),
        "exec " + shlex.join(argv),
    ])
    return shlex.join([
        "srun", f"--job-name={job_name}", f"--cpus-per-task={config.cpus}",
        f"--partition={config.partition}", f"--qos={config.qos}",
        f"--immediate={config.allocation_wait_seconds}",
        f"--time={hours:02d}:{minutes:02d}:{seconds:02d}", "/bin/bash", "-lc", script,
    ])


def probe_command(config: RemoteConfig, token: str) -> str:
    script = ("import json, os, socket, sys; "
              "assert os.environ.get('SLURM_JOB_ID'), 'Not running inside a Slurm job'; "
              f"print({token!r} + json.dumps(dict(host=socket.gethostname(), cwd=os.getcwd(), "
              "python=sys.executable, job_id=os.environ['SLURM_JOB_ID'])))")
    return slurm_command(config, [config.python, "-c", script], "capstone-check-" + token)


def remote_check_node(config, run_dir: Path) -> StageResult:
    remote = RemoteConfig.load(config.remote_config)
    token = uuid4().hex
    job_name = "capstone-check-" + token
    command = probe_command(remote, token)
    transcript = run_dir / "remote.log"
    report = run_dir / "remote_result.json"
    artifacts = {"log": str(transcript), "result": str(report)}
    results = {"job_name": job_name, "host": remote.host, "project_dir": remote.project_dir,
               "validation": "not_run"}
    success = False
    interrupted = False
    with transcript.open("w", encoding="utf-8") as log:
        def output(name, text):
            log.write(text)
            log.flush()
            stream = sys.stderr if name == "stderr" else sys.stdout
            stream.write(text)
            stream.flush()

        ssh = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
        launched = False
        try:
            if config.ask_password:
                # Never fall back to an echoed prompt or persist the credential in RunConfig.
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    ssh.password = getpass.getpass(f"SSH password for {remote.username}@{remote.host}: ")
            # run() includes connection failures in its structured result.
            results["login"] = run_with_password_retries(ssh, "command -v srun && pwd",
                                       ask_password=config.ask_password, cwd=remote.project_dir,
                                       timeout=30, on_output=output)
            if results["login"]["ok"]:
                launched = True
                results["compute"] = ssh.run(command, timeout=remote.timeout_seconds, on_output=output)
                compute = results["compute"]
                evidence = [line[len(token):] for line in compute["stdout"].splitlines() if line.startswith(token)]
                if compute["ok"] and len(evidence) == 1:
                    results["probe"] = json.loads(evidence[0])
                    success = bool(results["probe"].get("job_id"))
        except KeyboardInterrupt:
            interrupted = True
            results["error"] = "Interrupted"
        except Exception as exc:
            results["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            # A dropped SSH channel does not establish that a Slurm job stopped.
            if launched and not success:
                results["cancellation"] = ssh.run(shlex.join(["scancel", "--name", job_name]), timeout=15)
            ssh.close()
            ssh.password = None
    results["status"] = "passed" if success else "failed"
    report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    message = ("SSH and Slurm environment preflight passed; validation was not run." if success else
               "Remote preflight interrupted; see remote_result.json." if interrupted else
               "SSH/Slurm preflight failed; see remote_result.json and remote.log.")
    if not success and not interrupted:
        failed = results.get("compute", results.get("login", {}))
        detail = results.get("error") or failed.get("stderr", "").strip()
        if detail:
            message = f"Remote preflight failed: {detail[-1000:]}"
        if failed.get("error_type") == "AuthenticationException":
            message += (" Check CAPSTONE_SSH_USER and CAPSTONE_SSH_KEY or your SSH agent."
                        + (" The supplied password was not accepted for this connection." if config.ask_password else
                           " If your account needs a password, rerun with --ask-password."))
        with transcript.open("a", encoding="utf-8") as log:
            log.write("\n" + message + "\n")
    return StageResult("remote-check", "passed" if success else "failed", message, artifacts)
