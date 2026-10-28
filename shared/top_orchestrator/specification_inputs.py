"""Read-only Experimental input inspection and portable run-owned snapshots."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

from .frequency import clock_requirements
from .nodes import FRONTEND


def document(path):
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML input: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected YAML mapping: {path}")
    return value


def inspect_input(source, approval=None, target=None):
    source = Path(source).expanduser().resolve(strict=True)
    data = document(source)
    files = [source]
    if "specification_version" in data:
        mode = "package"
        for relative in data["documents"].values():
            path = (source.parent / relative).resolve(strict=True)
            if not path.is_relative_to(source.parent):
                raise ValueError("Package documents must stay inside their package directory")
            files.append(path)
        shared_path = (source.parent / data["documents"]["shared"]).resolve()
        shared = document(shared_path)
        files.append((shared_path.parent / shared["timing_profile"]["source"]).resolve(strict=True))
        clock_source = data.get("generation", {}).get("request", {}).get("constraints", {})
    elif "architecture_version" in data:
        mode = "approved"
        if approval is None:
            raise ValueError("Approved architecture input requires --spec-approval")
        approval = Path(approval).expanduser().resolve(strict=True)
        # The native compiler validates embedded source, fingerprint and approval.
        check = subprocess.run([sys.executable, "-c",
            "import sys; from specification import read_document; "
            "from architecture import validate_architecture; "
            "from specification_lowering import is_specification_manifest, require_approval; "
            "m=read_document(sys.argv[1]); validate_architecture(m); "
            "assert is_specification_manifest(m), 'Expected specification-derived architecture'; "
            "require_approval(m, sys.argv[2])", str(source), str(approval)],
            cwd=FRONTEND, capture_output=True, text=True)
        if check.returncode:
            raise ValueError("Architecture/approval validation failed: " + check.stderr.strip())
        clock_source = data["provenance"]["specification"]["snapshot"]["package"].get(
            "generation", {}).get("request", {}).get("constraints", {})
    else:
        mode = "request"
        clock_source = data
    if mode != "approved" and approval is not None:
        raise ValueError("--spec-approval requires a specification-derived architecture input")
    requirements = clock_requirements(clock_source)
    if target is not None and any(not math.isclose(target, value, rel_tol=1e-6) for _, value in requirements):
        raise ValueError("Target controller clock conflicts with the supplied specification")
    return {"mode": mode, "source": source, "approval": approval, "files": list(dict.fromkeys(files)),
            "clock_requirements": requirements}


def snapshot_input(info, destination):
    destination = Path(destination)
    common = Path(os.path.commonpath([str(p.parent) for p in info["files"]]))
    destination.mkdir(parents=True)
    inventory = []
    for source in info["files"]:
        relative = source.relative_to(common)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        inventory.append({"path": str(relative), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    approval = None
    if info["approval"]:
        approval = destination / "approval.json"
        if approval.exists():
            raise ValueError("Approval snapshot name conflicts with input")
        approval.write_bytes(info["approval"].read_bytes())
        inventory.append({"path": approval.name, "sha256": hashlib.sha256(approval.read_bytes()).hexdigest()})
    (destination / "snapshot.json").write_text(json.dumps(inventory, indent=2) + "\n")
    return destination / info["source"].relative_to(common), approval


def planned_snapshot_path(info, destination):
    common = Path(os.path.commonpath([str(p.parent) for p in info["files"]]))
    return Path(destination) / info["source"].relative_to(common)


def copy_resume_build(source, destination, architecture):
    source = Path(source).expanduser().resolve(strict=True)
    if Path(destination).resolve().is_relative_to(source):
        raise ValueError("Resume destination must be outside the source build")
    if document(source / "architecture.yaml") != document(architecture):
        raise ValueError("Resume build architecture differs from the approved input")
    if any(path.is_symlink() for path in source.rglob("*")):
        raise ValueError("Resume build cannot contain symlinks")
    # Resume reuses reviewed caches, not arbitrary patched RTL. Stop before
    # generation if it could replace an intentional edit with an older cache.
    manifest = document(architecture)
    top = source / "rtl_output" / (manifest.get("design_name", "") + ".sv")
    if top.is_file():
        expected = subprocess.run([sys.executable, "-c",
            "import sys; from specification import read_document; "
            "from architecture import validate_architecture, emit_wrapper; "
            "m=read_document(sys.argv[1]); sys.stdout.write(emit_wrapper(m, validate_architecture(m)))",
            str(architecture)], cwd=FRONTEND, capture_output=True, text=True)
        if expected.returncode or expected.stdout != top.read_text():
            raise ValueError("Resume structural top was modified; use scoped recheck instead of regeneration")
    for module in manifest["modules"]:
        name = module["spec"]["design_name"]
        spec_path = source / "microarch/gen_exp" / (name + ".yaml")
        rtl_path = source / "rtl_output" / (name + ".sv")
        if rtl_path.exists():
            if not spec_path.is_file():
                raise ValueError("Resume RTL lacks its lowered contract")
            digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
            cache = source / ".llm_cache" / digest / (name + ".sv")
            if not cache.is_file() or cache.read_bytes() != rtl_path.read_bytes():
                raise ValueError("Resume RTL differs from reviewed cache; preserve patches through a scoped recheck instead of regeneration")
    shutil.copytree(source, destination)
