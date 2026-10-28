"""Upload a published revision, run the existing validator, and collect evidence."""

import getpass
import hashlib
import json
import time
import shlex
import shutil
import sys
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from shared.remote.ssh_executor import SSHExecutor
from .auth import cached_password, run_with_password_retries
from .contracts import FailureReport, StageResult
from .display import TimestampedOutput
from .mailbox import verify, verify_frontend_revision
from .remote import RemoteConfig, slurm_command


REQUIRED_STAGES = {"agent1", "agent2", "agent3"}
GAPS = ("failed_requirements", "untested_requirements", "failed_tests", "untested_tests", "coverage_limitations")


def fetch_result_file(ssh, remote_name, local_name):
    """Bounded retrieval retry for cross-node shared-filesystem visibility.

    Never rerun the job or retry authentication/permission failures here.
    """
    for attempt in range(3):
        fetched = ssh.fetch_file(remote_name, local_name)
        if fetched.get('ok'):
            return fetched
        error = str(fetched.get('error', ''))
        if attempt == 2 or not ('No such file' in error or '[Errno 2]' in error):
            return fetched
        time.sleep((0.25, 0.75)[attempt])


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


def assess(summary: dict, expected: dict[str, str], remote_input: str, mode: str = "modules",
           top_module: str | None = None) -> None:
    if not isinstance(summary, dict):
        raise ValueError("Missing structured verification summary")
    if mode != "system":
        check_complete(summary)
    expected_directory = remote_input + "/rtl" if mode == "system" else remote_input
    if summary.get("input_directory") != expected_directory:
        raise ValueError("Summary belongs to a different input directory")
    expected_inputs = ({remote_input + "/rtl/manifest.json"} if mode == "system" else
                       {remote_input + "/rtl", remote_input + "/specs"})
    if set(summary.get("inputs", [])) != expected_inputs:
        raise ValueError("Summary does not cover the requested RTL/spec directories")
    if mode == "system":
        if summary.get("mode") != "system":
            raise ValueError("Expected an explicit system verification summary")
        if top_module is not None and summary.get("top_module") != top_module:
            raise ValueError("System summary identifies a different controller top")
        if summary.get("manifest_path") != remote_input + "/rtl/manifest.json":
            raise ValueError("System summary belongs to a different controller manifest")
        check_readiness(summary)
        return
    elif summary.get("kind") == "batch":
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


def check_readiness(report: dict) -> None:
    """Use the validator's Readiness decision instead of reclassifying its evidence."""
    readiness = report.get("verification_readiness")
    if readiness != "READY_FOR_BACKEND":
        raise ValueError(f"System verification Readiness is {readiness or 'missing'}; READY_FOR_BACKEND is required")


def verify_backend_handoff(result_path: Path, mailbox: Path) -> dict:
    """Bind the accepted verification decision to the snapshot sent to backend."""
    metadata = verify(mailbox)
    if metadata.get("frontend_flow") in ("specification", "specification-module"):
        raise ValueError("Experimental backend readiness requires the future system verification adapter")
    record = json.loads(result_path.read_text())
    if record.get("status") != "passed" or record.get("phase") != "complete":
        raise ValueError("Validation did not accept this snapshot for backend")
    if Path(record.get("mailbox", "")).resolve() != mailbox.resolve():
        raise ValueError("Validation belongs to a different mailbox")
    digest = hashlib.sha256((mailbox / "snapshot.json").read_bytes()).hexdigest()
    payload = record.get("remote_result", {})
    if record.get("snapshot_digest") != digest or payload.get("snapshot_digest") != digest:
        raise ValueError("Validation belongs to a different snapshot revision")
    if payload.get("input_unchanged") is not True or payload.get("pipeline_returncode") != 0:
        raise ValueError("Validation execution or input identity was not verified")
    mode = record.get("validation_mode")
    if mode not in ("system", "modules"):
        raise ValueError("Validation mode is missing or unsupported")
    top = json.loads((mailbox / "rtl/manifest.json").read_text())["top_module"]
    assess(payload.get("summary"), expected_designs(mailbox), record["remote_directory"] + "/input", mode, top)
    if mode == "system":
        final = record.get("system_result", {})
        if final.get("mode") != "system" or final.get("top_module") != top:
            raise ValueError("Final system report identifies a different controller")
        check_readiness(final)
    return record


