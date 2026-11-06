"""Stream a verified archive to a receiver running in a compute allocation."""

from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
import shlex
import time
import zipfile

from .allocation import active_allocation, cancellation_command
from .remote import slurm_command


RESULT_MARKER = "CAPSTONE_TRANSFER_RESULT:"


class ComputeTransferError(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__("Compute archive transfer failed: " + result.get("stderr", ""))


def receive_archive(workspace, expected, job_id, create_workspace=False):
    # Keep this function self-contained: its source is executed on the compute node.
    import hashlib
    import json
    import os
    from pathlib import Path, PurePosixPath
    import shutil
    import socket
    import stat
    import sys
    import tempfile
    import zipfile

    actual_job = os.environ.get("SLURM_JOB_ID", "")
    if not actual_job.isdigit() or int(actual_job) < 1 or (job_id is not None and actual_job != job_id):
        raise RuntimeError("Archive receiver must run in the requested compute allocation")
    root = Path(workspace)
    if not root.is_absolute():
        raise ValueError("Transfer workspace must be absolute")
    if create_workspace:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=False)
    root = root.resolve(strict=True)
    print("[TRANSFER] Receiving archive on " + socket.gethostname() + " (job " + actual_job + ")", flush=True)
    with tempfile.TemporaryFile() as stream:
        shutil.copyfileobj(sys.stdin.buffer, stream, length=65536)
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) != len(expected) or {item.filename for item in members} != set(expected):
                raise ValueError("Transfer archive inventory differs from the requested files")
            targets = {}
            for member in members:
                name = member.filename
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != name:
                    raise ValueError("Unsafe transfer archive path: " + name)
                if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Transfer archive members must be regular files")
                if member.file_size != expected[name]["size"]:
                    raise ValueError("Transfer file size differs from manifest: " + name)
                target = root.joinpath(*relative.parts)
                target.resolve().relative_to(root)
                probe = target
                while probe != root:
                    if probe.is_symlink():
                        raise ValueError("Transfer destination contains a symlink: " + name)
                    probe = probe.parent
                if target.exists():
                    raise FileExistsError("Transfer destination already exists: " + name)
                targets[name] = target
            for member in members:
                name = member.filename
                target = targets[name]
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with archive.open(member) as incoming, target.open("xb") as outgoing:
                    while True:
                        chunk = incoming.read(65536)
                        if not chunk:
                            break
                        outgoing.write(chunk)
                        digest.update(chunk)
                if digest.hexdigest() != expected[name]["sha256"]:
                    raise ValueError("Transfer file hash mismatch: " + name)
                if hashlib.sha256(target.read_bytes()).hexdigest() != expected[name]["sha256"]:
                    raise ValueError("Transferred file read-back mismatch: " + name)
    receipt = {"host": socket.gethostname(), "job_id": actual_job,
               "verified_files": len(expected), "bytes": sum(item["size"] for item in expected.values())}
    print("[TRANSFER] Verified " + str(len(expected)) + " files on " + receipt["host"], flush=True)
    print("CAPSTONE_TRANSFER_RESULT:" + json.dumps(receipt), flush=True)


def upload_validation_archive(ssh, remote, invocation, sources, archive_path, job_name, output):
    """Verification uses its existing workspace and requires the shared allocation."""
    if active_allocation(remote) is None:
        raise ValueError("Compute archive transfer requires an active shared allocation")
    return upload_compute_archive(ssh, remote, invocation, sources, archive_path, job_name, output)


def upload_compute_archive(ssh, remote, invocation, sources, archive_path, job_name, output,
                           *, create_workspace=False):
    """Package explicit files; compute creates/writes/verifies them, including for standalone jobs."""
    lease = active_allocation(remote)
    job_id = str(lease["job_id"]) if lease else None
    manifest = {}
    archive_path = Path(archive_path)
    with zipfile.ZipFile(archive_path, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, source in sources.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
                raise ValueError("Unsafe transfer path: " + name)
            source = Path(source)
            if source.is_symlink() or not source.is_file():
                raise ValueError("Transfer source must be a regular file: " + str(source))
            data = source.read_bytes()
            manifest[name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            archive.writestr(name, data)
    script = inspect.getsource(receive_archive) + "\nreceive_archive(" + ", ".join(
        repr(item) for item in (str(invocation), manifest, job_id, create_workspace)
    ) + ")\n"
    # Receiving bytes needs Python, not the design-tool setup scripts or aliases.
    transfer_remote = replace(remote, setup_commands=(), required_tools=())
    command = slurm_command(transfer_remote, [remote.python, "-u", "-c", script], job_name)
    allocation_label = job_id if lease else "a new Slurm allocation"
    output("stdout", f"[TRANSFER] Streaming {len(sources)} files ({archive_path.stat().st_size} archive bytes) "
           f"to compute allocation {allocation_label}; deadline=300s.\n")
    started = time.monotonic()
    result = None
    try:
        result = ssh.run(command, timeout=300, stdin_path=archive_path, on_output=output)
    finally:
        if result is None or result.get("exit_code", -1) < 0:
            # Cancel only this receiver step, never the shared allocation.
            ssh.run(shlex.join(cancellation_command(remote, job_name)), timeout=15)
    if not result["ok"]:
        raise ComputeTransferError(result)
    receipts = [json.loads(line[len(RESULT_MARKER):]) for line in result["stdout"].splitlines()
                if line.startswith(RESULT_MARKER)]
    if (len(receipts) != 1 or receipts[0].get("verified_files") != len(sources)
            or receipts[0].get("bytes") != sum(item["size"] for item in manifest.values())
            or not receipts[0].get("host") or not str(receipts[0].get("job_id", "")).isdigit()
            or (job_id is not None and receipts[0].get("job_id") != job_id)):
        raise ValueError("Compute transfer did not return matching verified-file evidence")
    receipt = receipts[0]
    receipt.update(mode="compute_archive", archive_bytes=archive_path.stat().st_size,
                   elapsed_seconds=round(time.monotonic()-started, 6), command=result.get("command", command))
    return receipt
