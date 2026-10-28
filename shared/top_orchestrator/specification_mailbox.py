"""Authenticated Experimental snapshots and explicit compatibility projections.

CLI: python -m shared.top_orchestrator.specification_mailbox BUILD DEST --target-mhz MHZ
No model, remote agent or backend is invoked.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

from .frequency import positive_mhz
from .mailbox import file_hashes
from .nodes import FRONTEND


def read_json(path):
    return json.loads(Path(path).read_text())


def relative_file(root, value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Nonportable snapshot path: " + str(value))
    result = root.joinpath(*path.parts)
    if result.is_symlink() or not result.is_file() or not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("Missing/nonportable snapshot file: " + str(value))
    return result


def validate_native(handoff):
    result = subprocess.run([sys.executable, str(FRONTEND / "verification_handoff.py"), str(handoff)],
                            cwd=FRONTEND, text=True, capture_output=True)
    if result.returncode:
        raise ValueError("Native handoff authentication failed: " + result.stderr.strip())
    document = read_json(handoff)
    if document.get("phase") != "rtl":
        raise ValueError("Publication requires an RTL-phase handoff")
    return document


def projection(document, target_mhz):
    modules = []
    for module in document["design"]["modules"]:
        name = module["module_name"]
        modules.append({"instance": module["instance"], "module_name": name,
                        "kind": "functional", "rtl": "rtl/" + name + ".sv",
                        "specification": "specs/" + Path(module["specification"]).name,
                        "source_specification": "handoff/" + module["specification"]})
    top = document["design"]["top_module"]
    modules.append({"module_name": top, "kind": "structural",
                    "rtl": "rtl/" + top + ".sv", "contract_kind": "architecture",
                    "contract": "handoff/architecture.yaml", "verification": "not_run"})
    return {"schema": "capstone.specification_rtl_inventory", "schema_version": 1,
            "frontend_flow": "specification", "top_module": top,
            "target_mhz": target_mhz, "identity": document["identity"],
            "compile_order": ["rtl/" + Path(path).name for path in document["rtl_files"]],
            "modules": modules, "backend_ready": False}


def publish_specification(build, destination, target_mhz):
    build, destination = Path(build).resolve(), Path(destination).absolute()
    target_mhz = positive_mhz(str(target_mhz))
    if destination.exists():
        raise FileExistsError(destination)
    # Reject symlinked evidence before reading it or copying references.
    for name in ("verification_handoff.json", "build_status.json", "integration_lint.json"):
        relative_file(build, name)
    document = validate_native(build / "verification_handoff.json")
    status = read_json(build / "build_status.json")
    if any(status.get(key) != value for key, value in {
            "rtl_generation": "passed", "integration_lint": "passed",
            "specification_approval": "approved", "verification_handoff": "exported"}.items()):
        raise ValueError("Build lacks passed generation, approval, lint or handoff evidence")
    if read_json(build / "integration_lint.json").get("ok") is not True:
        raise ValueError("Integration lint evidence did not pass")
    # Compare any declared operating clock without rewriting approved contracts.
    from .specification_inputs import inspect_input
    inspect_input(build / "architecture.yaml", build / "specification_approval.json", target_mhz)
    inventory = projection(document, target_mhz)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".specification-publishing-", dir=destination.parent) as tmp:
        snapshot = Path(tmp) / "snapshot"
        snapshot.mkdir()
        references = {row["path"] for row in document["inputs"]}
        references.update(("verification_handoff.json", "build_status.json", "integration_lint.json"))
        for name in sorted(references):
            source = relative_file(build, name)
            target = snapshot / "handoff" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        for name in document["rtl_files"]:
            target = snapshot / "rtl" / Path(name).name
            target.parent.mkdir(exist_ok=True)
            if target.exists():
                raise ValueError("Duplicate RTL projection name")
            target.write_bytes(relative_file(snapshot / "handoff", name).read_bytes())
        for row in document["inputs"]:
            if row["role"] == "module_spec":
                target = snapshot / "specs" / Path(row["path"]).name
                target.parent.mkdir(exist_ok=True)
                if target.exists():
                    raise ValueError("Duplicate specification projection name")
                target.write_bytes(relative_file(snapshot / "handoff", row["path"]).read_bytes())
        (snapshot / "rtl/manifest.json").write_text(json.dumps(inventory, indent=2) + "\n")
        (snapshot / "rtl/filelist.f").write_text("\n".join(Path(p).name for p in document["rtl_files"]) + "\n")
        (snapshot / "verification_inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
        metadata = {"schema_version": 1, "frontend_flow": "specification",
                    "rtl_revision": document["identity"]["rtl_revision"],
                    "handoff_fingerprint": document["handoff_fingerprint"],
                    "target_mhz": target_mhz, "files": file_hashes(snapshot),
                    "backend_ready": False, "verification": "not_run",
                    "modules_without_specs": [document["design"]["top_module"]],
                    "structural_contract": "handoff/architecture.yaml"}
        (snapshot / "snapshot.json").write_text(json.dumps(metadata, indent=2) + "\n")
        verify_specification(snapshot)
        snapshot.rename(destination)
    return destination.resolve()


def verify_specification(mailbox):
    mailbox = Path(mailbox).resolve()
    metadata = read_json(mailbox / "snapshot.json")
    if metadata.get("frontend_flow") != "specification" or metadata.get("backend_ready") is not False:
        raise ValueError("Unsupported Experimental snapshot metadata")
    actual = file_hashes(mailbox)
    actual.pop("snapshot.json", None)
    if actual != metadata["files"]:
        raise ValueError("Experimental snapshot contents have changed")
    document = validate_native(mailbox / "handoff/verification_handoff.json")
    status = read_json(mailbox / "handoff/build_status.json")
    if (status.get("rtl_generation") != "passed" or status.get("integration_lint") != "passed"
            or status.get("specification_approval") != "approved"
            or status.get("verification_handoff") != "exported"
            or read_json(mailbox / "handoff/integration_lint.json").get("ok") is not True):
        raise ValueError("Snapshot lacks passed build/lint evidence")
    if (metadata["rtl_revision"] != document["identity"]["rtl_revision"]
            or metadata["handoff_fingerprint"] != document["handoff_fingerprint"]):
        raise ValueError("Experimental snapshot revision mismatch")
    expected = projection(document, positive_mhz(str(metadata["target_mhz"])))
    if (read_json(mailbox / "verification_inventory.json") != expected
            or read_json(mailbox / "rtl/manifest.json") != expected):
        raise ValueError("Compatibility inventory differs from authenticated handoff")
    if (mailbox / "rtl/filelist.f").read_text() != "\n".join(Path(p).name for p in document["rtl_files"]) + "\n":
        raise ValueError("Projected compile order differs from handoff")
    for name in document["rtl_files"]:
        if relative_file(mailbox, "rtl/" + Path(name).name).read_bytes() != relative_file(mailbox / "handoff", name).read_bytes():
            raise ValueError("RTL projection differs from handoff")
    for row in document["inputs"]:
        if row["role"] == "module_spec":
            if relative_file(mailbox, "specs/" + Path(row["path"]).name).read_bytes() != relative_file(mailbox / "handoff", row["path"]).read_bytes():
                raise ValueError("Contract projection differs from handoff")
    return metadata


def prepare_module(mailbox, module_name, destination):
    """One real YAML/SV pair with lossless context, not a system pass."""
    import hashlib
    mailbox, destination = Path(mailbox).resolve(), Path(destination).absolute()
    metadata = verify_specification(mailbox)
    inventory = read_json(mailbox / "verification_inventory.json")
    matches = [row for row in inventory["modules"] if row["module_name"] == module_name]
    if len(matches) != 1 or matches[0]["kind"] != "functional":
        raise ValueError("Select one functional module; structural top uses an architecture-backed contract")
    if destination.exists():
        raise FileExistsError(destination)
    row = matches[0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".module-preparing-", dir=destination.parent) as tmp:
        root = Path(tmp) / "input"
        root.mkdir()
        for name in (row["rtl"], row["specification"]):
            target = root / name
            target.parent.mkdir()
            target.write_bytes(relative_file(mailbox, name).read_bytes())
        handoff = read_json(mailbox / "handoff/verification_handoff.json")
        (root / "source_context.json").write_text(json.dumps({
            "module": row, "identity": handoff["identity"],
            "shared": handoff["shared"], "verification_intent": handoff["verification_intent"],
            "scope": "one_module_only", "system_verification": "not_run",
        }, indent=2) + "\n")
        record = {"schema_version": 1, "frontend_flow": "specification-module",
                  "source_snapshot_digest": hashlib.sha256((mailbox / "snapshot.json").read_bytes()).hexdigest(),
                  "parent_rtl_revision": metadata["rtl_revision"], "module_name": module_name,
                  "files": file_hashes(root), "backend_ready": False,
                  "rtl_revision": hashlib.sha256(json.dumps(file_hashes(root / "rtl"), sort_keys=True).encode()).hexdigest()}
        (root / "snapshot.json").write_text(json.dumps(record, indent=2) + "\n")
        root.rename(destination)
    return destination


def verify_module_probe(inputs, parent):
    """Bind the isolated pair and complete context to its full published parent."""
    import hashlib
    inputs, parent = Path(inputs), Path(parent)
    full = verify_specification(parent)
    metadata = read_json(inputs / 'snapshot.json')
    actual = file_hashes(inputs)
    actual.pop('snapshot.json', None)
    if (metadata.get('frontend_flow') != 'specification-module' or metadata.get('backend_ready') is not False
            or actual != metadata['files']
            or metadata.get('source_snapshot_digest') != hashlib.sha256((parent / 'snapshot.json').read_bytes()).hexdigest()
            or metadata.get('parent_rtl_revision') != full['rtl_revision']):
        raise ValueError('Module probe identity differs from its immutable parent')
    inventory = read_json(parent / 'verification_inventory.json')
    rows = [row for row in inventory['modules'] if row['module_name'] == metadata['module_name'] and row['kind'] == 'functional']
    if len(rows) != 1:
        raise ValueError('Module probe does not identify one functional module')
    row = rows[0]
    if set(actual) != {row['rtl'], row['specification'], 'source_context.json'}:
        raise ValueError('Module probe inventory differs from its parent')
    for name in (row['rtl'], row['specification']):
        if relative_file(inputs, name).read_bytes() != relative_file(parent, name).read_bytes():
            raise ValueError('Module probe pair differs from its parent')
    handoff = read_json(parent / 'handoff/verification_handoff.json')
    expected = {'module': row, 'identity': handoff['identity'], 'shared': handoff['shared'],
                'verification_intent': handoff['verification_intent'], 'scope': 'one_module_only',
                'system_verification': 'not_run'}
    if read_json(inputs / 'source_context.json') != expected:
        raise ValueError('Module probe context differs from its parent')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--target-mhz", type=positive_mhz, required=True)
    parser.add_argument("--module", help="Also prepare this functional module for consumer compatibility checks")
    parser.add_argument("--module-output", type=Path)
    args = parser.parse_args()
    if bool(args.module) != bool(args.module_output):
        parser.error("--module and --module-output are required together")
    try:
        mailbox = publish_specification(args.build, args.destination, args.target_mhz)
        print(mailbox)
        if args.module:
            print(prepare_module(mailbox, args.module, args.module_output))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, "Publication stopped: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
