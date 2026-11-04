import inspect
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from probes.run_innovus_GDSII_universal import (
    auto_find_stdcell_gds, build_innovus_tcl, stream_out_command,
)


class GDSLibraryTests(unittest.TestCase):
    def test_selected_pdk_library_is_used_and_combined_file_is_preferred(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "libs.ref/sky130_fd_sc_hd/gds"
            folder.mkdir(parents=True)
            combined = folder / "sky130_fd_sc_hd.gds"
            combined.write_bytes(b"combined")
            (folder / "other.gds").write_bytes(b"split cell")
            self.assertEqual(auto_find_stdcell_gds(root, "sky130_fd_sc_hd"), [combined])

    def test_split_cell_libraries_are_all_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "libs.ref/sky130_fd_sc_hd/gds"
            folder.mkdir(parents=True)
            paths = [folder / "a.gds", folder / "b.gdsii"]
            for path in paths:
                path.write_bytes(b"cell")
            (folder / "README").write_text("ignore")
            self.assertEqual(auto_find_stdcell_gds(root, "sky130_fd_sc_hd"), paths)
            with self.assertRaisesRegex(SystemExit, "No standard-cell GDS layouts"):
                auto_find_stdcell_gds(root, "sky130_fd_sc_hs")

    def test_missing_library_cannot_silently_export_references_only(self):
        with self.assertRaisesRegex(ValueError, "requires standard-cell GDS libraries"):
            stream_out_command("top", None, [])


@unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
class MergedExportTclTests(unittest.TestCase):
    def export_block(self):
        args = {name: 1 for name in inspect.signature(build_innovus_tcl).parameters}
        args.update(top="top", netlist=Path("mapped.v"), lef_files=[], mmmc_file=Path("mmmc.tcl"),
                    outdir=Path("out"), site="site", routing_layers=["met1", "met2"], filler_cells=[],
                    stream_map=None, pwr_pins=[], gnd_pins=[], antenna_diode_cell=None,
                    tiehi_net=None, tielo_net=None, timing_eco_plan=None, physical_repair_plan=None,
                    gds_libraries=[Path("/pdk with spaces/hd.gds"), Path("/pdk/extra.gds")])
        script = build_innovus_tcl(**args)
        start = script.index('puts "INFO: entering final export stage"')
        return script[start:script.index("saveDesign $DBS_DIR/06_final.enc", start)]

    def test_final_export_merges_both_libraries_with_correct_tcl_paths(self):
        stub = '''
set RESULTS_DIR out/results
proc defOut {args} {}
proc saveNetlist {args} {}
proc streamOut {file args} {
    set libs [lindex $args [expr {[lsearch -exact $args -merge] + 1}]]
    if {[llength $libs] != 2 || [lindex $libs 0] ne "/pdk with spaces/hd.gds"} {
        error "Incorrect merge library paths"
    }
    puts "MERGED:$file"
}
'''
        result = subprocess.run(["tclsh"], input=stub + self.export_block(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MERGED:out/results/top.gds", result.stdout)

    def test_failed_merge_stops_export(self):
        stub = '''
set RESULTS_DIR out/results
proc defOut {args} {}
proc saveNetlist {args} {}
proc streamOut {args} {error "Missing cell layout"}
'''
        result = subprocess.run(["tclsh"], input=stub + self.export_block(), text=True, capture_output=True)
        self.assertEqual(result.returncode, 8)
        self.assertIn("merged GDS streamOut failed: Missing cell layout", result.stdout)

    def test_map_file_and_merge_arguments_can_coexist(self):
        command = stream_out_command("top", Path("/pdk with spaces/stream.map"), [Path("/pdk/hd.gds")])
        stub = '''
set RESULTS_DIR out/results
proc streamOut {file args} {
    puts [lindex $args [expr {[lsearch -exact $args -mapFile] + 1}]]
    puts [lindex $args [expr {[lsearch -exact $args -merge] + 1}]]
}
'''
        result = subprocess.run(["tclsh"], input=stub + command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["/pdk with spaces/stream.map", "/pdk/hd.gds"])
