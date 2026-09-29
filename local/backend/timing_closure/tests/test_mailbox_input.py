import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from services.mailbox_input import stage_mailbox
from workers.workers import _build_prepare_command


class MailboxTests(unittest.TestCase):
    def test_upload_preserves_tree_and_records_actual_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rtl/inc").mkdir(parents=True)
            (root / "rtl/top.sv").write_text("module custom_top; endmodule")
            (root / "rtl/inc/defs.svh").write_text("// definitions")
            (root / "rtl/manifest.json").write_text(json.dumps({"top_module": "custom_top"}))
            (root / "specs").mkdir()
            (root / "specs/test.yaml").write_text("design_name: custom_top")
            uploaded = {}
            with patch("services.mailbox_input.SSHExecutor") as executor:
                ssh = executor.return_value.__enter__.return_value
                ssh.run.return_value = {"ok": True}
                def upload(local, remote, exclusive):
                    self.assertTrue(exclusive)
                    uploaded[remote] = local.read_bytes()
                ssh.upload_file.side_effect = upload
                state = stage_mailbox(root, "/remote/project with spaces", {})
            self.assertEqual(state["top_module"], "custom_top")
            self.assertEqual(len(uploaded), 3)
            for name, digest in state["mailbox_file_hashes"].items():
                data = uploaded[state["remote_prepare_input"] + "/" + name]
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
            command = _build_prepare_command(state)
            self.assertIn("--top custom_top", command)
            self.assertNotIn("MemoryController.zip", command)

    def test_bad_inputs_fail_before_ssh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("services.mailbox_input.SSHExecutor") as executor:
                with self.assertRaises(ValueError):
                    stage_mailbox(root, "/remote", {})
                (root / "rtl").mkdir()
                with self.assertRaises(ValueError):
                    stage_mailbox(root, "/remote", {})
                (root / "rtl/escape.sv").symlink_to(root / "absent")
                with self.assertRaises(ValueError):
                    stage_mailbox(root, "/remote", {})
                executor.assert_not_called()

    def test_transfer_failure_does_not_start_flow(self):
        with patch.object(app, "_load_service_exports"), patch.object(
                app, "stage_mailbox", side_effect=OSError("upload failed")), patch.object(app, "run_flow") as flow:
            with self.assertRaisesRegex(OSError, "upload failed"):
                app.main(app._parse_args(["--mailbox", "/mailbox/revision", "--target-mhz", "200"]))
            flow.assert_not_called()

    def test_app_passes_uploaded_input_and_sweep_reuses_it(self):
        states = []
        staged = {"remote_prepare_input": "/remote/mailbox/rtl", "mailbox_source": "/local/revision",
                  "mailbox_revision": "hash", "top_module": "custom_top"}
        def flow(**kwargs):
            states.append(kwargs["initial_state"])
            return {"current_stage": "done"}
        def sweep(run_target):
            run_target(230)
            return run_target(240)
        with patch.object(app, "_load_service_exports"), patch.object(
                app, "stage_mailbox", return_value=staged) as upload, patch.object(
                app, "run_flow", side_effect=flow), patch.object(app, "run_max_clocking", side_effect=sweep):
            app.main(app._parse_args(["--mailbox", "/local/revision", "--max-clocking"]))
            upload.assert_called_once()
        self.assertEqual(len(states), 2)
        for state in states:
            self.assertEqual(state["remote_prepare_input"], staged["remote_prepare_input"])
            self.assertEqual(state["top_module"], "custom_top")


if __name__ == "__main__":
    unittest.main()
