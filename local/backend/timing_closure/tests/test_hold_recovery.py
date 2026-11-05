import unittest

from timing_closure.hold_recovery import legal_hold_targets, fallback_hold_plan, plan_signature
from timing_closure.timing_analyzer import TimingPath, TimingReport


class HoldRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.buffers = {f"queue_reg[{i}]/D": {
            "from_cell": "sky130_fd_sc_hd__dfxtp_1",
            "allowed_cells": ["sky130_fd_sc_hd__buf_12", "sky130_fd_sc_hd__buf_2"],
        } for i in range(6)}
        self.report = TimingReport("hold.rpt", paths=[
            TimingPath("hold.rpt", endpoint=pin, slack=-0.05)
            for pin in self.buffers
        ])

    def test_only_measured_negative_hold_data_sinks_are_eligible(self):
        self.report.paths.extend([
            TimingPath("hold.rpt", endpoint="unknown/D", slack=-0.1),
            TimingPath("hold.rpt", endpoint="queue_reg[0]/CLK", slack=-0.1),
        ])
        for slack in (0, 0.1, None, float("nan")):
            with self.subTest(slack=slack):
                self.report.paths[0].slack = slack
                targets = legal_hold_targets(self.report, self.buffers)
                self.assertNotIn("queue_reg[0]/D", targets)
                self.assertEqual(len(targets), 5)

    def test_bounded_numeric_size_order_and_no_repeated_experiments(self):
        targets = legal_hold_targets(self.report, self.buffers)
        tried = set()
        counts = []
        while plan := fallback_hold_plan(targets, self.buffers, "checkpoint", tried):
            counts.append(len(plan["actions"]))
            signature = plan_signature("checkpoint", plan["actions"])
            self.assertNotIn(signature, tried)
            tried.add(signature)
            if len(tried) == 1:
                self.assertTrue(all(action["cell"].endswith("_2") for action in plan["actions"]))
            self.assertLessEqual(len(tried), 4)
        self.assertEqual(counts, [4, 4, 2, 2])
        self.assertIsNotNone(fallback_hold_plan(targets, self.buffers, "new_checkpoint", tried))

    def test_no_targets_returns_no_fallback(self):
        self.assertIsNone(fallback_hold_plan({}, self.buffers, "checkpoint", set()))

    def test_unsupported_cells_cannot_enter_fallback(self):
        for cell in ("sky130_fd_sc_hd__inv_2", "unknown_buffer"):
            self.buffers["queue_reg[0]/D"]["allowed_cells"] = [cell]
            self.assertNotIn("queue_reg[0]/D", legal_hold_targets(self.report, self.buffers))


if __name__ == "__main__":
    unittest.main()
