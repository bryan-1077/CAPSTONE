import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class FillerOrderTests(unittest.TestCase):
    def generate(self, actions=None, no_filler=False):
        root = Path(__file__).resolve().parents[2]
        fixture = root / "timing_closure" / "fixtures"
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(root / "probes/run_innovus_GDSII_universal.py"),
                       "--mappeddir", str(fixture / "minimal_mapped"), "--outdir", directory,
                       "--pdk-root", str(fixture / "minimal_pdk"),
                       "--final-postroute-setup-opt", "--setup-only"]
            if actions is not None:
                command += ["--timing-eco-json", json.dumps({"checkpoint": "/project/best/db/06_final.enc", "actions": actions})]
            if no_filler:
                command.append("--no-filler")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return (Path(directory) / "tcl/run_innovus.tcl").read_text()

    def assert_final_checks(self, script):
        checks = ["addFiller -cell", "ecoRoute -target", "checkDesign -all} check_final_err",
                  "verify_drc} drc_verify_err", "report_timing -max_paths", "report_power -power_unit W",
                  "report_timing -early", 'puts "INFO: entering final export stage"']
        positions = [script.index(text) for text in checks]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("optDesign ", script[script.index("addFiller -cell"):])

    def test_fresh_flow_optimizes_before_filling(self):
        script = self.generate()
        self.assertLess(script.index("optDesign -postRoute -setup"), script.index("addFiller -cell"))
        self.assert_final_checks(script)

    def test_restore_without_resizes_still_removes_fillers_and_repairs_hold(self):
        script = self.generate([])
        self.assertLess(script.index("deleteFiller -prefix FILL"), script.index("optDesign -postRoute -setup"))
        self.assertLess(script.index("optDesign -postRoute -setup"), script.index("optDesign -postRoute -hold"))
        self.assertLess(script.index("optDesign -postRoute -hold"), script.index("addFiller -cell"))
        self.assert_final_checks(script)

    def test_resize_removes_only_designated_fillers_before_editing(self):
        script = self.generate([{"instance": "gate1", "from_cell": "sky130_fd_sc_hd__inv_2",
                                 "to_cell": "sky130_fd_sc_hd__inv_4", "reason": "test"}])
        self.assertLess(script.index("deleteFiller -prefix FILL"), script.index("ecoChangeCell -inst"))
        self.assertLess(script.index("optDesign -postRoute -hold"), script.index("addFiller -cell"))
        self.assertEqual([line for line in script.splitlines() if line.startswith("delete")], ["deleteFiller -prefix FILL"])
        self.assert_final_checks(script)

    def test_explicit_no_filler_keeps_final_checks(self):
        script = self.generate([], no_filler=True)
        self.assertNotIn("addFiller -cell", script)
        self.assertLess(script.index("optDesign -postRoute -hold"), script.index("verify_drc} drc_verify_err"))
        self.assertIn("report_timing -early", script)
