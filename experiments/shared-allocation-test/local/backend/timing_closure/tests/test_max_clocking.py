import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from timing_closure.max_clocking import run_max_clocking


def passing_state(mhz):
    return {
        "current_stage": "done",
        "stage_status": {"gdsii": "success"},
        "timing_closure_status": "passed",
        "timing_closure_analysis": {"wns": 0.012},
        "remote_gdsii_dir": f"build_GDSII_{mhz}MHz_02",
    }


class MaxClockingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name)
        self.stdout = io.StringIO()

    def run_sweep(self, runner):
        with contextlib.redirect_stdout(self.stdout):
            return run_max_clocking(runner, output_dir=self.output)

    def test_sequential_targets_stop_at_first_failure_and_preserve_best(self):
        calls = []

        def run(mhz):
            # Previous target must be finished and persisted before the next starts.
            summary_path = next(self.output.glob("*/summary.json"))
            saved = json.loads(summary_path.read_text())
            self.assertEqual(saved["best_mhz"], calls[-1] if calls else None)
            calls.append(mhz)
            print(f"Flow output at {mhz} MHz")
            if mhz == 260:
                return {"current_stage": "failed", "last_error": "Setup timing remains violated"}
            return passing_state(mhz)

        result = self.run_sweep(run)
        self.assertEqual(calls, [230, 240, 250, 260])
        self.assertEqual(result["best_mhz"], 250)
        self.assertEqual(result["first_failed_mhz"], 260)
        self.assertEqual(result["current_stage"], "done")
        self.assertEqual(json.loads(Path(result["best_state_path"]).read_text()), passing_state(250))
        report = Path(result["report_path"])
        self.assertIn("Highest passing frequency: 250 MHz", report.read_text())
        self.assertEqual(json.loads((report.parent / "summary.json").read_text()), result)
        for mhz in calls:
            self.assertIn(f"Flow output at {mhz} MHz", (report.parent / f"{mhz}MHz.log").read_text())
        self.assertEqual(result["attempts"][2]["gdsii_dir"], "build_GDSII_250MHz_02")
        self.assertIn("Best passing frequency: 250 MHz", self.stdout.getvalue())

    def test_first_failure_reports_no_success(self):
        runner = Mock(return_value={"current_stage": "failed", "last_error": "tool failed"})
        result = self.run_sweep(runner)
        runner.assert_called_once_with(230)
        self.assertIsNone(result["best_mhz"])
        self.assertIsNone(result["best_state_path"])
        self.assertEqual(result["current_stage"], "failed")
        self.assertEqual(result["first_failed_mhz"], 230)
        self.assertIn("No passing frequency found", self.stdout.getvalue())

    def test_completion_without_validated_timing_is_not_success(self):
        for updates in (
            {"timing_closure_status": "disabled"},
            {"stage_status": {"gdsii": "failed"}},
            {"current_stage": "failed"},
            {"last_error": "missing report"},
        ):
            with self.subTest(updates=updates):
                runner = Mock(return_value={**passing_state(230), **updates})
                result = self.run_sweep(runner)
                runner.assert_called_once_with(230)
                self.assertIsNone(result["best_mhz"])

    def test_exception_preserves_prior_success_and_records_error(self):
        runner = Mock(side_effect=[passing_state(230), RuntimeError("SSH disconnected")])
        result = self.run_sweep(runner)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(result["best_mhz"], 230)
        self.assertEqual(result["first_failed_mhz"], 240)
        self.assertIn("SSH disconnected", result["last_error"])
        failure = json.loads(Path(result["attempts"][-1]["state_path"]).read_text())
        self.assertEqual(failure["current_stage"], "failed")

    def test_missing_result_stops_sweep(self):
        runner = Mock(return_value=None)
        result = self.run_sweep(runner)
        runner.assert_called_once_with(230)
        self.assertIsNone(result["best_mhz"])
        self.assertIn("no usable result state", result["last_error"])


if __name__ == "__main__":
    unittest.main()
