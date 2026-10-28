"""Uploaded scoped-consumer capability probe; Python 3.6 and standard library.

Execute discovery with Python networking disabled, before any agent invocation.
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import runpy
import socket
import sys

CAPABILITY = 'capstone.module_execution_plan.v1'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def consumer_hashes(pipeline, names):
    pipeline = Path(pipeline).resolve()
    if not isinstance(names, list) or not names or len(set(names)) != len(names) or pipeline.name not in names:
        raise ValueError('Capability response requires a unique consumer file inventory including the pipeline')
    hashes = {}
    for name in names:
        path = PurePosixPath(name)
        if not name or '\\' in name or path.is_absolute() or '..' in path.parts or path.suffix != '.py':
            raise ValueError('Nonportable consumer file')
        target = pipeline.parent.joinpath(*path.parts)
        try:
            target.resolve().relative_to(pipeline.parent.resolve())
        except ValueError:
            raise ValueError('Consumer file escapes pipeline directory')
        if any(part.is_symlink() for part in (target, *target.parents) if part != pipeline.parent.parent):
            raise ValueError('Symlinked consumer file')
        if not target.is_file():
            raise ValueError('Missing declared consumer file')
        hashes[name] = digest(target)
    return hashes


def check_request(inputs, plan_path, request_path):
    plan = json.loads(Path(plan_path).read_text())
    request = json.loads(Path(request_path).read_text())
    metadata = json.loads((inputs / 'snapshot.json').read_text())
    if (request.get('schema') != 'capstone.module_execution_request' or request.get('schema_version') != 1
            or plan.get('schema') != 'capstone.module_execution_plan' or plan.get('schema_version') != 1
            or plan.get('plan_sha256') != fingerprint({k: v for k, v in plan.items() if k != 'plan_sha256'})
            or request.get('plan_sha256') != plan['plan_sha256']
            or request.get('plan_file_sha256') != digest(plan_path)
            or request.get('input_snapshot_sha256') != digest(inputs / 'snapshot.json')
            or request.get('consumer_capability') != CAPABILITY or plan.get('consumer_capability') != CAPABILITY
            or request.get('identity') != plan.get('identity') or request.get('backend_ready') is not False
            or plan.get('backend_ready') is not False):
        raise ValueError('Scoped request/plan digest, schema or capability mismatch')
    identity = plan['identity']
    if (identity.get('module_name') != metadata.get('module_name')
            or identity.get('rtl_revision') != metadata.get('parent_rtl_revision')
            or identity.get('snapshot_sha256') != metadata.get('source_snapshot_digest')):
        raise ValueError('Scoped request belongs to different module inputs/parent')
    specs = list((inputs / 'specs').glob('*.yaml'))
    if len(specs) != 1 or digest(specs[0]) != identity.get('specification_sha256'):
        raise ValueError('Scoped request specification hash differs from uploaded inputs')
    return plan


def preflight(pipeline, inputs, plan_path, request_path):
    def blocked(*args, **kwargs):
        raise RuntimeError('Network disabled during scoped consumer discovery')
    socket.socket.connect = blocked
    socket.create_connection = blocked
    plan = check_request(inputs, plan_path, request_path)
    pipeline = Path(pipeline).resolve()
    before = {p.relative_to(pipeline.parent).as_posix(): digest(p)
              for p in pipeline.parent.rglob('*.py') if p.is_file() and not p.is_symlink()}
    sys.path.insert(0, str(pipeline.parent))
    sys.argv = [str(pipeline), '--scope-capabilities']
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            runpy.run_path(str(pipeline), run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise ValueError('Consumer does not support --scope-capabilities: ' + errors.getvalue()[-1500:])
    try:
        response = json.loads(output.getvalue())
    except ValueError:
        raise ValueError('Scoped consumer discovery must return one JSON capability document')
    if (response.get('schema') != 'capstone.module_execution_capabilities' or response.get('schema_version') != 1
            or CAPABILITY not in response.get('capabilities', [])
            or response.get('request_flag') != '--execution-request'
            or response.get('results_flag') != '--execution-results'):
        raise ValueError('Deployed consumer lacks the required scoped request/result interface')
    hashes = consumer_hashes(pipeline, response.get('consumer_files'))
    if any(before.get(name) != value for name, value in hashes.items()):
        raise ValueError('Consumer changed during scoped capability discovery')
    if pipeline.name not in hashes:
        raise ValueError('Capability inventory omits pipeline')
    return {'status': 'passed', 'scope': 'scoped_consumer_preflight',
            'module_name': plan['identity']['module_name'], 'plan_sha256': plan['plan_sha256'],
            'consumer_capability': CAPABILITY, 'pipeline_digest': digest(pipeline),
            'consumer_files': hashes, 'network_enabled': False, 'agent_execution': 'not_run',
            'backend_ready': False}


if __name__ == '__main__':
    pipeline, inputs, plan, request, output = map(Path, sys.argv[1:])
    try:
        result = preflight(pipeline, inputs, plan, request)
        code = 0
    except Exception as exc:
        result = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)}
        code = 1
    output.write_text(json.dumps(result, indent=2) + '\n')
    raise SystemExit(code)
