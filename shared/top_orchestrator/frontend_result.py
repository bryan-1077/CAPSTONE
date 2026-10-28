"""Common frontend evidence index; references authenticated route-native contracts."""
import hashlib
import json
from pathlib import Path


def write_frontend_result(output, *, flow, build, top, target_mhz, revision,
                          rtl_files, contracts, requirements, checks, evidence, timing_model=None):
    inventory = []
    for role, paths in (("rtl", rtl_files), ("contract", contracts)):
        for value in paths:
            path = Path(value).resolve(strict=True)
            inventory.append({"role": role, "path": str(path),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not rtl_files or not top or not revision:
        raise ValueError("Frontend result requires RTL, top and revision identity")
    document = {"schema": "capstone.frontend_result", "schema_version": 1,
                "frontend_flow": flow, "build": str(Path(build).resolve()),
                "top_module": top, "target_mhz": target_mhz, "revision": revision,
                "files": inventory, "requirements": requirements, "checks": checks,
                "evidence": evidence, "backend_ready": False, "timing_model": timing_model}
    output = Path(output)
    output.write_text(json.dumps(document, indent=2) + "\n")
    return output


def standard_result(mailbox, config, output, artifacts):
    import yaml
    from .mailbox import verify
    metadata = verify(mailbox)
    manifest = json.loads((mailbox / "rtl/manifest.json").read_text())
    approved = mailbox / "approved_specs.yaml"
    from .timing_model import apply_timing_model
    source_config = yaml.safe_load(approved.read_text()) if approved.is_file() else {}
    timing_model = apply_timing_model(source_config, standard=True)["timing_model"]
    # Legacy manifest has no compile-order guarantee; retain the native manifest
    # rather than labeling sorted inventory as compile order.
    return write_frontend_result(output, flow="expand-spec", build=mailbox,
        top=manifest["top_module"], target_mhz=config.target_mhz,
        revision=metadata["rtl_revision"],
        rtl_files=sorted((mailbox / "rtl").glob("*.sv")) + sorted((mailbox / "rtl").glob("*.v")),
        contracts=sorted((mailbox / "specs").rglob("*.yaml")),
        requirements={"source": str(mailbox / "specs"), "coverage": "not_evaluated"},
        checks={"frontend": "passed", "bist": artifacts.get("bist", "see_stage_evidence")},
        timing_model=timing_model,
        evidence={"mailbox": str(mailbox), "manifest": str(mailbox / "rtl/manifest.json"),
                  "compile_order": "see_native_manifest"})
