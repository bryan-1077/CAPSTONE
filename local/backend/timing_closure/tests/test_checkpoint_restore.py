import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from probes.run_innovus_GDSII_universal import build_timing_eco_tcl


@unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
class CheckpointRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checkpoint = Path(self.temp.name) / "06_final.enc"
        self.database = Path(str(self.checkpoint) + ".dat")
        self.database.mkdir()
        self.checkpoint.write_text(
            'if {[is_common_ui_mode]} {\n'
            'read_db [file dirname [file normalize [info script]]]/06_final.enc.dat\n'
            '} else {\n'
            'restoreDesign [file dirname [file normalize [info script]]]/06_final.enc.dat top\n'
            '}\n')

    def run_restore(self, common=False):
        stub = """
proc is_common_ui_mode {} {return COMMON}
proc restoreDesign {path top} {
    if {![file isdirectory $path]} {error "Expected a database directory"}
    puts "RESTORED $path $top"
}
proc read_db {path} {
    if {![file isdirectory $path]} {error "Expected a database directory"}
    puts "READ_DB $path"
}
proc deleteFiller {args} {puts "REMOVED $args"}
proc eda_die_area_mm2 {} {return 0.2}
""".replace("COMMON", "1" if common else "0")
        script = "\n".join(build_timing_eco_tcl(
            {"checkpoint": str(self.checkpoint), "actions": []}, "top"))
        return subprocess.run(["tclsh"], input=stub + script, text=True, capture_output=True)

    def test_legacy_ui_restores_database_not_loader(self):
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"RESTORED {self.database} top", result.stdout)

    def test_common_ui_uses_generated_read_db(self):
        result = self.run_restore(common=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"READ_DB {self.database}", result.stdout)

    def test_missing_database_fails_before_restore(self):
        self.database.rmdir()
        result = self.run_restore()
        self.assertEqual(result.returncode, 6)
        self.assertIn("Checkpoint database is missing", result.stdout)
        self.assertNotIn("RESTORED", result.stdout)

    def test_missing_loader_fails_before_restore(self):
        self.checkpoint.unlink()
        result = self.run_restore()
        self.assertEqual(result.returncode, 6)
        self.assertIn("Checkpoint restore script is missing", result.stdout)
        self.assertNotIn("RESTORED", result.stdout)
