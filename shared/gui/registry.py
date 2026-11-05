"""Linux worker identity and atomic lifecycle records; no credentials."""
import json
import os
from pathlib import Path
from .run_reader import REPO_ROOT

PRIVATE = REPO_ROOT / 'shared/gui/.runtime'
ACTIVE = PRIVATE / 'active.json'


def atomic_write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, indent=2)
        handle.write('\n')
    temporary.replace(path)


def process_identity(pid):
    try:
        # comm may contain spaces or parentheses; fields follow its final ')'.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': int(pid), 'start_ticks': fields[19],
                'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, ValueError, IndexError):
        return None


def alive(record):
    expected = record.get('identity')
    return bool(isinstance(expected, dict) and isinstance(expected.get('pid'), int)
                and process_identity(expected['pid']) == expected)


def read_record(path=ACTIVE):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def lifecycle_path(directory):
    directory = Path(directory)
    return directory.parent / (directory.name + '.lifecycle.json')


class WorkerHandle:
    """Popen-like monitor for a worker launched by an earlier GUI process."""
    def __init__(self, record):
        self.record = record
        self.gui_socket = record['socket_path']
        self.pid = record['identity']['pid']
        self.worker_id = record['worker_id']

    def poll(self):
        latest = read_record(lifecycle_path(self.record['run_dir']))
        if alive(self.record):
            return None
        return latest.get('returncode', 1)


def discover_worker():
    record = read_record()
    if not alive(record):
        if record.get('run_dir'):
            path = lifecycle_path(record['run_dir'])
            saved = read_record(path)
            if not saved or saved.get('lifecycle') == 'running':
                saved = saved or dict(record)
                saved.update(lifecycle='interrupted', returncode=1,
                             remote_cleanup={'status': 'cleanup_unconfirmed',
                                             'reason': 'Worker died without a cleanup receipt.'})
                atomic_write(path, saved)
        return None
    # Socket handshake is checked by the UI; liveness alone never authorizes input.
    return WorkerHandle(record)
