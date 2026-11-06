"""Stage an explicitly selected local mailbox revision for remote RTL prep."""

import errno
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from services.ssh_executor import SSHExecutor
from shared.remote.ssh_executor import paramiko
from shared.top_orchestrator.compute_transfer import ComputeTransferError, upload_compute_archive
from shared.top_orchestrator.remote import RemoteConfig
from .mailbox_snapshot import freeze_mailbox


def _retryable(error):
    """Inspect wrapped upload failures without retrying input/auth/permission errors."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (PermissionError, FileExistsError, FileNotFoundError)):
            return False
        if paramiko is not None and isinstance(
                error, (paramiko.AuthenticationException, paramiko.BadHostKeyException)):
            return False
        if isinstance(error, (TimeoutError, ConnectionError, EOFError)):
            return True
        if isinstance(error, ComputeTransferError):
            return error.result.get("exit_code") == -1 and error.result.get("error_type") in {
                "TimeoutError", "ConnectionError", "ConnectionResetError", "EOFError", "SSHException",
                "OSError", "BrokenPipeError"}
        if isinstance(error, OSError) and error.errno in {
                errno.EIO, errno.ESTALE, errno.ETIMEDOUT, errno.ECONNRESET,
                errno.ECONNABORTED, errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH}:
            return True
        if paramiko is not None and isinstance(error, paramiko.SSHException):
            return True
        error = error.__cause__ or error.__context__
    return False


def stage_mailbox(mailbox: Path, project_root: str, ssh_config: dict) -> dict:
    """Transport inputs; the caller remains responsible for the validation gate."""
    remote_root = PurePosixPath(project_root)
    if not remote_root.is_absolute():
        raise ValueError("Remote project root must be absolute.")

    with tempfile.TemporaryDirectory(prefix="backend-mailbox-") as temporary:
        snapshot = Path(temporary)
        frozen = freeze_mailbox(mailbox, snapshot)
        hashes = frozen["mailbox_file_hashes"]
        revision = frozen["mailbox_revision"]
        remote_config = RemoteConfig(
            host=ssh_config["host"], username=ssh_config["username"],
            port=ssh_config.get("port", 22), key_filename=ssh_config.get("key_filename"),
            project_dir=project_root, python="/usr/bin/python3",
            partition=os.getenv("CAPSTONE_SLURM_PARTITION") or "adademic",
            qos=os.getenv("CAPSTONE_SLURM_QOS") or "olympus-academic",
            setup_commands=(), job_seconds=240, timeout_seconds=300,
        )
        sources = {name: snapshot / name for name in hashes}
        def output(stream, text):
            print(text, end="", flush=True)

        for attempt in range(1, 4):
            remote = remote_root / "mailbox_inputs" / (revision[:16] + "_" + uuid4().hex)
            started = time.monotonic()
            print(f"[MAILBOX] Attempt {attempt}/3: staging {len(hashes)} files to {remote}", flush=True)
            try:
                with SSHExecutor(**ssh_config) as ssh:
                    transfer = upload_compute_archive(
                        ssh, remote_config, remote, sources, snapshot / f"transfer_{attempt}.zip",
                        "capstone-mailbox-" + uuid4().hex, output, create_workspace=True,
                    )
                print(f"[MAILBOX] Attempt {attempt}/3 complete in {time.monotonic() - started:.2f}s", flush=True)
                return {**frozen, "remote_prepare_input": str(remote), "mailbox_transfer": transfer}
            except Exception as exc:
                retry = attempt < 3 and _retryable(exc)
                print(f"[MAILBOX] Attempt {attempt}/3 failed after {time.monotonic() - started:.2f}s: "
                      f"{type(exc).__name__}: {exc}. Staging path: {remote}; "
                      f"{'retrying with a fresh connection and directory' if retry else 'stopping'}.", flush=True)
                if not retry:
                    raise
                time.sleep(2 ** attempt)
