import os
import shlex
from pathlib import Path
from typing import Optional

import paramiko


class SSHExecutor:
    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        key_filename: str | None = None,
        password: str | None = None,
    ):
        self.host = host
        self.username = username
        self.port = port
        self.key_filename = os.path.expanduser(key_filename) if key_filename else None
        self.password = password
        self._client: Optional[paramiko.SSHClient] = None

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def connect(self, timeout: int = 30) -> None:
        if self.is_connected:
            return

        self.close()

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            key_filename=self.key_filename,
            password=self.password,
            timeout=timeout,
            allow_agent=True,
            look_for_keys=True,
        )

        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int = 120,
        get_pty: bool = False,
        login_shell: bool = True,
        shell_preamble: str | None = None,
        use_srun: bool = False,
        srun_args: str | None = None,
    ) -> dict:
        wrapped_command = command

        try:
            self.connect(timeout=min(timeout, 30))

            full_command = command

            if cwd:
                full_command = f"cd {shlex.quote(cwd)} && {full_command}"

            if shell_preamble:
                full_command = f"{shell_preamble} && {full_command}"

            if login_shell:
                full_command = f"bash -lc {shlex.quote(full_command)}"

            if use_srun:
                srun_prefix = "srun"
                if srun_args:
                    srun_prefix += f" {srun_args}"
                wrapped_command = f"{srun_prefix} {full_command}"
            else:
                wrapped_command = full_command

            stdin, stdout, stderr = self._client.exec_command(
                wrapped_command,
                timeout=timeout,
                get_pty=get_pty,
            )

            stdout_text = stdout.read().decode("utf-8", errors="replace")
            stderr_text = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()

            return {
                "ok": exit_code == 0,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "command": wrapped_command,
            }

        except Exception as exc:
            self.close()
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "command": wrapped_command,
            }

    def upload_file(self, local_path: str | Path, remote_path: str, *, exclusive: bool = False) -> None:
        """Upload a runner to a caller-selected, unique remote filename."""
        self.connect()
        with self._client.open_sftp() as sftp:
            if exclusive:
                with sftp.open(remote_path, "wx") as destination:
                    destination.write(Path(local_path).read_bytes())
            else:
                sftp.put(str(local_path), remote_path)

    def fetch_file(
        self,
        remote_path: str,
        local_path: str | Path,
        *,
        timeout: int = 30,
    ) -> dict:
        try:
            self.connect(timeout=timeout)
            local = Path(local_path)
            local.parent.mkdir(parents=True, exist_ok=True)
            with self._client.open_sftp() as sftp:
                sftp.get(remote_path, str(local))
            return {
                "ok": True,
                "remote_path": remote_path,
                "local_path": str(local),
                "error": None,
            }
        except Exception as exc:
            self.close()
            return {
                "ok": False,
                "remote_path": remote_path,
                "local_path": str(local_path),
                "error": str(exc),
            }
