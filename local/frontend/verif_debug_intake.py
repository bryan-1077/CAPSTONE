"""Capture portable verification run artifacts without importing historical DUT RTL."""

import hashlib
import json
import re
import shutil
from pathlib import Path


def capture_verif_run(source: Path, intake: Path) -> str:
    source = source.expanduser().resolve()

    def read_object(path: Path) -> dict:
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError(f"Verification metadata must be inside the run: {path}")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object in {path}")
        return value

    summary = read_object(source / "summary.json")
    if "overall_status" not in summary or not isinstance(summary.get("stages"), dict):
        raise ValueError("Verification summary requires overall_status and stages.")
    stages = dict(summary["stages"])
    for directory in sorted(source.iterdir()):
        if re.fullmatch(r"agent\d+", directory.name) and not directory.is_symlink():
            report = directory / "report.json"
            if report.is_file():
                stages[directory.name] = read_object(report)

    validation_path = source / "agent1" / "validation_result.json"
    validation = read_object(validation_path) if validation_path.is_file() else {}
    issues = []
    failed_stages = []
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            raise ValueError(f"Expected an object for verification stage {name}")
        status = str(stage.get("status", "UNKNOWN")).upper()
        if status not in {"PASS", "SKIP", "SKIPPED"}:
            failed_stages.append(name)
            issues.append(f"{name} {status}: {stage.get('reason') or stage.get('failure_detail') or 'See stage report'}")
        for requirement in stage.get("requirements", []):
            if isinstance(requirement, dict) and requirement.get("status") == "FAIL":
                issues.append(f"{name} {requirement.get('id')}: {requirement.get('requirement')}; {requirement.get('evidence')}")
    report = {key: validation[key] for key in
              ("top_module_name", "top_module_file", "rtl_files", "spec_file") if key in validation}
    report.update(is_valid=summary["overall_status"] == "PASS" and not failed_stages,
                  failure_stage=", ".join(failed_stages), issues=issues,
                  summary=f"Verification run {summary.get('run_id', source.name)}: {summary['overall_status']}",
                  rtl_provenance="Current rtl_output is the DUT source. Run-to-current RTL identity is not established by this import.")

    manifest = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part.startswith(".") or part == "__MACOSX" for part in relative.parts):
            continue
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != source):
            continue
        if not path.is_file() or not path.resolve().is_relative_to(source):
            continue
        if len(relative.parts) > 1 and not re.fullmatch(r"agent\d+", relative.parts[0]):
            continue
        if path.suffix not in {".json", ".yaml", ".yml", ".log", ".txt", ".md", ".sv", ".svh", ".v"}:
            continue
        # Only generated verification sources are imported; DUT RTL stays in rtl_output.
        if path.suffix in {".sv", ".svh", ".v"} and "candidates" not in relative.parts:
            continue
        destination = intake / "evidence" / "verif_run" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        manifest.append({"source": str(path), "captured_as": str(destination),
                         "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                         "bytes": destination.stat().st_size})
    (intake / "evidence_files.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return json.dumps(report, indent=2) + "\n"
