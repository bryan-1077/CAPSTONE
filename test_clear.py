"""Exercise destructive cleanup only inside disposable workspaces."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ClearTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clear-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo with spaces"
        self.root.mkdir()
        shutil.copy2(Path(__file__).with_name("clear.sh"), self.root / "clear.sh")

    def create(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test data\n")
        return path

    def run_clear(self, *args):
        return subprocess.run(["bash", str(self.root / "clear.sh"), *args],
                              cwd=self.temp.name, capture_output=True, text=True)

    def test_cleanup_preserves_sources_and_is_repeatable(self):
        removed = [self.create(name) for name in (
            "rtl_output/design.sv", "expanded/design.yaml", "inputs/generated/design.yaml",
            "tb/design.sv", "obj_dir/simulator", "__pycache__/module.pyc", ".llm_cache/design.sv",
            "flow.log", "flow_lint.log", "bist.log", "ir/design_ir.json", "wave.vcd", "wave.fst",
            "debug/failures/0007_test/result/graph_checkpoint.json",
            "debug/failures/10000_test/attempts/attempt_01/patch.diff")]
        preserved = [self.create(name) for name in (
            "inputs/source.yaml", "configs/user_input.yaml", "jedec/source.yaml", "ir/notes.json",
            "debug/failures/.gitkeep", "debug/failures/notes/readme.md",
            "debug/fixtures/report.json", "debug/schemas/.gitkeep", "debug/templates/.gitkeep",
            "gtk_presets/view.gtkw", "debug_agent.py", ".git/config")]
        preview = self.run_clear("--dry-run")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(all(path.exists() for path in removed + preserved))
        self.assertIn("10000_test", preview.stdout)
        result = self.run_clear()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(not path.exists() for path in removed))
        self.assertTrue(all(path.exists() for path in preserved))
        self.assertEqual(self.run_clear().returncode, 0)

    def test_symlinked_parents_refused_before_any_deletion(self):
        for parent, artifact in (("inputs", "generated/file"), ("ir", "test_ir.json"),
                                 ("debug", "failures/0001_run/file"),
                                 ("debug/failures", "0001_run/file")):
            with self.subTest(parent=parent):
                external = Path(self.temp.name) / ("external-" + parent.replace("/", "-"))
                target = external / artifact
                target.parent.mkdir(parents=True)
                target.write_text("keep\n")
                link = self.root / parent
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(external, target_is_directory=True)
                rtl = self.create("rtl_output/design.sv")
                for args in ((), ("--dry-run",)):
                    result = self.run_clear(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("symlinked parent", result.stderr)
                    self.assertTrue(target.exists())
                    self.assertTrue(rtl.exists())
                link.unlink()

    def test_final_symlink_removed_without_following_it(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        target = external / "keep.sv"
        target.write_text("keep\n")
        (self.root / "rtl_output").symlink_to(external, target_is_directory=True)
        result = self.run_clear()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "rtl_output").is_symlink())
        self.assertTrue(target.exists())

    def test_invalid_argument_does_not_delete(self):
        rtl = self.create("rtl_output/design.sv")
        self.assertEqual(self.run_clear("--unknown").returncode, 2)
        self.assertTrue(rtl.exists())


if __name__ == "__main__":
    unittest.main()
