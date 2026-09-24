import inspect
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.mapped_snapshot import snapshot_mapped
from probes.run_innovus_GDSII_universal import build_innovus_tcl


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "results").mkdir(parents=True)
        (self.source / "results" / "top_mapped.v").write_text("module top; endmodule\n")
        (self.source / "results" / "top_mapped.sdc").write_text("create_clock -period 4.762 [get_ports clk]\n")
        self.dest = self.root / "snapshot"

    def test_snapshot_survives_source_overwrite(self):
        snapshot_mapped(self.source, self.dest)
        (self.source / "results" / "top_mapped.v").write_text("replacement")
        self.assertEqual((self.dest / "results" / "top_mapped.v").read_text(), "module top; endmodule\n")

    def test_changes_during_copy_are_rejected(self):
        copy = shutil.copytree
        def racing_copy(*args, **kwargs):
            result = copy(*args, **kwargs)
            (self.source / "results" / "top_mapped.v").write_text("concurrent replacement")
            return result
        with patch("services.mapped_snapshot.shutil.copytree", side_effect=racing_copy):
            with self.assertRaisesRegex(RuntimeError, "changed while copying"):
                snapshot_mapped(self.source, self.dest)

    def test_running_producer_is_rejected(self):
        (self.source / "agent_mapped_progress.log").write_text("mapped still running\n")
        with self.assertRaisesRegex(RuntimeError, "still running"):
            snapshot_mapped(self.source, self.dest)
        self.assertFalse(self.dest.exists())

    def test_completed_producer_is_accepted(self):
        (self.source / "agent_mapped_progress.log").write_text("EDA_MAPPED_RUN_COMPLETE:0\n")
        snapshot_mapped(self.source, self.dest)
        self.assertTrue((self.dest / "mapped_snapshot.json").exists())

    def test_existing_snapshot_cannot_be_overwritten(self):
        snapshot_mapped(self.source, self.dest)
        with self.assertRaises(FileExistsError):
            snapshot_mapped(self.source, self.dest)


@unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
class PostRouteTclTests(unittest.TestCase):
    def block(self):
        args = {name: 1 for name in inspect.signature(build_innovus_tcl).parameters}
        args.update(top="top", netlist=Path("mapped.v"), lef_files=[], mmmc_file=Path("mmmc.tcl"),
                    outdir=Path("out"), site="site", routing_layers=["met1", "met2"],
                    filler_cells=[], stream_map=None, pwr_pins=[], gnd_pins=[], antenna_diode_cell=None,
                    tiehi_net=None, tielo_net=None, final_postroute_setup_opt=True, timing_eco_plan=None)
        script = build_innovus_tcl(**args)
        start = script.index('puts "INFO: configuring OCV')
        end = script.index('if {[catch {report_timing', start)
        return script[start:end]

    def test_ocv_is_set_before_optimization_executes(self):
        stub = '''
set analysis single
proc setAnalysisMode {flag value} {set ::analysis $value}
proc optDesign {args} {
    if {$::analysis ne "onChipVariation"} {error "IMPOPT-6080"}
    puts "OPTIMIZATION_EXECUTED"
}
'''
        result = subprocess.run(["tclsh"], input=stub + self.block(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPTIMIZATION_EXECUTED", result.stdout)

    def test_analysis_configuration_failure_stops_optimization(self):
        stub = '''
proc setAnalysisMode {args} {error "configuration failed"}
proc optDesign {args} {puts "SHOULD_NOT_RUN"}
'''
        result = subprocess.run(["tclsh"], input=stub + self.block(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 4)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)


if __name__ == "__main__":
    unittest.main()
