"""Agent 4 gate between backend mapping and physical implementation."""

import hashlib
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from shared.controller_manifest import resolve_path
from .display import TimestampedOutput, print_node_header
from .gdsii_validation import remote_path
from .lec_runner import proof_passed
from .mailbox import verify
from .remote import RemoteConfig, slurm_command
from .validation import unpack_reports, verify_backend_handoff


def lec_request(state, remote):
    mailbox = Path(state['mailbox_source'])
    metadata = verify(mailbox)
    validation = verify_backend_handoff(Path(state['lec_validation_result']), mailbox)
    if validation.get('validation_mode') != 'system':
        raise ValueError('Agent 4 requires an accepted system verification run')
    if (state.get('mailbox_revision') != metadata['rtl_revision'] or
            any(state.get('stage_status', {}).get(stage) != 'success'
                for stage in ('rtl_prep', 'netlist', 'mapped_netlist'))):
        raise ValueError('LEC requires successful mapping of the verified mailbox revision')
    if (state.get('ssh_host') != remote.host or state.get('ssh_user') != remote.username
            or state.get('ssh_port', 22) != remote.port
            or validation.get('host') != remote.host or validation.get('username') != remote.username):
        raise ValueError('LEC, backend and verification must use the same SSH server/account')
    manifest_path = mailbox / 'rtl/manifest.json'
    manifest = json.loads(manifest_path.read_text())
    top = manifest['top_module']
    if state.get('top_module') != top or state.get('mapped_top_module', top) != top:
        raise ValueError('Mapped top differs from the verified controller top')
    run_id = validation['remote_result']['summary']['run_id']
    if not isinstance(run_id, str) or PurePosixPath(run_id).name != run_id or run_id in ('.', '..'):
        raise ValueError('Invalid saved verification run identity')
    reference = PurePosixPath(validation['remote_directory']) / 'input/rtl'
    validation_run = reference / 'verification_reports' / run_id
    root = state['remote_project_root']
    mapped = remote_path(root, state.get('outputs', {}).get('mapped_remote_dir') or state.get('mapped_candidate_path'))
    staged_hashes = {name.removeprefix('rtl/'): value for name, value in metadata['files'].items()
                     if name.startswith('rtl/')}
    if state.get('mailbox_file_hashes') != staged_hashes:
        raise ValueError('Backend staged inventory differs from the verified mailbox')
    reference_hashes = {name: metadata['files'][resolve_path(manifest_path, manifest, name).relative_to(mailbox).as_posix()]
                        for name in manifest['rtl_files']}
    identity = {'mapped_dir': mapped, 'mailbox_revision': metadata['rtl_revision'], 'top_module': top}
    return {'mapped_state_digest': hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
            'mailbox_revision': metadata['rtl_revision'], 'project_dir': remote.project_dir,
            'pipeline': remote_path(remote.project_dir, remote.lec_script), 'shared_dir': remote.project_dir + '/shared',
            'mapped_dir': mapped, 'netlist': remote.lec_netlist, 'top_module': top,
            'reference_dir': str(reference), 'validation_run': str(validation_run),
            'snapshot_digest': validation['snapshot_digest'], 'reference_rtl_hashes': reference_hashes,
            'backend_rtl_dir': state['remote_prepare_input'], 'backend_rtl_hashes': staged_hashes,
            'libraries': [remote_path(remote.project_dir, path) for path in remote.lec_libraries],
            'eqy': remote.lec_eqy, 'yosys': remote.lec_yosys, 'proof_seconds': remote.lec_proof_seconds}


