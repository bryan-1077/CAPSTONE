"""Copy and verify mapped results before allowing a backend run to consume them.

Standalone stdlib-only module, also executed on the remote host.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


def manifest(results: Path) -> dict:
    files = sorted(path for path in results.rglob("*") if path.is_file())
    if not any(path.suffix == ".v" for path in files) or not any(path.suffix == ".sdc" for path in files):
        raise RuntimeError("Mapped snapshot requires both Verilog and SDC results.")
    digests = {}
    for path in files:
        data = path.read_bytes()
        if not data:
            raise RuntimeError(f"Empty mapped artifact: {path}")
        digests[str(path.relative_to(results))] = hashlib.sha256(data).hexdigest()
    return digests


def completed_marker(source: Path) -> str | None:
    path = source / "agent_mapped_progress.log"
    if not path.exists():
        return None
    text = path.read_text()
    if not text.rstrip().endswith("EDA_MAPPED_RUN_COMPLETE:0"):
        raise RuntimeError("Mapped producer is still running or did not complete successfully.")
    return text


def snapshot_mapped(source: Path, destination: Path) -> dict:
    marker = completed_marker(source)
    before = manifest(source / "results")
    # Refuse to merge into any previous snapshot; copy bytes, not symlinks/hard links.
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source / "results", destination / "results", symlinks=False)
    copied = manifest(destination / "results")
    after = manifest(source / "results")
    if before != copied or before != after or marker != completed_marker(source):
        raise RuntimeError("Mapped inputs changed while copying; snapshot rejected.")
    result = {"source": str(source), "snapshot": str(destination), "sha256": copied}
    (destination / "mapped_snapshot.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(snapshot_mapped(Path(sys.argv[1]), Path(sys.argv[2]))))
