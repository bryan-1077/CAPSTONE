"""Controller v2 metadata and mailbox portability without model/EDA calls."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from shared.controller_manifest import discover_system
from shared.top_orchestrator.mailbox import publish, verify

FRONTEND = Path(__file__).resolve().parents[2] / "local/frontend"


@unittest.skipUnless((FRONTEND / "rtl_output/manifest.json").is_file(),
                     "Requires a generated legacy controller snapshot in local/frontend/rtl_output")
class ControllerHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frontend = self.root / "frontend"
        for name in ("rtl_output", "microarch", "tb", "configs"):
            shutil.copytree(FRONTEND / name, self.frontend / name,
                            ignore=shutil.ignore_patterns("verification_reports", "__pycache__", "proposals"))
        self.manifest = self.frontend / "rtl_output/manifest.json"

    def test_manifest_refresh_preserves_rtl_and_testbench_bytes(self):
        with patch.object(sys, "path", [str(FRONTEND), *sys.path]):
            import generate_wrapper as wrapper
        inputs = [*self.frontend.rglob("*.sv")]
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
        with patch.multiple(wrapper, SCRIPT_DIR=self.frontend,
                            RTL_OUTPUT_DIR=self.frontend / "rtl_output",
                            GENERATED_BANK_INPUT_PATH=self.frontend / "microarch/gen_basic/ddr4_bank.yaml",
                            GENERATED_SCHEDULER_INPUT_PATH=self.frontend / "microarch/gen_basic/ddr4_scheduler.yaml",
                            USER_CONFIG_PATH=self.frontend / "configs/user_input.yaml",
                            SCHEDULER_IR_PATH=self.frontend / "microarch/ir/ddr4_scheduler_scheduler_ir.json"), \
             patch.object(sys, "argv", ["generate_wrapper.py", "--manifest-only", "--output",
                                        str(self.frontend / "rtl_output/ddr4_controller_top.sv")]):
            self.assertEqual(wrapper.main(), 0)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs})
        discovery = discover_system(self.manifest)
        self.assertEqual(discovery["discovery_result"], "PASS")
        self.assertEqual(discovery["module_spec_count"], 9)

    def test_mailbox_contains_all_manifest_dependencies_and_relocates(self):
        snapshot = publish(self.frontend, self.root / "mailbox/revision_001")
        metadata = verify(snapshot)
        self.assertEqual(metadata["system_manifest"], "rtl/manifest.json")
        for name in ("microarch/gen_basic/ddr4_bank.yaml", "microarch/jedec/jedec_dictionary.yaml",
                     "microarch/ir/ddr4_request_queue_ir.json", "tb/tb_ddr4_controller_top.sv"):
            self.assertIn(name, metadata["files"])
        shutil.rmtree(self.frontend)
        relocated = self.root / "moved snapshot"
        shutil.move(snapshot, relocated)
        self.assertEqual(discover_system(relocated / "rtl/manifest.json")["discovery_result"], "PASS")
        self.assertEqual(verify(relocated), metadata)
        (relocated / "tb/tb_ddr4_controller_top.sv").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify(relocated)

    def test_missing_reference_stops_publication_without_partial_revision(self):
        (self.frontend / "tb/tb_ddr4_controller_top.sv").unlink()
        destination = self.root / "mailbox/revision_001"
        with self.assertRaisesRegex(ValueError, "Missing system input"):
            publish(self.frontend, destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
