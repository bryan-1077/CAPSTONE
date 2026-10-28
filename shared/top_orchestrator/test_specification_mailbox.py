"""Experimental publication guards and lossless module consumer preparation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

from .mailbox import file_hashes, verify
from .specification_mailbox import publish_specification, verify_specification, prepare_module


class SpecificationMailboxTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.build = self.root / "build"
        self.build.mkdir()
        self.doc = {"phase": "rtl", "handoff_fingerprint": "handoff-hash",
            "identity": {"rtl_revision": "revision"},
            "design": {"top_module": "top", "modules": [{"instance": "unit", "module_name": "unit",
                "specification": "microarch/gen_exp/unit.yaml"}]},
            "rtl_files": ["rtl_output/unit.sv", "rtl_output/top.sv"],
            "shared": {}, "verification_intent": {"requirements": {"R1": {"statement": "keep me"}}},
            "inputs": [{"role": role, "path": path} for role, path in [
                ("architecture", "architecture.yaml"), ("approval", "specification_approval.json"),
                ("module_spec", "microarch/gen_exp/unit.yaml"), ("module_ir", "microarch/ir/unit.json"),
                ("rtl", "rtl_output/unit.sv"), ("rtl", "rtl_output/top.sv")]]}
        spec = {"design_name": "unit", "behavior": {"specification_context": {},
            "contract": {"reset": "clear"}, "verification_obligations": {
                "system.verification.V1": {"scenario": "reset", "expected": "zero", "rules": ["R1"]}}},
            "verification_requirements": [{"id": "R1", "text": "Keep exact text.\nNext line.",
                                             "owners": ["unit"], "verification": "full obligation"}]}
        for row in self.doc["inputs"]:
            path = self.build / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(spec) if row["role"] == "module_spec" else "fixture")
        (self.build / "verification_handoff.json").write_text(json.dumps(self.doc))
        (self.build / "build_status.json").write_text(json.dumps({"rtl_generation": "passed",
            "integration_lint": "passed", "specification_approval": "approved", "verification_handoff": "exported"}))
        (self.build / "integration_lint.json").write_text('{"ok":true}')
        for target, value in [("shared.top_orchestrator.specification_mailbox.validate_native", self.doc),
                              ("shared.top_orchestrator.specification_inputs.inspect_input", {})]:
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.mailbox = self.root / "revision"

    def publish(self):
        return publish_specification(self.build, self.mailbox, 200)

    def test_inventory_preserves_wrapper_and_all_native_inputs(self):
        self.publish()
        metadata = verify(self.mailbox)
        self.assertFalse(metadata["backend_ready"])
        inventory = json.loads((self.mailbox / "verification_inventory.json").read_text())
        self.assertEqual(inventory["modules"][-1]["contract_kind"], "architecture")
        for row in self.doc["inputs"]:
            self.assertEqual((self.mailbox / "handoff" / row["path"]).read_bytes(), (self.build / row["path"]).read_bytes())
        self.assertEqual(inventory["compile_order"], ["rtl/unit.sv", "rtl/top.sv"])
        with self.assertRaises(FileExistsError):
            self.publish()

    def test_offline_verification_plan_preserves_scope_and_rejects_tampering(self):
        from .specification_verification_plan import verification_plan
        self.publish()
        plan = verification_plan(self.mailbox)
        self.assertFalse(plan["backend_ready"])
        self.assertEqual(plan["system_verification"]["coverage"], "not_evaluated")
        self.assertEqual(plan["modules"][-1]["kind"], "structural")
        self.assertEqual(plan["modules"][-1]["status"], "not_run")
        sources = plan["modules"][0]["required_sources"]
        self.assertEqual(sources["R1"]["source_requirement"]["text"], "Keep exact text.\nNext line.")
        self.assertIn("system.verification.V1", sources)
        self.assertIn("behavior.contract", sources)
        (self.mailbox / "rtl/unit.sv").write_text("changed")
        with self.assertRaises(ValueError):
            verification_plan(self.mailbox)

    def test_tampered_projection_rejected_even_with_updated_outer_hashes(self):
        self.publish()
        (self.mailbox / "rtl/unit.sv").write_text("changed")
        metadata = json.loads((self.mailbox / "snapshot.json").read_text())
        files = file_hashes(self.mailbox)
        files.pop("snapshot.json")
        metadata["files"] = files
        (self.mailbox / "snapshot.json").write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "projection differs"):
            verify_specification(self.mailbox)

    def test_missing_pair_and_failed_lint_prevent_partial_revision(self):
        (self.build / "rtl_output/unit.sv").unlink()
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(self.mailbox.exists())

        (self.build / "rtl_output/unit.sv").write_text("restored")
        (self.build / "integration_lint.json").write_text('{"ok":false}')
        with self.assertRaisesRegex(ValueError, "lint"):
            self.publish()
        self.assertFalse(self.mailbox.exists())

    def test_experimental_evidence_cannot_enter_legacy_verification_or_backend(self):
        from .contracts import RunConfig
        from .validation import validation_node, verify_backend_handoff
        from .nodes import backend_node
        self.publish()
        run = self.root / "validation"
        run.mkdir()
        with patch("shared.top_orchestrator.validation.RemoteConfig.load", side_effect=AssertionError("Remote setup attempted")):
            result = validation_node(RunConfig("validation", mailbox=self.mailbox), run)
        self.assertEqual(result.status, "needs_attention")
        self.assertIn("consumer adapter", result.message)
        with self.assertRaisesRegex(ValueError, "readiness"):
            verify_backend_handoff(self.root / "nonexistent-result.json", self.mailbox)
        with self.assertRaisesRegex(ValueError, "backend integration"):
            backend_node(RunConfig("backend", mailbox=self.mailbox, target_mhz=200, allow_unvalidated=True), run)

    def test_real_module_contract_reader_retains_identity_obligations_and_context(self):
        self.publish()
        output = prepare_module(self.mailbox, "unit", self.root / "module-input")
        context = json.loads((output / "source_context.json").read_text())
        self.assertEqual(context["verification_intent"], self.doc["verification_intent"])
        with self.assertRaisesRegex(ValueError, "structural top"):
            prepare_module(self.mailbox, "top", self.root / "bad")
        path = Path(__file__).resolve().parents[2] / "server/validation/requirement_verification.py"
        loader = importlib.util.spec_from_file_location("specification_requirement_reader", path)
        reader = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(reader)
        spec = yaml.safe_load((output / "specs/unit.yaml").read_text())
        catalog = reader.build_requirement_catalog(spec)
        source = next(row for row in catalog if row.get("source_id") == "R1")
        self.assertEqual(source["text"], spec["verification_requirements"][0]["text"])
        self.assertEqual(source["source_requirement"], spec["verification_requirements"][0])
        obligation = next(row for row in catalog if row.get("source_id") == "system.verification.V1")
        self.assertEqual(obligation["source_obligation"], spec["behavior"]["verification_obligations"][obligation["source_id"]])
        reader.attach_static_results(catalog, "module unit; endmodule")
        result = reader.combine_requirement_results(catalog, {"requirements": [], "overall_status": "INCOMPLETE"})
        self.assertEqual(result["overall_status"], "INCOMPLETE")
        self.assertTrue(any(row.get("source_id") == "R1" for row in result["requirements"]))
