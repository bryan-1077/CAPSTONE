"""Explicit single-request/open-page profile; expectations use admitted traffic.

No refresh, waiting FIFO, or tRP is inferred. Only the four declared timings are
checked. Passing directed traces is not exhaustive proof of source requirements.
"""
from collections import Counter
import random
import re

from simulation.controller import CheckFailure

PROFILE = 'ddr4-single-request-v1'
HOST = {'clk': ('input', 1), 'rst_n': ('input', 1), 'txn_valid': ('input', 1),
        'txn_ready': ('output', 1), 'txn_addr': ('input', 8), 'txn_bank': ('input', 2),
        'txn_is_write': ('input', 1), 'txn_wdata': ('input', 32),
        'rsp_valid': ('output', 1), 'rsp_rdata': ('output', 32),
        'cmd_valid': ('output', 1), 'cmd': ('output', 45)}
LAYOUTS = {'request': [('bank', 2, 41), ('address', 8, 33), ('is_write', 1, 32), ('wdata', 32, 0)],
           'command': [('opcode', 3, 42), ('bank', 2, 40), ('row', 4, 36), ('column', 4, 32), ('wdata', 32, 0)]}


def profile_check(h):
    if h['phase'] != 'rtl':
        raise ValueError('Single-request simulation requires an rtl-phase handoff')
    shared = h['shared']
    for name, value in {'BANK_COUNT': 4, 'DATA_WIDTH': 32, 'HOST_ADDR_WIDTH': 8,
                        'ROW_WIDTH': 4, 'COLUMN_WIDTH': 4, 'REQUEST_CAPACITY': 1,
                        'READ_RESPONSE_LATENCY': 1, 'COMMAND_OPCODE_WIDTH': 3,
                        'TFAW_ACT_LIMIT': 4, 'HOST_BANK_WIDTH': 2, 'MEMORY_WORDS_PER_BANK': 256}.items():
        if type(shared['parameters'].get(name)) is not int or shared['parameters'][name] != value:
            raise ValueError(f'{PROFILE} requires {name}={value}')
    if {v['name']: (v['direction'], v['width']) for v in h['design']['top_ports']} != HOST:
        raise ValueError('Top interface is outside the single-request profile')
    cr = shared['clock_reset']
    if any(cr.get(k) != v for k, v in {'clock': 'clk', 'reset': 'rst_n', 'edge': 'rising',
                                      'active_level': 0, 'assertion_style': 'synchronous'}.items()):
        raise ValueError('Single-request profile requires synchronous active-low reset')
    timings = shared['timing_profile']
    if timings['unit'] != 'cycles' or set(timings['values']) != {'tRCD', 'tRAS', 'tRRD', 'tFAW'}:
        raise ValueError('Single-request profile supports exactly tRCD/tRAS/tRRD/tFAW')
    if any(type(v) is not int or not 1 <= v <= 100000 for v in timings['values'].values()):
        raise ValueError('Invalid single-request timing cycles')
    types = shared['types']
    if types['opcode']['encodings'] != {'NOP': 0, 'ACT': 1, 'PRE': 2, 'READ': 3, 'WRITE': 4}:
        raise ValueError('Single-request opcode vocabulary mismatch (refresh is not supported)')
    for name, expected in LAYOUTS.items():
        typ = types[name]
        if typ['kind'] != 'record' or [(f['name'], f['width'], f['lsb']) for f in typ['fields']] != expected:
            raise ValueError('Unsupported single-request packed layout: ' + name)
    for name, width in (('bank_rows', 4), ('bank_valid', 1)):
        typ = types[name]
        if any(typ.get(k) != v for k, v in {'kind': 'array', 'count': 4, 'element_width': width,
                                           'packed_order': 'ascending_index_from_lsb'}.items()):
            raise ValueError('Unsupported bank observation layout')
    bindings = h['verification_intent']['bindings']
    if bindings['sampling']['phase'] != 'pre_edge' or not bindings['sampling']['reset_gates_events']:
        raise ValueError('Single-request profile requires reset-gated pre-edge sampling')
    for name in ('request_admission', 'command_commit', 'response_delivery', 'retirement'):
        if bindings['events'].get(name, {}).get('status') != 'bound':
            raise ValueError('Missing required bound event: ' + name)
    for name in ('candidate_command', 'candidate_valid', 'candidate_ok', 'open_rows', 'open_valid', 'engine_idle'):
        if name not in bindings['observations']:
            raise ValueError('Missing required observation: ' + name)
    return HOST


