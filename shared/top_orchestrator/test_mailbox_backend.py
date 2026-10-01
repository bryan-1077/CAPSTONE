"""Snapshot and subprocess boundaries, without SSH, models, or EDA tools."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from shared.top_orchestrator.contracts import RunConfig, StageResult, FailureReport
from shared.top_orchestrator.mailbox import publish, verify
from shared.top_orchestrator.nodes import backend_node
from shared.top_orchestrator.orchestrator import run


class MailboxBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.frontend = self.root / "frontend"
        rtl = self.frontend / "rtl_output"
        (rtl / "inc").mkdir(parents=True)
        (rtl / "top.sv").write_text("module top; endmodule")
        (rtl / "inc/defs.svh").write_text("// dependency")
        (rtl / "manifest.json").write_text(json.dumps({"top_module": "top", "modules": ["top"]}))
        specs = self.frontend / "expanded/unit"
        specs.mkdir(parents=True)
        (specs / "top.yaml").write_text("design_name: top\n")
        (specs / "unused.yaml").write_text("design_name: unused\n")
        self.mailbox = publish(self.frontend, self.root / "mailbox/revision_001")

    def test_snapshot_preserves_inputs_and_detects_changes(self):
        verify(self.mailbox)
        self.assertTrue((self.mailbox / "rtl/inc/defs.svh").is_file())
        self.assertTrue((self.mailbox / "specs/unit/top.yaml").is_file())
        self.assertFalse((self.mailbox / "specs/unit/unused.yaml").exists())
        (self.frontend / "rtl_output/top.sv").write_text("repaired")
        self.assertEqual((self.mailbox / "rtl/top.sv").read_text(), "module top; endmodule")
        second = publish(self.frontend, self.root / "mailbox/revision_002")
        self.assertNotEqual(verify(self.mailbox)["rtl_revision"], verify(second)["rtl_revision"])
        with self.assertRaises(FileExistsError):
            publish(self.frontend, self.mailbox)
        (self.mailbox / "specs/unit/top.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify(self.mailbox)

    def test_symlinks_and_missing_matching_specs_are_rejected(self):
        link = self.frontend / "rtl_output/link.sv"
        link.symlink_to(self.frontend / "rtl_output/top.sv")
        destination = self.root / "bad"
        with self.assertRaises(ValueError):
            publish(self.frontend, destination)
        self.assertFalse(destination.exists())
        link.unlink()
        (self.frontend / "expanded/unit/top.yaml").write_text("design_name: other\n")
        with self.assertRaisesRegex(ValueError, "No expanded YAML"):
            publish(self.frontend, destination)

    def test_microarch_specs_take_precedence_and_exclude_masters(self):
        specs = self.frontend / "microarch/gen_exp"
        specs.mkdir(parents=True)
        (specs / "top.yaml").write_text("design_name: top\nsource: microarch\n")
        (specs / "top_master.yaml").write_text("design_name: top\n")
        (specs / "unused.yaml").write_text("design_name: unused\n")
        destination = publish(self.frontend, self.root / "migrated")
        verify(destination)
        self.assertEqual(list((destination / "specs").iterdir()), [destination / "specs/top.yaml"])
        self.assertEqual((destination / "specs/top.yaml").read_bytes(), (specs / "top.yaml").read_bytes())

    def test_incomplete_microarch_does_not_fall_back_to_legacy(self):
        (self.frontend / "microarch").mkdir()
        destination = self.root / "incomplete"
        with self.assertRaisesRegex(ValueError, "directory is missing"):
            publish(self.frontend, destination)
        self.assertFalse(destination.exists())

    def test_microarch_spec_symlinks_are_rejected(self):
        microarch = self.frontend / "microarch"
        microarch.mkdir()
        (microarch / "gen_exp").symlink_to(self.frontend / "expanded", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            publish(self.frontend, self.root / "linked")

    def config(self):
        return RunConfig("backend", mailbox=self.mailbox, target_mhz=200, allow_unvalidated=True)

    def payload(self):
        return {"current_stage": "done", "mailbox_revision": verify(self.mailbox)["rtl_revision"],
                "timing_target_clock_period_ns": 5.0, "timing_closure_enabled": True,
                "timing_closure_status": "passed", "stage_status": dict.fromkeys(
                    ("rtl_prep", "netlist", "mapped_netlist", "gdsii"), "success")}

    def test_backend_subprocess_and_result_checks(self):
        payload = self.payload()
        cases = [(payload, 0, "passed"), (payload, 1, "failed"),
                 ({**payload, "mailbox_revision": "stale"}, 0, "failed"),
                 ({**payload, "timing_target_clock_period_ns": 4}, 0, "failed"),
                 ({**payload, "stage_status": {"rtl_prep": "success"}}, 0, "failed"),
                 ({**payload, "timing_closure_status": "error"}, 0, "failed"),
                 ({**payload, "current_stage": "failed"}, 0, "failed"),
                 (None, 0, "failed")]
        for index, (result, code, expected) in enumerate(cases):
            with self.subTest(index=index):
                output = self.root / f"run{index}"
                output.mkdir()
                script = ("import sys, pathlib\n"
                          "print('fake backend progress')\n"
                          "path = pathlib.Path(sys.argv[sys.argv.index('--result-json') + 1])\n"
                          f"path.write_text({json.dumps(result)!r})\n"
                          f"sys.exit({code})\n")
                with patch("shared.top_orchestrator.nodes.backend_command", return_value=[sys.executable, "-c", script]):
                    stage = backend_node(self.config(), output)
                self.assertEqual(stage.status, expected)
                self.assertEqual(stage.artifacts["validation"], "not_run")
                self.assertIn("fake backend progress", (output / "backend.log").read_text())
                self.assertIsNone(stage.failure)

    def test_backend_gate_and_missing_result(self):
        with patch("shared.top_orchestrator.nodes.stream_command", return_value=0) as command:
            self.assertEqual(backend_node(replace(self.config(), allow_unvalidated=False), self.root).status,
                             "needs_attention")
            command.assert_not_called()
            self.assertEqual(backend_node(self.config(), self.root).status, "failed")

    def test_repair_publishes_new_revision_and_preserves_target(self):
        seen = []
        evidence = self.root / "failure.log"
        evidence.write_text("failure")
        def validation(config, directory):
            seen.append(config)
            if len(seen) == 1:
                return StageResult("validation", "failed", "test", failure=FailureReport("verif", evidence))
            return StageResult("validation", "not_implemented", "deferred")
        def debug(config, directory):
            (self.frontend / "rtl_output/top.sv").write_text("module top; wire repaired; endmodule")
            return StageResult("debug", "passed", "repaired")
        with patch("shared.top_orchestrator.orchestrator.FRONTEND", self.frontend), patch(
                "shared.top_orchestrator.orchestrator.REPO_ROOT", self.root), patch.dict(
                "shared.top_orchestrator.orchestrator.NODES", {
                    "generate": lambda c, d: StageResult("generate", "passed", "checks passed"),
                    "validation": validation, "debug": debug}):
            state = run(RunConfig("generate", recheck_only=True, target_mhz=200), self.root / "run")
        self.assertEqual([c.target_mhz for c in seen], [200, 200])
        self.assertNotEqual(seen[0].mailbox, seen[1].mailbox)
        self.assertNotEqual(verify(seen[0].mailbox)["rtl_revision"], verify(seen[1].mailbox)["rtl_revision"])
        self.assertEqual(state.config.mailbox, seen[1].mailbox)


if __name__ == "__main__":
    unittest.main()
