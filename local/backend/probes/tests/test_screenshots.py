import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[4]
RUNNERS = [ROOT / "local/backend/probes/run_innovus_GDSII_universal.py",
           ROOT / "local/backend/server_scripts/run_innovus_GDSII_universal.py"]


def load_runner(path):
    spec = importlib.util.spec_from_file_location("runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_bytes():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    rows = b"".join(b"\x00" + bytes([i, 100, 200]) * 64 for i in range(64))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter required")
class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Real Tcl and subprocesses exercise argument quoting and the generated loader.
        self.out = self.root / 'design $literal [brackets] "quoted"'
        (self.out / "db").mkdir(parents=True)
        self.fixture = self.root / "fixture.png"
        self.fixture.write_bytes(png_bytes())
        self.shim = self.root / "shim.tcl"
        self.shim.write_text('''
proc setDrawView {view} {
    global restored
    if {![info exists restored]} {error "checkpoint was not restored"}
    if {$view ni {fplan place physical}} {error "bad view"}
}
proc dbGet {args} {return {met1 met2}}
proc setLayerPreference {args} {}
proc deselectAll {} {}
proc fit {} {}
proc redraw {} {}
proc dumpToPNG {image} {file copy -force $::env(SCREENSHOT_FIXTURE) $image}
source [lindex $argv 0]
''')
        self.innovus = self.root / "fake_innovus"
        self.innovus.write_text('''#!/usr/bin/env python3
import os, subprocess, sys
script = sys.argv[sys.argv.index('-files') + 1]
raise SystemExit(subprocess.call(['tclsh', os.environ['SCREENSHOT_SHIM'], script]))
''')
        self.innovus.chmod(0o755)
        self.env = patch.dict(os.environ, {"DISPLAY": ":test", "SCREENSHOT_FIXTURE": str(self.fixture),
                                          "SCREENSHOT_SHIM": str(self.shim)})
        self.env.start()
        self.addCleanup(self.env.stop)
        for _, name, _ in load_runner(RUNNERS[0]).SCREENSHOT_STAGES:
            (self.out / "db" / name).write_text("set restored 1\n")
            (self.out / "db" / (name + ".dat")).mkdir()

    def manifest(self):
        return json.loads((self.out / "results/screenshots/manifest.json").read_text())

    def recovery_build(self, name, parent):
        module = load_runner(RUNNERS[0])
        build = self.root / name
        (build / "db").mkdir(parents=True)
        (build / "db/06_final.enc").write_text("set restored 1\n")
        (build / "db/06_final.enc.dat").mkdir()
        module.write_screenshot_lineage(build, parent / "db/06_final.enc")
        return build

    def test_recovery_exports_ancestor_stages_and_its_own_routing(self):
        first = self.recovery_build("first_recovery", self.out)
        second = self.recovery_build("second_recovery", first)
        for path in RUNNERS:
            with self.subTest(runner=path):
                self.assertTrue(load_runner(path).export_screenshots(second, self.innovus, "display"))
                manifest = json.loads((second / "results/screenshots/manifest.json").read_text())
                floorplan, placement, routing = manifest["images"]
                for entry in (floorplan, placement):
                    self.assertEqual(entry["source_build"], str(self.out))
                    self.assertEqual(entry["origin"], "ancestor_build")
                self.assertEqual(routing["source_build"], str(second))
                self.assertEqual(routing["origin"], "current_build")

    def test_older_recovery_finds_source_in_generated_tcl(self):
        build = self.recovery_build("old_recovery", self.out)
        (build / "screenshot_lineage.json").unlink()
        (build / "tcl").mkdir()
        (build / "tcl/run_innovus.tcl").write_text("set eco_checkpoint {" + str(self.out / "db/06_final.enc") + "}\n")
        self.assertTrue(load_runner(RUNNERS[0]).export_screenshots(build, self.innovus, "display"))

    def test_explicit_source_option_recovers_build_without_lineage(self):
        build = self.recovery_build("no_lineage", self.out)
        (build / "screenshot_lineage.json").unlink()
        result = subprocess.run(["python3", str(RUNNERS[1]), "--screenshots-only", "--outdir", str(build),
                                 "--innovus", str(self.innovus), "--screenshots", "display",
                                 "--screenshot-source-build", str(self.out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_routing_never_uses_ancestor_checkpoint(self):
        module = load_runner(RUNNERS[0])
        build = self.recovery_build("missing_route", self.out)
        (build / "db/06_final.enc").unlink()
        self.assertFalse(module.export_screenshots(build, self.innovus, "display"))
        manifest = json.loads((build / "results/screenshots/manifest.json").read_text())
        self.assertEqual([i["status"] for i in manifest["images"]], ["success", "success", "failed"])

    def test_cycle_in_lineage_fails_without_guessing_another_build(self):
        module = load_runner(RUNNERS[0])
        first = self.recovery_build("cycle_first", self.out)
        second = self.recovery_build("cycle_second", first)
        module.write_screenshot_lineage(first, second / "db/06_final.enc")
        self.assertFalse(module.export_screenshots(first, self.innovus, "display"))
        manifest = json.loads((first / "results/screenshots/manifest.json").read_text())
        self.assertIn("Cycle", manifest["images"][0]["error"])

    def test_startup_error_is_preserved_in_manifest(self):
        self.innovus.write_text("#!/usr/bin/env python3\nimport sys\nprint('Cannot open display: stale SSH DISPLAY', file=sys.stderr)\nraise SystemExit(1)\n")
        self.assertFalse(load_runner(RUNNERS[0]).export_screenshots(self.out, self.innovus, "display"))
        for entry in self.manifest()["images"]:
            self.assertIn("code 1", entry["error"])
            self.assertIn("Cannot open display", entry["log_tail"])
            self.assertEqual(entry["command"][0], str(self.innovus))

    def test_tcl_error_includes_command_and_stack_trace(self):
        self.shim.write_text('source [lindex $argv 0]\n')
        self.assertFalse(load_runner(RUNNERS[0]).export_screenshots(self.out, self.innovus, "display"))
        self.assertIn('invalid command name "setDrawView"', self.manifest()["images"][0]["log_tail"])

    def test_both_runners_export_all_stages_using_real_tcl(self):
        for path in RUNNERS:
            with self.subTest(runner=path):
                self.assertTrue(load_runner(path).export_screenshots(self.out, self.innovus, "display"))
                result = self.manifest()
                self.assertEqual(result["status"], "success")
                self.assertEqual([i["stage"] for i in result["images"]], ["floorplan", "placement", "routing"])
                self.assertTrue(all(i["width"] == 64 and i["height"] == 64 for i in result["images"]))

    def test_missing_checkpoint_does_not_block_other_stages_or_accept_stale_png(self):
        module = load_runner(RUNNERS[0])
        self.assertTrue(module.export_screenshots(self.out, self.innovus, "display"))
        (self.out / "db/03_place.enc").unlink()
        self.assertFalse(module.export_screenshots(self.out, self.innovus, "display"))
        self.assertEqual([i["status"] for i in self.manifest()["images"]], ["success", "failed", "success"])
        self.assertFalse((self.out / "results/screenshots/placement.png").exists())

    def test_exporter_failure_is_reported(self):
        self.shim.write_text('exit 9\n')
        self.assertFalse(load_runner(RUNNERS[0]).export_screenshots(self.out, self.innovus, "display"))
        self.assertTrue(all("code 9" in i["error"] for i in self.manifest()["images"]))

    def test_no_display_and_no_xvfb_reports_actionable_failure(self):
        module = load_runner(RUNNERS[0])
        with patch.dict(os.environ, {"DISPLAY": ""}), patch.object(module.shutil, "which", return_value=None):
            self.assertFalse(module.export_screenshots(self.out, self.innovus))
        self.assertTrue(all("xvfb-run" in i["error"] for i in self.manifest()["images"]))

    def test_xvfb_launch_has_graphics_and_resolution(self):
        module = load_runner(RUNNERS[0])
        real_popen = subprocess.Popen
        commands = []
        def launch(command, **kwargs):
            commands.append(command)
            return real_popen(command[4:], **kwargs)
        with patch.object(module.shutil, "which", return_value="xvfb-run"), patch.object(module.subprocess, "Popen", side_effect=launch):
            self.assertTrue(module.export_screenshots(self.out, self.innovus, "xvfb"))
        self.assertTrue(all(c[:5] == ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", str(self.innovus)] for c in commands))

    def test_auto_prefers_xvfb_even_with_inherited_display(self):
        module = load_runner(RUNNERS[0])
        real_popen = subprocess.Popen
        commands = []
        def launch(command, **kwargs):
            commands.append(command)
            return real_popen(command[4:], **kwargs)
        with patch.object(module.shutil, "which", return_value="xvfb-run"), patch.object(module.subprocess, "Popen", side_effect=launch):
            self.assertTrue(module.export_screenshots(self.out, self.innovus, "auto"))
        self.assertEqual(self.manifest()["display_mode"], "xvfb")
        self.assertTrue(all(c[0] == "xvfb-run" for c in commands))

    def test_timeout_terminates_process_group(self):
        module = load_runner(RUNNERS[0])
        with patch.object(module.subprocess, "Popen") as popen, patch("os.killpg") as kill:
            popen.return_value.pid = 123456
            popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("innovus", 1), 0] * 3
            self.assertFalse(module.export_screenshots(self.out, self.innovus, "display", 1))
        self.assertEqual(kill.call_count, 3)
        self.assertTrue(all("timed out" in i["error"] for i in self.manifest()["images"]))

    def test_corrupt_png_is_rejected(self):
        data = bytearray(png_bytes())
        data[-6] ^= 1
        self.fixture.write_bytes(data)
        with self.assertRaises(ValueError):
            load_runner(RUNNERS[0]).screenshot_png_size(self.fixture)

    def test_screenshots_only_cli_needs_no_mapped_inputs_or_pdk(self):
        for path in RUNNERS:
            with self.subTest(runner=path):
                result = subprocess.run(["python3", str(path), "--screenshots-only", "--outdir", str(self.out),
                                         "--innovus", str(self.innovus), "--screenshots", "display"],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.out / "db/03_place.enc").unlink()
        result = subprocess.run(["python3", str(RUNNERS[1]), "--screenshots-only", "--outdir", str(self.out),
                                 "--innovus", str(self.innovus), "--screenshots", "display"], capture_output=True)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
