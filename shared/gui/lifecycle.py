"""Cooperative cancellation and bounded cleanup for the owned worker session."""
import os
from pathlib import Path
import signal
import time
from .registry import process_identity, read_record


class RunCancelled(BaseException):
    """Bypass stage repair routing while allowing adapter finally blocks."""


def session_processes():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[3]) == os.getpid():  # Linux stat field 6: session ID.
                identity = process_identity(int(entry.name))
                if identity:
                    found.append(identity)
        except (OSError, ValueError, IndexError):
            continue
    return found


def cleanup_local(grace=2):
    targets = session_processes()
    for number in (signal.SIGTERM, signal.SIGKILL):
        for target in targets:
            if process_identity(target['pid']) == target:
                try:
                    os.kill(target['pid'], number)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            targets = [p for p in targets if process_identity(p['pid']) == p]
            if not targets:
                return {'status': 'terminated', 'remaining': []}
            time.sleep(0.05)
    return {'status': 'cleanup_unconfirmed', 'remaining': targets}


def remote_cleanup(directory, required):
    """Report evidence conservatively: release receipt is not a queue query."""
    if not required:
        return {'status': 'not_required'}
    records = list(Path(directory).rglob('shared_allocation.json'))
    if not records:
        return {'status': 'not_submitted', 'reason': 'No allocation request recorded'}
    allocation = read_record(records[-1])
    return {'status': allocation.get('cleanup_status', 'cleanup_unconfirmed'),
            'allocation': allocation.get('lease'), 'job_name': allocation.get('job_name'),
            'release_status': allocation.get('status'),
            'queue_confirmation': allocation.get('queue_confirmation'),
            'reason': 'Queue disappearance is confirmed only when cleanup_status is terminated.'}
