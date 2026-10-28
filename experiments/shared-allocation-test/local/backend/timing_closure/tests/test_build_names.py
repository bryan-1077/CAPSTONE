import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.build_names import full_flow_names, reserve_full_flow


class LocalSSH:
    def run(self, command, *, cwd):
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        return {"ok": result.returncode == 0, "stderr": result.stderr, "stdout": result.stdout}


class BuildNameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("prepare_rtl_for_genus_universal.py", "run_genus_netlist_universal.py", "run_genus_mapped_universal.py", "run_innovus_GDSII_universal.py"):
            (self.root / name).write_text(f"# source {name}\n")
        self.state = {**full_flow_names(4.762), "remote_project_root": str(self.root), "remote_python_cmd": sys.executable}

    def test_reserves_builds_and_copies_named_scripts(self):
        reserve_full_flow(LocalSSH(), self.state)
        self.assertTrue((self.root / "build_mapped_210MHz_01").is_dir())
        self.assertEqual(
            (self.root / self.state["remote_mapped_script"]).read_text(),
            (self.root / "run_genus_mapped_universal.py").read_text(),
        )

    def test_second_run_cannot_overwrite_first(self):
        reserve_full_flow(LocalSSH(), self.state)
        artifact = self.root / self.state["remote_mapped_dir"] / "keep.txt"
        artifact.write_text("preserved")
        reserve_full_flow(LocalSSH(), self.state)
        self.assertEqual(self.state["remote_mapped_dir"], "build_mapped_210MHz_02")
        self.assertTrue((self.root / self.state["remote_mapped_script"]).is_file())
        self.assertEqual(artifact.read_text(), "preserved")

    def test_preexisting_build_is_not_modified(self):
        (self.root / self.state["remote_netlist_dir"]).mkdir()
        reserve_full_flow(LocalSSH(), self.state)
        self.assertEqual(self.state["remote_netlist_dir"], "build_netlist_210MHz_02")
        self.assertEqual(list((self.root / "build_netlist_210MHz_01").iterdir()), [])

    def test_existing_scripts_and_gdsii_are_skipped(self):
        (self.root / "scripts_210MHz_01").mkdir()
        (self.root / "build_GDSII_210MHz_02").mkdir()
        reserve_full_flow(LocalSSH(), self.state)
        self.assertEqual(self.state["remote_gdsii_dir"], "build_GDSII_210MHz_03")
        self.assertFalse((self.root / self.state["remote_gdsii_dir"]).exists())

    def test_concurrent_runs_reserve_distinct_names(self):
        states = [dict(self.state) for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda state: reserve_full_flow(LocalSSH(), state), states))
        self.assertEqual(len({state["remote_prepare_dir"] for state in states}), 4)
        for state in states:
            self.assertEqual(
                (self.root / state["gdsii_script"]).read_text(),
                (self.root / "run_innovus_GDSII_universal.py").read_text(),
            )

    def test_missing_source_does_not_reserve_directory(self):
        (self.root / "run_innovus_GDSII_universal.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "Cannot reserve"):
            reserve_full_flow(LocalSSH(), self.state)
        self.assertFalse((self.root / "scripts_210MHz_01").exists())

    def test_220mhz_names(self):
        names = full_flow_names(4.545)
        self.assertEqual(names["remote_mapped_dir"], "build_mapped_220MHz_01")
        self.assertEqual(names["remote_prepare_script"], "scripts_220MHz_01/prepare_rtl_for_genus_220MHz_01.py")


if __name__ == "__main__":
    unittest.main()
