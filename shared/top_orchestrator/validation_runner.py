"""Standalone remote wrapper. Uploaded by the adapter; uses only the standard library.

Runs the deployed validation scripts unchanged, with per-invocation inputs/work.
"""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from collections import deque
from pathlib import Path


PYTHON_DIAGNOSTICS = """
import importlib, json, os, pwd, site, socket, sys
sys.path[0] = sys.argv[1]
result = dict(username=pwd.getpwuid(os.getuid()).pw_name, hostname=socket.gethostname(),
              executable=sys.executable, version=sys.version, cwd=os.getcwd(),
              sys_path=sys.path, user_site=site.getusersitepackages(),
              user_site_enabled=site.ENABLE_USER_SITE,
              environment={key: os.environ.get(key) for key in
                           ('PYTHONHOME', 'PYTHONPATH', 'PYTHONNOUSERSITE', 'PYTHONUSERBASE')},
              dependencies={})
for name in ('yaml', 'requests', 'dotenv'):
    try:
        module = importlib.import_module(name)
        if name == 'dotenv':
            from dotenv import load_dotenv
        result['dependencies'][name] = dict(available=True, file=module.__file__)
    except Exception as exc:
        result['dependencies'][name] = dict(available=False, error=type(exc).__name__ + ': ' + str(exc))
print(json.dumps(result))
"""


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_inputs(root, expected_digest, report_relative="verification_reports"):
    manifest = root / "snapshot.json"
    if digest(manifest) != expected_digest:
        raise ValueError("Snapshot manifest digest mismatch")
    metadata = json.loads(manifest.read_text())
    actual = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        # Generated report workspaces may contain simulator symlinks. They are
        # not published inputs; still reject a symlink at the report root itself.
        report_parts = Path(report_relative).parts
        if relative.parts[:len(report_parts)] == report_parts and len(relative.parts) > len(report_parts):
            continue
        if path.is_symlink():
            raise ValueError("Symlink in validation inputs")
        if path.is_file() and path != manifest:
            actual[relative.as_posix()] = digest(path)
    if actual != metadata["files"]:
        raise ValueError("Validation input hashes do not match the published snapshot")
    return metadata


