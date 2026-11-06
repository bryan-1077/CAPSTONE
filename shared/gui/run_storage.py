"""Contained worker artifacts with read compatibility for older GUI runs."""
from pathlib import Path
import json
import threading

RUNTIME_WORKERS = Path(__file__).with_name('.runtime') / 'workers'


def startup_directory(directory):
    return RUNTIME_WORKERS / Path(directory).name


def artifact_path(directory, name, legacy_suffix):
    directory = Path(directory)
    contained = directory / name
    legacy = directory.parent / (directory.name + legacy_suffix)
    startup = startup_directory(directory) / name
    return next((path for path in (contained, legacy, startup) if path.is_file()), contained)


def lifecycle_path(directory):
    return artifact_path(directory, 'gui_lifecycle.json', '.lifecycle.json')


def worker_log_path(directory):
    return artifact_path(directory, 'worker.log', '.worker.log')


def consolidate_completed_run(directory):
    """Move old sidecars only after a terminal receipt and verified worker exit.

    Preserve existing destinations and all receipt contents. No live worker's
    evidence is moved, since older code can still write to its sidecar paths.
    """
    from .registry import alive
    from .platform_support import host_capabilities
    if not host_capabilities().local_execution:
        return []
    directory = Path(directory)
    receipt = directory.parent / (directory.name + '.lifecycle.json')
    try:
        record = json.loads(receipt.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('lifecycle') not in (
                'complete', 'failed', 'cancelled', 'needs_attention', 'interrupted') or alive(record):
            return []
        directory.mkdir(parents=True, exist_ok=True)
        moved = []
        for source, name in ((receipt, 'gui_lifecycle.json'),
                             (directory.parent / (directory.name + '.worker.log'), 'worker.log')):
            destination = directory / name
            if source.is_file() and not destination.exists():
                source.replace(destination)
                moved.append(name)
        return moved
    except (OSError, ValueError):
        # Read-only history folders and malformed legacy receipts remain readable
        # through the compatibility paths; opening the GUI must still succeed.
        return []


class RunStorage:
    """Stage startup evidence privately until the orchestrator creates the run.

    The orchestrator requires a nonexistent run directory. Publishing on the first
    log write after it creates that directory keeps its ownership check intact.
    An open log descriptor stays valid when its file is moved on the same disk.
    """
    def __init__(self, directory, record):
        from .registry import atomic_write
        self.directory = Path(directory)
        self.startup = startup_directory(directory)
        self.startup.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.log = self.startup / 'worker.log'
        self.lock = threading.RLock()
        atomic_write(self.startup / 'gui_lifecycle.json', record)

    def publish(self, *, create=False):
        with self.lock:
            if create:
                self.directory.mkdir(parents=True, exist_ok=True)
            if not self.directory.is_dir():
                return
            for name in ('worker.log', 'gui_lifecycle.json'):
                source, destination = self.startup / name, self.directory / name
                if source.is_file():
                    if destination.exists():
                        raise FileExistsError(f'Run artifact already exists: {destination}')
                    source.replace(destination)
            try:
                self.startup.rmdir()
            except OSError:
                pass


class RunOutput:
    def __init__(self, storage, stream):
        self.storage, self.stream = storage, stream

    def write(self, text):
        with self.storage.lock:
            self.storage.publish()
            return self.stream.write(text)

    def flush(self):
        if not self.stream.closed:
            self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)
