"""Approved-YAML launch validation and private worker startup."""
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from .run_reader import REPO_ROOT, DEFAULT_RUNS


def validate(config):
    import yaml
    description = str(config.get('description', '')).strip()
    path = None
    if not description:
        path = Path(config['input_yaml']).expanduser().resolve()
        try:
            valid = path.is_file() and isinstance(yaml.safe_load(path.read_text()), dict)
        except yaml.YAMLError as exc:
            raise ValueError('Invalid YAML specification') from exc
        if not valid:
            raise ValueError('Select approved YAML or enter a design description.')
    frequency = float(config['target_mhz'])
    if not math.isfinite(frequency) or frequency <= 0:
        raise ValueError('Target MHz must be finite and positive.')
    if config.get('skip_lec') and config.get('no_backend'):
        raise ValueError('Skip LEC requires backend execution.')
    remote = config.get('remote_config')
    if remote:
        remote = str(Path(remote).expanduser().resolve())
        if not Path(remote).is_file():
            raise ValueError('Remote configuration does not exist.')
    if not (config.get('skip_verification') and config.get('no_backend')):
        from shared.top_orchestrator.remote import RemoteConfig
        RemoteConfig.load(Path(remote) if remote else None)
    return dict(config, input_yaml=str(path) if path else None, target_mhz=frequency, remote_config=remote)


def launch(config, password):
    config = validate(config)
    private = REPO_ROOT / 'shared/gui/.runtime'
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    lock = (private / 'active.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError('A GUI workflow is already active in this checkout.')
    run_dir = DEFAULT_RUNS / ('gui_' + uuid.uuid4().hex)
    socket_dir = tempfile.mkdtemp(prefix='capstone-gui-')
    socket_path = str(Path(socket_dir) / 'input.sock')
    read_fd, write_fd = os.pipe()
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, '-m', 'shared.gui.worker', str(run_dir), str(lock.fileno()), str(read_fd), socket_path],
            cwd=REPO_ROOT, pass_fds=(lock.fileno(), read_fd), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(read_fd)
        read_fd = None
        from .registry import ACTIVE, atomic_write, process_identity
        import time
        record = dict(worker_id=run_dir.name, identity=process_identity(process.pid),
                      socket_path=socket_path, run_dir=str(run_dir), started=time.time())
        atomic_write(ACTIVE, record)
        process.worker_id = run_dir.name
        channel = os.fdopen(write_fd, 'w')
        write_fd = None
        with channel:
            json.dump({'config': config, 'password': password}, channel)
        process.gui_socket = socket_path
        return process, run_dir
    finally:
        if write_fd is not None:
            os.close(write_fd)
        if process is None:
            Path(socket_dir).rmdir()
        if read_fd is not None:
            os.close(read_fd)
        lock.close()
