"""Publish complete frontend snapshots without modifying earlier revisions."""

import hashlib
import json
import shutil
import tempfile
from pathlib import Path


def file_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Unsupported snapshot entry: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def publish(frontend: Path, destination: Path, approved_specs: Path | None = None) -> Path:
    """Copy RTL plus matching expanded YAMLs; atomically expose a new revision."""
    import yaml

    rtl = frontend / "rtl_output"
    if not rtl.is_dir() or rtl.is_symlink():
        raise ValueError("Frontend rtl_output must be a real directory")
    hashes = file_hashes(rtl)
    if not any(Path(name).suffix.lower() in {".v", ".sv"} for name in hashes):
        raise ValueError("Frontend RTL snapshot is empty")
    manifest = json.loads((rtl / "manifest.json").read_text())
    modules = set(manifest["modules"])
    if not manifest.get("top_module") or manifest["top_module"] not in modules:
        raise ValueError("RTL manifest must identify its top module")
    specs = []
    microarch = frontend / "microarch"
    # A migrated workspace must never silently publish stale legacy specs.
    expanded = microarch / "gen_exp" if microarch.exists() or microarch.is_symlink() else frontend / "expanded"
    if microarch.is_symlink() or expanded.is_symlink():
        raise ValueError("Expanded specs must not be a symlink")
    if not expanded.is_dir():
        raise ValueError(f"Expanded specs directory is missing: {expanded}")
    for name in file_hashes(expanded):
        path = expanded / name
        if (path.suffix.lower() not in {".yaml", ".yml"}
                or path.name == "master.yaml" or path.stem.endswith("_master")):
            continue
        spec = yaml.safe_load(path.read_text())
        if isinstance(spec, dict) and spec.get("design_name") in modules:
            specs.append((path, spec["design_name"]))
    if not specs:
        raise ValueError("No expanded YAML specs match the RTL manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(prefix=".publishing-", dir=destination.parent) as temporary:
        snapshot = Path(temporary) / "snapshot"
        shutil.copytree(rtl, snapshot / "rtl", symlinks=True)
        for path, _ in specs:
            target = snapshot / "specs" / path.relative_to(expanded)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        if approved_specs is not None:
            (snapshot / "approved_specs.yaml").write_bytes(approved_specs.read_bytes())
        files = file_hashes(snapshot)
        metadata = {"schema_version": 1, "files": files,
                    "rtl_revision": hashlib.sha256(json.dumps(file_hashes(snapshot / "rtl"), sort_keys=True).encode()).hexdigest(),
                    "modules_without_specs": sorted(modules - {name for _, name in specs})}
        (snapshot / "snapshot.json").write_text(json.dumps(metadata, indent=2) + "\n")
        snapshot.rename(destination)
    return destination.resolve()


def verify(mailbox: Path) -> dict:
    """Reject changed or incomplete published snapshots before launching backend."""
    metadata = json.loads((mailbox / "snapshot.json").read_text())
    actual = file_hashes(mailbox)
    actual.pop("snapshot.json", None)
    if actual != metadata["files"]:
        raise ValueError("Mailbox snapshot contents have changed")
    rtl_revision = hashlib.sha256(json.dumps(file_hashes(mailbox / "rtl"), sort_keys=True).encode()).hexdigest()
    if rtl_revision != metadata["rtl_revision"]:
        raise ValueError("Mailbox RTL revision does not match snapshot metadata")
    return metadata
