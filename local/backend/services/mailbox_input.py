"""Stage an explicitly selected local mailbox revision for remote RTL prep."""

import hashlib
import json
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from services.ssh_executor import SSHExecutor


def stage_mailbox(mailbox: Path, project_root: str, ssh_config: dict) -> dict:
    """Transport inputs; the caller remains responsible for the validation gate."""
    mailbox = mailbox.expanduser().resolve(strict=True)
    rtl = mailbox / "rtl"
    if not rtl.is_dir() or rtl.is_symlink():
        raise ValueError("Mailbox revision needs a real rtl/ directory.")
    files = []
    for path in sorted(rtl.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Mailbox RTL must not contain symlinks: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported mailbox entry: {path}")
    if not any(path.suffix.lower() in {".v", ".sv"} for path in files):
        raise ValueError("Mailbox rtl/ contains no Verilog/SystemVerilog sources.")
    remote_root = PurePosixPath(project_root)
    if not remote_root.is_absolute():
        raise ValueError("Remote project root must be absolute.")

    with tempfile.TemporaryDirectory(prefix="backend-mailbox-") as temporary:
        snapshot = Path(temporary)
        hashes = {}
        # Freeze the bytes before SSH; hashes describe exactly what is uploaded.
        for source in files:
            relative = source.relative_to(rtl)
            data = source.read_bytes()
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if not isinstance(manifest, dict):
            raise ValueError("rtl/manifest.json must be an object.")
        top = manifest.get("top_module")
        if top is not None and (not isinstance(top, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top)):
            raise ValueError("Invalid top_module in rtl/manifest.json.")
        revision = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        remote = remote_root / "mailbox_inputs" / (revision[:16] + "_" + uuid4().hex)
        with SSHExecutor(**ssh_config) as ssh:
            command = (f"mkdir -p -- {shlex.quote(str(remote.parent))} && "
                       f"mkdir -- {shlex.quote(str(remote))}")
            result = ssh.run(command)
            if not result["ok"]:
                raise RuntimeError("Could not create remote mailbox staging directory: " + result.get("stderr", ""))
            directories = sorted({str(remote / Path(name).parent.as_posix()) for name in hashes})
            result = ssh.run("mkdir -p -- " + " ".join(shlex.quote(path) for path in directories))
            if not result["ok"]:
                raise RuntimeError("Could not create remote RTL subdirectories: " + result.get("stderr", ""))
            for name in hashes:
                ssh.upload_file(snapshot / name, str(remote / name), exclusive=True)
        return {"mailbox_source": str(mailbox), "mailbox_revision": revision,
                "mailbox_file_hashes": hashes, "remote_prepare_input": str(remote),
                **({"top_module": top} if top else {})}