def execute(invocation):
    invocation = Path(invocation).resolve()
    request = json.loads((invocation / "request.json").read_text())
    inputs = invocation / "input"
    mode = request.get("validation_mode", "modules")
    report_relative = "rtl/verification_reports" if mode == "system" else "verification_reports"
    reports = inputs / report_relative
    result = {"schema_version": 1, "invocation_id": request["invocation_id"],
              "snapshot_digest": request["snapshot_digest"], "input_directory": str(inputs),
              "validation_mode": mode,
              "python_executable": sys.executable,
              "pipeline_returncode": None, "summary": None, "input_unchanged": False}
    module_probe = request.get('specification_module_probe') is True
    preflight_only = request.get('preflight_only') is True
    scoped = request.get('scope_protocol')
    try:
        if scoped and (not module_probe or scoped != 'capstone.module_execution_plan.v1'):
            raise ValueError('Unsupported scoped transport protocol')
        if mode not in ("system", "modules"):
            raise ValueError("Unsupported validation mode")
        metadata = check_inputs(inputs, request["snapshot_digest"], report_relative)
        if module_probe:
            if (mode != 'modules' or metadata.get('frontend_flow') != 'specification-module'
                    or metadata.get('parent_rtl_revision') != request.get('parent_rtl_revision')
                    or metadata.get('source_snapshot_digest') != request.get('parent_snapshot_digest')
                    or metadata.get('module_name') != request.get('module_name')):
                raise ValueError('Module invocation identity mismatch')
            result.update(scope='one_module_only', backend_ready=False, module_name=metadata['module_name'],
                          system_verification='not_run',
                          parent_rtl_revision=metadata['parent_rtl_revision'],
                          parent_snapshot_digest=metadata['source_snapshot_digest'], preflight_only=preflight_only)
        elif preflight_only:
            raise ValueError('Preflight-only requires a specification module probe')
        if reports.exists():
            raise ValueError("Reports already exist before this invocation")
        pipeline = Path(request["pipeline"])
        if not pipeline.is_file():
            raise FileNotFoundError(f"Validation entry point does not exist: {pipeline}")
        result["pipeline_digest"] = digest(pipeline)
        work = invocation / "work"
        if not work.is_dir():
            raise FileNotFoundError(f"Work directory must be prepared before running: {work}")
        command = [sys.executable, "-u", str(pipeline), str(inputs / "rtl")]
        if mode == "system":
            command.append("--system")
        else:
            command.extend([str(inputs / "specs"), "--jobs", str(request["jobs"])])
        result["command"] = command
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        # Do not inherit another verification invocation's report destinations.
        environment.pop("RTL_VERIFICATION_REPORT_RUN", None)
        environment.pop("RTL_VALIDATION_RESULT_FILE", None)
        # Probe with the pipeline's script directory and working directory,
        # rather than the uploaded wrapper's import path. Never print .env values.
        probe = subprocess.run([sys.executable, "-c", PYTHON_DIAGNOSTICS, str(pipeline.parent)],
                               cwd=work, env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True)
        try:
            result["python_environment"] = json.loads(probe.stdout)
            print("[PYTHON] " + json.dumps(result["python_environment"]), flush=True)
        except ValueError:
            result["python_environment"] = {"diagnostic_error": "Python environment probe failed",
                                            "returncode": probe.returncode}
        if module_probe:
            helper = invocation / ('scope_probe.py' if scoped else 'module_probe.py')
            if digest(helper) != request.get('scope_probe_digest' if scoped else 'module_probe_digest'):
                raise ValueError('Module preflight helper digest mismatch')
            preflight_path = invocation / 'consumer_preflight.json'
            probe_command = [sys.executable, str(helper), str(pipeline), str(inputs)]
            if scoped:
                if (digest(invocation / 'execution_plan.json') != request.get('plan_file_digest')
                        or digest(invocation / 'scope_request.json') != request.get('scope_request_digest')):
                    raise ValueError('Scoped plan/request upload digest mismatch')
                result.update(scope_protocol=scoped, plan_sha256=request['plan_sha256'],
                              plan_file_digest=request['plan_file_digest'], scope_request_digest=request['scope_request_digest'])
                probe_command.extend([str(invocation / 'execution_plan.json'), str(invocation / 'scope_request.json')])
            probe_command.append(str(preflight_path))
            checked = subprocess.run(probe_command,
                                     cwd=work, env=environment, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, universal_newlines=True, timeout=45)
            result['consumer_preflight'] = json.loads(preflight_path.read_text()) if preflight_path.is_file() else {}
            if checked.returncode or result['consumer_preflight'].get('status') != 'passed':
                raise ValueError(result['consumer_preflight'].get('error', 'Consumer preflight failed'))
            if result['consumer_preflight'].get('pipeline_digest') != result['pipeline_digest']:
                raise ValueError('Consumer preflight pipeline mismatch')
            if scoped:
                if (result['consumer_preflight'].get('consumer_capability') != scoped
                        or result['consumer_preflight'].get('plan_sha256') != request['plan_sha256']):
                    raise ValueError('Scoped capability/plan mismatch')
                if (digest(helper) != request['scope_probe_digest']
                        or digest(invocation / 'execution_plan.json') != request['plan_file_digest']
                        or digest(invocation / 'scope_request.json') != request['scope_request_digest']):
                    raise ValueError('Scoped helper/plan/request changed during discovery')
                command.extend(['--execution-request', str(invocation / 'scope_request.json'),
                                '--execution-results', str(reports / 'module_execution_results.json')])
            if preflight_only:
                check_inputs(inputs, request['snapshot_digest'], report_relative)
                result['input_unchanged'] = True
                return 0
        # Preserve startup tracebacks in the result while still streaming them live.
        stderr_tail = deque(maxlen=40)
        with subprocess.Popen(command, cwd=work, env=environment, stderr=subprocess.PIPE,
                              universal_newlines=True, errors="replace") as process:
            for line in process.stderr:
                stderr_tail.append(line[-8000:])
                print(line, end="", file=sys.stderr, flush=True)
            result["pipeline_returncode"] = process.wait()
        result["pipeline_stderr_tail"] = "".join(stderr_tail)[-8000:]
        if module_probe:
            if scoped:
                from scope_probe import consumer_hashes
                declared = result['consumer_preflight']['consumer_files']
                if (consumer_hashes(pipeline, list(declared)) != declared
                        or digest(helper) != request['scope_probe_digest']
                        or digest(invocation / 'execution_plan.json') != request['plan_file_digest']
                        or digest(invocation / 'scope_request.json') != request['scope_request_digest']):
                    raise ValueError('Scoped consumer/plan/request changed during invocation')
            elif (digest(pipeline) != result['pipeline_digest'] or
                    digest(pipeline.parent / 'requirement_verification.py') != result['consumer_preflight']['consumer_digest']):
                raise ValueError('Deployed pipeline/requirement reader changed during invocation')
        check_inputs(inputs, request["snapshot_digest"], report_relative)
        result["input_unchanged"] = True
        # A fresh input directory has exactly one top-level run; never use global latest.
        summaries = list(reports.glob("*/summary.json"))
        if len(summaries) != 1:
            if result["pipeline_returncode"] != 0:
                raise RuntimeError(
                    f"Validation pipeline exited with code {result['pipeline_returncode']} "
                    f"using {sys.executable}; expected one invocation summary, found {len(summaries)}. "
                    + result["pipeline_stderr_tail"].strip()
                )
            raise ValueError(f"Expected one invocation summary, found {len(summaries)}")
        result["summary"] = json.loads(summaries[0].read_text())
        result["summary_file"] = str(summaries[0].relative_to(inputs))
        if module_probe:
            if scoped:
                scoped_result = reports / 'module_execution_results.json'
                if scoped_result.is_symlink():
                    raise ValueError('Symlinked scoped result')
                result.update(scoped_result=json.loads(scoped_result.read_text()),
                              scoped_result_file=scoped_result.relative_to(inputs).as_posix(),
                              scoped_result_digest=digest(scoped_result))
            else:
                from module_probe import verify_source_coverage
                result['source_coverage'] = verify_source_coverage(reports, result['consumer_preflight']['source_catalog'])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
        with zipfile.ZipFile(invocation / "reports.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            if reports.is_dir():
                for path in sorted(reports.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        archive.write(path, path.relative_to(inputs).as_posix())
        result["reports_digest"] = digest(invocation / "reports.zip")
        temporary = invocation / "result.json.tmp"
        temporary.write_text(json.dumps(result, indent=2) + "\n")
        temporary.replace(invocation / "result.json")
    return 0 if not result.get("error") and result["pipeline_returncode"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(execute(sys.argv[1]))
