import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from services.mailbox_input import stage_mailbox
from workers.workers import _build_prepare_command
from shared.top_orchestrator.compute_transfer import ComputeTransferError


class MailboxTests(unittest.TestCase):
    ssh_config = {"host": "example.test", "username": "user"}

    def staging_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "rtl").mkdir()
        (root / "rtl/a.sv").write_bytes(b"module a; endmodule")
        (root / "rtl/b.sv").write_bytes(b"module b; endmodule")
        return root

    def test_timeout_restarts_entire_frozen_snapshot_on_new_connection_and_path(self):
        root = self.staging_fixture()
        clients = [MagicMock(), MagicMock()]
        for client in clients:
            client.__enter__.return_value = client
        uploaded = [[], []]
        def upload(ssh, remote_config, remote, sources, archive, job_name, output, **kwargs):
            index = clients.index(ssh)
            for name, source in sources.items():
                uploaded[index].append((str(remote / name), source.read_bytes(), kwargs['create_workspace']))
            if index == 0:
                (root / "rtl/a.sv").write_bytes(b"changed during retry")
                raise ComputeTransferError({'exit_code': -1, 'error_type': 'TimeoutError', 'stderr': 'stalled'})
            return {'host': 'compute', 'verified_files': len(sources)}
        with patch("services.mailbox_input.SSHExecutor", side_effect=clients) as factory, patch(
                "services.mailbox_input.upload_compute_archive", side_effect=upload), patch(
                "services.mailbox_input.time.sleep") as sleep, patch("sys.stdout", new_callable=io.StringIO) as log:
            state = stage_mailbox(root, "/remote", self.ssh_config)
        self.assertEqual(factory.call_count, 2)
        sleep.assert_called_once_with(2)
        clients[0].__exit__.assert_called_once()
        self.assertEqual([entry[1:] for entry in uploaded[0]], [entry[1:] for entry in uploaded[1]])
        self.assertNotEqual(uploaded[0][0][0], uploaded[1][0][0])
        for remote, data, exclusive in uploaded[1]:
            self.assertTrue(exclusive)
            name = Path(remote).name
            self.assertEqual(remote, state["remote_prepare_input"] + "/" + name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), state["mailbox_file_hashes"][name])
        self.assertIn("failed after", log.getvalue())
        self.assertIn("Attempt 2/3 complete", log.getvalue())
        self.assertEqual(state['mailbox_transfer']['host'], 'compute')
        for client in clients:
            client.upload_file.assert_not_called()

    def test_transient_failures_stop_after_three_attempts(self):
        root = self.staging_fixture()
        clients = [MagicMock() for _ in range(3)]
        for client in clients:
            client.__enter__.return_value = client
        with patch("services.mailbox_input.SSHExecutor", side_effect=clients) as factory, patch(
                "services.mailbox_input.upload_compute_archive", side_effect=TimeoutError("stalled")) as upload, patch(
                "services.mailbox_input.time.sleep") as sleep:
            with self.assertRaisesRegex(TimeoutError, "stalled"):
                stage_mailbox(root, "/remote", self.ssh_config)
        self.assertEqual(factory.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        destinations = [call.args[2] for call in upload.call_args_list]
        self.assertEqual(len(set(destinations)), 3)
        for client in clients:
            client.__exit__.assert_called_once()

    def test_permanent_upload_failures_and_interrupts_are_not_retried(self):
        from shared.remote.ssh_executor import paramiko
        root = self.staging_fixture()
        errors = [PermissionError("denied"), FileExistsError("exists"), KeyboardInterrupt()]
        if paramiko is not None:
            errors.append(paramiko.AuthenticationException("authentication failed"))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch(
                    "services.mailbox_input.SSHExecutor") as factory, patch(
                    "services.mailbox_input.upload_compute_archive", side_effect=error), patch(
                    "services.mailbox_input.time.sleep") as sleep:
                with self.assertRaises(type(error)):
                    stage_mailbox(root, "/remote", self.ssh_config)
                factory.assert_called_once()
                sleep.assert_not_called()

    def test_compute_transport_timeout_retries_but_remote_rejection_does_not(self):
        root = self.staging_fixture()
        for result, attempts in [
                ({"ok": False, "exit_code": -1, "error_type": "TimeoutError", "stderr": "stalled"}, 3),
                ({"ok": False, "exit_code": 1, "stderr": "Permission denied"}, 1)]:
            with self.subTest(result=result), patch("services.mailbox_input.SSHExecutor") as factory, patch(
                    "services.mailbox_input.time.sleep"):
                ssh = factory.return_value.__enter__.return_value
                ssh.run.return_value = result
                with self.assertRaisesRegex(RuntimeError, "Compute archive transfer failed"):
                    stage_mailbox(root, "/remote", self.ssh_config)
                self.assertEqual(factory.call_count, attempts)
                ssh.upload_file.assert_not_called()

    def test_upload_preserves_tree_and_records_actual_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "rtl/inc").mkdir(parents=True)
            (root / "rtl/top.sv").write_text("module custom_top; endmodule")
            (root / "rtl/inc/defs.svh").write_text("// definitions")
            (root / "rtl/manifest.json").write_text(json.dumps({"top_module": "custom_top"}))
            (root / "specs").mkdir()
            (root / "specs/test.yaml").write_text("design_name: custom_top")
            project = root / 'project with spaces'
            project.mkdir()
            commands = []
            with patch("services.mailbox_input.SSHExecutor") as executor:
                ssh = executor.return_value.__enter__.return_value
                def run(command, *, stdin_path, timeout, on_output):
                    arguments = shlex.split(command)
                    self.assertEqual(arguments[0], 'srun')
                    commands.append(command)
                    with Path(stdin_path).open('rb') as stream:
                        completed = subprocess.run(['/bin/bash', '-c', arguments[-1]], stdin=stream,
                            env={**os.environ, 'SLURM_JOB_ID': '777'}, capture_output=True, text=True, timeout=10)
                    on_output('stdout', completed.stdout)
                    return dict(ok=completed.returncode == 0, exit_code=completed.returncode,
                                stdout=completed.stdout, stderr=completed.stderr)
                ssh.run.side_effect = run
                state = stage_mailbox(root, str(project), self.ssh_config)
                ssh.upload_file.assert_not_called()
            self.assertEqual(state["top_module"], "custom_top")
            self.assertEqual(len(commands), 1)
            self.assertEqual(state['mailbox_transfer']['verified_files'], 3)
            self.assertEqual(state['mailbox_transfer']['job_id'], '777')
            for name, digest in state["mailbox_file_hashes"].items():
                data = (Path(state["remote_prepare_input"]) / name).read_bytes()
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