def testbench(h):
    ports = profile_check(h)
    design = h['design']
    identifier = re.compile(r'[a-zA-Z_][a-zA-Z0-9_$]*\Z')
    if not identifier.fullmatch(design['top_module']):
        raise ValueError('Invalid top module identifier')
    endpoints = {'top.' + name: (name, width) for name, (_, width) in ports.items()}
    for module in design['modules']:
        if not identifier.fullmatch(module['instance']):
            raise ValueError('Invalid module instance identifier')
        for port in module['ports']:
            if not identifier.fullmatch(port['name']):
                raise ValueError('Invalid module port identifier')
            suffix = module['instance'] + '.' + port['name']
            endpoints[suffix] = (suffix, port['width'])
    def leaf(binding):
        suffix, width = endpoints[binding['endpoint']]
        if binding['dut_path_suffix'] != suffix:
            raise ValueError('Observation must name an authenticated declared port')
        signal = 'dut.' + suffix
        return signal if binding['lsb'] == 0 and binding['msb'] == width - 1 else f'{signal}[{binding["msb"]}:{binding["lsb"]}]'
    def predicate(value):
        for key, joiner in (('all', ' && '), ('any', ' || ')):
            if key in value:
                return '(' + joiner.join(predicate(v) for v in value[key]) + ')'
        return f'({leaf(value)} == {value["width"]}\'d{value["equals"]})'
    b = h['verification_intent']['bindings']
    signals = {name: name for name in ports if name != 'clk'}
    for name in ('candidate_command', 'candidate_valid', 'candidate_ok', 'open_rows', 'open_valid', 'engine_idle'):
        signals[name] = leaf(b['observations'][name])
    for name in ('request_admission', 'command_commit', 'response_delivery', 'retirement'):
        signals[name] = predicate(b['events'][name]['when'])
    signals['incoming_request'] = leaf(b['events']['request_admission']['payload']['request'])
    declarations = '\n'.join(f'logic {"" if w == 1 else f"[{w-1}:0] "}{n};' for n, (_, w) in ports.items())
    connections = ', '.join(f'.{n}({n})' for n in ports)
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
  txn_bank = 2'(bank_value); txn_addr = 8'(addr_value); txn_wdata = data_value;
  #5;
  accepted = txn_valid && txn_ready;
  $fwrite(trace_fd, "{fmt}", {values});
  clk = 1; #5; clk = 0;
  cycle = cycle + 1;
  if (cycle >= cycles_limit) $fatal(1, "SIMULATION_CYCLE_LIMIT %0d", cycle);
