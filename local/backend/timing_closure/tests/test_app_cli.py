import contextlib
import io
import tempfile
import unittest
from unittest.mock import patch

import app
from timing_closure.max_clocking import run_max_clocking


class AppCLITests(unittest.TestCase):
    def run_app(self, argv, env=None):
        with (
            patch.dict("os.environ", env or {}, clear=True),
            patch.object(app, "_load_service_exports"),
            patch.object(app, "run_flow", return_value={}) as run,
        ):
            app.main(app._parse_args(argv))
        return run.call_args.kwargs["initial_state"]

    def test_mhz_controls_period_and_build_names(self):
        state = self.run_app(["--target-mhz", "230"])
        self.assertAlmostEqual(state["timing_target_clock_period_ns"], 1000 / 230)
        self.assertEqual(state["remote_prepare_dir"], "build_prep_230MHz_01")
        self.assertEqual(state["remote_gdsii_dir"], "build_GDSII_230MHz_01")
        self.assertFalse(state["timing_use_overconstraint"])

    def test_explicit_target_overrides_environment(self):
        state = self.run_app(["--target-mhz=230.5"], {
            "EDA_TIMING_TARGET_CLOCK_PERIOD_NS": "5",
            "EDA_TIMING_USE_OVERCONSTRAINT": "1",
            "EDA_TIMING_OVERCONSTRAINT_CLOCK_PERIOD_NS": "4",
        })
        self.assertAlmostEqual(state["timing_target_clock_period_ns"], 1000 / 230.5)
        self.assertFalse(state["timing_use_overconstraint"])

    def test_no_flags_preserves_environment(self):
        state = self.run_app([], {
            "EDA_TIMING_TARGET_CLOCK_PERIOD_NS": "5",
            "EDA_TIMING_USE_OVERCONSTRAINT": "1",
            "EDA_TIMING_OVERCONSTRAINT_CLOCK_PERIOD_NS": "4.5",
        })
        self.assertEqual(state["timing_target_clock_period_ns"], 5)
        self.assertEqual(state["timing_overconstraint_clock_period_ns"], 4.5)
        self.assertTrue(state["timing_use_overconstraint"])

    def test_explicit_overconstraint_and_disable(self):
        state = self.run_app(["--target-mhz", "230", "--overconstraint-mhz", "240"])
        self.assertTrue(state["timing_use_overconstraint"])
        self.assertAlmostEqual(state["timing_overconstraint_clock_period_ns"], 1000 / 240)
        self.assertEqual(state["remote_gdsii_dir"], "build_GDSII_230MHz_01")
        state = self.run_app(["--no-overconstraint"], {"EDA_TIMING_USE_OVERCONSTRAINT": "1"})
        self.assertFalse(state["timing_use_overconstraint"])

    def test_invalid_frequency_rejected(self):
        for value in ("0", "-1", "nan", "inf", "1e-320", "abc"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app._parse_args(["--target-mhz", value])

    def test_looser_overconstraint_rejected_before_flow(self):
        with self.assertRaisesRegex(ValueError, "higher than"):
            self.run_app(["--target-mhz", "230", "--overconstraint-mhz", "220"])

    def test_max_clocking_command_and_flag(self):
        for argv in (["max", "clocking"], ["--max-clocking"]):
            self.assertTrue(app._parse_args(argv).max_clocking)

    def test_max_clocking_rejects_conflicting_options_and_unknown_command(self):
        for argv in (
            ["max", "clocking", "--target-mhz", "240"],
            ["--max-clocking", "--overconstraint-mhz", "250"],
            ["max", "clock"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app._parse_args(argv)

    def test_sweep_uses_fresh_states_and_forces_exact_target_validation(self):
        states = []

        def flow(**kwargs):
            state = kwargs["initial_state"]
            states.append(state)
            self.assertEqual(state["history"], [])
            self.assertEqual(state["retry_counts"], {})
            state["history"].append("previous run history")
            state["retry_counts"]["gdsii"] = 3
            passed = len(states) == 1
            return {
                **state,
                "current_stage": "done" if passed else "failed",
                "stage_status": {"gdsii": "success" if passed else "failed"},
                "timing_closure_status": "passed" if passed else "failed",
            }

        with (
            tempfile.TemporaryDirectory() as output,
            patch.dict("os.environ", {
                "EDA_TIMING_TARGET_CLOCK_PERIOD_NS": "5",
                "EDA_TIMING_CLOSURE_ENABLED": "0",
                "EDA_TIMING_USE_OVERCONSTRAINT": "1",
                "EDA_TIMING_OVERCONSTRAINT_CLOCK_PERIOD_NS": "3",
            }, clear=True),
            patch.object(app, "_load_service_exports"),
            patch.object(app, "run_flow", side_effect=flow),
            patch.object(app, "run_max_clocking", side_effect=lambda run: run_max_clocking(run, output_dir=output)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = app.main(app._parse_args(["max", "clocking"]))
        self.assertEqual(result["best_mhz"], 230)
        self.assertEqual(len(states), 2)
        for state, mhz in zip(states, (230, 240)):
            self.assertAlmostEqual(state["timing_target_clock_period_ns"], 1000 / mhz)
            self.assertTrue(state["timing_closure_enabled"])
            self.assertFalse(state["timing_use_overconstraint"])
            self.assertEqual(state["remote_gdsii_dir"], f"build_GDSII_{mhz}MHz_01")


if __name__ == "__main__":
    unittest.main()
