"""Run names follow the selected clock while run-local inputs remain usable."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .contracts import RunConfig, StageResult
from .orchestrator import run
from .run_paths import run_name


class RunPathTests(unittest.TestCase):
    def test_names_include_frequency_date_and_time(self):
        self.assertRegex(run_name(220.5), r"^internal_220\.5MHz_\d{8}_\d{6}_\d{6}$")

    def test_selected_frequency_renames_default_run_before_stages(self):
        for auto_name in (True, False):
            with self.subTest(auto_name=auto_name), tempfile.TemporaryDirectory() as temporary:
                original = Path(temporary) / "internal_unknown_frequency_20261102_143025_123456"
                expected = original.with_name("internal_220.5MHz_20261102_143025_123456") if auto_name else original

                def prepare(config, directory):
                    snapshot = directory / "approved_specs.yaml"
                    snapshot.write_text("design_name: controller\n")
                    intake = directory / "intake.json"
                    intake.write_text("{}")
                    return replace(config, input_yaml=snapshot, intake=intake, target_mhz=220.5)

                def generate(config, directory):
                    self.assertEqual(directory, expected)
                    self.assertEqual(config.input_yaml, expected / "approved_specs.yaml")
                    self.assertTrue(config.input_yaml.is_file())
                    self.assertTrue(config.intake.is_file())
                    return StageResult("generate", "failed", "stop after inspecting paths")

                def close(session):
                    self.assertEqual(session.directory, expected)

                with patch("shared.top_orchestrator.orchestrator.prepare_generation", side_effect=prepare), patch(
                        "shared.top_orchestrator.orchestrator.prepare_password"), patch.dict(
                        "shared.top_orchestrator.orchestrator.NODES", {"generate": generate}), patch(
                        "shared.top_orchestrator.allocation.AllocationSession.close", autospec=True, side_effect=close):
                    state = run(RunConfig("generate", shared_allocation=True), original, auto_name=auto_name)
                self.assertEqual(state.status, "failed")
                saved = json.loads((expected / "state.json").read_text())
                self.assertEqual(saved["config"]["input_yaml"], str(expected / "approved_specs.yaml"))
                if auto_name:
                    self.assertFalse(original.exists())
