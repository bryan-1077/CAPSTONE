"""Upload a published revision, run the existing validator, and collect evidence."""

import getpass
import hashlib
import json
import shlex
import shutil
import sys
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from shared.remote.ssh_executor import SSHExecutor
from .contracts import StageResult
from .mailbox import verify
from .remote import RemoteConfig, slurm_command


REQUIRED_STAGES = {"agent1", "agent2", "agent3"}
GAPS = ("failed_requirements", "untested_requirements", "failed_tests", "untested_tests", "coverage_limitations")


def expected_designs(snapshot: Path) -> dict[str, str]:
    import yaml
    designs = {}
    for path in sorted((snapshot / "specs").rglob("*")):
        if path.suffix.lower() in {".yaml", ".yml"}:
            name = yaml.safe_load(path.read_text()).get("design_name")
            if not isinstance(name, str) or not name or name in designs:
                raise ValueError("Every published YAML needs a unique design_name")
            designs[name] = path.relative_to(snapshot).as_posix()
    if not designs:
        raise ValueError("Mailbox contains no design specs")
    return designs


def check_complete(summary: dict) -> None:
    if not isinstance(summary, dict):
        raise ValueError("Missing structured verification summary")
    if summary.get("overall_status") != "PASS" or summary.get("verification_complete") is not True:
        raise ValueError(f"Verification is {summary.get('overall_status', 'missing')}; complete PASS evidence is required")
    if summary.get("execution_completed") is not True or summary.get("execution_succeeded") is not True:
        raise ValueError("Verification execution did not complete successfully")
    if summary.get("needs_review") is not False or any(summary.get(field) for field in GAPS):
        raise ValueError("Verification has failures, coverage gaps, or review requirements")


def assess(summary: dict, expected: dict[str, str], remote_input: str) -> None:
    check_complete(summary)
    if summary.get("input_directory") != remote_input:
        raise ValueError("Summary belongs to a different input directory")
    if set(summary.get("inputs", [])) != {remote_input + "/rtl", remote_input + "/specs"}:
        raise ValueError("Summary does not cover the requested RTL/spec directories")
    if summary.get("kind") == "batch":
        designs = summary.get("designs", [])
        if len(designs) != len(expected) or {d.get("module_name") for d in designs} != set(expected):
            raise ValueError("Batch summary is missing or duplicates requested designs")
        children = []
        for design in designs:
            if design.get("yaml_file") != remote_input + "/" + expected[design["module_name"]]:
                raise ValueError("Batch design refers to an unexpected YAML")
            children.append(design.get("result"))
    else:
        if len(expected) != 1:
            raise ValueError("Expected a batch summary covering every design")
        children = [summary]
    for child in children:
        check_complete(child)
        requested = child.get("requested_stages", [])
        if not REQUIRED_STAGES.issubset(requested):
            raise ValueError("Required structural/behavioral stages are missing")
        stages = child.get("stages", {})
        for name in requested:
            stage = stages.get(name, {})
            if stage.get("status") != "PASS" or stage.get("exit_code") != 0:
                raise ValueError(f"{name} did not pass")
            if any(stage.get(field) for field in GAPS):
                raise ValueError(f"{name} has verification gaps")


def unpack_reports(archive: Path, destination: Path) -> None:
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "verification_reports":
                raise ValueError("Invalid report archive path")
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Report archive contains a symlink")
            target = destination.joinpath(*path.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)


