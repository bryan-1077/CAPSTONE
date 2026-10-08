"""Standalone remote wrapper. Uploaded by the adapter; uses only the standard library.

Runs the deployed validation scripts unchanged, with per-invocation inputs/work.
"""

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_inputs(root, expected_digest):
    manifest = root / "snapshot.json"
    if digest(manifest) != expected_digest:
        raise ValueError("Snapshot manifest digest mismatch")
    metadata = json.loads(manifest.read_text())
    actual = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlink in validation inputs")
        if path.is_file() and path != manifest:
            # Pipeline reports are created under the common RTL/spec input root.
            relative = path.relative_to(root)
            if relative.parts[0] != "verification_reports":
                actual[relative.as_posix()] = digest(path)
    if actual != metadata["files"]:
        raise ValueError("Validation input hashes do not match the published snapshot")
    return metadata


def execute(invocation):
    invocation = Path(invocation).resolve()
    request = json.loads((invocation / "request.json").read_text())
    inputs = invocation / "input"
    result = {"schema_version": 1, "invocation_id": request["invocation_id"],
              "snapshot_digest": request["snapshot_digest"], "input_directory": str(inputs),
              "pipeline_returncode": None, "summary": None, "input_unchanged": False}
    try:
        check_inputs(inputs, request["snapshot_digest"])
        reports = inputs / "verification_reports"
        if reports.exists():
            raise ValueError("Reports already exist before this invocation")
        pipeline = Path(request["pipeline"])
        if not pipeline.is_file():
            raise FileNotFoundError(f"Validation entry point does not exist: {pipeline}")
        result["pipeline_digest"] = digest(pipeline)
        work = invocation / "work"
        if not work.is_dir():
            raise FileNotFoundError(f"Work directory must be prepared before running: {work}")
        command = [sys.executable, "-u", str(pipeline), str(inputs / "rtl"), str(inputs / "specs"),
                   "--jobs", str(request["jobs"])]
        result["command"] = command
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        # Do not inherit another verification invocation's report destinations.
        environment.pop("RTL_VERIFICATION_REPORT_RUN", None)
        environment.pop("RTL_VALIDATION_RESULT_FILE", None)
        result["pipeline_returncode"] = subprocess.call(command, cwd=work, env=environment)
        check_inputs(inputs, request["snapshot_digest"])
        result["input_unchanged"] = True
        # A fresh input directory has exactly one top-level run; never use global latest.
        summaries = list(reports.glob("*/summary.json"))
        if len(summaries) != 1:
            raise ValueError(f"Expected one invocation summary, found {len(summaries)}")
        result["summary"] = json.loads(summaries[0].read_text())
        result["summary_file"] = str(summaries[0].relative_to(inputs))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(result["error"], file=sys.stderr, flush=True)
    finally:
        with zipfile.ZipFile(invocation / "reports.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
            reports = inputs / "verification_reports"
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
