"""Exercise SSH/Slurm boundaries without credentials or a live server."""

import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from shared.remote.ssh_executor import SSHExecutor
from shared.top_orchestrator.contracts import RunConfig
from shared.top_orchestrator.remote import RemoteConfig, probe_command, remote_check_node, slurm_command


ENV = {"CAPSTONE_SSH_HOST": "example.test", "CAPSTONE_SSH_USER": "tester",
       "CAPSTONE_REMOTE_PROJECT_DIR": "/remote/project with spaces",
       "CAPSTONE_SLURM_PARTITION": "academic", "CAPSTONE_SLURM_QOS": "test"}


class RemoteTests(unittest.TestCase):
    def test_noninteractive_job_supports_setup_alias_and_keeps_failures_fatal(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", ENV, clear=True):
            root = Path(temporary)
            bashrc = root / "bashrc"
            bashrc.write_text("alias load-test-tools='export CAPSTONE_TEST_SETUP=loaded'\n")
            config = replace(RemoteConfig.load(), project_dir=temporary,
                             setup_commands=("source " + shlex.quote(str(bashrc)), "load-test-tools"))
            command = slurm_command(config, [sys.executable, "-c",
                "import os; print(os.environ['CAPSTONE_TEST_SETUP'])"], "test")
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", shlex.split(command)[-1]],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "loaded")
            config = replace(config, setup_commands=("false",))
            script = shlex.split(slurm_command(config, ["echo", "must not run"], "test"))[-1]
            result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", script],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("must not run", result.stdout)

    def test_password_is_passed_to_connection_but_not_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", ENV, clear=True), patch(
                "shared.top_orchestrator.remote.SSHExecutor") as factory, patch(
                "shared.top_orchestrator.remote.getpass.getpass", return_value="test-password-only") as prompt:
            ssh = factory.return_value
            def login(*args, **kwargs):
                self.assertEqual(ssh.password, "test-password-only")
                return {"ok": False, "exit_code": -1, "stdout": "", "stderr": "Authentication failed",
                        "error_type": "AuthenticationException"}
            ssh.run.side_effect = login
            result = remote_check_node(RunConfig("remote-check", ask_password=True), Path(temporary))
            self.assertEqual(prompt.call_count, 4)
            self.assertEqual(ssh.run.call_count, 4)
            self.assertEqual(result.status, "failed")
            self.assertIsNone(ssh.password)
            for path in Path(temporary).iterdir():
                self.assertNotIn("test-password-only", path.read_text())

    def test_password_retry_recovers_and_other_failures_do_not_prompt(self):
        from shared.top_orchestrator.auth import run_with_password_retries
        rejected = dict(ok=False, error_type="AuthenticationException")
        accepted = dict(ok=True)
        for results, interactive, prompts in (([rejected, accepted], True, 1),
                                              ([rejected], False, 0),
                                              ([dict(ok=False, error_type="TimeoutError")], True, 0)):
            ssh = MagicMock(host="host", username="user")
            ssh.run.side_effect = results
            with patch("shared.top_orchestrator.auth.getpass.getpass", return_value="replacement") as prompt:
                result = run_with_password_retries(ssh, "pwd", ask_password=interactive)
            self.assertEqual(result, results[-1])
            self.assertEqual(prompt.call_count, prompts)
            self.assertEqual(ssh.run.call_count, len(results))

    def test_cancelled_password_prompt_does_not_connect(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", ENV, clear=True), patch(
                "shared.top_orchestrator.remote.SSHExecutor") as factory, patch(
                "shared.top_orchestrator.remote.getpass.getpass", side_effect=KeyboardInterrupt):
            result = remote_check_node(RunConfig("remote-check", ask_password=True), Path(temporary))
            factory.return_value.run.assert_not_called()
            self.assertEqual(result.status, "failed")

    def test_environment_and_json_precedence(self):
        with patch.dict("os.environ", ENV, clear=True):
            config = RemoteConfig.load()
            self.assertEqual(config.project_dir, ENV["CAPSTONE_REMOTE_PROJECT_DIR"])
            self.assertIsNone(config.key_filename)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "remote.json"
                path.write_text(json.dumps({"project_dir": "/different/server/path", "cpus": 2}))
                override = RemoteConfig.load(path)
                self.assertEqual(override.project_dir, "/different/server/path")
                self.assertEqual(override.cpus, 2)
                path.write_text(json.dumps({"unexpected": True}))
                with self.assertRaisesRegex(ValueError, "Unknown"):
                    RemoteConfig.load(path)
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(ValueError, "CAPSTONE_SSH_HOST"):
            RemoteConfig.load()
        for key, value in (("CAPSTONE_SSH_PORT", "abc"), ("CAPSTONE_REMOTE_PROJECT_DIR", "relative"),
                           ("CAPSTONE_REMOTE_TIMEOUT_SECONDS", "10")):
            with patch.dict("os.environ", {**ENV, key: value}, clear=True), self.assertRaises(ValueError):
                RemoteConfig.load()

    def test_slurm_quotes_paths_and_initializes_same_job_shell(self):
        with patch.dict("os.environ", ENV, clear=True):
            config = RemoteConfig.load()
        argv = ["/python with spaces", "script.py", "a'; touch /tmp/unwanted"]
        command = slurm_command(config, argv, "unique-job")
        outer = shlex.split(command)
        self.assertEqual(outer[0], "srun")
        self.assertEqual(outer[-3:-1], ["/bin/bash", "-lc"])
        script = outer[-1]
        self.assertNotIn("load-ecen-454", script)
        self.assertNotIn("srun", script)
        self.assertLess(script.index("source ~/.bashrc"), script.index("exec "))
        self.assertEqual(shlex.split(script.splitlines()[-1])[1:], argv)
        self.assertEqual(shlex.split(script.splitlines()[-2])[2], config.project_dir)

    def test_preflight_success_failures_and_cancellation(self):
        for outcome in ("success", "login_failure", "job_failure", "missing_marker", "interrupt"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary, patch.dict(
                    "os.environ", ENV, clear=True), patch(
                    "shared.top_orchestrator.remote.uuid4", return_value=SimpleNamespace(hex="TOKEN")), patch(
                    "shared.top_orchestrator.remote.SSHExecutor") as factory:
                ssh = factory.return_value
                login = {"ok": outcome != "login_failure", "exit_code": 0, "stdout": "login", "stderr": ""}
                if outcome == "login_failure":
                    login.update(exit_code=-1, stderr="AuthenticationException: Authentication failed.",
                                 error_type="AuthenticationException")
                compute = {"ok": outcome != "job_failure", "exit_code": 0, "stderr": "",
                           "stdout": 'TOKEN{"job_id":"123", "cwd":"/remote"}\n' if outcome != "missing_marker" else ""}
                ssh.run.side_effect = [login, KeyboardInterrupt() if outcome == "interrupt" else compute,
                                       {"ok": True, "exit_code": 0}]
                result = remote_check_node(RunConfig("remote-check"), Path(temporary))
                self.assertEqual(result.status, "passed" if outcome == "success" else "failed")
                report = json.loads((Path(temporary) / "remote_result.json").read_text())
                self.assertEqual(report["validation"], "not_run")
                ssh.close.assert_called_once()
                if outcome not in ("success", "login_failure"):
                    self.assertIn("scancel --name capstone-check-TOKEN", ssh.run.call_args.args[0])
                if outcome == "login_failure":
                    self.assertEqual(ssh.run.call_count, 1)
                    self.assertIn("Authentication failed", result.message)
                    self.assertIn("CAPSTONE_SSH_USER", (Path(temporary) / "remote.log").read_text())

    def test_plan_does_not_connect_or_create_artifacts(self):
        from shared.top_orchestrator.__main__ import main
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "unused"
            with patch.dict("os.environ", ENV, clear=True), patch("sys.argv", [
                    "orchestrator", "remote-check", "--ask-password", "--plan", "--run-dir", str(output)]), patch(
                    "shared.top_orchestrator.remote.SSHExecutor") as factory, patch("sys.stdout", io.StringIO()), patch(
                    "shared.top_orchestrator.remote.getpass.getpass") as prompt:
                self.assertEqual(main(), 0)
                factory.assert_not_called()
                prompt.assert_not_called()
                self.assertFalse(output.exists())


class FakeChannel:
    def __init__(self, stalled=False):
        self.out = [b"hello\xe2", b"\x82\xac"] if not stalled else []
        self.err = [b"diagnostic"] if not stalled else []
        self.stalled = stalled
        self.closed = False
        self.reads = []

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return bool(self.out)

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv(self, size):
        self.reads.append("stdout")
        return self.out.pop(0)

    def recv_stderr(self, size):
        self.reads.append("stderr")
        return self.err.pop(0)

    def exit_status_ready(self):
        return not self.stalled and not self.out and not self.err

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_connect_uses_agent_and_closes_failed_client(self):
        with patch("shared.remote.ssh_executor.paramiko") as paramiko:
            ssh = SSHExecutor("host", "user", key_filename="~/.ssh/test")
            client = paramiko.SSHClient.return_value
            ssh.connect()
            kwargs = client.connect.call_args.kwargs
            self.assertTrue(kwargs["allow_agent"])
            self.assertTrue(kwargs["look_for_keys"])
            self.assertEqual(kwargs["key_filename"], str(Path("~/.ssh/test").expanduser()))
            ssh.close()
            client.reset_mock()
            client.connect.side_effect = RuntimeError("authentication failed")
            with self.assertRaises(RuntimeError):
                ssh.connect()
            client.close.assert_called_once()
            self.assertIsNone(ssh._client)

    def test_sftp_preserves_exclusive_upload_and_download_interface(self):
        ssh = SSHExecutor("host", "user")
        ssh._client = MagicMock()
        sftp = ssh._client.open_sftp.return_value.__enter__.return_value
        with tempfile.TemporaryDirectory() as temporary, patch.object(ssh, "connect"):
            source = Path(temporary) / "source"
            source.write_bytes(b"RTL bytes")
            ssh.upload_file(source, "/remote/top.sv", exclusive=True)
            sftp.open.assert_called_once_with("/remote/top.sv", "wx")
            sftp.open.return_value.__enter__.return_value.write.assert_called_once_with(b"RTL bytes")
            destination = Path(temporary) / "reports/result.json"
            result = ssh.fetch_file("/remote/result.json", destination)
            self.assertTrue(result["ok"])
            sftp.get.assert_called_once_with("/remote/result.json", str(destination))

    def executor(self, channel):
        ssh = SSHExecutor("host", "user")
        client = MagicMock()
        client.exec_command.return_value = (MagicMock(), SimpleNamespace(channel=channel), MagicMock())
        ssh._client = client
        return ssh

    def test_drains_both_streams_and_preserves_split_unicode(self):
        channel = FakeChannel()
        ssh = self.executor(channel)
        emitted = []
        with patch.object(ssh, "connect"):
            result = ssh.run("echo test", cwd="/path with spaces", on_output=lambda name, text: emitted.append((name, text)))
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "hello€")
        self.assertEqual(result["stderr"], "diagnostic")
        self.assertEqual(channel.reads[:2], ["stdout", "stderr"])
        self.assertTrue(channel.closed)
        self.assertIn(("stderr", "diagnostic"), emitted)

    def test_timeout_closes_channel_and_reports_transport_error(self):
        channel = FakeChannel(stalled=True)
        ssh = self.executor(channel)
        with patch.object(ssh, "connect"), patch("shared.remote.ssh_executor.time.monotonic", side_effect=[0, 100]):
            result = ssh.run("stalled", timeout=1)
        self.assertEqual(result["exit_code"], -1)
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertTrue(channel.closed)
        self.assertIsNone(ssh._client)

    def test_dependency_error_is_structured(self):
        with patch("shared.remote.ssh_executor.paramiko", None):
            result = SSHExecutor("host", "user").run("true")
        self.assertFalse(result["ok"])
        self.assertIn("Paramiko", result["stderr"])


if __name__ == "__main__":
    unittest.main()
