#!/usr/bin/env python3
"""Run bounded local Verilator scenarios against an authenticated RTL handoff.

Explicit executable profiles cover compact FIFO controllers and single-request
open-page controllers with declared-port observations. It does not execute arbitrary intent
prose, replace UVM, or mark whole source requirements verified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import uuid

from specification import SpecificationError, read_document
from verification_handoff import validate_handoff
from simulation.controller import CheckFailure, ControllerOracle, scenarios

PROFILE = 'ddr4-fifo-v1'
IDENTIFIER = re.compile(r'[a-zA-Z_][a-zA-Z0-9_$]*\Z')
HOST = {'clk': ('input', 1), 'rst_n': ('input', 1), 'txn_valid': ('input', 1),
        'txn_is_write': ('input', 1), 'txn_addr': ('input', 4), 'txn_wdata': ('input', 32),
        'txn_ready': ('output', 1), 'cmd_ready': ('output', 1),
        'rsp_valid': ('output', 1), 'rsp_rdata': ('output', 32)}


def profile_check(handoff):
    if handoff['phase'] != 'rtl':
        raise ValueError('Simulation requires an rtl-phase handoff')
    shared = handoff['shared']
    p = shared['parameters']
    for name, value in {'ACTIVE_CAPACITY': 1, 'DATA_WIDTH': 32, 'HOST_ADDR_WIDTH': 4,
                        'MEMORY_WORDS_PER_BANK': 16, 'READ_RESPONSE_LATENCY': 1}.items():
        if p.get(name) != value:
            raise ValueError(f'{PROFILE} requires {name}={value}')
    if p.get('BANK_COUNT') not in (1, 2, 4) or p.get('PAGE_POLICY') not in ('open_page', 'close_page'):
        raise ValueError('Unsupported bank count or page policy')
    if type(p.get('WAITING_CAPACITY')) is not int or p['WAITING_CAPACITY'] <= 0:
        raise ValueError('Unsupported waiting capacity')
    cr = shared['clock_reset']
    if any(cr.get(k) != v for k, v in {'clock': 'clk', 'reset': 'rst_n', 'edge': 'rising',
                                      'active_level': 0, 'assertion_style': 'synchronous'}.items()):
        raise ValueError('Profile requires synchronous active-low reset and rising clk')
    if shared['timing_profile'].get('unit') != 'cycles':
        raise ValueError('Profile requires numeric timing cycles')
    for key in ('tRCD', 'tRAS', 'tRP', 'tRRD', 'tFAW', 'tREFI', 'tRFC'):
        value = shared['timing_profile']['values'].get(key)
        if type(value) is not int or not 1 <= value <= 100000:
            raise ValueError(f'Unsupported timing value: {key}')
    if type(p.get('TFAW_ACT_LIMIT')) is not int or p['TFAW_ACT_LIMIT'] < 1:
        raise ValueError('Unsupported tFAW activation limit')
    ports = {v['name']: (v['direction'], v['width']) for v in handoff['design']['top_ports']}
    expected = dict(HOST)
    if p['BANK_COUNT'] > 1:
        expected['txn_bank'] = ('input', (p['BANK_COUNT'] - 1).bit_length())
    if ports != expected:
        raise ValueError('Top interface is outside the compact-host simulation profile')
    b = handoff['verification_intent']['bindings']
    if b['sampling']['phase'] != 'pre_edge' or not b['sampling']['reset_gates_events']:
        raise ValueError('Profile requires reset-gated pre-edge event sampling')
    for event, payload in (('admission', 'request'), ('dispatch', 'request'),
                           ('command_commit', 'command'), ('response_delivery', 'data')):
        entry = b['events'].get(event, {})
        if entry.get('status') != 'bound' or payload not in entry.get('payload', {}):
            raise ValueError(f'Missing required bound event: {event}')
    if set(shared['types']['opcode']['encodings']) != {'NOP', 'ACT', 'PRE', 'READ', 'WRITE', 'REF'}:
        raise ValueError('Unsupported command vocabulary')
    if shared['types']['opcode']['encodings']['NOP'] != 0:
        raise ValueError('Profile requires canonical zero NOP')
    for name, width in (('bank_rows', shared['types']['row']['width']), ('bank_valid', 1)):
        array = shared['types'].get(name, {})
        if any(array.get(k) != v for k, v in {'kind': 'array', 'count': p['BANK_COUNT'],
                  'element_width': width, 'packed_order': 'ascending_index_from_lsb'}.items()):
            raise ValueError('Unsupported observation array layout: ' + name)
    for name, fields in [('request', {'bank', 'row', 'column', 'is_write', 'wdata'}),
                         ('command', {'opcode', 'bank', 'row', 'column', 'wdata'})]:
        typ = shared['types'][name]
        if typ['kind'] != 'record' or {f['name'] for f in typ['fields']} != fields:
            raise ValueError(f'Unsupported packed type: {name}')
    return ports


def testbench(handoff):
    ports = profile_check(handoff)
    design = handoff['design']
    if not IDENTIFIER.fullmatch(design['top_module']):
        raise ValueError('Unsupported HDL top identifier')
    endpoints = {'top.' + name: name for name in ports}
    widths = {'top.' + name: value[1] for name, value in ports.items()}
    for module in design['modules']:
        if not IDENTIFIER.fullmatch(module['instance']):
            raise ValueError('Unsupported HDL instance identifier')
        for port in module['ports']:
            if not IDENTIFIER.fullmatch(port['name']):
                raise ValueError('Unsupported HDL port identifier')
            suffix = module['instance'] + '.' + port['name']
            endpoints[suffix] = suffix
            widths[suffix] = port['width']
    def leaf(binding):
        endpoint = binding['endpoint']
        if endpoint not in endpoints or binding['dut_path_suffix'] != endpoints[endpoint]:
            raise ValueError('Observation must name a declared top/module port')
        signal = f'dut.{endpoints[endpoint]}'
        if binding['lsb'] == 0 and binding['msb'] == widths[endpoint] - 1:
            return signal
        return f'{signal}[{binding["msb"]}:{binding["lsb"]}]'
    def predicate(value):
        for key, joiner in [('all', ' && '), ('any', ' || ')]:
            if key in value:
                return '(' + joiner.join(predicate(v) for v in value[key]) + ')'
        return f'({leaf(value)} == {value["width"]}\'d{value["equals"]})'
    bindings = handoff['verification_intent']['bindings']
    events, obs = bindings['events'], bindings['observations']
    signals = {'rst_n': 'rst_n', 'valid': 'txn_valid', 'ready': 'txn_ready', 'cmd_ready': 'cmd_ready',
               'write': 'txn_is_write', 'bank': 'txn_bank' if 'txn_bank' in ports else "1'b0",
               'addr': 'txn_addr', 'wdata': 'txn_wdata', 'rsp_valid': 'rsp_valid', 'rsp_data': 'rsp_rdata'}
    for label, event in [('admission', 'admission'), ('dispatch', 'dispatch'), ('commit', 'command_commit')]:
        # Keep raw event predicates visible during reset so the checker can
        # detect missing DUT gating rather than masking it in the testbench.
        signals[label] = predicate(events[event]['when'])
    for label, event, payload in [('incoming_request', 'admission', 'request'),
                                  ('dispatch_request', 'dispatch', 'request'),
                                  ('commit_command', 'command_commit', 'command')]:
        signals[label] = leaf(events[event]['payload'][payload])
    for label, observation in [('candidate_valid', 'candidate_valid'), ('candidate_command', 'candidate_command'),
                                ('candidate_ok', 'candidate_ok'), ('probe_valid', 'probe_valid'),
                                ('probe_ok', 'probe_ok'), ('open_valid', 'open_valid'),
                                ('open_rows', 'open_rows'), ('pending', 'refresh_pending'), ('busy', 'refresh_busy')]:
        if observation not in obs:
            raise ValueError('Missing required observation: ' + observation)
        signals[label] = leaf(obs[observation])
    declarations = '\n'.join(f'logic {"" if width == 1 else f"[{width-1}:0] "}{name};'
                              for name, (_, width) in ports.items())
    connections = ', '.join(f'.{name}({name})' for name in ports)
    header = 'cycle ' + ' '.join(signals)
    fmt = ' '.join(['%0h'] * (len(signals) + 1)) + '\\n'
    values = ', '.join(['cycle', *signals.values()])
    return f'''`timescale 1ns/1ps
module capstone_sim_tb;
{declarations}
{design['top_module']} dut ({connections});
integer vectors, trace_fd, scan, op, amount, bank_arg, addr_arg, cycles_limit;
integer cycle = 0;
logic [31:0] data_arg;
bit accepted;
string vector_path, trace_path;
task automatic step(input bit reset_value, input bit valid_value, input bit write_value,
                    input integer bank_value, input integer addr_value, input logic [31:0] data_value);
    clk = 0; rst_n = reset_value; txn_valid = valid_value; txn_is_write = write_value;
    txn_addr = 4'(addr_value); txn_wdata = data_value;
    {f"txn_bank = {ports['txn_bank'][1]}'(bank_value);" if 'txn_bank' in ports else ''}
    #5;
    accepted = txn_valid && txn_ready;
    $fwrite(trace_fd, "{fmt}", {values});
    clk = 1;
    #5;
    clk = 0;
    cycle = cycle + 1;
    if (cycle >= cycles_limit) $fatal(1, "SIMULATION_CYCLE_LIMIT %0d", cycle);
endtask
initial begin
    if (!$value$plusargs("VECTORS=%s", vector_path) || !$value$plusargs("TRACE=%s", trace_path)
        || !$value$plusargs("MAX_CYCLES=%d", cycles_limit)) $fatal(1, "Missing runner arguments");
    vectors = $fopen(vector_path, "r"); trace_fd = $fopen(trace_path, "w");
    if (vectors == 0 || trace_fd == 0) $fatal(1, "Cannot open simulation evidence files");
    $fwrite(trace_fd, "{header}\\n");
    while (!$feof(vectors)) begin
        scan = $fscanf(vectors, "%d %d %d %d %h", op, amount, bank_arg, addr_arg, data_arg);
        if (scan == 5) begin
            case (op)
              0: repeat (amount) step(0, 1, 1, 0, 15, 32'hdeadbeef);
              1: repeat (amount) step(1, 0, 0, 0, 0, 0);
              2: begin
                  accepted = 0;
                  while (!accepted) step(1, 1, 1'(amount), bank_arg, addr_arg, data_arg);
              end
              default: $fatal(1, "Invalid vector operation");
            endcase
        end else if (scan != -1) $fatal(1, "Malformed vector row");
    end
    $fclose(trace_fd); $fclose(vectors);
    $display("SIMULATION_DONE cycles=%0d", cycle);
    $finish;
end
endmodule
'''


def read_trace(path):
    with Path(path).open() as stream:
        header = stream.readline().split()
        if not header or header[0] != 'cycle' or len(set(header)) != len(header):
            raise ValueError('Malformed simulation trace header')
        expected_cycle = 0
        for line in stream:
            values = line.split()
            if len(values) != len(header):
                raise ValueError('Incomplete simulation trace row')
            row = dict(zip(header, (int(v, 16) for v in values)))
            if row['cycle'] != expected_cycle:
                raise ValueError('Missing/duplicate simulation trace cycle')
            expected_cycle += 1
            yield row
        if not expected_cycle:
            raise ValueError('Empty simulation trace')


def check_trace(path, shared, scenario):
    oracle = ControllerOracle(shared)
    cycle = -1
    try:
        for row in read_trace(path):
            cycle = row['cycle']
            oracle.step(row)
        oracle.finish(cycle)
        required = {'reset_zero': ('READ', 'responses'), 'fifo_memory': ('READ', 'WRITE', 'responses'),
                    'queue_pressure': ('READ', 'WRITE', 'host_stall_edges', 'full_simultaneous'),
                    'refresh_traffic': ('REF', 'READ', 'WRITE', 'responses'),
                    'reset_inflight': ('READ', 'responses')}[scenario]
        for key in required:
            oracle.check(oracle.counts[key] > 0, 'scenario_coverage', cycle, f'No executed {key} coverage')
        if scenario == 'refresh_traffic':
            oracle.check(oracle.counts['REF'] >= 2, 'scenario_coverage', cycle, 'Fewer than two refresh services')
            for counter in ('pending_active_edges', 'admissions_after_refresh'):
                oracle.check(oracle.counts[counter] > 0, 'scenario_coverage', cycle, f'No {counter} coverage')
        if scenario == 'reset_inflight':
            oracle.check(oracle.counts['reset_canceled'] > 0, 'scenario_coverage', cycle, 'No accepted work interrupted by reset')
        return {'status': 'passed', 'cycles': cycle + 1, 'counters': dict(oracle.counts)}
    except CheckFailure as exc:
        return {'status': 'failed', 'cycles': cycle + 1, 'counters': dict(oracle.counts),
                'failure': {'check': exc.check, 'cycle': exc.cycle, 'message': str(exc)}}


def run(handoff_path, output=None, *, seed=1, max_cycles=200000, timeout=120, profile=PROFILE):
    handoff_path = Path(handoff_path).resolve()
    output = Path(output).resolve() if output else handoff_path.parent / 'simulations' / (
        datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'capstone.simulation_run', 'schema_version': 1, 'profile': profile,
              'status': 'invalid_input', 'seed': seed, 'scenarios': {}, 'limitations': [
                  'Bounded directed scenarios, not exhaustive verification or on-chip BIST.',
                  'Verilator simulation uses two-state initialization; X propagation is not verified.',
                  'Source requirements/obligations remain unverified; profile check results are separate.',
                  'No isolated-module permission tests, exhaustive timing-boundary tests or configuration matrix.',
                  'UVM, the legacy system consumer, orchestrator and backend are not invoked.']}
    def command(argv, log):
        report['commands'].append(argv)
        with (output / log).open('w') as stream:
            result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
        return result.returncode
    try:
        if type(max_cycles) is not int or max_cycles < 1 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Cycle and wall-clock bounds must be positive')
        if profile == PROFILE:
            check_profile, make_tb, make_scenarios, check_result = profile_check, testbench, scenarios, check_trace
        elif profile == 'ddr4-single-request-v1':
            from simulation import single_request
            check_profile, make_tb = single_request.profile_check, single_request.testbench
            make_scenarios, check_result = single_request.scenarios, single_request.check_trace
            report['limitations'].append(
                'Single-request/open-page profile: no refresh, waiting FIFO or tRP semantics. '
                'tRCD/tRAS blocked and exact-boundary coverage is required; tRRD/tFAW are checked '
                'on encountered candidates, without isolated timing-boundary coverage.')
        elif profile == 'ddr4-timing-guard-v1':
            from simulation import timing_guard
            check_profile, make_tb = timing_guard.profile_check, timing_guard.testbench
            make_scenarios, check_result = timing_guard.scenarios, timing_guard.check_trace
            report['limitations'] = [v for v in report['limitations'] if not v.startswith('No isolated-module')]
            report['limitations'].append('Isolated timing-role module only. Bank state and commit inputs are stubs; '
                'stimuli need not be reachable through the full controller. No queue, memory, AXI or full-system '
                'verification is performed; tFAW is isolated from bank-row lifecycle to exercise its limit.')
        else:
            raise ValueError('Unsupported simulation profile: ' + str(profile))
        validate_handoff(handoff_path)
        handoff = read_document(handoff_path)
        check_profile(handoff)
        architecture_entry = next(v for v in handoff['inputs'] if v['role'] == 'architecture')
        architecture = read_document(handoff_path.parent / architecture_entry['path'])
        request = architecture['provenance']['specification']['snapshot']['package'].get('generation', {}).get('request', {})
        features = request.get('constraints', {}).get('features', {})
        policy = features.get('scheduler')
        if profile == PROFILE and policy is not None and policy != 'fifo':
            raise ValueError(f'{PROFILE} does not support requested scheduler {policy!r}')
        if profile == PROFILE and any(features.get(key) is False for key in ('refresh', 'tRRD', 'tFAW')):
            raise ValueError(f'{PROFILE} requires refresh, tRRD and tFAW enabled')
        if profile == 'ddr4-single-request-v1' and features.get('refresh') is True:
            raise ValueError(f'{profile} does not support requested refresh')
        report['identity'] = dict(handoff['identity'], handoff_fingerprint=handoff['handoff_fingerprint'])
        report['source_results'] = {group: {key: {'status': 'unverified', 'stage': 'local_simulation',
                'test_ids': [], 'evidence': []} for key in handoff['verification_intent'][group]}
                for group in ('requirements', 'obligations')}
        # Compile a private authenticated snapshot, avoiding stale or changed RTL
        # and leaving the user's build and legacy outputs untouched.
        snapshot = output / 'inputs'
        for entry in handoff['inputs']:
            dest = snapshot / entry['path']
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(handoff_path.parent / entry['path'], dest)
        shutil.copyfile(handoff_path, snapshot / 'verification_handoff.json')
        validate_handoff(snapshot / 'verification_handoff.json')
        if read_document(snapshot / 'verification_handoff.json') != handoff:
            raise ValueError('Handoff changed while freezing simulation inputs')
        tb = output / 'testbench.sv'
        tb.write_text(make_tb(handoff))
        report['commands'] = []
        report['status'] = 'tool_error'
        verilator = shutil.which('verilator')
        if not verilator:
            raise ValueError('Verilator is not installed')
        version = subprocess.run([verilator, '--version'], capture_output=True, text=True, timeout=10, check=True)
        report['simulator'] = version.stdout.strip()
        rtl = [str(snapshot / filename) for filename in handoff['rtl_files']]
        cmd = [verilator, '--binary', '--timing', '-Wno-fatal', '--top-module', 'capstone_sim_tb',
               '--Mdir', str(output / 'obj'), '-j', '2', *rtl, str(tb)]
        if command(cmd, 'compile.log'):
            raise ValueError('Verilator build failed; see compile.log')
        for name, vectors in make_scenarios(handoff['shared'], seed).items():
            vector_path, trace = output / (name + '.vectors'), output / (name + '.trace')
            vector_path.write_text(''.join(f'{op} {amount} {bank} {addr} {data:08x}\n'
                                           for op, amount, bank, addr, data in vectors))
            cmd = [str(output / 'obj/Vcapstone_sim_tb'), '+VECTORS=' + str(vector_path),
                   '+TRACE=' + str(trace), '+MAX_CYCLES=' + str(max_cycles)]
            code = command(cmd, name + '.log')
            log = (output / (name + '.log')).read_text()
            if code or 'SIMULATION_DONE cycles=' not in log:
                result = {'status': 'failed' if 'SIMULATION_CYCLE_LIMIT' in log else 'tool_error',
                          'failure': {'check': 'bounded_completion' if 'SIMULATION_CYCLE_LIMIT' in log else 'simulation_execution',
                                      'message': f'Simulator exit {code}; see {name}.log'}}
            else:
                result = check_result(trace, handoff['shared'], name)
            report['scenarios'][name] = dict(result, trace=trace.name, vectors=vector_path.name, log=name + '.log')
            print(f'[SIMULATION] {name}: {result["status"]}' + (
                ' — ' + result['failure']['message'] if 'failure' in result else ''), flush=True)
        statuses = {row['status'] for row in report['scenarios'].values()}
        report['status'] = 'tool_error' if 'tool_error' in statuses else ('failed' if 'failed' in statuses else 'passed')
    except (ValueError, KeyError, OSError, SpecificationError, subprocess.SubprocessError) as exc:
        report['error'] = str(exc)
    finally:
        report['evidence'] = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                              for pattern in ('*.sv', '*.vectors', '*.trace', '*.log') for path in output.glob(pattern)}
        report['runner_sources'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (Path(__file__), Path(__file__).parent / 'simulation/controller.py')}
        if profile == 'ddr4-single-request-v1':
            source = Path(__file__).parent / 'simulation/single_request.py'
            report['runner_sources']['single_request.py'] = hashlib.sha256(source.read_bytes()).hexdigest()
        if profile == 'ddr4-timing-guard-v1':
            source = Path(__file__).parent / 'simulation/timing_guard.py'
            report['runner_sources']['timing_guard.py'] = hashlib.sha256(source.read_bytes()).hexdigest()
        (output / 'simulation_report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'[SIMULATION] {report["status"]}: {output / "simulation_report.json"}', flush=True)
    if 'error' in report:
        print('[SIMULATION] ERROR: ' + report['error'], flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('handoff', type=Path)
    parser.add_argument('--output-dir', type=Path, help='Fresh simulation evidence directory')
    parser.add_argument('--profile', choices=[PROFILE, 'ddr4-single-request-v1', 'ddr4-timing-guard-v1'], default=PROFILE)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--max-cycles', type=int, default=200000, help='Per-scenario simulated edge limit')
    parser.add_argument('--timeout', type=float, default=120, help='Seconds per compiler/simulator process')
    args = parser.parse_args()
    try:
        report = run(args.handoff, args.output_dir, seed=args.seed, max_cycles=args.max_cycles, timeout=args.timeout, profile=args.profile)
    except (OSError, ValueError) as exc:
        print(f'[SIMULATION] ERROR: {exc}')
        return 2
    return 0 if report['status'] == 'passed' else (1 if report['status'] == 'failed' else 2)


if __name__ == '__main__':
    raise SystemExit(main())