def validation_node(config, run_dir: Path) -> StageResult:
    artifacts = {}
    if config.mailbox is None:
        return StageResult("validation", "needs_attention", "Validation requires --mailbox or a successful frontend snapshot.")
    ssh = None
    launched = False
    finished = False
    record = {"status": "running", "mailbox": str(config.mailbox), "phase": "configuration"}
    status = "needs_attention"
    message = "Validation did not finish."
    code = None
    log_path = run_dir / "validation.log"
    result_path = run_dir / "validation_result.json"
    artifacts.update(log=str(log_path), result=str(result_path), mailbox=str(config.mailbox))
    with log_path.open("w", encoding="utf-8") as log:
        def output(name, text):
            log.write(text)
            log.flush()
            (sys.stderr if name == "stderr" else sys.stdout).write(text)
            (sys.stderr if name == "stderr" else sys.stdout).flush()

        try:
            remote = RemoteConfig.load(config.remote_config)
            if not remote.allow_validation_output_dirs:
                raise ValueError(
                    "Validation is disabled because the deployed pipeline creates report and scratch directories. "
                    "No remote changes were made. Set allow_validation_output_dirs=true only if those "
                    "validator-created directories are allowed; the orchestrator never creates remote directories."
                )
            record["phase"] = "snapshot"
            verify(config.mailbox)
            snapshot = run_dir / "validation_input"
            shutil.copytree(config.mailbox, snapshot, symlinks=True)
            metadata = verify(snapshot)
            expected = expected_designs(snapshot)
            snapshot_digest = hashlib.sha256((snapshot / "snapshot.json").read_bytes()).hexdigest()
            token = uuid4().hex
            job_name = "capstone-verif-" + token
            invocation = PurePosixPath(remote.validation_workspace)
            if not invocation.is_absolute():
                invocation = PurePosixPath(remote.project_dir) / invocation
            pipeline = PurePosixPath(remote.validation_script)
            if not pipeline.is_absolute():
                pipeline = PurePosixPath(remote.project_dir) / pipeline
            request = {"invocation_id": token, "snapshot_digest": snapshot_digest,
                       "pipeline": str(pipeline), "jobs": remote.validation_jobs}
            request_path = run_dir / "validation_request.json"
            request_path.write_text(json.dumps(request, indent=2) + "\n")
            record.update(request, remote_directory=str(invocation), expected_designs=expected,
                          modules_without_specs=metadata.get("modules_without_specs", []))
            ssh = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
            record["phase"] = "transfer"
            if config.ask_password:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    ssh.password = getpass.getpass(f"SSH password for {remote.username}@{remote.host}: ")
            def checked(command):
                result = ssh.run(command, timeout=60, on_output=output)
                if not result["ok"]:
                    raise RuntimeError(result.get("stderr") or f"Remote preparation failed ({result['exit_code']})")
                return result
            # Resolve on the server, where symlinks and physical paths are known.
            # The runner uses Path.resolve(); all transfers and evidence checks
            # must refer to that same workspace identity.
            marker = "CAPSTONE_WORKSPACE_" + token + ":"
            resolved = checked(f"cd -P -- {shlex.quote(str(invocation))} && "
                               f"printf '%s%s\\n' {shlex.quote(marker)} \"$PWD\"")
            paths = [line[len(marker):] for line in resolved["stdout"].splitlines()
                     if line.startswith(marker)]
            if len(paths) != 1 or not PurePosixPath(paths[0]).is_absolute():
                raise ValueError("Could not determine the canonical remote workspace path")
            record["configured_remote_directory"] = str(invocation)
            invocation = PurePosixPath(paths[0])
            record["remote_directory"] = str(invocation)
            directories = {str(invocation), str(invocation / "input"), str(invocation / "work")}
            files = sorted(p for p in snapshot.rglob("*") if p.is_file())
            for path in files:
                directories.add(str(invocation / "input" / path.relative_to(snapshot).parent.as_posix()))
            for directory in sorted(directories):
                quoted = shlex.quote(directory)
                message = shlex.quote(f"Required remote directory must already exist and be writable: {directory}")
                checked(f"test -d {quoted} && test -w {quoted} && test -x {quoted} || "
                        f"{{ echo {message} >&2; exit 1; }}")
            # A workspace is single-use: never mix a new snapshot with old evidence.
            for directory in (invocation / "input", invocation / "work"):
                checked(f"test -z \"$(find {shlex.quote(str(directory))} -mindepth 1 ! -type d -print -quit)\" || "
                        "{ echo 'Remote input/work directories must contain no files' >&2; exit 1; }")
            for name in ("runner.py", "request.json", "result.json", "reports.zip", "input/verification_reports"):
                quoted = shlex.quote(str(invocation / name))
                checked(f"test ! -e {quoted} && test ! -L {quoted} || "
                        "{ echo 'Remote workspace already used; select a fresh prepared workspace' >&2; exit 1; }")
            for path in files:
                ssh.upload_file(path, str(invocation / "input" / path.relative_to(snapshot).as_posix()), exclusive=True)
            ssh.upload_file(request_path, str(invocation / "request.json"), exclusive=True)
            ssh.upload_file(Path(__file__).with_name("validation_runner.py"), str(invocation / "runner.py"), exclusive=True)
            job_config = replace(remote, job_seconds=remote.validation_job_seconds)
            command = slurm_command(job_config, [remote.python, "-u", str(invocation / "runner.py"), str(invocation)], job_name)
            record["command"] = command
            record["phase"] = "execution"
            launched = True
            execution = ssh.run(command, timeout=remote.validation_timeout_seconds, on_output=output)
            record["execution"] = execution
            code = execution["exit_code"]
            finished = code >= 0
            record["phase"] = "report_collection"
            for remote_name, local_name in (("result.json", "remote_validation_result.json"), ("reports.zip", "reports.zip")):
                fetched = ssh.fetch_file(str(invocation / remote_name), run_dir / local_name)
                if not fetched["ok"]:
                    raise RuntimeError(f"Could not retrieve {remote_name}: {fetched.get('error')}")
            payload = json.loads((run_dir / "remote_validation_result.json").read_text())
            record["remote_result"] = payload
            if payload.get("invocation_id") != token or payload.get("snapshot_digest") != snapshot_digest:
                raise ValueError("Remote result does not match this invocation and snapshot")
            archive = run_dir / "reports.zip"
            if hashlib.sha256(archive.read_bytes()).hexdigest() != payload.get("reports_digest"):
                raise ValueError("Downloaded report archive digest mismatch")
            reports = run_dir / "reports"
            unpack_reports(archive, reports)
            artifacts["reports"] = str(reports)
            summary = payload.get("summary")
            record["verification_scope"] = "Published module YAML specs; wrappers without specs are not individually verified."
            if payload.get("error") or payload.get("input_unchanged") is not True:
                raise ValueError(payload.get("error") or "Input identity was not verified after validation")
            relative = PurePosixPath(payload.get("summary_file", ""))
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "verification_reports":
                raise ValueError("Invalid summary path")
            saved_summary = reports.joinpath(*relative.parts)
            if json.loads(saved_summary.read_text()) != summary:
                raise ValueError("Retrieved summary differs from remote result")
            artifacts["summary"] = str(saved_summary)
            record["phase"] = "verification_evidence"
            if isinstance(summary, dict) and summary.get("overall_status") == "FAIL":
                status = "failed"
            assess(summary, expected, str(invocation / "input"))
            if code != 0 or payload.get("pipeline_returncode") != 0:
                raise ValueError("Validation process did not exit successfully")
            verify(snapshot)
            status = "passed"
            record["phase"] = "complete"
            message = f"Verification passed for {len(expected)} published YAML designs. Backend was not run."
            missing = metadata.get("modules_without_specs", [])
            if missing:
                message += " No individual YAML verification for: " + ", ".join(missing) + "."
        except KeyboardInterrupt:
            message = "Validation interrupted."
        except Exception as exc:
            message = f"Validation stopped: {type(exc).__name__}: {exc}"
        finally:
            if ssh is not None:
                if launched and not finished:
                    record["cancellation"] = ssh.run(shlex.join(["scancel", "--name", job_name]), timeout=15)
                ssh.close()
                ssh.password = None
            record.update(status=status, message=message)
            result_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            log.write("\n" + message + "\n")
    return StageResult("validation", status, message, artifacts, code)