def unpack_reports(archive: Path, destination: Path, report_relative: str = "verification_reports") -> None:
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            prefix = PurePosixPath(report_relative).parts
            if path.is_absolute() or ".." in path.parts or path.parts[:len(prefix)] != prefix:
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
    if config.scope_plan and not (config.module_probe or config.module_probe_parent):
        return StageResult('validation', 'needs_attention', 'Scoped transport requires an explicit Experimental module probe.')
    if config.module_probe:
        from .specification_mailbox import prepare_module
        try:
            pair = prepare_module(config.mailbox, config.module_probe, run_dir / 'module_input')
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return StageResult('validation', 'needs_attention', f'Module preparation stopped: {exc}')
        return validation_node(replace(config, mailbox=pair, module_probe=None,
                                       module_probe_parent=config.mailbox), run_dir)
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
        timestamps = {name: TimestampedOutput() for name in ("stdout", "stderr")}
        def output(name, text):
            text = timestamps["stderr" if name == "stderr" else "stdout"].format(text)
            log.write(text)
            log.flush()
            (sys.stderr if name == "stderr" else sys.stdout).write(text)
            (sys.stderr if name == "stderr" else sys.stdout).flush()

        try:
            module_probe = config.module_probe_parent is not None
            if module_probe:
                from .specification_mailbox import verify_module_probe
                parent = verify_module_probe(config.mailbox, config.module_probe_parent)
                record.update(scope='one_module_only', backend_ready=False,
                              system_verification='not_run',
                              parent_mailbox=str(config.module_probe_parent),
                              parent_snapshot_digest=parent['source_snapshot_digest'],
                              parent_rtl_revision=parent['parent_rtl_revision'], module_name=parent['module_name'],
                              preflight_only=config.preflight_only)
            elif json.loads((config.mailbox / "snapshot.json").read_text()).get("frontend_flow") in ("specification", "specification-module"):
                raise ValueError("Experimental verification requires its module/system consumer adapter; legacy validation is disabled.")
            remote = RemoteConfig.load(config.remote_config)
            if module_probe:
                remote = replace(remote, validation_mode='modules')
            record.update(host=remote.host, username=remote.username, python=remote.python)
            if not remote.allow_validation_output_dirs:
                raise ValueError(
                    "Validation is disabled because the deployed pipeline creates report and scratch directories. "
                    "No remote changes were made. Set allow_validation_output_dirs=true only if those "
                    "validator-created directories are allowed."
                )
            record["phase"] = "snapshot"
            verify(config.mailbox)
            snapshot = run_dir / "validation_input"
            shutil.copytree(config.mailbox, snapshot, symlinks=True)
            metadata = verify(snapshot)
            scoped_plan = None
            if config.scope_plan:
                from .verification_scope import validate_plan, execution_request
                plan_bytes = config.scope_plan.read_bytes()
                scoped_plan = json.loads(plan_bytes)
                validate_plan(config.module_probe_parent, scoped_plan)
                if scoped_plan['identity']['module_name'] != metadata['module_name']:
                    raise ValueError('Scope plan differs from selected module')
                plan_path = run_dir / 'execution_plan.json'
                plan_path.write_bytes(plan_bytes)
                scope_request = execution_request(scoped_plan, plan_path, snapshot)
                scope_request_path = run_dir / 'scope_request.json'
                scope_request_path.write_text(json.dumps(scope_request, indent=2) + '\n')
                artifacts.update(scope_plan=str(plan_path), scope_request=str(scope_request_path))
                record.update(scope_protocol=scoped_plan['consumer_capability'], plan_sha256=scoped_plan['plan_sha256'])
            expected = expected_designs(snapshot)
            top_module = metadata['module_name'] if module_probe else json.loads((snapshot / "rtl/manifest.json").read_text())["top_module"]
            snapshot_digest = hashlib.sha256((snapshot / "snapshot.json").read_bytes()).hexdigest()
            token = uuid4().hex
            job_name = "capstone-verif-" + token
            invocation = PurePosixPath(remote.validation_workspace or f"shared/validation_runs/run_{token}")
            if not invocation.is_absolute():
                invocation = PurePosixPath(remote.project_dir) / invocation
            pipeline = PurePosixPath(remote.validation_script)
            if not pipeline.is_absolute():
                pipeline = PurePosixPath(remote.project_dir) / pipeline
            request = {"invocation_id": token, "snapshot_digest": snapshot_digest,
                       "pipeline": str(pipeline), "jobs": remote.validation_jobs,
                       "validation_mode": remote.validation_mode}
            if module_probe:
                request.update(specification_module_probe=True, preflight_only=config.preflight_only,
                               module_name=metadata['module_name'], parent_rtl_revision=metadata['parent_rtl_revision'],
                               parent_snapshot_digest=metadata['source_snapshot_digest'],
                               module_probe_digest=hashlib.sha256(Path(__file__).with_name('module_probe.py').read_bytes()).hexdigest())
                if scoped_plan:
                    request.update(scope_protocol=scoped_plan['consumer_capability'],
                                   plan_sha256=scoped_plan['plan_sha256'],
                                   plan_file_digest=hashlib.sha256(plan_bytes).hexdigest(),
                                   scope_request_digest=hashlib.sha256(scope_request_path.read_bytes()).hexdigest(),
                                   scope_probe_digest=hashlib.sha256(Path(__file__).with_name('scope_probe.py').read_bytes()).hexdigest())
            request_path = run_dir / "validation_request.json"
            request_path.write_text(json.dumps(request, indent=2) + "\n")
            record.update(request, remote_directory=str(invocation), expected_designs=expected,
                          modules_without_specs=metadata.get("modules_without_specs", []))
            ssh = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
            record["phase"] = "transfer"
            ssh.password = cached_password(remote.host, remote.username)
            interactive_password = config.ask_password or ssh.password is not None
            if config.ask_password and ssh.password is None:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    ssh.password = getpass.getpass(f"SSH password for {remote.username}@{remote.host}: ")
            initial_login = True
            def checked(command):
                nonlocal initial_login
                if initial_login:
                    result = run_with_password_retries(ssh, command, ask_password=interactive_password,
                                                       timeout=60, on_output=output)
                    initial_login = False
                else:
                    result = ssh.run(command, timeout=60, on_output=output)
                if not result["ok"]:
                    raise RuntimeError(result.get("stderr") or f"Remote preparation failed ({result['exit_code']})")
                return result
            # Create a unique default workspace exclusively. Explicit paths retain
            # support for prepared workspaces, but missing paths are created too.
            record["phase"] = "workspace_setup"
            quoted = shlex.quote(str(invocation))
            checked("mkdir -p -- " + shlex.quote(str(invocation.parent)))
            if remote.validation_workspace is None:
                checked(f"mkdir -- {quoted}")
            else:
                checked(f"if test ! -e {quoted} && test ! -L {quoted}; then mkdir -- {quoted}; fi")
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
            directories = {str(invocation), str(invocation / "input/rtl"),
                           str(invocation / "input/specs"), str(invocation / "work")}
            files = sorted(p for p in snapshot.rglob("*") if p.is_file())
            for path in files:
                directories.add(str(invocation / "input" / path.relative_to(snapshot).parent.as_posix()))
            for directory in sorted(directories):
                quoted = shlex.quote(directory)
                checked(f"mkdir -p -- {quoted}")
                message = shlex.quote(f"Required remote directory must be writable: {directory}")
                checked(f"test -d {quoted} && test -w {quoted} && test -x {quoted} || "
                        f"{{ echo {message} >&2; exit 1; }}")
            # A workspace is single-use: never mix a new snapshot with old evidence.
            for directory in (invocation / "input", invocation / "work"):
                checked(f"test -z \"$(find {shlex.quote(str(directory))} -mindepth 1 ! -type d -print -quit)\" || "
                        "{ echo 'Remote input/work directories must contain no files' >&2; exit 1; }")
            for name in ("runner.py", "request.json", "result.json", "reports.zip", "input/verification_reports", "module_probe.py", "consumer_preflight.json",
                         'scope_probe.py', 'scope_request.json', 'execution_plan.json'):
                quoted = shlex.quote(str(invocation / name))
                checked(f"test ! -e {quoted} && test ! -L {quoted} || "
                        "{ echo 'Remote workspace already used; select a fresh workspace' >&2; exit 1; }")
            for path in files:
                ssh.upload_file(path, str(invocation / "input" / path.relative_to(snapshot).as_posix()), exclusive=True)
            ssh.upload_file(request_path, str(invocation / "request.json"), exclusive=True)
            ssh.upload_file(Path(__file__).with_name("validation_runner.py"), str(invocation / "runner.py"), exclusive=True)
            if module_probe:
                ssh.upload_file(Path(__file__).with_name('module_probe.py'), str(invocation / 'module_probe.py'), exclusive=True)
                if scoped_plan:
                    for local, remote_name in ((plan_path, 'execution_plan.json'), (scope_request_path, 'scope_request.json'),
                                               (Path(__file__).with_name('scope_probe.py'), 'scope_probe.py')):
                        ssh.upload_file(local, str(invocation / remote_name), exclusive=True)
            job_config = replace(remote, job_seconds=remote.job_seconds if config.preflight_only else remote.validation_job_seconds)
            command = slurm_command(job_config, [remote.python, "-u", str(invocation / "runner.py"), str(invocation)], job_name)
            record["command"] = command
            record["phase"] = "execution"
            launched = True
            execution = ssh.run(command, timeout=remote.timeout_seconds if config.preflight_only else remote.validation_timeout_seconds, on_output=output)
            record["execution"] = execution
            code = execution["exit_code"]
            finished = code >= 0
            record["phase"] = "report_collection"
            for remote_name, local_name in (("result.json", "remote_validation_result.json"), ("reports.zip", "reports.zip")):
                fetched = fetch_result_file(ssh, str(invocation / remote_name), run_dir / local_name)
                if not fetched["ok"]:
                    detail = (execution.get("stderr") or "").strip()[-8000:]
                    raise RuntimeError(
                        f"Could not retrieve {remote_name}: {fetched.get('error')}"
                        + (f"\nRemote execution stderr:\n{detail}" if detail else "")
                    )
            payload = json.loads((run_dir / "remote_validation_result.json").read_text())
            record["remote_result"] = payload
            if payload.get("invocation_id") != token or payload.get("snapshot_digest") != snapshot_digest:
                raise ValueError("Remote result does not match this invocation and snapshot")
            archive = run_dir / "reports.zip"
            if hashlib.sha256(archive.read_bytes()).hexdigest() != payload.get("reports_digest"):
                raise ValueError("Downloaded report archive digest mismatch")
            reports = run_dir / "reports"
            report_relative = "rtl/verification_reports" if remote.validation_mode == "system" else "verification_reports"
            unpack_reports(archive, reports, report_relative)
            artifacts["reports"] = str(reports)
            summary = payload.get("summary")
            record["verification_scope"] = (
                "Controller system verification of the published RTL tree; module YAMLs are retained as snapshot evidence."
                if remote.validation_mode == "system" else
                "Published module YAML specs; wrappers without specs are not individually verified.")
            if payload.get("error") or payload.get("input_unchanged") is not True:
                raise ValueError(payload.get("error") or "Input identity was not verified after validation")
            if module_probe:
                from .specification_mailbox import verify_module_probe
                verify_module_probe(config.mailbox, config.module_probe_parent)
                if (payload.get('scope') != 'one_module_only' or payload.get('backend_ready') is not False
                        or payload.get('module_name') != metadata['module_name']
                        or payload.get('parent_rtl_revision') != metadata['parent_rtl_revision']
                        or payload.get('parent_snapshot_digest') != metadata['source_snapshot_digest']
                        or payload.get('preflight_only') is not config.preflight_only):
                    raise ValueError('Remote module receipt differs from the selected parent revision/scope')
                checked = payload.get('consumer_preflight', {})
                if checked.get('status') != 'passed' or checked.get('module_name') != metadata['module_name']:
                    raise ValueError('Missing deployed consumer capability evidence')
                if scoped_plan:
                    from .verification_scope import validate_plan
                    validate_plan(config.module_probe_parent, scoped_plan)
                    if (payload.get('scope_protocol') != scoped_plan['consumer_capability']
                            or payload.get('plan_sha256') != scoped_plan['plan_sha256']
                            or checked.get('consumer_capability') != scoped_plan['consumer_capability']
                            or checked.get('plan_sha256') != scoped_plan['plan_sha256']
                            or payload.get('plan_file_digest') != request['plan_file_digest']
                            or payload.get('scope_request_digest') != request['scope_request_digest']):
                        raise ValueError('Scoped consumer receipt differs from reviewed plan/request')
                    if (hashlib.sha256(plan_path.read_bytes()).hexdigest() != request['plan_file_digest']
                            or hashlib.sha256(scope_request_path.read_bytes()).hexdigest() != request['scope_request_digest']):
                        raise ValueError('Local scoped plan/request changed during invocation')
                else:
                    import yaml
                    from .module_probe import expected_source_catalog
                    spec_path = next((snapshot / 'specs').glob('*.yaml'))
                    if checked.get('source_catalog') != expected_source_catalog(yaml.safe_load(spec_path.read_text())):
                        raise ValueError('Consumer preflight source catalog differs from selected module')
                if config.preflight_only:
                    if code != 0 or payload.get('pipeline_returncode') is not None or payload.get('summary') is not None:
                        raise ValueError('Preflight-only receipt unexpectedly contains agent execution')
                    record.update(phase='consumer_preflight_complete', verification_scope='consumer_preflight_only')
                    status = 'passed'
                    message = 'Deployed module consumer preflight passed; no verification agents ran and backend readiness is false.'
                    # Continue through the normal finally/receipt path without
                    # interpreting this capability check as a verification pass.
                    return StageResult('validation', status, message, artifacts, code)
                if scoped_plan:
                    from .verification_scope import accept_results
                    body_path = reports / report_relative / 'module_execution_results.json'
                    if (payload.get('scoped_result_file') != report_relative + '/module_execution_results.json'
                            or hashlib.sha256(body_path.read_bytes()).hexdigest() != payload.get('scoped_result_digest')):
                        raise ValueError('Scoped result path/hash differs from downloaded archive')
                    body = json.loads(body_path.read_text())
                    if body != payload.get('scoped_result'):
                        raise ValueError('Scoped result body differs from its invocation receipt')
                    acceptance = accept_results(config.module_probe_parent, scoped_plan, body, reports / report_relative)
                    record['scoped_acceptance'] = acceptance
                    artifacts['scoped_results'] = str(body_path)
                else:
                    from .module_probe import verify_source_coverage
                    checked_coverage = verify_source_coverage(reports / report_relative, checked['source_catalog'])
                    if checked_coverage != payload.get('source_coverage'):
                        raise ValueError('Retrieved module source coverage differs from its remote receipt')
            relative = PurePosixPath(payload.get("summary_file", ""))
            prefix = PurePosixPath(report_relative).parts
            if relative.is_absolute() or ".." in relative.parts or relative.parts[:len(prefix)] != prefix:
                raise ValueError("Invalid summary path")
            saved_summary = reports.joinpath(*relative.parts)
            if json.loads(saved_summary.read_text()) != summary:
                raise ValueError("Retrieved summary differs from remote result")
            artifacts["summary"] = str(saved_summary)
            record["phase"] = "verification_evidence"
            if remote.validation_mode == "system":
                remote_rtl = str(invocation / "input/rtl")
                if (not isinstance(summary, dict) or summary.get("mode") != "system"
                        or summary.get("top_module") != top_module
                        or summary.get("input_directory") != remote_rtl
                        or summary.get("inputs") != [remote_rtl + "/manifest.json"]
                        or summary.get("manifest_path") != remote_rtl + "/manifest.json"):
                    raise ValueError("System summary does not identify the requested controller snapshot")
                final_name = PurePosixPath(summary.get("final_system_report", "")) if isinstance(summary, dict) else PurePosixPath("")
                if final_name.is_absolute() or len(final_name.parts) != 1 or final_name.name in (".", ".."):
                    raise ValueError("Missing or invalid final system report path")
                final_path = saved_summary.parent / final_name.name
                final = json.loads(final_path.read_text())
                artifacts["final_system_report"] = str(final_path)
                record["system_result"] = final
                if final.get("mode") != "system" or final.get("top_module") != top_module:
                    raise ValueError("Final system report identifies a different controller")
                if final.get("status") == "FAIL":
                    status = "failed"
                check_readiness(final)
            if isinstance(summary, dict) and summary.get("overall_status") == "FAIL":
                status = "failed"
            assess(summary, expected, str(invocation / "input"), remote.validation_mode, top_module)
            if code != 0 or payload.get("pipeline_returncode") != 0:
                raise ValueError("Validation process did not exit successfully")
            verify(snapshot)
            status = "passed"
            record["phase"] = "complete"
            message = ("Controller system verification reports Readiness: READY_FOR_BACKEND for the published RTL snapshot. Backend was not run."
                       if remote.validation_mode == "system" else
                       f"Verification passed for {len(expected)} published YAML designs. Backend was not run.")
            if module_probe:
                record.update(verification_scope='one_module_only', backend_ready=False)
                message = 'One-module verification compatibility passed for ' + metadata['module_name'] + '; system verification and backend readiness remain pending.'
                if scoped_plan:
                    record['verification_scope'] = 'module_checks_only'
                    record['scoped_acceptance']['remote_invocation_authentication'] = 'checked_by_validation_adapter'
                    message = 'Scoped module checks passed for ' + metadata['module_name'] + '; deferred system coverage and backend readiness remain pending.'
            missing = metadata.get("modules_without_specs", [])
            if missing and remote.validation_mode == "modules":
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
    result = StageResult("validation", status, message, artifacts, code)
    # Only authenticated system behavioral failures can enter automatic repair.
    # Debug edits the live frontend, so it must still be the verified revision.
    if status == "failed" and record.get("phase") == "verification_evidence":
        summary_path = artifacts.get("summary")
        final = record.get("system_result", {})
        if summary_path and final.get("status") == "FAIL":
            try:
                summary_path = Path(summary_path)
                summary = json.loads(summary_path.read_text())
                stages = summary.get("stages", {})
                if (stages.get("agent1", {}).get("status") == "PASS"
                        and any(stages.get(name, {}).get("status") == "FAIL"
                                for name in ("agent2", "agent3"))):
                    frontend = Path(__file__).resolve().parents[2] / "local/frontend"
                    verify_frontend_revision(frontend, config.mailbox)
                    result.failure = FailureReport("verif", summary_path.parent)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                result.message += f" Automatic debug handoff stopped: {exc}."
    return result
