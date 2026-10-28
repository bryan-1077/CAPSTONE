"""Stage an explicitly selected local mailbox revision for remote RTL prep."""

import shlex
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from services.ssh_executor import SSHExecutor
from .mailbox_snapshot import freeze_mailbox


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
        remote = remote_root / "mailbox_inputs" / (revision[:16] + "_" + uuid4().hex)
        with SSHExecutor(**ssh_config) as ssh:
            command = (f"mkdir -p -- {shlex.quote(str(remote.parent))} && "
                       f"mkdir -- {shlex.quote(str(remote))}")
            result = ssh.run(command, timeout=300)
            if not result["ok"]:
                raise RuntimeError("Could not create remote mailbox staging directory: " + result.get("stderr", ""))
            directories = sorted({str(remote / Path(name).parent.as_posix()) for name in hashes})
            result = ssh.run("mkdir -p -- " + " ".join(shlex.quote(path) for path in directories), timeout=300)
            if not result["ok"]:
                raise RuntimeError("Could not create remote RTL subdirectories: " + result.get("stderr", ""))
            for name in hashes:
                ssh.upload_file(snapshot / name, str(remote / name), exclusive=True)
        return {**frozen, "remote_prepare_input": str(remote)}