endtask
initial begin
  if (!$value$plusargs("VECTORS=%s", vector_path) || !$value$plusargs("TRACE=%s", trace_path)
      || !$value$plusargs("MAX_CYCLES=%d", cycles_limit)) $fatal(1, "Missing runner arguments");
  vectors = $fopen(vector_path, "r"); trace_fd = $fopen(trace_path, "w");
  if (vectors == 0 || trace_fd == 0) $fatal(1, "Cannot open simulation evidence");
  $fwrite(trace_fd, "{header}\\n");
  while (!$feof(vectors)) begin
    scan = $fscanf(vectors, "%d %d %d %d %h", op, amount, bank_arg, addr_arg, data_arg);
    if (scan == 5) begin
      case (op)
        0: repeat (amount) step(0, 1, 1, 3, 255, 32'hdeadbeef);
        1: repeat (amount) step(1, 0, 1, 3, 255, 32'hdeadbeef);
        2: begin
          accepted = 0;
          while (!accepted) step(1, 1, 1'(amount), bank_arg, addr_arg, data_arg);
        end
        3: while (!rsp_valid) step(1, 0, 0, 0, 0, 0);
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


class SingleRequestOracle:
    def __init__(self, shared):
        self.t = shared['timing_profile']['values']
        self.counts = Counter()
        self.last_reset = False
        self.reset()

    def check(self, condition, name, cycle, message):
        self.counts['check:' + name] += 1
        if not condition:
            raise CheckFailure(name, cycle, message)

    def reset(self):
        self.active = None
        self.rows = [None] * 4
        self.acts = [None] * 4
        self.history = []
        self.memory = {}
        self.response = None
        self.last_data = 0
        self.held = None

    @staticmethod
    def decode(value):
        return {n: (value >> lsb) & ((1 << width) - 1) for n, width, lsb in LAYOUTS['command']}

    def allowed(self, cmd, cycle):
        b, row, op = cmd['bank'], cmd['row'], cmd['opcode']
        if op == 1:
            return (self.rows[b] is None and all(cycle - e >= self.t['tRRD'] for e in self.history)
                    and sum(cycle - e < self.t['tFAW'] for e in self.history) < 4)
        if op == 2:
            return self.rows[b] is not None and (self.acts[b] is None or cycle - self.acts[b] >= self.t['tRAS'])
        if op in (3, 4):
            return self.rows[b] == row and self.acts[b] is not None and cycle - self.acts[b] >= self.t['tRCD']
        return False

    def step(self, s):
        c = s['cycle']
        if not s['rst_n']:
            self.check(not any(s[k] for k in ('txn_ready', 'cmd_valid', 'rsp_valid', 'candidate_valid',
                      'request_admission', 'command_commit', 'response_delivery', 'retirement')),
                       'reset_gating', c, 'Events/valid/ready must be suppressed during reset')
            self.check(s['cmd'] == s['rsp_rdata'] == 0, 'reset_payload', c, 'Reset payload is not zero')
            if self.last_reset:
                self.check(s['open_rows'] == s['open_valid'] == 0, 'reset_rows', c, 'Bank state did not clear')
            self.counts['reset_canceled'] += int(self.active is not None)
            self.counts['reset_response_canceled'] += int(self.response is not None)
            self.reset()
            self.last_reset = True
            return
        self.last_reset = False
        self.check(bool(s['request_admission']) == bool(s['txn_valid'] and s['txn_ready']),
                   'admission_binding', c, 'Admission binding differs from host handshake')
        self.check(bool(s['command_commit']) == bool(s['cmd_valid']), 'commit_binding', c, 'Commit event mismatch')
        self.check(bool(s['response_delivery']) == bool(s['rsp_valid']), 'response_binding', c, 'Response event mismatch')
        expected_valid = self.response is not None and self.response[0] == c
        self.check(bool(s['rsp_valid']) == expected_valid, 'response_latency', c, 'READ must respond exactly at E+1')
        self.check(s['rsp_rdata'] == self.last_data, 'read_data', c, 'Read data or retained response data is wrong')
        if expected_valid:
            self.counts['responses'] += 1
            self.response = None
        for b in range(4):
            self.check(bool(s['open_valid'] & (1 << b)) == (self.rows[b] is not None),
                       'row_state', c, 'Bank open validity differs from committed history')
            if self.rows[b] is not None:
                self.check((s['open_rows'] >> (4*b)) & 15 == self.rows[b], 'row_state', c, 'Open row mismatch')
        self.check(self.active is None or not s['txn_ready'], 'single_request_capacity', c,
                   'Ready asserted before active request retirement')
        if s['txn_valid'] and not s['txn_ready']:
            self.counts['host_stall_edges'] += 1
        if self.held is not None:
            self.check(s['candidate_valid'] and s['candidate_command'] == self.held,
                       'candidate_hold', c, 'Blocked candidate changed or disappeared')
        self.check(bool(s['cmd_valid']) == bool(s['candidate_valid'] and s['candidate_ok']),
                   'commit_gating', c, 'Commit must match candidate validity and timing permission')
        self.check(s['cmd'] == (s['candidate_command'] if s['cmd_valid'] else 0),
                   'command_payload', c, 'Committed/idle command payload mismatch')
        self.held = None
        if s['candidate_valid']:
            self.check(self.active is not None, 'request_ownership', c, 'Candidate without an admitted request')
            a = self.active
            b, address, write, data = a
            row, column = address >> 4, address & 15
            op = 1 if self.rows[b] is None else 2 if self.rows[b] != row else 4 if write else 3
            expected = {'opcode': op, 'bank': b, 'row': row if op != 2 else 0,
                        'column': column if op in (3, 4) else 0, 'wdata': data if op == 4 else 0}
            cmd = self.decode(s['candidate_command'])
            self.check(cmd == expected, 'captured_request_sequence', c, 'Candidate differs from admitted request/row policy')
            permit = self.allowed(cmd, c)
            self.check(bool(s['candidate_ok']) == permit, 'timing_permission', c,
                       'Timing permission differs from independent commit history')
            if op in (2, 3, 4) and self.acts[b] is not None:
                timing = 'tRAS' if op == 2 else 'tRCD'
                age = c - self.acts[b]
                self.counts[timing + ':blocked'] += int(age < self.t[timing])
                self.counts[timing + ':boundary'] += int(age == self.t[timing])
            if not permit:
                self.held = s['candidate_command']
                self.counts['blocked_candidate_edges'] += 1
        else:
            self.check(not s['candidate_ok'], 'invalid_candidate_permission', c, 'Invalid candidate was permitted')
        if s['cmd_valid']:
            cmd = self.decode(s['cmd'])
            op, b = cmd['opcode'], cmd['bank']
            self.counts[{1:'ACT', 2:'PRE', 3:'READ', 4:'WRITE'}[op]] += 1
            if op == 1:
                self.rows[b], self.acts[b] = cmd['row'], c
                self.history.append(c)
            elif op == 2:
                self.rows[b] = None
            else:
                address = (cmd['row'] << 4) | cmd['column']
                if op == 4:
                    self.memory[b, address] = cmd['wdata']
                else:
                    self.last_data = self.memory.get((b, address), 0)
                    self.response = (c+1, self.last_data)
                self.active = None
                self.counts['retirements'] += 1
        self.check(bool(s['retirement']) == bool(s['cmd_valid'] and self.decode(s['cmd'])['opcode'] in (3,4)),
                   'retirement_binding', c, 'Retirement event mismatch')
        if s['request_admission']:
            self.check(self.active is None, 'single_request_capacity', c, 'Overlapping admitted requests')
            self.active = (s['txn_bank'], s['txn_addr'], s['txn_is_write'], s['txn_wdata'])
            packed = (s['txn_bank'] << 41) | (s['txn_addr'] << 33) | (s['txn_is_write'] << 32) | s['txn_wdata']
            self.check(s['incoming_request'] == packed, 'admission_payload', c, 'Admission payload mapping mismatch')
            self.counts['admissions'] += 1

    def finish(self, cycle):
        self.check(self.active is None and self.response is None, 'bounded_completion', cycle, 'Admitted work remains unfinished')


def scenarios(shared, seed):
    rng = random.Random(seed)
    reset = [(0, 2, 0, 0, 0), (1, 2, 0, 0, 0)]
    drain = [(1, 4 * max(shared['timing_profile']['values'].values()) + 32, 0, 0, 0)]
    def txn(bank, address, write=0, data=0):return (2, write, bank, address, data)
    zero = [txn(b, a) for b in range(4) for a in range(256)]
    memory = []
    for b in range(4):
        for a in (0x12, 0x13, 0x92, 0x93):
            memory.append(txn(b, a, 1, (0x12345678 * (b+1) + a) & 0xffffffff))
    memory += [txn(b, a) for b in range(4) for a in (0x12, 0x13, 0x92, 0x93)]
    pressure = [txn(rng.randrange(4), rng.randrange(256), rng.randrange(2), rng.getrandbits(32)) for _ in range(160)]
    pressure.append(txn(0, 0))
    conflict = [txn(b, a, w, 0xabcdef01) for b in range(4) for a,w in ((0x10,1),(0x20,1),(0x20,0))]
    interrupted = [txn(0, 0x51, 1, 0xfeedbeef), (1, 2, 0, 0, 0), (0, 2, 0, 0, 0),
                   txn(0,0x51), txn(1,0x72), (1,3,0,0,0), (0,2,0,0,0), txn(1,0x72),
                   txn(0,0x51), (3,0,0,0,0), (0,2,0,0,0), txn(0,0x51)]
    return {'reset_zero':reset+zero+drain, 'memory_row_paths':reset+memory+drain,
            'backpressure_capture':reset+pressure+drain, 'timing_row_conflict':reset+conflict+drain,
            'reset_inflight':reset+interrupted+drain}


def check_trace(path, shared, scenario):
    from simulate_handoff import read_trace
    oracle = SingleRequestOracle(shared)
    cycle = -1
    try:
        for row in read_trace(path):
            cycle = row['cycle']
            oracle.step(row)
        oracle.finish(cycle)
        required = {'reset_zero': ('READ','responses'), 'memory_row_paths': ('ACT','PRE','READ','WRITE'),
                    'backpressure_capture': ('READ','WRITE','host_stall_edges'),
                    'timing_row_conflict': ('PRE','tRAS:blocked','tRAS:boundary','tRCD:blocked','tRCD:boundary'),
                    'reset_inflight': ('reset_canceled','reset_response_canceled','READ','responses')}[scenario]
        for name in required:
            oracle.check(oracle.counts[name] > 0, 'scenario_coverage', cycle, 'Missing executed coverage: '+name)
        return {'status':'passed','cycles':cycle+1,'counters':dict(oracle.counts)}
    except CheckFailure as exc:
        return {'status':'failed','cycles':cycle+1,'counters':dict(oracle.counts),
                'failure':{'check':exc.check,'cycle':exc.cycle,'message':str(exc)}}
