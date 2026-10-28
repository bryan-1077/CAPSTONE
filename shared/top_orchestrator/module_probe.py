"""Standalone module consumer preflight/evidence helpers, compatible with Python 3.6.

Uploaded beside validation_runner.py. Preflight disables Python socket connections
before importing the deployed reader or examining pipeline --help.
"""
import contextlib
import hashlib
import importlib
import io
import json
from pathlib import Path
import runpy
import socket
import sys

SOURCE_FIELDS = ('source_requirement', 'source_obligation', 'source_contract')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_catalog(rows):
    result = {}
    for row in rows:
        key = row.get('source_id')
        if key:
            if key in result:
                raise ValueError('Duplicate specification source ID: ' + key)
            result[key] = {name: row[name] for name in SOURCE_FIELDS if name in row}
    return result


def expected_source_catalog(spec):
    behavior = spec['behavior']
    expected = {}
    for row in spec['verification_requirements']:
        if row['id'] in expected:
            raise ValueError('Duplicate source requirement ID')
        expected[row['id']] = {'source_requirement': row}
    for key, value in behavior['verification_obligations'].items():
        if key in expected:
            raise ValueError('Source requirement and obligation IDs collide')
        expected[key] = {'source_obligation': value}
    if 'behavior.contract' in expected:
        raise ValueError('Reserved contract source ID collision')
    expected['behavior.contract'] = {'source_contract': behavior['contract']}
    return expected


def preflight(pipeline, inputs):
    def blocked(*args, **kwargs):
        raise RuntimeError('Network disabled during consumer preflight')
    socket.socket.connect = blocked
    socket.create_connection = blocked
    pipeline, inputs = Path(pipeline).resolve(), Path(inputs).resolve()
    sys.path.insert(0, str(pipeline.parent))
    import yaml
    reader = importlib.import_module('requirement_verification')
    expected_reader = pipeline.parent / 'requirement_verification.py'
    if Path(reader.__file__).resolve() != expected_reader.resolve():
        raise ValueError('Loaded requirement reader is outside the deployed pipeline directory')
    if getattr(reader, 'SPECIFICATION_CATALOG_VERSION', None) != 1:
        raise ValueError('Deployed reader lacks SPECIFICATION_CATALOG_VERSION=1; deploy the updated requirement reader before agent execution')
    reader_digest, pipeline_digest = digest(expected_reader), digest(pipeline)
    files = list((inputs / 'specs').glob('*.yaml'))
    if len(files) != 1 or len(list((inputs / 'rtl').glob('*.sv'))) != 1:
        raise ValueError('Module compatibility requires exactly one YAML/SV pair')
    spec = yaml.safe_load(files[0].read_text())
    expected = expected_source_catalog(spec)
    catalog = reader.build_requirement_catalog(spec)
    if not expected or source_catalog(catalog) != expected:
        raise ValueError('Deployed catalog drops or changes source requirements, obligations or contract')
    reader.attach_static_results(catalog, next((inputs / 'rtl').glob('*.sv')).read_text())
    combined = reader.combine_requirement_results(catalog, {'requirements': [], 'overall_status': 'INCOMPLETE'})
    if source_catalog(combined['requirements']) != expected:
        raise ValueError('Deployed combined results drop source metadata')
    for row in combined['requirements']:
        if row.get('source_id') and row.get('status') != 'NOT_TESTED':
            raise ValueError('Unexecuted source obligations must remain NOT_TESTED')
    # Conventional CLI discovery only, with networking disabled. The real run
    # still has to prove module-mode execution and source coverage.
    output = io.StringIO()
    sys.argv = [str(pipeline), '--help']
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(str(pipeline), run_name='__main__')
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise ValueError('Deployed pipeline --help failed')
    if '--jobs' not in output.getvalue():
        raise ValueError('Deployed pipeline does not advertise the module --jobs interface')
    if digest(expected_reader) != reader_digest or digest(pipeline) != pipeline_digest:
        raise ValueError('Deployed consumer changed during preflight')
    metadata = json.loads((inputs / 'snapshot.json').read_text())
    if spec['design_name'] != metadata['module_name']:
        raise ValueError('Prepared module identity mismatch')
    return {'status': 'passed', 'scope': 'one_module_consumer_preflight',
            'module_name': spec['design_name'], 'pipeline_digest': pipeline_digest,
            'consumer_digest': reader_digest, 'catalog_version': 1,
            'source_catalog': expected, 'network_enabled': False, 'agent_execution': 'not_run',
            'backend_ready': False}


def requirement_lists(value):
    if isinstance(value, dict):
        if isinstance(value.get('requirements'), list):
            yield value['requirements']
        for child in value.values():
            for rows in requirement_lists(child):
                yield rows
    elif isinstance(value, list):
        for child in value:
            for rows in requirement_lists(child):
                yield rows


def verify_source_coverage(reports, expected):
    """Require one complete passed source catalog in actual retrieved reports."""
    if not expected:
        raise ValueError('Missing expected specification source catalog')
    reports = Path(reports)
    for path in sorted(reports.rglob('*.json')):
        if path.is_symlink():
            raise ValueError('Symlinked module report')
        try:
            document = json.loads(path.read_text())
        except (ValueError, UnicodeError):
            continue
        for rows in requirement_lists(document):
            try:
                actual = source_catalog(rows)
            except (ValueError, AttributeError):
                continue
            if actual != expected:
                continue
            selected = [row for row in rows if row.get('source_id')]
            if not all(row.get('status') == 'PASS' and row.get('method') == 'simulation'
                       and isinstance(row.get('evidence'), str) and row['evidence'].strip()
                       for row in selected):
                continue
            return {'status': 'passed', 'report': path.relative_to(reports).as_posix(),
                    'report_digest': digest(path), 'source_ids': sorted(expected)}
    raise ValueError('Reports lack complete passed specification source requirements/obligations/contract; module compatibility is not accepted')


def main():
    output = Path(sys.argv[3])
    try:
        result = preflight(sys.argv[1], sys.argv[2])
    except Exception as exc:
        result = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc),
                  'agent_execution': 'not_run', 'backend_ready': False}
    output.write_text(json.dumps(result, indent=2) + '\n')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
