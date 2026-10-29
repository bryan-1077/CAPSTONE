"""Experimental per-run Slurm reservation, reused by sequential remote stages."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sys
from uuid import uuid4

from shared.remote.ssh_executor import SSHExecutor
from .auth import cached_password, run_with_password_retries
from .display import TimestampedOutput


_session = ContextVar('shared_allocation_session', default=None)
ENVIRONMENT = 'CAPSTONE_EXPERIMENTAL_ALLOCATION'


def active_allocation(remote=None):
    session = _session.get()
    lease = session.lease if session is not None else None
    if lease is None and os.environ.get(ENVIRONMENT):
        lease = json.loads(os.environ[ENVIRONMENT])
    if lease is None:
        return None
    if not re.fullmatch(r'[1-9][0-9]*', str(lease.get('job_id', ''))):
        raise ValueError('Invalid shared Slurm job ID')
    if type(lease.get('cpus')) is not int or lease['cpus'] < 1:
        raise ValueError('Invalid shared CPU count')
    if remote is not None and (lease['host'], lease['username'], lease['port']) != (
            remote.host, remote.username, remote.port):
        raise ValueError('Shared allocation belongs to a different SSH server/account')
    return lease


def step_arguments(lease, job_name):
    return ['srun', '--jobid=' + str(lease['job_id']), '--nodes=1', '--ntasks=1',
            '--cpus-per-task=' + str(lease['cpus']), '--exact', '--job-name=' + job_name]


def thread_exports(cpus):
    return [f'export MAKEFLAGS=-j{cpus}', f'export OMP_NUM_THREADS={cpus}',
            'export PYTHONDONTWRITEBYTECODE=1']


def workspace(remote):
    lease = active_allocation(remote)
    root = PurePosixPath(remote.project_dir) / 'shared'
    return root / 'experiments/shared-allocation' / str(lease['job_id']) if lease else root


def backend_environment(remote):
    lease = active_allocation(remote)
    return {ENVIRONMENT: json.dumps(lease)} if lease else {}


def cancellation_command(remote, job_name):
    lease = active_allocation(remote)
    return ['scancel', lease['job_id']] if lease else ['scancel', '--name', job_name]


class AllocationSession:
    def __init__(self, config, directory):
        self.config = config
        self.directory = Path(directory)
        self.lease = None
        self.ssh = None
        self.record = {'status': 'not_requested'}
        self.job_name = 'capstone-shared-test-' + uuid4().hex
        self.timestamps = {name: TimestampedOutput() for name in ('stdout', 'stderr')}

    def output(self, name, text):
        name = 'stderr' if name == 'stderr' else 'stdout'
        text = self.timestamps[name].format(text)
        with (self.directory / 'shared_allocation.log').open('a', encoding='utf-8') as log:
            log.write(text)
        stream = sys.stderr if name == 'stderr' else sys.stdout
        stream.write(text)
        stream.flush()

    def save(self):
        (self.directory / 'shared_allocation.json').write_text(json.dumps(self.record, indent=2) + '\n')

    def acquire(self):
        if self.lease is not None:
            return
        from .remote import RemoteConfig
        remote = RemoteConfig.load(self.config.remote_config)
        cpus = self.config.shared_allocation_cpus
        hours = self.config.shared_allocation_hours
        queue_hours = self.config.shared_allocation_queue_hours
        if any(type(value) is not int or value < 1 for value in (cpus, hours, queue_hours)):
            raise ValueError('Shared CPUs, execution hours and queue hours must be positive integers')
        self.ssh = SSHExecutor(remote.host, remote.username, remote.port, remote.key_filename)
        self.ssh.password = cached_password(remote.host, remote.username)
        self.record.update(status='waiting', job_name=self.job_name, cpus=cpus, hours=hours,
                           queue_hours=queue_hours, host=remote.host, username=remote.username)
        self.save()
        from .legacy_allocations import cleanup_idle_legacy
        try:
            self.record['legacy_cleanup'] = cleanup_idle_legacy(self.ssh, remote, self.output)
        except Exception as exc:
            self.record['legacy_cleanup'] = {'error': str(exc)}
            self.output('stderr', '[SHARED] Could not verify old allocations; leaving them intact.\n')
        self.save()
        self.output('stdout', f'[SHARED] Requesting {cpus} CPUs once for {hours} hours; '
                    f'queue allowance {queue_hours} hours.\n')
        command = shlex.join(['salloc', '--no-shell', '--nodes=1', '--ntasks=1',
                              f'--cpus-per-task={cpus}', f'--time={hours:02d}:00:00',
                              '--partition=' + remote.partition, '--qos=' + remote.qos,
                              '--job-name=' + self.job_name])
        result = run_with_password_retries(self.ssh, command, ask_password=True,
                    timeout=queue_hours * 3600 + 90, on_output=self.output)
        self.record['allocation_result'] = result
        text = result.get('stdout', '') + '\n' + result.get('stderr', '')
        matches = re.findall(r'Granted job allocation ([1-9][0-9]*)', text)
        if not result['ok'] or len(set(matches)) != 1:
            self.record['status'] = 'allocation_failed'
            self.save()
            raise RuntimeError('Shared Slurm allocation failed: ' + text.strip())
        self.lease = {'job_id': matches[0], 'cpus': cpus, 'host': remote.host,
                      'username': remote.username, 'port': remote.port}
        self.record.update(status='allocated', lease=self.lease)
        self.save()
        self.output('stdout', f"[SHARED] Allocation {matches[0]} granted; remote stages will reuse it.\n")

    def close(self):
        if self.ssh is None:
            return
        try:
            # The unique job name also covers interruptions before salloc returns an ID.
            command = ['scancel', self.lease['job_id']] if self.lease else ['scancel', '--name', self.job_name]
            result = self.ssh.run(shlex.join(command), timeout=30)
            self.record['release_result'] = result
            gone = 'invalid job id' in result.get('stderr', '').lower()
            released = result['ok'] or gone
            self.record['status'] = 'released' if released else 'release_failed'
            self.save()
            self.output('stdout' if released else 'stderr',
                        '[SHARED] Allocation released or already ended.\n' if released else
                        '[SHARED] Could not release allocation; inspect shared_allocation.json.\n')
        except Exception as exc:
            self.record.update(status='release_failed', release_error=str(exc))
            self.save()
            self.output('stderr', '[SHARED] Allocation cleanup failed: ' + str(exc) + '\n')
        finally:
            self.ssh.close()
            self.ssh.password = None


@contextmanager
def shared_session(config, directory):
    session = AllocationSession(config, directory) if config.shared_allocation else None
    token = _session.set(session)
    try:
        yield
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            _session.reset(token)


def ensure_allocation():
    session = _session.get()
    if session is not None:
        session.acquire()
