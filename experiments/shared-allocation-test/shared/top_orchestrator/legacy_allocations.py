"""Release only old, demonstrably idle one-CPU CAPSTONE shell allocations."""

import json
from pathlib import PurePosixPath
import re
import shlex


def value(text, key):
    match = re.search(r'(?<!\S)' + re.escape(key) + r'=(.*?)(?=\s+[A-Za-z][A-Za-z0-9_/]*=|\n|$)', text)
    return match.group(1).strip() if match else ''


def eligible(text, remote):
    runtime = value(text, 'RunTime')
    try:
        days, clock = runtime.split('-', 1) if '-' in runtime else ('0', runtime)
        hours, minutes, seconds = map(int, clock.split(':'))
        elapsed = int(days) * 86400 + hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return False
    return (value(text, 'JobName') == 'ecen-454' and value(text, 'JobState') == 'RUNNING'
            and value(text, 'UserId').split('(')[0] == remote.username
            and value(text, 'NumCPUs') == '1' and value(text, 'Command') in ('bash', '/bin/bash')
            and PurePosixPath(value(text, 'WorkDir')) == PurePosixPath(remote.project_dir)
            and elapsed >= 600)


PROBE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
job = sys.argv[1]
own_step = os.environ.get('SLURM_STEP_ID') or os.environ.get('SLURM_STEPID')
def observe():
    result = subprocess.run(['scontrol', 'listpids', job], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
    if result.returncode != 0 or not own_step:
        return None
    rows = [line.split() for line in result.stdout.splitlines()[1:] if line.strip()]
    if not rows or any(len(row) < 3 or not row[0].isdigit() for row in rows):
        return None
    # No other workload steps may coexist with the legacy shell and this probe.
    if any(row[2] not in ('0', own_step) for row in rows):
        return None
    original = [row[0] for row in rows if row[2] == '0']
    if len(original) != 1:
        return None
    pid = original[0]
    root = Path('/proc') / pid
    if root.joinpath('comm').read_text().strip() != 'bash':
        return None
    if root.joinpath('task', pid, 'children').read_text().strip():
        return None
    stat = root.joinpath('stat').read_text().rsplit(')', 1)[1].split()
    if stat[0] != 'S':
        return None
    # Include process start ticks to reject PID reuse between observations.
    return [pid, stat[19]]
try:
    before = observe()
    time.sleep(1)
    after = observe()
    print(json.dumps({'idle': before is not None and before == after, 'identity': after}))
except Exception as exc:
    print(json.dumps({'idle': False, 'error': str(exc)}))
'''


def cleanup_idle_legacy(ssh, remote, output):
    record = {'released': [], 'retained': []}
    queued = ssh.run(shlex.join(['squeue', '--me', '--noheader', '--format=%i|%j|%T']), timeout=30)
    if not queued['ok']:
        record['query_error'] = queued.get('stderr') or queued.get('error') or 'Could not inspect allocations'
        return record
    for line in queued.get('stdout', '').splitlines():
        fields = [part.strip() for part in line.split('|')]
        if len(fields) != 3 or fields[1:] != ['ecen-454', 'RUNNING'] or not fields[0].isdigit():
            continue
        job = fields[0]
        description = ssh.run(shlex.join(['scontrol', 'show', 'job', job]), timeout=30)
        if not description['ok'] or not eligible(description.get('stdout', ''), remote):
            continue
        output('stdout', f'[SHARED] Checking old project shell allocation {job} for active work.\n')
        command = shlex.join(['srun', '--jobid=' + job, '--overlap', '--nodes=1', '--ntasks=1',
                              '--cpus-per-task=1', '--time=00:01:00', remote.python, '-c', PROBE, job])
        result = ssh.run(command, timeout=45)
        try:
            evidence = json.loads(result.get('stdout', '')) if result['ok'] else {}
        except ValueError:
            evidence = {}
        if evidence.get('idle') is not True:
            record['retained'].append({'job_id': job, 'reason': 'Active work or unverifiable process state', 'evidence': evidence})
            output('stdout', f'[SHARED] Keeping allocation {job}: active work or idle state could not be verified.\n')
            continue
        # Recheck ownership and project identity immediately before cancellation.
        current = ssh.run(shlex.join(['scontrol', 'show', 'job', job]), timeout=30)
        if not current['ok'] or not eligible(current.get('stdout', ''), remote):
            record['retained'].append({'job_id': job, 'reason': 'Allocation identity changed'})
            continue
        released = ssh.run(shlex.join(['scancel', job]), timeout=30)
        record['released' if released['ok'] else 'retained'].append(
            {'job_id': job, 'evidence': evidence, 'cancellation': released})
        output('stdout' if released['ok'] else 'stderr',
               f'[SHARED] Released verified idle legacy allocation {job}.\n' if released['ok'] else
               f'[SHARED] Could not release idle allocation {job}.\n')
    return record