def run_lec_gate(state, ssh):
    token = uuid4().hex
    logs = state.get('backend_log_dir') or Path(__file__).resolve().parents[2] / 'local/backend/logs'
    directory = Path(logs) / 'lec' / token
    directory.mkdir(parents=True, exist_ok=False)
    result_path = directory / 'lec_result.json'
    record = {'status': 'failed'}
    launched = finished = False
    print_node_header('lec')
    with (directory / 'lec.log').open('w', encoding='utf-8') as log:
        timestamps = {name: TimestampedOutput() for name in ('stdout', 'stderr')}

        def output(name, text):
            stream_name = 'stderr' if name == 'stderr' else 'stdout'
            text = timestamps[stream_name].format(text)
            log.write(text)
            log.flush()
            stream = sys.stderr if stream_name == 'stderr' else sys.stdout
            stream.write(text)
            stream.flush()

        try:
            remote = RemoteConfig.load(Path(state['lec_remote_config']) if state.get('lec_remote_config') else None)
            if not remote.allow_validation_output_dirs:
                raise ValueError('Remote validation output directories are disabled')
            request = lec_request(state, remote)
            request['invocation_id'] = token
            from .allocation import workspace
            invocation = workspace(remote) / 'lec_runs' / token
            record.update(request=request, remote_directory=str(invocation))
            request_path = directory / 'lec_request.json'
            request_path.write_text(json.dumps(request, indent=2) + '\n')
            prepared = ssh.run('mkdir -p -- ' + shlex.quote(str(invocation.parent)) +
                               ' && mkdir -- ' + shlex.quote(str(invocation)), timeout=60, on_output=output)
            if not prepared['ok']:
                raise RuntimeError(prepared.get('stderr') or 'Could not create LEC workspace')
            ssh.upload_file(request_path, str(invocation / 'request.json'), exclusive=True)
            ssh.upload_file(Path(__file__).with_name('lec_runner.py'), str(invocation / 'runner.py'), exclusive=True)
            job_name = 'capstone-lec-' + token
            command = slurm_command(replace(remote, job_seconds=remote.lec_job_seconds, cpus=remote.lec_cpus),
                                    [remote.python, '-B', '-u', str(invocation / 'runner.py'), str(invocation)], job_name,
                                    queue_normally=True)
            record['command'] = command
            record['queue_wait_seconds'] = remote.lec_queue_wait_seconds
            record['timeout_seconds'] = remote.lec_timeout_seconds
            from .allocation import active_allocation
            lease = active_allocation(remote)
            if lease:
                output('stdout', f"[LEC] Reusing shared allocation {lease['job_id']} with {lease['cpus']} CPUs.\n")
            else:
                output('stdout', '[LEC] Waiting in the Slurm queue; SSH time allowance includes '
                       f'{remote.lec_queue_wait_seconds // 60} minutes for queue waiting and '
                       f'{remote.lec_job_seconds // 60} minutes for execution.\n')
            launched = True
            execution = ssh.run(command, timeout=remote.lec_timeout_seconds, on_output=output)
            finished = execution['exit_code'] >= 0
            record['execution'] = execution
            if not finished:
                raise RuntimeError(execution.get('stderr') or 'LEC execution interrupted')
            diagnostic = (execution.get('stderr') or execution.get('stdout') or '').strip()
            allocation_errors = ('unable to allocate resources', 'invalid partition', 'invalid qos',
                                 'invalid account', 'unable to contact slurm controller', 'job submit/allocate failed')
            if execution['exit_code'] != 0 and any(marker in diagnostic.lower() for marker in allocation_errors):
                raise RuntimeError(f"LEC Slurm allocation failed (exit {execution['exit_code']}): {diagnostic}")
            for name in ('result.json', 'reports.zip'):
                fetched = ssh.fetch_file(str(invocation / name), directory / name)
                if not fetched['ok']:
                    detail = diagnostic or fetched.get('error') or 'No execution diagnostics were returned'
                    raise RuntimeError(f"LEC job exited with code {execution['exit_code']}; "
                                       f"could not collect {name}: {detail}")
            payload = json.loads((directory / 'result.json').read_text())
            record['remote_result'] = payload
            if (payload.get('invocation_id') != token or
                    payload.get('mapped_state_digest') != request['mapped_state_digest']):
                raise ValueError('LEC report belongs to a different mapped invocation')
            if hashlib.sha256((directory / 'reports.zip').read_bytes()).hexdigest() != payload.get('reports_digest'):
                raise ValueError('LEC report archive digest mismatch')
            unpack_reports(directory / 'reports.zip', directory / 'reports')
            report = payload.get('report') or {}
            report_file = payload.get('report_file')
            if report_file:
                relative = PurePosixPath(report_file)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Invalid LEC report path')
                if json.loads((directory / 'reports' / report_file).read_text()) != report:
                    raise ValueError('Archived LEC report differs from the invocation result')
            if (execution['exit_code'] != 0 or payload.get('agent_returncode') != 0
                    or payload.get('input_unchanged') is not True or not report_file
                    or not payload.get('input_hashes') or not proof_passed(report)):
                raise ValueError(payload.get('error') or report.get('failure_detail') or
                                 'Agent 4 did not establish complete equivalence')
            record.update(status='passed', message='Agent 4 proved mapped-netlist equivalence.')
        except (Exception, KeyboardInterrupt) as exc:
            record['message'] = 'LEC stopped: ' + type(exc).__name__ + ': ' + str(exc)
        finally:
            if launched and not finished:
                try:
                    from .allocation import cancellation_command
                    record['cancellation'] = ssh.run(shlex.join(cancellation_command(remote, job_name)), timeout=15)
                except Exception as exc:
                    record['cancellation_error'] = str(exc)
    result_path.write_text(json.dumps(record, indent=2) + '\n')
    passed = record['status'] == 'passed'
    return {**state, 'lec_status': record['status'], 'lec_result': str(result_path),
            'lec_report_dir': str(directory), 'lec_input_hashes': record.get('remote_result', {}).get('input_hashes', {}) if passed else {},
            'stage_status': {**state.get('stage_status', {}), 'lec': 'success' if passed else 'failed'},
            'history': state['history'] + [record['message']], 'last_error': None if passed else record['message']}


def verify_lec_inputs(state, ssh):
    """Reject modified mapped inputs before starting physical implementation."""
    if state.get('lec_status') != 'passed' or not state.get('lec_input_hashes'):
        raise ValueError('GDSII requires a passing LEC receipt for this mapped build')
    receipt = json.loads(Path(state['lec_result']).read_text())
    mapped = remote_path(state['remote_project_root'], state.get('outputs', {}).get('mapped_remote_dir')
                         or state.get('mapped_candidate_path'))
    if (receipt.get('status') != 'passed' or receipt['request']['mapped_dir'] != mapped
            or receipt['request']['mailbox_revision'] != state.get('mailbox_revision')
            or receipt['remote_result']['input_hashes'] != state['lec_input_hashes']):
        raise ValueError('LEC receipt does not cover this mapped build and mailbox revision')
    script = ('import hashlib,json,pathlib; hashes=json.loads(' + repr(json.dumps(state['lec_input_hashes'])) + '); '
              'assert all(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()==h for p,h in hashes.items()), '
              '"LEC inputs changed after proof"')
    result = ssh.run(shlex.join([state.get('remote_python_cmd') or 'python3', '-c', script]),
                     cwd=state['remote_project_root'], timeout=300)
    if not result['ok']:
        raise ValueError('Mapped inputs changed or could not be verified after LEC')
