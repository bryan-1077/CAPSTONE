"""Freeze the backend transport tree without SSH or physical-design tools."""
import hashlib
import json
import re
from pathlib import Path


def freeze_mailbox(mailbox, destination):
    mailbox = Path(mailbox).expanduser().resolve(strict=True)
    rtl, destination = mailbox / 'rtl', Path(destination)
    if not rtl.is_dir() or rtl.is_symlink():
        raise ValueError('Mailbox revision needs a real rtl/ directory.')
    files = []
    for path in sorted(rtl.rglob('*')):
        if path.is_symlink():
            raise ValueError('Mailbox RTL must not contain symlinks: ' + str(path))
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError('Unsupported mailbox entry: ' + str(path))
    if not any(path.suffix.lower() in {'.v', '.sv'} for path in files):
        raise ValueError('Mailbox rtl/ contains no Verilog/SystemVerilog sources.')
    if destination.resolve().is_relative_to(mailbox):
        raise ValueError('Backend snapshot destination must be outside the source mailbox.')
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise FileExistsError(destination)
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for source in files:
        relative = source.relative_to(rtl)
        data = source.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        hashes[relative.as_posix()] = hashlib.sha256(data).hexdigest()
    manifest_path = destination / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if not isinstance(manifest, dict):
        raise ValueError('rtl/manifest.json must be an object.')
    top = manifest.get('top_module')
    if top is not None and (not isinstance(top, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', top)):
        raise ValueError('Invalid top_module in rtl/manifest.json.')
    revision = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {'mailbox_source': str(mailbox), 'mailbox_revision': revision,
            'mailbox_file_hashes': hashes, **({'top_module': top} if top else {})}
