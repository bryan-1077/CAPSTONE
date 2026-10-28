"""Offline coverage inventory; this plan never authorizes backend execution."""
import argparse
import hashlib
import json
from pathlib import Path

import yaml

from .module_probe import expected_source_catalog
from .specification_mailbox import verify_specification, relative_file, read_json


def verification_plan(mailbox):
    mailbox = Path(mailbox).resolve()
    metadata = verify_specification(mailbox)
    inventory = read_json(mailbox / "verification_inventory.json")
    handoff = read_json(mailbox / "handoff/verification_handoff.json")
    modules = []
    for row in inventory["modules"]:
        entry = dict(row, status="not_run")
        entry["rtl_sha256"] = hashlib.sha256(relative_file(mailbox, row["rtl"]).read_bytes()).hexdigest()
        if row["kind"] == "functional":
            spec = yaml.safe_load(relative_file(mailbox, row["specification"]).read_text())
            entry["required_sources"] = expected_source_catalog(spec)
            entry["contract_sha256"] = hashlib.sha256(relative_file(mailbox, row["specification"]).read_bytes()).hexdigest()
            entry["required_evidence"] = "simulation"
        else:
            entry["contract_sha256"] = hashlib.sha256(relative_file(mailbox, row["contract"]).read_bytes()).hexdigest()
            entry["required_evidence"] = "structural_and_system_verification"
        modules.append(entry)
    return {"schema": "capstone.specification_verification_plan", "schema_version": 1,
            "snapshot_sha256": hashlib.sha256((mailbox / "snapshot.json").read_bytes()).hexdigest(),
            "rtl_revision": metadata["rtl_revision"],
            "top_module": inventory["top_module"], "target_mhz": inventory["target_mhz"],
            "compile_order": inventory["compile_order"], "modules": modules,
            "system_verification": {"status": "not_run", "coverage": "not_evaluated",
                                    "consumer_support": "not_established",
                                    "verification_intent": handoff["verification_intent"],
                                    "shared": handoff["shared"]},
            "repair_policy": "publish_new_revision_and_recheck_affected_coverage",
            "backend_ready": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mailbox", type=Path)
    args = parser.parse_args()
    print(json.dumps(verification_plan(args.mailbox), indent=2))


if __name__ == "__main__":
    main()
