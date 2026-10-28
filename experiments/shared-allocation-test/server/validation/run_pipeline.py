#!/usr/bin/env python3
"""
run_pipeline.py
===============
Chains all three agents in sequence.

Examples:
  python3 run_pipeline.py rtl/basicCounter.sv
  python3 run_pipeline.py rtl/
  python3 run_pipeline.py rtl/top.sv rtl/submodule.sv

Add --reuse-tb to rerun Agent 2's existing reviewed testbench without regeneration.
Reports are written beneath the tested input directory in verification_reports.
"""

import sys
import os
import json
import subprocess
import shutil
import tempfile
import argparse
import re
import signal
import threading
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

CYAN   = "\033[96m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

VALIDATION_RESULT_FILE = "validation_result.json"
REPORTS_DIRECTORY = "verification_reports"
REPORT_RUN_ENV = "RTL_VERIFICATION_REPORT_RUN"
STAGES = ('agent1', 'agent2', 'agent3')
_ACTIVE_PROCESSES = set()
_PROCESS_LOCK = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_report_json(path, data):
    """Readers see either the previous complete JSON document or the new one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=os.path.dirname(path), delete=False) as handle:
        json.dump(data, handle, indent=2)
        handle.write('\n')
        pending = handle.name
    os.replace(pending, path)


def read_report_json(path):
    try:
        with open(path) as handle:
            result = json.load(handle)
            return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def report_input_directory(paths):
    paths = [os.path.abspath(p) for p in paths if p and not p.startswith('--')]
    if not paths:
        raise ValueError('An RTL input path is needed to locate verification reports.')
    directories = [p if os.path.isdir(p) else os.path.dirname(p) for p in paths]
    return os.path.commonpath(directories)


def allocate_report_directory(root):
    started_at = datetime.now().astimezone()
    timestamp = started_at.strftime('%Y-%m-%d_%H-%M-%S')
    os.makedirs(root, exist_ok=True)
    sequence = 1
    while True:
        run_id = timestamp if sequence == 1 else '{}_{:02d}'.format(timestamp, sequence)
        run_dir = os.path.join(root, run_id)
        try:
            # Reserve the name atomically so same-second runs never overwrite reports.
            os.mkdir(run_dir)
            break
        except FileExistsError:
            sequence += 1
    return run_dir, started_at


def create_report_run(paths, requested=STAGES, run_dir=None, metadata_extra=None):
    input_directory = report_input_directory(paths)
    if run_dir is None:
        run_dir, started_at = allocate_report_directory(os.path.join(input_directory, REPORTS_DIRECTORY))
    else:
        os.makedirs(run_dir)
        started_at = datetime.now().astimezone()
    run_id = os.path.basename(run_dir)
    write_report_json(os.path.join(run_dir, 'run.json'), {
        'schema_version': 1, 'run_id': run_id,
        'created_at': started_at.astimezone(timezone.utc).isoformat(),
        'created_at_local': started_at.isoformat(),
        'input_directory': input_directory,
        'inputs': [os.path.abspath(p) for p in paths],
        'requested_stages': list(requested), **(metadata_extra or {})})
    for stage in dict.fromkeys(STAGES + tuple(requested)):
        write_report_json(os.path.join(run_dir, stage, 'report.json'), {
            'schema_version': 1, 'run_id': run_id, 'agent': stage,
            'status': 'PENDING' if stage in requested else 'SKIPPED',
            'reason': '' if stage in requested else 'Not requested in this run.',
            'exit_code': None, 'requirements': [], 'failed_requirements': [],
            'untested_requirements': [], 'coverage_limitations': [], 'artifacts': {}})
    refresh_report_summary(run_dir, publish_latest=True)
    return run_dir


def refresh_report_summary(run_dir, publish_latest=False):
    metadata = read_report_json(os.path.join(run_dir, 'run.json'))
    stages = {s: read_report_json(os.path.join(run_dir, s, 'report.json'))
              for s in dict.fromkeys(STAGES + tuple(metadata['requested_stages']))}
    requested = [stages[s] for s in metadata['requested_stages']]
    statuses = [s['status'] for s in requested]
    pending = any(s in ('PENDING', 'RUNNING') for s in statuses)
    gaps = any(s.get('untested_requirements') or s.get('untested_tests') or s.get('coverage_limitations')
               for s in requested)
    if pending:
        status = 'RUNNING'
    elif metadata.get('pipeline_error') or any(s in ('FAIL', 'ERROR', 'INTERRUPTED') for s in statuses):
        status = 'FAIL'
    elif any(s in ('SKIPPED', 'NOT_TESTED', 'INCOMPLETE') for s in statuses):
        status = 'INCOMPLETE'
    elif gaps or 'PASS_WITH_GAPS' in statuses:
        status = 'PASS_WITH_GAPS'
    else:
        status = 'PASS'
    summary = dict(metadata, updated_at=utc_now(), overall_status=status,
                   execution_completed=not pending,
                   execution_succeeded=not pending and not metadata.get('pipeline_error') and
                                       all(s.get('exit_code') == 0 for s in requested),
                   verification_complete=status == 'PASS', needs_review=status != 'PASS',
                   stages=stages,
                   failed_requirements=[dict(row, agent=s['agent']) for s in requested
                                        for row in s.get('failed_requirements', [])],
                   untested_requirements=[dict(row, agent=s['agent']) for s in requested
                                          for row in s.get('untested_requirements', [])],
                   failed_tests=[dict(row, agent=s['agent']) for s in requested for row in s.get('failed_tests', [])],
                   untested_tests=[dict(row, agent=s['agent']) for s in requested for row in s.get('untested_tests', [])],
                   coverage_limitations=[{'agent': s['agent'], 'reason': reason} for s in requested
                                         for reason in s.get('coverage_limitations', [])],
                   artifact_base=metadata.get('artifact_base', metadata['run_id']),
                   summary_file=metadata.get('artifact_base', metadata['run_id']) + '/summary.json')
    write_report_json(os.path.join(run_dir, 'summary.json'), summary)
    lines = ['# Verification report', '', 'Run: `{}`'.format(metadata['run_id']), '',
             'Overall status: **{}**'.format(status), '',
             '| Stage | Status | Report |', '| --- | --- | --- |']
    for stage in stages:
        lines.append('| {} | {} | [JSON]({}/report.json) |'.format(stage, stages[stage]['status'], stage))
    if metadata.get('pipeline_error'):
        lines.extend(['', 'Pipeline error: ' + metadata['pipeline_error']])
    for label, field in (('Failed requirements', 'failed_requirements'), ('Untested requirements', 'untested_requirements'),
                         ('Failed vector tests', 'failed_tests'), ('Untested vector tests', 'untested_tests')):
        if summary[field]:
            lines.extend(['', '## ' + label, ''])
            for row in summary[field]:
                lines.append('- {} / {}: {}'.format(row['agent'], row.get('id', row.get('name', '?')),
                              row.get('requirement', row.get('text', row.get('evidence', '')))))
    if summary['coverage_limitations']:
        lines.extend(['', '## Coverage limitations', ''])
        lines.extend('- {}: {}'.format(row['agent'], row['reason']) for row in summary['coverage_limitations'])
    lines.extend(['', 'Each stage report lists its saved logs and evidence under `artifacts`.',
                  'Artifact paths are relative to this run folder. The front end can read',
                  '`verification_reports/latest.json` for the latest status and `artifact_base`.', ''])
    with open(os.path.join(run_dir, 'summary.md'), 'w') as handle:
        handle.write('\n'.join(lines))
    if metadata.get('batch_parent'):
        refresh_batch_summary(metadata['batch_parent'])
        return summary
    latest = os.path.join(os.path.dirname(run_dir), 'latest.json')
    previous = read_report_json(latest)
    # An older run finishing late must not replace the newest run's pointer.
    if publish_latest or previous is None or previous.get('run_id') == metadata['run_id']:
        write_report_json(latest, summary)
    return summary


class AgentReport:
    """Reporting used by both agent CLIs and the pipeline, without changing tests."""
    def __init__(self, agent, inputs=None, run_dir=None):
        self.agent = agent
        self.run_dir = run_dir or os.environ.get(REPORT_RUN_ENV)
        if not self.run_dir:
            self.run_dir = create_report_run(inputs or [], requested=(agent,))
        self.directory = os.path.join(self.run_dir, agent)
        self.path = os.path.join(self.directory, 'report.json')
        self.data = read_report_json(self.path)
        if not self.data:
            raise RuntimeError('Missing report session: ' + self.path)

    def update(self, **values):
        self.data.update(values)
        self.data['updated_at'] = utc_now()
        write_report_json(self.path, self.data)
        refresh_report_summary(self.run_dir)

    def step(self, stage):
        self.update(status='RUNNING', failure_stage=None, failure_detail=None, phase=stage,
                    started_at=self.data.get('started_at') or utc_now())

    def artifact(self, name, source=None, text=None, data=None):
        destination = os.path.join(self.directory, name)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if data is not None:
            write_report_json(destination, data)
        elif text is not None:
            with open(destination, 'w') as handle:
                handle.write(text)
        elif source and os.path.isfile(source):
            if os.path.abspath(source) != os.path.abspath(destination):
                # Shared cluster filesystems may reject extended-attribute copies.
                shutil.copyfile(source, destination)
        else:
            return
        self.data['artifacts'][name] = os.path.relpath(destination, self.run_dir)
        self.update()

    def requirements(self, report):
        report = dict(report)
        # Point copied evidence at this run, not mutable build_auto scratch files.
        saved_files = {'simulation_log': os.path.basename(report.get('simulation_log') or ''),
                       'combined_report_file': 'combined_requirement_verification.json',
                       'yaml_spec_file': 'approved_spec.yaml'}
        for field, name in saved_files.items():
            saved = self.data['artifacts'].get(name)
            if saved:
                report[field] = os.path.abspath(os.path.join(self.run_dir, saved))
        rows = report.get('requirements', [])
        self.artifact('requirement_traceability.json', data=report)
        self.update(verification_status=report['overall_status'], requirements=rows,
                    failed_requirements=[r for r in rows if r.get('status') == 'FAIL'],
                    untested_requirements=[r for r in rows if r.get('status') == 'NOT_TESTED'],
                    failure_stage=report.get('failure_stage'),
                    checks={'passed': report.get('scoreboard_passed_checks'),
                            'failed': report.get('scoreboard_failed_checks'),
                            'uvm_errors': report.get('uvm_errors'), 'uvm_fatals': report.get('uvm_fatals')})

    def finish(self, exit_code, error=None):
        verification = self.data.get('verification_status')
        if exit_code == 0:
            status = verification or 'PASS'
            if status == 'PASS' and self.data.get('coverage_limitations'):
                status = 'PASS_WITH_GAPS'
        elif verification in ('FAIL', 'NOT_TESTED') and (self.data.get('phase') == 'simulation' or self.agent == 'agent1'):
            status = verification
        else:
            status = 'ERROR'
        if exit_code in (130, -2):
            status = 'INTERRUPTED'
        self.update(status=status, exit_code=exit_code, finished_at=utc_now(),
                    failure_stage=(self.data.get('failure_stage') or self.data.get('phase')) if exit_code else None,
                    reason=error or ('' if exit_code == 0 else self.data.get('failure_detail') or 'Agent stopped during {}. See its saved logs.'.format(
                        self.data.get('phase', 'execution'))))


def run_reported_agent(agent, entrypoint, inputs):
    """Wrap early exits and exceptions as well as successful simulations."""
    report = AgentReport(agent, inputs)
    print('[REPORTS] ' + report.path, flush=True)
    report.step('input_validation')
    try:
        entrypoint(report)
    except SystemExit as exc:
        report.finish(exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1))
        raise
    except KeyboardInterrupt:
        report.finish(130, 'Interrupted before verification finished.')
        raise
    except Exception as exc:
        report.finish(1, '{}: {}'.format(type(exc).__name__, exc))
        raise
    else:
        report.finish(0)


def skip_report_stages(run_dir, stages, reason):
    for stage in stages:
        AgentReport(stage, run_dir=run_dir).update(status='SKIPPED', reason=reason, finished_at=utc_now())


def abort_report_run(run_dir, error, interrupted=False):
    """Preserve completed evidence and finalize pending stages on a pipeline error."""
    metadata_path = os.path.join(run_dir, 'run.json')
    metadata = read_report_json(metadata_path)
    metadata['pipeline_error'] = error
    write_report_json(metadata_path, metadata)
    for stage in metadata['requested_stages']:
        reporter = AgentReport(stage, run_dir=run_dir)
        if reporter.data['status'] == 'RUNNING':
            reporter.finish(130 if interrupted else 1, error)
        elif reporter.data['status'] == 'PENDING':
            skip_report_stages(run_dir, (stage,), 'Pipeline stopped: ' + error)
    refresh_report_summary(run_dir)


def execute_reported_process(agent, command, run_dir, environment, working_directory=None, cancel_event=None):
    reporter = AgentReport(agent, run_dir=run_dir)
    reporter.step('validation' if agent == 'agent1' else 'starting')
    console = os.path.join(reporter.directory, 'console.log')
    process = None
    error = None
    code = 1
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise KeyboardInterrupt()
        with open(console, 'w') as output:
            process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1,
                                       cwd=working_directory, start_new_session=True)
            with _PROCESS_LOCK:
                _ACTIVE_PROCESSES.add(process)
                if cancel_event is not None and cancel_event.is_set():
                    os.killpg(process.pid, signal.SIGTERM)
            for line in process.stdout:
                print(line, end='', flush=True)
                output.write(line)
                output.flush()
            code = process.wait()
            if cancel_event is not None and cancel_event.is_set():
                code, error = 130, 'Batch verification interrupted.'
    except KeyboardInterrupt:
        code, error = 130, 'Pipeline interrupted before verification finished.'
    except OSError as exc:
        error = str(exc)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        with _PROCESS_LOCK:
            _ACTIVE_PROCESSES.discard(process)
        # Reload changes written by the child instead of overwriting its results.
        reporter = AgentReport(agent, run_dir=run_dir)
        reporter.artifact('console.log', source=console)
        if code == 0 and agent != 'agent1' and not reporter.data.get('verification_status'):
            code, error = 1, 'Agent exited without a current verification result.'
        elif code == 0 and reporter.data.get('verification_status') in ('FAIL', 'NOT_TESTED', 'ERROR'):
            code, error = 1, 'Agent did not produce successful verification evidence. See its saved results.'
        if reporter.data['status'] in ('RUNNING', 'PENDING') or error:
            reporter.finish(code, error)
    return code


def header(title, color=CYAN):
    w = 58
    print(f"\n{color}{'='*w}{RESET}")
    print(f"{BOLD}  {title}{RESET}")
    print(f"{color}{'='*w}{RESET}")


def collect_rtl_files(paths):
    rtl_files = []

    for path in paths:
        if not os.path.exists(path):
            print(f"{RED}[ERROR] Path not found: {path}{RESET}")
            sys.exit(1)

        if os.path.isfile(path):
            if path.endswith((".sv", ".v")):
                rtl_files.append(path)

        elif os.path.isdir(path):
            for root, directories, files in os.walk(path):
                directories[:] = [d for d in directories if d not in (
                    REPORTS_DIRECTORY, 'tb_auto', 'tv_auto', 'build_auto', 'build_auto_tv', 'csrc', '.git')]
                for f in files:
                    if f.endswith((".sv", ".v")):
                        rtl_files.append(os.path.join(root, f))

    rtl_files = sorted(set(rtl_files))

    if not rtl_files:
        print(f"{RED}[ERROR] No RTL files (.sv/.v) found.{RESET}")
        sys.exit(1)

    return rtl_files


def load_validation_result():
    if not os.path.exists(VALIDATION_RESULT_FILE):
        return None
    try:
        with open(VALIDATION_RESULT_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def print_agent1_failure_details(validation_result=None, current_run=False):
    if not current_run:
        validation_result = load_validation_result()

    print(f"\n{RED}[PIPELINE] RTL failed validation.{RESET}")

    if not validation_result:
        print(f"           ➜ Could not read {VALIDATION_RESULT_FILE}.{RESET}")
        print(f"           ➜ Return to RTL generation and fix the RTL.{RESET}\n")
        return

    yaml_sane = validation_result.get("yaml_sane")
    spec_consistent = validation_result.get("spec_consistent")
    rtl_sane = validation_result.get("rtl_sane")
    compile_passed = validation_result.get("compile_passed")
    summary = validation_result.get("summary", "")
    issues = validation_result.get("issues", [])
    compile_output = validation_result.get("compile_output_snippet", "")

    if yaml_sane is False:
        print(f"           ➜ Failure category: YAML sanity")
        print(f"           ➜ The YAML spec itself appears invalid or contradictory.")
    elif spec_consistent is False:
        print(f"           ➜ Failure category: Spec ↔ RTL consistency")
        print(f"           ➜ The RTL does not match the YAML spec.")
    elif rtl_sane is False:
        print(f"           ➜ Failure category: RTL intrinsic sanity")
        print(f"           ➜ The RTL appears structurally/logically invalid on its own.")
    elif compile_passed is False:
        print(f"           ➜ Failure category: Compile sanity")
        print(f"           ➜ The RTL failed VCS/compile validation.")
    else:
        print(f"           ➜ Failure category: General validation failure")

    if summary:
        print(f"           ➜ Summary: {summary}")

    if issues:
        print(f"           ➜ Issues:")
        for issue in issues[:8]:
            if "\n" in issue:
                first = issue.split("\n")[0]
                print(f"              - {first}")
            else:
                print(f"              - {issue}")

    if compile_passed is False and compile_output:
        print(f"           ➜ Compile output snippet:")
        for ln in compile_output.splitlines()[:12]:
            print(f"              {ln}")

    print(f"           ➜ Return to RTL generation and fix the failing stage.{RESET}\n")


def discover_designs(inputs, rtl_files):
    """Pair each YAML design_name with its exact module and source dependencies."""
    import agent1_rtl_validator as validator
    specs = sorted({path for source in inputs for path in validator.collect_yaml_files(source)})
    texts = {path: validator.strip_comments_sv(validator.read_text(path)) for path in rtl_files}
    symbols = {}
    for path, text in texts.items():
        for name in re.findall(r'\b(?:module|package)\s+(?:(?:automatic|static)\s+)?([A-Za-z_]\w*)', text):
            symbols.setdefault(name, []).append(path)
    if all(os.path.isfile(path) for path in inputs):
        selected = []
        for path in specs:
            try:
                spec = validator.load_yaml(path)
                matches = isinstance(spec, dict) and spec.get('design_name') in symbols
            except Exception:
                matches = os.path.splitext(os.path.basename(path))[0] in symbols
            if matches:
                selected.append(path)
        specs = selected

    def source_for(name, context):
        candidates = symbols.get(name, [])
        local = [path for path in candidates if os.path.dirname(path) == os.path.dirname(context)]
        candidates = local or candidates
        if len(candidates) != 1:
            raise ValueError('Expected one RTL definition for {!r}; found {}.'.format(name, len(candidates)))
        return candidates[0]

    def dependencies(top, name):
        ordered, visiting = [], set()
        def visit(path):
            if path in visiting:
                return
            visiting.add(path)
            text = re.sub(r'"(?:\\.|[^"\\])*"', '""', texts[path])
            for symbol in sorted(symbols):
                # Match actual instances or package references, not name substrings.
                instance = r'\b' + re.escape(symbol) + r'\s*(?:#\s*\([\s\S]*?\)\s*)?[A-Za-z_]\w*\s*(?:\[[^]]*\]\s*)?\('
                if re.search(instance, text) or re.search(r'\b' + re.escape(symbol) + r'\s*::', text):
                    target = source_for(symbol, path)
                    if target != path:
                        visit(target)
            ordered.append(path)
        visit(top)
        return ordered

    designs, used = [], set()
    for spec_path in specs:
        design = {'yaml_file': os.path.abspath(spec_path)}
        try:
            spec = validator.load_yaml(spec_path)
            name = spec.get('design_name') if isinstance(spec, dict) else None
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w*', name):
                raise ValueError('YAML needs a valid design_name to pair it with an RTL module.')
            design['module_name'] = name
            top = source_for(name, spec_path)
            if not re.search(r'\bmodule\s+(?:(?:automatic|static)\s+)?' + re.escape(name) + r'\b', texts[top]):
                raise ValueError('design_name must identify an RTL module, not a package.')
            design.update(top_file=top, rtl_files=dependencies(top, name))
        except Exception as exc:
            design['discovery_error'] = '{}: {}'.format(type(exc).__name__, exc)
        label = re.sub(r'[^A-Za-z0-9_.-]', '_', design.get('module_name') or os.path.splitext(os.path.basename(spec_path))[0])
        label = label.strip('.') or 'design'
        key, number = label, 2
        while key in used:
            key = '{}_{:02d}'.format(label, number)
            number += 1
        used.add(key)
        design['id'] = key
        designs.append(design)
    return designs


def refresh_batch_summary(run_dir, publish_latest=False):
    # Children update this file from different processes when --jobs > 1.
    with open(os.path.join(run_dir, '.summary.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        metadata = read_report_json(os.path.join(run_dir, 'run.json'))
        designs = []
        for design in metadata['designs']:
            relative = 'designs/' + design['id']
            result = read_report_json(os.path.join(run_dir, relative, 'summary.json'))
            designs.append(dict(design, status=result['overall_status'] if result else 'PENDING',
                                summary_file=relative + '/summary.json', result=result))
        pending = any(d['status'] in ('PENDING', 'RUNNING') for d in designs)
        status = ('RUNNING' if pending else 'FAIL' if any(d['status'] == 'FAIL' for d in designs)
                  else 'INCOMPLETE' if any(d['status'] == 'INCOMPLETE' for d in designs)
                  else 'PASS_WITH_GAPS' if any(d['status'] == 'PASS_WITH_GAPS' for d in designs) else 'PASS')
        summary = dict(metadata, designs=designs, overall_status=status, updated_at=utc_now(),
                       execution_completed=not pending, verification_complete=status == 'PASS',
                       execution_succeeded=not pending and all(d['result'].get('execution_succeeded') for d in designs),
                       needs_review=status != 'PASS', artifact_base=metadata['run_id'],
                       summary_file=metadata['run_id'] + '/summary.json')
        for field in ('failed_requirements', 'untested_requirements', 'failed_tests', 'untested_tests', 'coverage_limitations'):
            summary[field] = [dict(row, design=d['id']) for d in designs
                              for row in (d['result'] or {}).get(field, [])]
        write_report_json(os.path.join(run_dir, 'summary.json'), summary)
        lines = ['# Batch verification report', '', 'Overall status: **{}**'.format(status), '',
                 '| Design | Status | Report |', '| --- | --- | --- |']
        lines.extend('| {} | {} | [Report](designs/{}/summary.md) |'.format(d['id'], d['status'], d['id']) for d in designs)
        with open(os.path.join(run_dir, 'summary.md'), 'w') as handle:
            handle.write('\n'.join(lines) + '\n')
        latest_path = os.path.join(os.path.dirname(run_dir), 'latest.json')
        latest = read_report_json(latest_path)
        if publish_latest or not latest or latest.get('run_id') == metadata['run_id']:
            write_report_json(latest_path, summary)
        return summary


def run_batch(inputs, designs, workers):
    run_dir, started_at = allocate_report_directory(os.path.join(report_input_directory(inputs), REPORTS_DIRECTORY))
    write_report_json(os.path.join(run_dir, 'run.json'), {
        'schema_version': 1, 'kind': 'batch', 'run_id': os.path.basename(run_dir),
        'created_at': started_at.astimezone(timezone.utc).isoformat(), 'created_at_local': started_at.isoformat(),
        'input_directory': report_input_directory(inputs), 'inputs': inputs, 'designs': designs, 'workers': workers})
    refresh_batch_summary(run_dir, publish_latest=True)
    for design in designs:
        create_report_run([design['yaml_file']], run_dir=os.path.join(run_dir, 'designs', design['id']),
                          metadata_extra={'batch_parent': run_dir, 'module_name': design.get('module_name'),
                                          'artifact_base': os.path.basename(run_dir) + '/designs/' + design['id'],
                                          'yaml_file': design['yaml_file']})
    print('[REPORTS] Batch summary: ' + os.path.join(run_dir, 'summary.json'), flush=True)
    cancelled = threading.Event()

    def run_design(design):
        child = os.path.join(run_dir, 'designs', design['id'])
        print('[BATCH] Starting ' + design['id'], flush=True)
        if cancelled.is_set():
            abort_report_run(child, 'Batch interrupted before this design started.', interrupted=True)
            return 130
        if design.get('discovery_error'):
            reporter = AgentReport('agent1', run_dir=child)
            reporter.step('input_discovery')
            reporter.finish(1, design['discovery_error'])
            skip_report_stages(child, ('agent2', 'agent3'), 'RTL/YAML pairing failed.')
            return 1
        work = os.path.join(child, 'work')
        os.makedirs(work)
        try:
            run_pipeline_stages([design['top_file']], design['rtl_files'], False, child,
                                design=design, working_directory=work, cancel_event=cancelled)
            return 0
        except SystemExit as exc:
            return exc.code
        except Exception as exc:
            abort_report_run(child, '{}: {}'.format(type(exc).__name__, exc))
            return 1

    executor = ThreadPoolExecutor(max_workers=workers)
    futures = [executor.submit(run_design, design) for design in designs]
    interrupted = False
    try:
        for future in as_completed(futures):
            future.result()
    except KeyboardInterrupt:
        interrupted = True
        cancelled.set()
        with _PROCESS_LOCK:
            for process in list(_ACTIVE_PROCESSES):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    finally:
        executor.shutdown(wait=True)
    summary = refresh_batch_summary(run_dir)
    print('[PIPELINE] Batch complete: {}. Reports: {}'.format(summary['overall_status'], run_dir))
    return 130 if interrupted else (0 if summary['execution_succeeded'] else 1)


def main():
    parser = argparse.ArgumentParser(description='Verify each RTL/YAML design and save per-design reports.')
    parser.add_argument('inputs', nargs='+', help='RTL files or directories containing RTL and YAML specs')
    parser.add_argument('--reuse-tb', action='store_true', help='Rerun an existing reviewed single-design testbench')
    parser.add_argument('--jobs', type=int, default=1,
                        help='Designs running concurrently; all discovered designs are queued (default: 1)')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be a positive integer')
    inputs, reuse_tb = args.inputs, args.reuse_tb
    rtl_files = [os.path.abspath(p) for p in collect_rtl_files(inputs)]
    designs = discover_designs(inputs, rtl_files)
    if len(designs) > 1:
        if reuse_tb:
            parser.error('--reuse-tb is for one design; batch designs require their own testbenches')
        sys.exit(run_batch(inputs, designs, args.jobs))
    run_dir = create_report_run(inputs)
    print('[REPORTS] ' + os.path.join(run_dir, 'summary.json'), flush=True)
    try:
        design = designs[0] if designs and not designs[0].get('discovery_error') else None
        run_pipeline_stages(inputs, design['rtl_files'] if design else rtl_files, reuse_tb, run_dir, design=design)
    except KeyboardInterrupt:
        abort_report_run(run_dir, 'Pipeline interrupted before verification finished.', interrupted=True)
        sys.exit(130)
    except Exception as exc:
        error = '{}: {}'.format(type(exc).__name__, exc)
        abort_report_run(run_dir, error)
        print('[PIPELINE] ' + error, file=sys.stderr)
        sys.exit(1)


def run_pipeline_stages(inputs, rtl_files, reuse_tb, run_dir, design=None, working_directory=None, cancel_event=None):
    environment = dict(os.environ, **{REPORT_RUN_ENV: run_dir})
    validation_path = os.path.join(run_dir, 'agent1', 'validation_result.json')
    environment['RTL_VALIDATION_RESULT_FILE'] = validation_path
    scripts = os.path.dirname(os.path.abspath(__file__))

    print(f"{CYAN}[PIPELINE] RTL inputs:{RESET}")
    for f in rtl_files:
        print(f"  - {f}")

    # ── Agent 1 ─────────────────────────────────────────
    header("AGENT 1 — RTL VALIDATOR")
    # Keep a directory argument intact: Agent 1 resolves its module dependencies.
    agent1_inputs = [os.path.abspath(p) for p in inputs]
    if design:
        agent1_inputs = [design['top_file'], '--top', design['module_name'], '--yaml', design['yaml_file']]
        for path in rtl_files:
            agent1_inputs.extend(['--rtl-file', path])
    code = execute_reported_process('agent1', [sys.executable, '-u', os.path.join(scripts, 'agent1_rtl_validator.py')]
                                    + agent1_inputs + ['--out', validation_path], run_dir, environment,
                                    working_directory, cancel_event)
    validation = read_report_json(validation_path)
    if validation:
        shutil.copyfile(validation_path, os.path.join(working_directory or '.', VALIDATION_RESULT_FILE))
        reporter = AgentReport('agent1', run_dir=run_dir)
        reporter.artifact('validation_result.json', source=validation_path)
        reporter.update(verification_status='PASS' if validation.get('is_valid') else 'FAIL',
                        validation_summary=validation.get('summary'),
                        failure_stage=validation.get('failure_stage'))
        reporter.finish(code)
    if code != 0 or not validation or not validation.get('is_valid'):
        if code == 0:
            AgentReport('agent1', run_dir=run_dir).finish(1, 'Agent 1 did not produce an approved validation result.')
        skip_report_stages(run_dir, ('agent2', 'agent3'), 'Agent 1 did not approve the inputs.')
        print_agent1_failure_details(validation, current_run=True)
        sys.exit(1)

    # ── Agent 2 ─────────────────────────────────────────
    header("AGENT 2 — UVM TESTBENCH")
    code = execute_reported_process('agent2', [sys.executable, '-u', os.path.join(scripts, 'agent2_uvm_tb.py')]
                                    + (['--reuse-tb'] if reuse_tb else []) + rtl_files, run_dir, environment,
                                    working_directory, cancel_event)
    if code != 0:
        skip_report_stages(run_dir, ('agent3',), 'Agent 2 did not complete successfully.')
        print(f"\n{RED}[PIPELINE] Agent 2 did not pass verification. See its generation, compile, simulation, and requirement results above.{RESET}\n")
        sys.exit(1)

    # ── Agent 3 ─────────────────────────────────────────
    header("AGENT 3 — TEST VECTOR GENERATOR", YELLOW)
    code = execute_reported_process('agent3', [sys.executable, '-u', os.path.join(scripts, 'agent3_testvectors.py')]
                                    + rtl_files, run_dir, environment, working_directory, cancel_event)
    if code != 0:
        print(f"\n{RED}[PIPELINE] Agent 3 did not complete successfully. See its reported stage and error above.{RESET}\n")
        sys.exit(1)

    summary = refresh_report_summary(run_dir)
    color = GREEN if summary['verification_complete'] else YELLOW
    print(f"\n{color}{BOLD}[PIPELINE] Complete — {summary['overall_status']}.{RESET}")
    metadata = read_report_json(os.path.join(run_dir, 'run.json'))
    summary_path = (os.path.join(run_dir, 'summary.json') if metadata.get('batch_parent') else
                    os.path.join(os.path.dirname(run_dir), 'latest.json'))
    print('[REPORTS] Front-end summary: ' + summary_path)


if __name__ == "__main__":
    main()
