import json
import unittest
from unittest.mock import Mock, patch

from workers import orchestrator as flow


class NetlistEnvironmentTests(unittest.TestCase):
    def result(self, **overrides):
        result = {
            "ok": False, "exit_code": 1, "command": "netlist",
            "stdout": "EDA_NETLIST_RUN_COMPLETE:1\n",
            "stderr": "Could not find volare sky130 root: /home/user/.volare/volare/sky130/versions\n",
        }
        result.update(overrides)
        return result

    def test_missing_pdk_stops_before_review_edit_and_retry(self):
        ssh = Mock()
        ssh.run.return_value = self.result()
        state = {
            "logs": {}, "history": [], "validation": {}, "stage_status": {},
            "remote_prepare_dir": "prep", "remote_netlist_dir": "netlist",
            "netlist_max_iterations": 3,
        }
        with (
            patch.object(flow, "_run_netlist_review") as review,
            patch.object(flow, "_run_netlist_edit") as edit,
        ):
            output = flow._run_stage_with_retries(
                state, stage_name="netlist", runner=flow._make_netlist_runner(ssh), max_retries=3,
            )
        ssh.run.assert_called_once()
        review.assert_not_called()
        edit.assert_not_called()
        self.assertEqual(output["current_stage"], "failed")
        self.assertEqual(output["failure_class"], "netlist_environment_error")
        self.assertEqual(output["stage_status"]["netlist"], "failed")
        self.assertEqual(output["retry_counts"]["netlist"], 1)
        self.assertIn("Could not find volare sky130 root:", output["last_error"])
        evidence = json.loads(output["logs"]["netlist_generate_1_completed"])
        self.assertTrue(evidence["run_completed"])
        self.assertIn("Could not find volare sky130 root:", evidence["environment_error"])

    def test_explicit_environment_marker_is_preserved(self):
        result = self.result(stderr="EDA_NETLIST_ENV_ERROR:genus_executable_not_found\n")
        self.assertEqual(flow._netlist_run_environment_error(result), "genus_executable_not_found")

    def test_synthesis_failure_is_not_an_environment_failure(self):
        result = self.result(stderr="Error: RTL elaboration failed\n")
        self.assertIsNone(flow._netlist_run_environment_error(result))

    def test_success_with_old_pdk_diagnostic_does_not_stop(self):
        result = self.result(ok=True, exit_code=0, stdout="EDA_NETLIST_RUN_COMPLETE:0\n")
        self.assertIsNone(flow._netlist_run_environment_error(result))


if __name__ == "__main__":
    unittest.main()
