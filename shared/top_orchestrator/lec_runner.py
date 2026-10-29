"""Uploaded standard-library wrapper for Agent 4's authenticated system interface."""

import hashlib
import json
import os
import re
import runpy
import shutil
import sys
import zipfile
from pathlib import Path


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def proof_passed(report):
    return (report.get('status') == 'PASS' and report.get('equivalence') == 'EQUIVALENT'
            and report.get('verification_readiness') == 'READY_FOR_BACKEND_IMPLEMENTATION'
            and all(report.get(key) is True for key in (
                'ready_for_backend_implementation', 'allow_physical_design',
                'reference_hashes_verified', 'agent123_lineage_verified', 'formal_invoked'))
            and report.get('exit_code') == 0)


def execute(invocation):
    invocation = Path(invocation).resolve()
    request = json.loads((invocation / 'request.json').read_text())
    result = {'invocation_id': request['invocation_id'], 'mapped_state_digest': request['mapped_state_digest'],
              'input_unchanged': False, 'report': None}
    reports = invocation / 'verification_reports'
    reports.mkdir(exist_ok=False)
    code = 1
    try:
        mapped = Path(request['mapped_dir']).resolve(strict=True)
        netlist = Path(request['netlist']) if request.get('netlist') else None
        if netlist is not None:
            netlist = (netlist if netlist.is_absolute() else mapped / netlist).resolve(strict=True)
        else:
            choices = sorted((mapped / 'results').glob('*_mapped.v'))
            if len(choices) != 1:
                raise ValueError('Mapped netlist selection is ambiguous or missing; configure lec_netlist')
            netlist = choices[0].resolve(strict=True)
        if mapped not in netlist.parents or not netlist.is_file():
            raise ValueError('LEC netlist must belong to this mapped build')
        library_paths = request['libraries']
        provenance = []
        if not library_paths:
            summary = (mapped / 'setup_summary.txt').read_text()
            generic = re.findall(r'^Generic dir\s*:\s*(.+)$', summary, re.M)
            if len(generic) == 1:
                script = Path(generic[0].strip()) / 'run_genus_netlist.tcl'
                text = script.read_text()
                # Read a literal path from the actual synthesis script; never evaluate Tcl.
                library_paths = re.findall(r'^\s*set\s+LIBERTY\s+"([^"$\[\]\n]+)"\s*$', text, re.M)
                provenance.append(script)
        libraries = [Path(path).resolve(strict=True) for path in library_paths]
        if not libraries:
            raise ValueError('Configure lec_libraries / CAPSTONE_LEC_LIBRARIES with actual functional synthesis models')
        inputs = [netlist, *libraries, *provenance]
        source = Path(request['backend_rtl_dir']).resolve(strict=True)
        for name, expected in request['backend_rtl_hashes'].items():
            path = (source / name).resolve(strict=True)
            if source not in path.parents or digest(path) != expected:
                raise ValueError('Backend RTL input differs from the verified mailbox: ' + name)
            inputs.append(path)
        synthesis = {'library_files': [str(path) for path in libraries]}
        for key, filename in (('script', 'run_genus_mapped.tcl'), ('setup_summary', 'setup_summary.txt')):
            path = mapped / filename
            if not path.is_file():
                raise ValueError('Missing mapped synthesis provenance: ' + filename)
            synthesis[key] = str(path)
            inputs.append(path)
        reference = Path(request['reference_dir']).resolve(strict=True)
        snapshot = reference.parent
        inventory = json.loads((snapshot / 'snapshot.json').read_text())
        # Authenticate the complete published input inventory before using the saved run.
        if digest(snapshot / 'snapshot.json') != request['snapshot_digest']:
            raise ValueError('Remote reference snapshot differs from accepted validation')
        for name, expected in inventory['files'].items():
            path = (snapshot / name).resolve(strict=True)
            if snapshot not in path.parents or digest(path) != expected:
                raise ValueError('Remote verified input changed: ' + name)
            inputs.append(path)
        inputs.append(snapshot / 'snapshot.json')
        before = {str(path): digest(path) for path in inputs}
        handoff = {'schema_version': 1,
                   'reference': {'top': request['top_module'], 'validation_run': request['validation_run'],
                                 'rtl_hashes': request['reference_rtl_hashes']},
                   'implementation': {'top': request['top_module'], 'netlist': str(netlist),
                                      'hash': before[str(netlist)]}, 'synthesis': synthesis}
        handoff_path = reports / 'lec_handoff.json'
        handoff_path.write_text(json.dumps(handoff, indent=2) + '\n')
        sys.path[:0] = [str(Path(request['pipeline']).parent), request['shared_dir']]
        agent = runpy.run_path(request['pipeline'])
        tools = Path(request['project_dir']) / 'tools/oss-cad-suite/bin'
        executables = {}
        for name in ('eqy', 'yosys'):
            value = request[name]
            candidate = tools / name
            executables[name] = str(candidate) if value == name and not shutil.which(value) and candidate.is_file() else value
        tool_dirs = [str(Path(value).resolve().parent) for value in executables.values() if '/' in value]
        os.environ['PATH'] = os.pathsep.join([*tool_dirs, os.environ.get('PATH', '')])
        args = [str(reference), '--system', '--validation-run', request['validation_run'],
                '--lec-handoff', str(handoff_path), '--report-root', str(reports / 'agent4_runs'),
                '--timeout', str(request['proof_seconds']), '--eqy', executables['eqy'], '--yosys', executables['yosys']]
        print('[LEC] Comparing verified RTL against ' + str(netlist), flush=True)
        result['agent_returncode'] = agent['system_main'](args)
        paths = list((reports / 'agent4_runs').rglob('lec_report.json'))
        if len(paths) != 1:
            raise ValueError('Agent 4 did not produce exactly one LEC report')
        result['report'] = json.loads(paths[0].read_text())
        result['report_file'] = paths[0].relative_to(invocation).as_posix()
        after = {name: digest(name) for name in before}
        if before != after:
            raise ValueError('Protected mapped build or synthesis inputs changed during LEC')
        result.update(input_unchanged=True, input_hashes=before, netlist=str(netlist))
        if result['agent_returncode'] == 0 and proof_passed(result['report']):
            code = 0
        print('[LEC] Result: ' + str(result['report'].get('equivalence')), flush=True)
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        print(result['error'], file=sys.stderr, flush=True)
    finally:
        archive_path = invocation / 'reports.zip'
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(reports.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    archive.write(path, path.relative_to(invocation).as_posix())
        result['reports_digest'] = digest(archive_path)
        (invocation / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return code


if __name__ == '__main__':
    raise SystemExit(execute(sys.argv[1]))
