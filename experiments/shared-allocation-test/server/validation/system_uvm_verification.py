"""Full-controller Agent 2 adapter around the existing VCS/evidence/handoff APIs.

LLM-generated UVM infrastructure surrounds protected, contract-derived checker
semantics. Repair stops at successful compilation; runtime failures return
frontend evidence. Compatible eligible checker reuse preserves exact bytes.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from controller_manifest import discover_system, file_digest, manifest_paths, resolve_path
from controller_verification import (ControllerReferenceModel, extract_controller_contract,
                                     parse_system_events)
from requirement_verification import build_system_requirement_catalog
from run_pipeline import (read_report_json, run_reported_agent, write_report_json)
from system_uvm_generation import (COMPONENTS, audit_environment, compile_failure_classification,
                                   audit_diagnostics, digest, generate_candidate, generation_inputs,
                                   select_saved_environment, validate_saved_environment)


TEMPLATES = Path(__file__).parent / 'templates' / 'controller_system'
TB_ORDER = ['dut_if.sv', 'dut_pkg.sv', 'dut_tb_top.sv']


def input_fingerprints(manifest_path):
    manifest_path = Path(manifest_path).resolve()
    manifest = discover_system(manifest_path)['manifest']
    paths = [manifest_path] + [resolve_path(manifest_path, manifest, p) for p in manifest_paths(manifest)]
    return {'schema_version': 1, 'mode': 'system', 'manifest': str(manifest_path),
            'files': [{'path': str(path), 'sha256': file_digest(path)} for path in paths]}


def verify_fingerprints(api, fingerprints):
    for row in fingerprints.get('files', []):
        if not Path(row['path']).is_file() or api.file_sha256(row['path']) != row['sha256']:
            raise ValueError('System input changed after Agent 1 approval: ' + row['path'])
    if not fingerprints.get('files'):
        raise ValueError('Agent 1 system input fingerprints are empty.')


def checker_identity(api, paths):
    rows = [{'name': Path(path).name, 'sha256': api.file_sha256(str(path))} for path in paths]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def controller_port_connectivity(contract, dut_ports):
    """Separate complete DUT wiring from the unchanged behavioral contract.

    Additional outputs are observed as four-state debug nets. Additional inputs
    or inouts need reviewed drive semantics; never guess an inactive tie value.
    """
    bindings = {name: 'bus.' + name for name in contract['host_ports']}
    bindings[contract['clock']['name']] = 'clk'
    bindings[contract['reset']['name']] = 'bus.rst_n' if contract['reset']['active_low'] else '~bus.rst_n'
    names = [port['name'] for port in dut_ports]
    if len(names) != len(set(names)) or not set(bindings).issubset(names):
        raise ValueError('Agent 1 port metadata must contain each controller behavioral port exactly once.')
    connections, declarations, monitor_inputs, inventory = [], [], [], []
    waveform = ['clk', 'bus.rst_n']
    for port in dut_ports:
        name, direction, width = port['name'], port['direction'], port.get('width')
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9$]*', name):
            raise ValueError('Unsupported controller port identifier: ' + name)
        if name in bindings:
            connection = bindings[name]
            role = 'behavioral_interface'
            signal = 'bus.rst_n' if name == contract['reset']['name'] else connection
        else:
            if direction != 'output':
                raise ValueError('Additional controller {} {} requires a reviewed drive/tie policy.'.format(direction, name))
            if not isinstance(width, int) or width < 1:
                raise ValueError('Additional controller output {} requires a resolved packed width.'.format(name))
            signal = 'bus.dut_port_' + name
            connection, role = signal, 'observed_output'
            packed = '' if width == 1 else '[{}:0] '.format(width - 1)
            declarations.append('    wire {}dut_port_{};'.format(packed, name))
            monitor_inputs.append('        input dut_port_{};'.format(name))
        connections.append('        .{}({})'.format(name, connection))
        if signal not in waveform:
            waveform.append(signal)
        inventory.append({'name': name, 'direction': direction, 'width': width,
                          'connection': connection, 'role': role})
    for role in contract['observability']:
        signal = 'bus.' + role
        if signal not in waveform:
            waveform.append(signal)
    return {'DUT_PORT_CONNECTIONS': ',\n'.join(connections),
            'ADDITIONAL_PORT_SIGNALS': '\n'.join(declarations),
            'ADDITIONAL_PORT_MONITOR_INPUTS': '\n'.join(monitor_inputs),
            'WAVEFORM_SIGNALS': ',\n'.join('            ' + signal for signal in waveform)}, inventory


def render_system_environment(contract, requirements, dut_ports):
    constants, widths, config = contract['constants'], contract['widths'], contract['configuration']
    timing = contract['timing_cycles']
    max_cycles = max(20000, 2 * timing.get('tREFI', 0) +
                     (1024 + constants['req_queue_depth'] * 48 + config['bank_count'] * 64) *
                     max(timing.get('tFAW', 1), timing.get('tRRD', 1), constants['slow_service_cycles']) * 4)
    values = {'DUT_TOP': contract['top_module'],
              'BANK_W': widths['bank'], 'ADDR_W': widths['address'], 'DATA_W': widths['data'],
              'BANKS': config['bank_count'], 'ROW_W': constants['row_width'], 'COL_W': constants['col_width'],
              'QUEUE_DEPTH': constants['req_queue_depth'], 'SLOT_W': max(1, (constants['req_queue_depth'] - 1).bit_length()),
              'OPEN_PAGE': int(config['page_policy'] == 'open_page'),
              'REFRESH_ENABLED': int(config['refresh_enabled']), 'TRRD_ENABLED': int(config['trrd_enabled']),
              'TFAW_ENABLED': int(config['tfaw_enabled']), 'TRRD': timing.get('tRRD', 0),
              'TFAW': timing.get('tFAW', 0), 'ACT_LIMIT': contract['tfaw_limit'],
              'REFI': timing.get('tREFI', 0), 'MAX_CYCLES': max_cycles}
    values.update(controller_port_connectivity(contract, dut_ports)[0])
    lines = []
    for role, signal in contract['observability'].items():
        if role == 'open_rows':
            for bank in range(config['bank_count']):
                lines.append('    assign bus.open_rows[{b}*{w} +: {w}] = dut.{s}[{b}];'.format(
                    b=bank, w=constants['row_width'], s=signal))
        else:
            lines.append('    assign bus.{} = dut.{};'.format(role, signal))
    if 'refresh' not in contract['observability']:
        lines.append("    assign bus.refresh = 1'b0;")
    values['OBSERVATION_BINDINGS'] = '\n'.join(lines)
    values['REQUIREMENT_REGISTRATION'] = '\n'.join(
        '            requirement_ids.push_back("{id}"); checks["{id}"]=0; failures["{id}"]=0;'.format(id=r['id']) for r in requirements)
    files = {}
    for name in TB_ORDER:
        source = (TEMPLATES / name).read_text()
        source = re.sub(r'@([A-Z_]+)@', lambda m: str(values[m.group(1)]), source)
        if re.search(r'@[A-Z_]+@', source):
            raise ValueError('Unresolved system UVM template token in ' + name)
        files[name] = source
    return files


@contextlib.contextmanager
def system_workspace(api, work):
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    names = ('TB_ROOT', 'BUILD_DIR', 'LOG_DIR', 'COMPILE_ORDER', 'REQUIREMENT_REPORT_FILE')
    previous = {name: getattr(api, name) for name in names}
    directory = os.getcwd()
    try:
        api.TB_ROOT = str(work / 'tb_auto')
        api.BUILD_DIR = str(work / 'build_auto')
        api.LOG_DIR = str(work / 'build_auto' / 'logs')
        api.COMPILE_ORDER = list(TB_ORDER)
        api.REQUIREMENT_REPORT_FILE = str(work / 'build_auto' / 'requirement_traceability.json')
        Path(api.TB_ROOT).mkdir(exist_ok=True)
        api.ensure_build_dirs()
        os.chdir(str(work))
        yield
    finally:
        os.chdir(directory)
        for name, value in previous.items():
            setattr(api, name, value)


def install_system_environment(api, contract, requirements, dut_ports, reuse_environment=None):
    trusted = render_system_environment(contract, requirements, dut_ports)
    if reuse_environment:
        saved = read_report_json(str(reuse_environment))
        validate_saved_environment(api, saved, contract, requirements, trusted)
        files = saved['testbench_files']
        for row in files:
            shutil.copyfile(row['path'], str(Path(api.TB_ROOT) / Path(row['path']).name))
        return saved['checker_identity']
    raise ValueError('New system environments require LLM generation and integrity audit.')


def prepare_system_checker(api, report, contract, requirements, agent1, reuse_environment):
    """Generate/reuse then compile; this function never sees simulation results."""
    trusted = render_system_environment(contract, requirements, agent1['dut_spec']['ports'])
    selected, saved = select_saved_environment(api, report, contract, requirements, trusted, reuse_environment)
    generation = {'mode': 'REUSED' if selected else 'LLM_GENERATED', 'reuse_eligible': False,
                  'model': None, 'attempts': [], 'source_environment': selected}
    candidate = trusted
    error, compile_output, classification = None, '', None
    compile_ok, audited = False, False
    history = []
    generation.update(vcs_invoked=False, repair_exhausted=False)
    if selected:
        install_system_environment(api, contract, requirements, agent1['dut_spec']['ports'], selected)
        candidate = {name: (Path(api.TB_ROOT) / name).read_text() for name in TB_ORDER}
        generation['origin'] = saved.get('generation')
        generation['audit'] = audit_environment(api, candidate, trusted)
        audited = True
    inputs = generation_inputs(contract, requirements, agent1, read_report_json(contract['sources']['manifest']))
    inputs['vcs_uvm_flags'] = list(api.VCS_UVM_FLAGS)
    generation.update(timestamp=datetime.now(timezone.utc).isoformat(),
                      rtl_hashes={path: api.file_sha256(path) for path in agent1['rtl_files']},
                      manifest_hash=api.file_sha256(contract['sources']['manifest']),
                      configuration_hash=digest(contract['configuration']),
                      contract_hash=digest(inputs['controller_contract']), requirement_hash=digest(requirements))
    limit = 1 if selected else min(5, api.MAX_ITERS)
    for attempt in range(limit):
        action = 'REUSED' if selected else 'INITIAL_GENERATION' if attempt == 0 else 'REPAIR'
        folder = 'generation/attempt_{}'.format(attempt + 1)
        print('\n' + '=' * 60 + '\nAGENT 2 UVM ATTEMPT {}/{}\n'.format(attempt + 1, limit) + '=' * 60)
        print('Action: ' + action)
        metadata = {'model': None if selected else os.getenv('TAMU_MODEL'), 'raw_response': None}
        diagnostic = ''
        terminal = False
        if not selected:
            report.step('generation' if error is None else 'testbench_repair')
            try:
                candidate, audit, metadata = generate_candidate(api, report, inputs, trusted, candidate, attempt, error)
                generation.update(audit=audit, model=metadata['model'])
                generation['attempts'].append(metadata)
                audited = audit['passed']
                if not audited:
                    error = audit_diagnostics(audit)
                    classification = 'TESTBENCH_GENERATION_FAILURE'
                    diagnostic = error
            except (ValueError, RuntimeError) as exc:
                error = str(exc)
                classification = 'TESTBENCH_GENERATION_FAILURE'
                audited = False
                diagnostic = 'GENERATION_TRANSPORT_OR_RESPONSE: ' + error
                audit = {'passed': False, 'checks': {key: 'NOT_RUN' for key in
                         ('required_classes', 'trusted_checker', 'topology', 'lifecycle', 'interface_contract')},
                         'classes_detected': [], 'failures': {'generation': [diagnostic]}}
                generation['audit'] = audit
                # Transport/auth failures cannot be corrected by source edits.
                terminal = isinstance(exc, RuntimeError)
        else:
            audit = generation['audit']
        vcs_invoked, vcs_result = False, None
        if audited:
            for name in TB_ORDER:
                (Path(api.TB_ROOT) / name).write_text(candidate[name])
            report.step('compile')
            vcs_invoked = True
            generation['vcs_invoked'] = True
            compile_ok, _, compile_output = api.vcs_compile(agent1['rtl_files'], attempt, top_module='dut_tb_top')
            vcs_result = 'PASS' if compile_ok else 'FAIL'
            report.artifact('compile_iter_{}.log'.format(attempt), text=compile_output)
            report.artifact(folder + '/compile.log', text=compile_output)
            classification = None if compile_ok else compile_failure_classification(compile_output, TB_ORDER, agent1['rtl_files'])
            if not compile_ok:
                # Preserve the exact compiler text in the next prompt/artifact.
                diagnostic = error = compile_output
                terminal = bool(selected or classification != 'TESTBENCH_COMPILE_FAILURE')
        exhausted = not compile_ok and not terminal and attempt + 1 == limit and not selected
        decision = ('ACCEPTED' if compile_ok else 'REPAIR_EXHAUSTED' if exhausted else
                    'STOP' if terminal else 'REPAIR_ALLOWED')
        failures = [failure for values in audit['failures'].values() for failure in values]
        if vcs_invoked and not compile_ok:
            failures.append(diagnostic)
        row = dict(metadata, attempt=attempt + 1, action=action, generated_files=sorted(candidate),
                   integrity_checks=audit['checks'], integrity_audit=audit,
                   exact_failures=failures,
                   diagnostic=diagnostic, vcs_invoked=vcs_invoked, vcs_result=vcs_result,
                   repair_decision=decision)
        history.append(row)
        for name in TB_ORDER:
            report.artifact(folder + '/' + name, text=candidate.get(name, trusted[name]))
        report.artifact(folder + '/audit.json', data=audit)
        report.artifact(folder + '/diagnostics.txt', text=diagnostic or 'Integrity audit and VCS compilation passed.')
        report.artifact('iteration_history.json', data=history)
        print('LLM model: ' + str(metadata['model'] if not selected else 'NOT_INVOKED'))
        print('Generated:')
        for name in COMPONENTS:
            print('  {} : {}'.format(name, 'FOUND' if name in audit['classes_detected'] else 'MISSING'))
        print('Integrity audit: ' + ('PASS' if audited else 'FAIL'))
        for key, status in audit['checks'].items():
            print('  {} : {}'.format(key.replace('_', ' ').title(), status))
        if diagnostic:
            print('Failure:\n' + diagnostic)
        print('VCS compile: ' + (vcs_result or 'NOT_RUN'))
        print('Repair decision: ' + decision)
        if exhausted:
            generation['repair_exhausted'] = True
            classification = 'TESTBENCH_GENERATION_FAILURE'
        if compile_ok:
            generation['mode'] = 'REUSED' if selected else 'LLM_GENERATED' if attempt == 0 else 'LLM_REPAIRED'
        if compile_ok or terminal or exhausted:
            break
    # Preserve even unsuccessful generated candidates in this run, but never
    # declare them reusable. Invalid/raw replies have their own immutable files.
    for name in TB_ORDER:
        source = candidate.get(name, trusted[name])
        destination = Path(api.TB_ROOT) / name
        if not selected:
            destination.write_text(source)
        report.artifact('tb_auto/' + name, source=str(destination))
    generation.update(compile_valid=compile_ok, integrity_audit_passed=audited,
                      reuse_eligible=bool(compile_ok and audited), input_hash=digest(inputs),
                      iteration_history=history, final_vcs_invoked=history[-1]['vcs_invoked'])
    return compile_ok, error if not audited else compile_output, classification, generation, bool(selected)


def marker_records(text, prefix):
    rows = [line.split(prefix + '|', 1)[1] for line in (text or '').splitlines() if prefix + '|' in line]
    records = []
    for row in rows:
        result = {}
        for entry in row.split('|'):
            if '=' in entry:
                key, value = entry.split('=', 1)
                try:
                    result[key] = int(value)
                except ValueError:
                    result[key] = value
        records.append(result)
    return records


def marker_fields(text, prefix):
    records = marker_records(text, prefix)
    return records[-1] if records else {}


def summarize_system_simulation(api, requirements, contract, simulation_text, simulation_ok):
    requirement_report = api.build_requirement_report(requirements, simulation_text, simulation_ok=simulation_ok)
    events = parse_system_events(simulation_text)
    model = ControllerReferenceModel()
    for event in events:
        model.observe(event)
    end = marker_fields(simulation_text, 'AGENT2_SYSTEM_END')
    counts = model.finish(bool(end))
    runtime = marker_fields(simulation_text, 'AGENT2_SYSTEM_TRAFFIC')
    coverage = marker_fields(simulation_text, 'AGENT2_SYSTEM_COVERAGE')
    checker_failures = marker_records(simulation_text, 'AGENT2_SYSTEM_FAILURE')
    if model.accepted and ('SYSTEM_DRIVER_TIMEOUT' in simulation_text or 'AGENT2_SYSTEM_TIMEOUT|' in simulation_text):
        watchdog = marker_fields(simulation_text, 'AGENT2_SYSTEM_TIMEOUT')
        model.fail({'scenario': watchdog.get('scenario', events[-1].get('scenario') if events else None),
                    'cycle': watchdog.get('cycle'), 'id': model.active},
                   'Accepted traffic did not progress before the trusted watchdog',
                   'acceptance/service/final drain before watchdog',
                   [line for line in simulation_text.splitlines() if 'SYSTEM_DRIVER_TIMEOUT' in line or
                    'AGENT2_SYSTEM_TIMEOUT|' in line])
    limitations = []
    if not end or not runtime:
        last = events[-1] if events else {}
        model.fail(last, 'Simulation did not emit complete final drain/accounting evidence')
        model.failures[-1]['verification_gap'] = True
    if not model.accepted or not model.responses_checked:
        model.fail({}, 'No independently observed accepted traffic/read comparison')
        model.failures[-1]['verification_gap'] = True
    for key in ('accepted', 'dispatched', 'completed', 'cancelled_by_reset', 'outstanding', 'responses_checked', 'data_mismatches', 'duplicated'):
        if key not in runtime or runtime[key] != counts[key]:
            model.fail({}, 'Runtime/replayed accounting mismatch: ' + key, counts[key], runtime.get(key))
            model.failures[-1]['verification_gap'] = key not in runtime
    counts['presented'] = sum(e.get('event') == 'presented' for e in events)
    if runtime.get('presented') != counts['presented']:
        model.fail({}, 'Presented request count differs from monitor history', counts['presented'], runtime.get('presented'))
        model.failures[-1]['verification_gap'] = 'presented' not in runtime
    for failure in model.failures:
        if failure.get('verification_gap'):
            continue
        reason = failure['reason'].lower()
        req_id = ('SYS-REQ-006' if 'data mismatch' in reason else 'SYS-REQ-005' if 'duplicate' in reason or 'twice' in reason else
                  'SYS-REQ-007' if 'changed accepted' in reason else 'SYS-REQ-002' if 'acceptance without' in reason else 'SYS-REQ-015')
        failure['requirement'] = req_id
        for row in requirement_report['requirements']:
            if row['id'] == req_id:
                row.setdefault('uvm_simulation', dict(row['simulation']))
                row.setdefault('trace_replay_failures', []).append(failure)
                row.update(status='FAIL', failures=row['failures']+1,
                           evidence='Independent monitor trace replay: ' + failure['reason'])
                row['simulation'] = dict(row['simulation'], status='FAIL', failures=row['failures'], evidence=row['evidence'])
    # An explicit trusted comparison failure is proof even when a subsequent
    # fatal prevents finalize() from publishing counts/requirement summaries.
    for failure in checker_failures:
        for row in requirement_report['requirements']:
            if row['id'] == failure.get('requirement'):
                row.setdefault('uvm_simulation', dict(row['simulation']))
                row.update(status='FAIL', failures=max(1, row['failures']),
                           evidence='Trusted executable comparison: ' + json.dumps(failure, sort_keys=True))
                row['simulation'] = dict(row['simulation'], status='FAIL', failures=row['failures'], evidence=row['evidence'])
    if model.failures or checker_failures:
        requirement_report['overall_status'] = 'FAIL'
        requirement_report['failed_requirements'] = sum(r['status'] == 'FAIL' for r in requirement_report['requirements'])
        requirement_report['passed_requirements'] = sum(r['status'] == 'PASS' for r in requirement_report['requirements'])
        requirement_report['not_tested_requirements'] = sum(r['status'] == 'NOT_TESTED' for r in requirement_report['requirements'])
        requirement_report['simulation_gaps'] = requirement_report['not_tested_requirements']
        requirement_report['simulation_passed_requirements'] = requirement_report['passed_requirements']
        requirement_report['failure_stage'] = 'simulation'
        requirement_report['failure_reason'] = 'Trusted runtime comparison or monitor trace replay failed; original UVM evidence is retained.'
        counts['accounting_proven'] = False
        # Absence of complete proof is not a zero loss/duplication result.
        if not end or not runtime:
            counts['lost'] = counts['duplicated'] = None
    if requirement_report['overall_status'] == 'FAIL':
        counts['accounting_proven'] = False
        if counts['lost'] == 0:
            counts['lost'] = None
        if counts['duplicated'] == 0:
            counts['duplicated'] = None
    failure_evidence = model.failures + checker_failures
    comparisons = api.parse_requirement_evidence(simulation_text)
    scenario_checks = {}
    for row in requirement_report['requirements']:
        for comparison in comparisons.get(row['id'], []):
            scenario_checks.setdefault(comparison['scenario'], set()).add(row['id'])
    failed_ids = {r['id'] for r in requirement_report['requirements'] if r['status'] == 'FAIL'}
    passed_ids = {r['id'] for r in requirement_report['requirements'] if r['status'] == 'PASS'}
    scenarios = {}
    for name in {e['scenario'] for e in events if e.get('scenario')} | set(scenario_checks):
        ids = scenario_checks.get(name, set())
        failed = ids & failed_ids or any(f.get('scenario') == name for f in failure_evidence)
        scenarios[name] = 'FAIL' if failed else 'PASS' if ids & passed_ids else 'NOT_TESTED'
    for name, key, requirement in (('consecutive_acceptance', 'consecutive_accepts', 'SYS-REQ-002'),
                      ('backpressure', 'backpressure_cycles', 'SYS-REQ-003'),
                      ('queue_saturation', 'saturation_cycles', 'SYS-REQ-004'),
                      ('multiple_outstanding', 'max_outstanding', 'SYS-REQ-004'),
                      ('completion_and_new_acceptance', 'completion_and_accept', 'SYS-REQ-004'),
                      ('trrd_boundary', 'trrd_boundary_hits', 'SYS-REQ-013')):
        if name == 'trrd_boundary' and not contract['configuration']['trrd_enabled']:
            continue
        exercised = coverage.get(key, 0) > (1 if name == 'multiple_outstanding' else 0)
        scenarios[name] = ('FAIL' if exercised and requirement in failed_ids else
                           'PASS' if exercised and requirement in passed_ids else 'NOT_TESTED')
        if not exercised:
            limitations.append(name + ': condition did not occur in this simulation')
    status = requirement_report['overall_status']
    if (not counts['accounting_proven'] or counts['data_mismatches'] > 0 or
            counts['lost'] != 0 or counts['duplicated'] != 0):
        status = 'FAIL'
        requirement_report['overall_status'] = 'FAIL'
    if status == 'PASS' and limitations:
        status = 'PASS_WITH_GAPS'
    critical = {'SYS-REQ-001', 'SYS-REQ-002', 'SYS-REQ-003', 'SYS-REQ-004', 'SYS-REQ-005',
                'SYS-REQ-006', 'SYS-REQ-007', 'SYS-REQ-011', 'SYS-REQ-015'}
    if contract['configuration']['bank_count'] > 1:
        critical.add('SYS-REQ-008')
    ready = (status in ('PASS', 'PASS_WITH_GAPS') and counts['accounting_proven'] and
             all(r['status'] == 'PASS' for r in requirement_report['requirements'] if r['id'] in critical))
    result = {'mode': 'system', 'top_module': contract['top_module'], 'status': status,
              'verification_scope': 'controller_functional_simulation', 'ready_for_backend': False,
              'configuration': contract['configuration'], 'traffic': counts,
              'reads': {'responses_checked': counts['responses_checked'], 'data_mismatches': counts['data_mismatches']},
              'requirements': {key: requirement_report[field] for key, field in (
                  ('passed', 'passed_requirements'), ('failed', 'failed_requirements'), ('not_tested', 'not_tested_requirements'))},
              'coverage': coverage, 'scenarios': scenarios, 'coverage_limitations': limitations,
              'observability_limitations': contract.get('observability_limitations', []),
              'failure_evidence': failure_evidence, 'accepted_request_history': [e for e in events if e.get('event') == 'accepted'],
              'outstanding_transactions': list(model.pending.values()),
              'pending_read_response_transactions': [{'transaction': identity, 'expected': expected}
                                                     for identity, expected in model.responses],
              'ready_for_agent3': ready, 'agent3_executed': False,
              'verification_readiness': 'READY_FOR_SYSTEM_AGENT_3' if ready else 'BLOCKED_SYSTEM_AGENT_2'}
    return result, requirement_report, events


def simulation_metrics(requirement_report, compile_ok, simulation_ok, simulation_ran):
    return {'compile_passed': compile_ok, 'simulation_passed': simulation_ok if simulation_ran else None,
            'uvm_error': requirement_report.get('uvm_errors') if simulation_ran else None,
            'uvm_fatal': requirement_report.get('uvm_fatals') if simulation_ran else None,
            'scoreboard_pass': requirement_report.get('scoreboard_passed_checks') if simulation_ran else None,
            'scoreboard_fail': requirement_report.get('scoreboard_failed_checks') if simulation_ran else None}


def classify_simulation_failure(result, requirement_report, simulation_text, simulation_ok, rtl_files=()):
    if result['status'] != 'FAIL':
        return None, None
    # Missing evidence/tool execution is not, by itself, a proven DUT fault.
    if not valid_simulation_startup(simulation_text):
        return 'VERIFICATION_SIMULATION_FAILURE', 'INVESTIGATE_VERIFICATION_ENVIRONMENT'
    proven = [f for f in result['failure_evidence'] if not f.get('verification_gap')]
    trusted_runtime_failure = bool(result['accepted_request_history']) and (
        'SYSTEM_DRIVER_TIMEOUT' in simulation_text or 'AGENT2_SYSTEM_TIMEOUT|' in simulation_text)
    rtl_assertion = any(re.search(r'(?i)assert(?:ion)?[^\n]*(?:fail|error)', line) and
                        any(Path(path).name in line for path in rtl_files) for line in simulation_text.splitlines())
    if proven or requirement_report.get('failed_requirements', 0) > 0 or trusted_runtime_failure or rtl_assertion:
        return 'RTL_FUNCTIONAL_FAILURE', 'RETURN_TO_FRONTEND_RTL_FIX'
    return 'VERIFICATION_SIMULATION_FAILURE', 'INVESTIGATE_VERIFICATION_ENVIRONMENT'


def valid_simulation_startup(text):
    # Trusted monitor events or the trusted runtime accounting/end markers
    # demonstrate executable startup. A severity count alone does not.
    return bool(parse_system_events(text) or marker_records(text, 'AGENT2_SYSTEM_TRAFFIC') or
                marker_records(text, 'AGENT2_SYSTEM_END'))


def frontend_failure_package(report, result, contract, simulation_text, events):
    """Evidence handoff only: frontend owns fixes and restarting at Agent 1."""
    failures = []
    for original in result['failure_evidence']:
        if original.get('verification_gap'):
            continue
        failure = dict(original)
        history = [e for e in events if failure.get('transaction') is not None and
                   e.get('id') == failure['transaction']]
        if not history and failure.get('cycle') is not None:
            history = [e for e in events if e.get('cycle') == failure['cycle']]
        request = next((e for e in history if e.get('event') == 'accepted'), {})
        cycles = {kind + '_cycle': next((e.get('cycle') for e in history if e.get('event') == kind), None)
                  for kind in ('accepted', 'dispatched', 'completed', 'response')}
        address = request.get('address', failure.get('address'))
        failure.update(actual=failure.get('observed'), transaction_history=history,
                       operation=('write' if request['write'] else 'read') if 'write' in request else None,
                       bank=request.get('bank', failure.get('bank')), address=address,
                       row=address >> contract['constants']['col_width'] if isinstance(address, int) else None,
                       column=address & ((1 << contract['constants']['col_width']) - 1) if isinstance(address, int) else None,
                       write_data=request.get('data') if request.get('write') else None, **cycles)
        failure['acceptance_cycle'] = cycles['accepted_cycle']
        failure['dispatch_cycle'] = cycles['dispatched_cycle']
        failure['completion_cycle'] = cycles['completed_cycle']
        # Dispatch is the documented entry into the single active service slot.
        failure['service_cycle'] = cycles['dispatched_cycle']
        failures.append(failure)
    paths = {name: str(Path(report.directory) / filename) for name, filename in (
        ('compile_log', 'compile.log'), ('simulation_log', 'simulation.log'),
        ('requirement_traceability', 'requirement_traceability.json'), ('system_report', 'system_report.json'))}
    waveform = Path(report.directory) / 'controller.vcd'
    paths['waveform'] = str(waveform) if waveform.is_file() else None
    lines = simulation_text.splitlines()
    requirement_report = read_report_json(str(Path(report.directory) / 'requirement_traceability.json')) or {}
    def evidence(pattern):
        return [line for line in lines if re.search(pattern, line)]
    package = {'schema_version': 1, 'failing_agent': 'Agent 2',
               'failure_classification': result['failure']['classification'],
               'disposition': result['failure']['disposition'],
               'failures': failures, 'request_transaction_history': events,
               'failed_requirements': [row for row in requirement_report.get('requirements', []) if row['status'] == 'FAIL'],
               'outstanding_requests': result['outstanding_transactions'],
               'pending_read_responses': result['traffic'].get('pending_read_responses'),
               'pending_read_response_transactions': result.get('pending_read_response_transactions', []),
               'rtl_snapshots': [str(Path(report.run_dir) / relative) for name, relative in
                                 report.data['artifacts'].items() if name.startswith('rtl_snapshot/')],
               'simulation_time': None, 'reset_state': None, 'refresh_state': None,
               'reported_simulation_times': re.findall(r'@\s*(\d+(?:\.\d+)?)\s*:', simulation_text),
               'reset_evidence': [e for e in events if e.get('event') == 'reset'],
               'refresh_timing_row_evidence': [f for f in failures if f.get('requirement') in
                                             ('SYS-REQ-009', 'SYS-REQ-010', 'SYS-REQ-011',
                                              'SYS-REQ-012', 'SYS-REQ-013', 'SYS-REQ-014')],
               # The current trace localizes architectural violations, not an
               # internal RTL culprit. Do not fabricate subsystem attribution.
               'suspected_subsystems': [], 'subsystem_attribution': 'Not established by this trace',
               'uvm_error_evidence': evidence(r'^\s*UVM_ERROR\s+(?!:)'),
               'uvm_fatal_evidence': evidence(r'^\s*UVM_FATAL\s+(?!:)'),
               'scoreboard_evidence': evidence(r'SCOREBOARD|AGENT2_SYSTEM_FAILURE\|'),
               'assertion_evidence': evidence(r'(?i)assert(?:ion)?[^\n]*(?:fail|error)'),
               'traffic': result['traffic'], 'paths': paths,
               'restart_from': 'Agent 1 SYSTEM', 'production_rtl_modified': False}
    report.artifact('frontend_feedback.json', data=package)
    return str(Path(report.directory) / 'frontend_feedback.json')


def print_system_report(result, contract):
    print('\n' + '='*60 + '\nAGENT 2 — FULL SYSTEM UVM VERIFICATION\n' + '='*60)
    print('DUT: ' + result['top_module'])
    print('RTL files: {}'.format(result.get('rtl_file_count', 0)))
    print('UVM Environment:')
    print('  Generation mode: ' + result.get('generation', {}).get('mode', 'NOT_RUN'))
    for component in COMPONENTS:
        print('  ' + component)
    print('Configuration: ' + json.dumps(result['configuration'], sort_keys=True))
    print('Acceptance: ' + contract['request_acceptance'])
    print('Dispatch: ' + contract['dispatch'])
    print('Write completion: ' + contract['write_completion'])
    print('Read association: ' + contract['read_response']['association'])
    print('Ordering: ' + contract['ordering']['global'])
    print('Simulation:')
    audit_passed = result.get('generation', {}).get('integrity_audit_passed')
    print('  Integrity audit   : ' + ('NOT_RUN' if audit_passed is None else 'PASS' if audit_passed else 'FAIL'))
    simulation = result.get('simulation', {})
    for label, field in (('VCS compile', 'compile_passed'), ('VCS simulation', 'simulation_passed')):
        value = simulation.get(field)
        print('  {:<18}: {}'.format(label, 'NOT_RUN' if value is None else 'PASS' if value else 'FAIL'))
    for label, field in (('UVM_ERROR', 'uvm_error'), ('UVM_FATAL', 'uvm_fatal'),
                         ('Scoreboard PASS', 'scoreboard_pass'), ('Scoreboard FAIL', 'scoreboard_fail')):
        value = simulation.get(field)
        print('  {:<18}: {}'.format(label, value if value is not None else 'NOT_OBSERVED'))
    for name, status in sorted(result.get('scenarios', {}).items()):
        print('  {}: {}'.format(name, status))
    print('Traffic accounting: ' + json.dumps(result.get('traffic', {}), sort_keys=True))
    print('Requirements: ' + json.dumps(result['requirements'], sort_keys=True))
    print('AGENT 2 SYSTEM RESULT : ' + result['status'])
    failure = result.get('failure', {})
    if failure.get('classification'):
        print('Failure classification: ' + failure['classification'])
        print('Disposition: ' + failure['disposition'])
    if failure.get('frontend_feedback'):
        print('Frontend feedback: ' + failure['frontend_feedback'])
    print('Verification readiness: ' + result['verification_readiness'])
    if result.get('failure_detail'):
        print(result['failure_detail'])
    print('Phase 3 stops before Agent 3. Backend readiness has not been established.')
    print('='*60)


def run_system_uvm(api, report, manifest_path, agent1_path, reuse_environment=None, fingerprints_path=None):
    manifest_path = Path(manifest_path).resolve()
    agent1 = read_report_json(str(agent1_path))
    if not agent1:
        raise ValueError('Agent 1 system result is missing or invalid: ' + str(agent1_path))
    if fingerprints_path:
        fingerprints = read_report_json(str(fingerprints_path))
        if not fingerprints or fingerprints.get('manifest') != str(manifest_path):
            raise ValueError('Agent 1 fingerprints do not identify the selected system manifest.')
        expected_paths = {row['path'] for row in input_fingerprints(manifest_path)['files']}
        if {row['path'] for row in fingerprints.get('files', [])} != expected_paths:
            raise ValueError('Agent 1 fingerprints must cover the complete manifest input inventory.')
        verify_fingerprints(api, fingerprints)
    contract = extract_controller_contract(manifest_path, agent1)
    requirements = build_system_requirement_catalog(contract['configuration'], contract)
    report.artifact('controller_verification_contract.json', data=contract)
    report.artifact('requirement_catalog.json', data=requirements)
    report.artifact('agent1_validation_result.json', data=agent1)
    report.artifact('source_manifest.json', source=str(manifest_path))
    for index, path in enumerate(agent1['rtl_files']):
        report.artifact('rtl_snapshot/{}_{}'.format(index, Path(path).name), source=path)
    result = None
    if not contract['supported']:
        rows = [{'id': r['id'], 'requirement': r['text'], 'source': r['source'], 'status': 'NOT_TESTED',
                 'evidence': '; '.join(contract['limitations'])} for r in requirements]
        requirement_report = {'overall_status': 'NOT_TESTED', 'requirements': rows,
                              'failure_stage': 'verification_contract', 'passed_requirements': 0,
                              'failed_requirements': 0, 'not_tested_requirements': len(rows)}
        result = {'mode': 'system', 'top_module': contract['top_module'], 'configuration': contract['configuration'],
                  'status': 'NOT_TESTED', 'ready_for_agent3': False, 'ready_for_backend': False,
                  'verification_readiness': 'BLOCKED_SYSTEM_AGENT_2',
                  'requirements': {'passed': 0, 'failed': 0, 'not_tested': len(rows)},
                  'failure_detail': '; '.join(contract['limitations']), 'traffic': {'lost': None, 'duplicated': None}}
        result.update(environment_saved=False, checker_compile_valid=False, checker_reuse_eligible=False,
                      simulation_completed=False, agent2_result='NOT_TESTED',
                      generation={'mode': 'NOT_RUN', 'reuse_eligible': False},
                      simulation=simulation_metrics(requirement_report, None, None, False),
                      failure={'classification': 'UNSUPPORTED_VERIFICATION_CONTRACT',
                               'disposition': 'REVIEW_CONTROLLER_CONTRACT', 'frontend_feedback': None})
        report.requirements(requirement_report)
    else:
        contract['_rtl_files'] = agent1['rtl_files']
        public_contract = {k: v for k, v in contract.items() if not k.startswith('_')}
        dut_ports = agent1['dut_spec']['ports']
        report.artifact('dut_port_connectivity.json', data=controller_port_connectivity(contract, dut_ports)[1])
        with system_workspace(api, Path(report.directory) / 'work'):
            compile_ok, compile_output, compile_classification, generation, reused = prepare_system_checker(
                api, report, contract, requirements, agent1, reuse_environment)
            identity = checker_identity(api, api.ordered_tb_files(api.TB_ROOT))
            if generation['vcs_invoked']:
                last_compile = next(row for row in reversed(generation['iteration_history']) if row['vcs_invoked'])
                report.artifact('compile.log', source=str(Path(report.directory) /
                                'generation/attempt_{}/compile.log'.format(last_compile['attempt'])))
            else:
                report.artifact('generation_failure.txt', text=compile_output)
            report.artifact('checker_compile_validation.json', data={
                'checker_identity': identity, 'checker_compile_valid': compile_ok,
                'compile_invoked': generation['final_vcs_invoked'],
                'compile_result': ('PASS' if compile_ok else 'FAIL') if generation['final_vcs_invoked'] else 'NOT_RUN',
                'checker_reuse_eligible': generation['reuse_eligible'],
                'generation': generation,
                'source_hashes': {name: api.file_sha256(str(Path(api.TB_ROOT) / name)) for name in TB_ORDER}})
            simulation_ok, simulation_text = False, ''
            if compile_ok:
                report.step('simulation')
                simulation_ok, simulation_log, simulation_text = api.vcs_run(0)
            if checker_identity(api, api.ordered_tb_files(api.TB_ROOT)) != identity:
                raise ValueError('Checker source changed after compilation; simulation evidence is invalid.')
            report.artifact('simulation.log', text=simulation_text)
            if fingerprints_path:
                verify_fingerprints(api, fingerprints)
            result, requirement_report, events = summarize_system_simulation(api, requirements, public_contract, simulation_text, simulation_ok)
            if not compile_ok:
                # A compiler failure did not exercise a DUT requirement. Keep
                # the run failed while its functional checks remain NOT_TESTED.
                requirement_report = api.build_requirement_report(requirements, '', simulation_ok=False)
                result['requirements'] = {'passed': 0, 'failed': 0, 'not_tested': len(requirements)}
                stage = 'compile' if generation['final_vcs_invoked'] else 'generation'
                reason = 'SYSTEM_UVM_COMPILE_FAILED' if stage == 'compile' else 'SYSTEM_UVM_GENERATION_FAILED'
                result['failure_evidence'] = [{'stage': stage, 'reason': reason,
                                             'expected': 'Complete controller hierarchy and UVM elaborate',
                                             'observed': compile_output}]
                result.update(status='FAIL', ready_for_agent3=False, verification_readiness='BLOCKED_SYSTEM_AGENT_2',
                              failure_detail=reason + ': ' + compile_output)
                requirement_report['failure_stage'] = stage
            classification, disposition = (classify_simulation_failure(result, requirement_report, simulation_text, simulation_ok, agent1['rtl_files'])
                                            if compile_ok else (compile_classification,
                                                'AGENT2_TB_REPAIR_EXHAUSTED' if generation['repair_exhausted'] else
                                                'AGENT2_TB_REPAIR' if compile_classification in
                                                ('TESTBENCH_COMPILE_FAILURE', 'TESTBENCH_GENERATION_FAILURE') else
                                                'REVALIDATE_AGENT1_RTL' if compile_classification == 'RTL_COMPILE_FAILURE' else
                                                'INVESTIGATE_VERIFICATION_ENVIRONMENT'))
            result['failure'] = {'classification': classification, 'disposition': disposition, 'frontend_feedback': None}
            generation.update(simulation_status='PASS' if simulation_ok else 'FAIL' if compile_ok else 'NOT_RUN',
                              agent2_result=result['status'],
                              checker_source_hashes={name: api.file_sha256(str(Path(api.TB_ROOT) / name)) for name in TB_ORDER})
            result['generation'] = generation
            compile_status = compile_ok if generation['final_vcs_invoked'] else None
            result['simulation'] = simulation_metrics(requirement_report, compile_status, simulation_ok, compile_ok)
            result['simulation'].update(compile_invoked=generation['final_vcs_invoked'], simulation_invoked=compile_ok,
                                        startup_valid=valid_simulation_startup(simulation_text) if compile_ok else None)
            result.update(compile_passed=compile_status, simulation_completed=simulation_ok,
                          rtl_file_count=len(agent1['rtl_files']), checker_identity=identity,
                          testbench_reused=reused, environment_saved=True, checker_compile_valid=compile_ok,
                          checker_reuse_eligible=generation['reuse_eligible'], agent2_result=result['status'],
                          agent1_result=str(Path(agent1_path).resolve()))
            compile_flags = ['-sverilog', '-full64', '-timescale=1ns/1ps'] + list(api.VCS_UVM_FLAGS) + ['-top', 'dut_tb_top']
            if api.ENABLE_DEBUG:
                compile_flags += ['-debug_access+r+w-memcbk', '-debug_region+cell']
            compile_flags += ['+incdir+' + p for p in api.gather_incdirs(api.TB_ROOT)]
            compile_flags += ['+incdir+' + p for p in sorted({str(Path(p).parent) for p in agent1['rtl_files']})]
            dut_spec = dict(agent1['dut_spec'], module_name=contract['top_module'])
            context = {'contract': public_contract, 'source_manifest': str(manifest_path),
                       'checker_identity': identity, 'compile_flags': compile_flags,
                       'ready_for_agent3': result['ready_for_agent3'],
                       'verification_readiness': result['verification_readiness'],
                       'system_verification_status': result['status'],
                       'generation': generation, 'compile_valid': compile_ok,
                       'reuse_eligible': generation['reuse_eligible'], 'simulation_completed': simulation_ok}
            environment_path = api.save_uvm_environment(requirement_report, requirements, agent1['rtl_files'], dut_spec, None,
                                                       compile_output, simulation_text, system_context=context)
            report.artifact('uvm_environment.json', source=environment_path)
            report.artifact('monitor_events.json', data=events)
            report.artifact('failure_evidence.json', data=result['failure_evidence'])
            waveform = Path(api.BUILD_DIR) / 'controller.vcd'
            if waveform.exists():
                report.artifact('controller.vcd', source=str(waveform))
            report.requirements(requirement_report)
            if (classification == 'RTL_FUNCTIONAL_FAILURE' and generation['integrity_audit_passed'] and
                    compile_ok and valid_simulation_startup(simulation_text)):
                result['failure']['frontend_feedback'] = frontend_failure_package(report, result, public_contract, simulation_text, events)
            result['uvm_environment_file'] = environment_path
            if fingerprints_path:
                verify_fingerprints(api, fingerprints)
    result['rtl_file_count'] = len(agent1['rtl_files'])
    result['artifacts'] = dict(report.data['artifacts'])
    report.artifact('system_report.json', data=result)
    report.update(verification_status=result['status'], verification_readiness=result['verification_readiness'],
                  ready_for_agent3=result['ready_for_agent3'], ready_for_backend=False,
                  failure_classification=result['failure']['classification'],
                  disposition=result['failure']['disposition'], frontend_feedback=result['failure']['frontend_feedback'],
                  frontend_feedback_artifact=report.data['artifacts'].get('frontend_feedback.json'),
                  checker_compile_valid=result['checker_compile_valid'], checker_reuse_eligible=result['checker_reuse_eligible'],
                  failure_detail=result.get('failure_detail'), coverage_limitations=result.get('coverage_limitations', []))
    print_system_report(result, contract)
    return result['ready_for_agent3']


def system_main(api, arguments):
    parser = argparse.ArgumentParser(description='Agent 2 full-system controller UVM verification')
    parser.add_argument('manifest', help='Frontend manifest.json')
    parser.add_argument('--system', action='store_true')
    parser.add_argument('--agent1-result', required=True)
    parser.add_argument('--agent1-inputs', required=True, help='Input fingerprints captured around Agent 1 validation by run_pipeline')
    parser.add_argument('--reuse-tb', action='store_true')
    parser.add_argument('--uvm-environment', help='Exact saved system UVM environment for reuse')
    args = parser.parse_args(arguments)
    if args.reuse_tb != bool(args.uvm_environment):
        parser.error('--reuse-tb and --uvm-environment must be supplied together in direct system mode.')
    def entrypoint(report):
        metadata_path = str(Path(report.run_dir) / 'run.json')
        metadata = read_report_json(metadata_path)
        metadata.update(mode='system', verification_scope='controller_functional_simulation', ready_for_backend=False)
        write_report_json(metadata_path, metadata)
        ok = run_system_uvm(api, report, args.manifest, args.agent1_result, args.uvm_environment, args.agent1_inputs)
        if not ok:
            raise SystemExit(1)
    run_reported_agent('agent2', entrypoint, [args.manifest])
