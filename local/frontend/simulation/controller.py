"""Independent FIFO/compact-address controller oracle, never derived from RTL.

This is an explicit executable profile, not an interpreter of contract prose.
Passing its directed scenarios does not discharge whole source requirements.
"""
from collections import Counter, deque


class CheckFailure(ValueError):
    def __init__(self, check, cycle, message):
        self.check, self.cycle = check, cycle
        super().__init__(f'{check} at cycle {cycle}: {message}')


class ControllerOracle:
    def __init__(self, shared):
        self.p = shared['parameters']
        self.t = shared['timing_profile']['values']
        self.types = shared['types']
        self.codes = self.types['opcode']['encodings']
        self.counts = Counter()
        self.reset(-1)

    def reset(self, cycle):
        self.waiting = deque()
        self.active = None
        self.cleanup = None
        self.memory = [[0] * 16 for _ in range(self.p['BANK_COUNT'])]
        self.rows = [None] * self.p['BANK_COUNT']
        self.acts = [None] * self.p['BANK_COUNT']
        self.pres = [None] * self.p['BANK_COUNT']
        self.act_history = []
        self.last_ref = None
        self.last_reset = cycle
        self.pending = False
        self.response = None
        self.previous = None

    def check(self, condition, name, cycle, message):
        if not condition:
            raise CheckFailure(name, cycle, message)
        self.counts['checks'] += 1
        self.counts['check:' + name] += 1

    def unpack(self, packed, name):
        return {f['name']: (packed >> f['lsb']) & ((1 << f['width']) - 1)
                for f in self.types[name]['fields']}

    @staticmethod
    def host(row):
        return {'bank': row['bank'], 'row': row['addr'] >> 2,
                'column': row['addr'] & 3, 'is_write': row['write'], 'wdata': row['wdata']}

    def step(self, s):
        c = s['cycle']
        ck = lambda ok, name, msg: self.check(ok, name, c, msg)
        if not s['rst_n']:
            ck(not any(s[k] for k in ('ready', 'cmd_ready', 'rsp_valid', 'admission',
                                     'dispatch', 'commit', 'candidate_valid', 'pending', 'busy')),
               'reset_gating', 'valid/ready/event asserted during reset')
            ck(s['candidate_command'] == 0 and s['commit_command'] == 0,
               'reset_payload', 'command payload is not zero NOP during reset')
            self.counts['reset_canceled'] += len(self.waiting) + (self.active is not None)
            self.reset(c)
            self.counts['reset_edges'] += 1
            return
        ck(c > self.last_reset and self.last_reset >= 0, 'reset_epoch', 'missing sampled reset')
        ck(s['admission'] == (s['valid'] and s['ready']), 'admission', 'binding disagrees with host handshake')
        ck(s['pending'] == self.pending, 'refresh_deadline', 'pending disagrees with independent periodic deadlines')
        if self.pending and self.active is not None:
            self.counts['pending_active_edges'] += 1
        busy = self.last_ref is not None and c < self.last_ref + self.t['tRFC']
        ck(bool(s['busy']) == busy, 'refresh_busy', 'busy does not match REF-edge+tRFC')
        ck(s['open_valid'] == sum(1 << i for i, v in enumerate(self.rows) if v is not None),
           'row_state', 'row validity changed without committed ACT/PRE/REF')
        row_width = self.types['row']['width']
        for bank, row in enumerate(self.rows):
            if row is not None:
                ck((s['open_rows'] >> (bank * row_width)) & ((1 << row_width) - 1) == row,
                   'row_state', f'bank {bank} row differs from commit history')
        expected = self.response
        ck(bool(s['rsp_valid']) == (expected is not None and expected[0] == c),
           'response_latency', f'expected response {expected}, got valid={s["rsp_valid"]}')
        if expected is not None:
            ck(s['rsp_data'] == expected[1], 'read_data', f'expected {expected[1]:08x}, got {s["rsp_data"]:08x}')
            self.counts['responses'] += 1
            self.response = None
        if self.previous:
            prev = self.previous
            if prev['candidate_valid'] and not prev['commit']:
                ck(s['candidate_valid'] and s['candidate_command'] == prev['candidate_command'],
                   'candidate_hold', 'blocked candidate changed or disappeared')
                self.counts['blocked_candidate_edges'] += 1
            if prev['commit'] or prev['dispatch']:
                ck(not s['candidate_valid'], 'candidate_bubble', 'candidate formed on dispatch/commit edge')
        ck(s['commit'] == bool(s['candidate_valid'] and s['candidate_ok']),
           'commit_permission', 'commit differs from valid && permission')
        ck(s['commit_command'] == s['candidate_command'], 'candidate_payload', 'commit payload does not mirror candidate')
        if not s['candidate_valid']:
            ck(s['candidate_command'] == 0, 'candidate_payload', 'invalid candidate is not zero NOP')
        idle = self.active is None and self.cleanup is None and not self.pending and not busy
        ck(bool(s['probe_valid']) == idle, 'diagnostic_ready', 'probe valid violates idle/cleanup/refresh gating')
        ck(s['cmd_ready'] == bool(idle and s['probe_ok']), 'diagnostic_ready', 'cmd_ready differs from independent idle gate and probe permission')
        occupied = len(self.waiting)
        ck(s['ready'] == (occupied < self.p['WAITING_CAPACITY'] or bool(s['dispatch'])),
           'queue_capacity', 'ready disagrees with independent waiting occupancy')
        if s['dispatch']:
            ck(self.active is None and self.cleanup is None and not self.pending and not busy,
               'dispatch_ownership', 'dispatch while active/cleanup/refresh owns service')
            ck(bool(self.waiting), 'fifo_order', 'empty queue bypass')
            request = self.waiting.popleft()
            ck(self.unpack(s['dispatch_request'], 'request') == request,
               'fifo_order', 'dispatched payload differs from pre-edge FIFO head')
            self.active = request
            self.counts['dispatches'] += 1
        if s['commit']:
            ck(not s['dispatch'], 'dispatch_ownership', 'dispatch and commit on same edge')
            command = self.unpack(s['commit_command'], 'command')
            self.command(command, c)
        if s['admission']:
            request = self.host(s)
            ck(0 <= request['bank'] < self.p['BANK_COUNT'], 'host_mapping', 'invalid bank stimulus')
            ck(self.unpack(s['incoming_request'], 'request') == request,
               'host_mapping', 'packed request differs from host payload')
            self.waiting.append(request)
            self.counts['admissions'] += 1
            if self.last_ref is not None:
                self.counts['admissions_after_refresh'] += 1
            if occupied == self.p['WAITING_CAPACITY'] and s['dispatch']:
                self.counts['full_simultaneous'] += 1
        if s['valid'] and not s['ready']:
            self.counts['host_stall_edges'] += 1
        ck(len(self.waiting) <= self.p['WAITING_CAPACITY'], 'queue_capacity', 'waiting queue overflow')
        due = (c - self.last_reset) % self.t['tREFI'] == 0
        ref = s['commit'] and self.unpack(s['commit_command'], 'command')['opcode'] == self.codes['REF']
        self.pending = due or (self.pending and not ref)
        self.previous = s

    def command(self, cmd, c):
        ck = lambda ok, name, msg: self.check(ok, name, c, msg)
        op = next((name for name, value in self.codes.items() if value == cmd['opcode']), None)
        ck(op in ('ACT', 'PRE', 'READ', 'WRITE', 'REF'), 'command_legality', 'NOP/reserved opcode committed')
        ck(self.last_ref is None or c - self.last_ref >= self.t['tRFC'], 'tRFC', 'command before REF+tRFC')
        b = cmd['bank']
        ck(b < self.p['BANK_COUNT'], 'command_legality', 'command bank out of range')
        self.counts[op] += 1
        def gap(last, minimum, name):
            ck(last is None or c - last >= minimum, name, f'edge {c}, trigger {last}, minimum {minimum}')
        if op == 'ACT':
            ck(self.active is not None and b == self.active['bank'] and cmd['row'] == self.active['row'],
               'command_sequence', 'ACT does not set up the active request')
            ck(self.rows[b] is None, 'command_sequence', 'ACT on open bank')
            gap(self.pres[b], self.t['tRP'], 'tRP')
            gap(self.act_history[-1] if self.act_history else None, self.t['tRRD'], 'tRRD')
            ck(sum(c - edge < self.t['tFAW'] for edge in self.act_history) < self.p['TFAW_ACT_LIMIT'],
               'tFAW', 'too many ACT commits within window')
            self.act_history.append(c)
            self.acts[b] = c
            self.rows[b] = cmd['row']
        elif op == 'PRE':
            ck(self.rows[b] is not None, 'command_sequence', 'PRE on closed bank')
            gap(self.acts[b], self.t['tRAS'], 'tRAS')
            if self.active is not None:
                ck(b == self.active['bank'] and self.rows[b] != self.active['row'],
                   'command_sequence', 'PRE does not resolve active row conflict')
            elif self.cleanup is not None:
                ck(b == self.cleanup, 'page_policy', 'PRE does not clear cleanup reservation')
                self.cleanup = None
            else:
                ck(self.pending and b == next(i for i, row in enumerate(self.rows) if row is not None),
                   'refresh_order', 'maintenance PRE without pending refresh or out of bank order')
            self.rows[b] = None
            self.pres[b] = c
        elif op in ('READ', 'WRITE'):
            ck(self.active is not None, 'command_sequence', 'access without active request')
            a = self.active
            ck((b, cmd['row'], cmd['column'], op == 'WRITE') ==
               (a['bank'], a['row'], a['column'], bool(a['is_write'])),
               'retirement_order', 'access does not match oldest active request')
            ck(self.rows[b] == a['row'], 'command_sequence', 'access to closed/wrong row')
            gap(self.acts[b], self.t['tRCD'], 'tRCD')
            address = (a['row'] << 2) | a['column']
            if op == 'WRITE':
                ck(cmd['wdata'] == a['wdata'], 'write_data', 'WRITE payload corrupted')
                self.memory[b][address] = a['wdata']
            else:
                self.response = (c + 1, self.memory[b][address])
            if self.p['PAGE_POLICY'] == 'close_page':
                self.cleanup = b
            self.active = None
            self.counts['retirements'] += 1
        else:
            ck(self.pending and self.active is None and self.cleanup is None and all(row is None for row in self.rows),
               'refresh_drain', 'REF before pending work/rows drained')
            for edge in self.pres:
                gap(edge, self.t['tRP'], 'refresh_tRP')
            self.last_ref = c

    def finish(self, cycle):
        self.check(not self.waiting and self.active is None and self.cleanup is None and self.response is None,
                   'bounded_completion', cycle, 'accepted transactions did not finish within scenario bound')


def scenarios(shared, seed=1):
    """Vector ops: reset cycles, idle cycles, transaction write/bank/address/data."""
    import random
    banks = shared['parameters']['BANK_COUNT']
    t = shared['timing_profile']['values']
    drain = 32 * (t['tRAS'] + t['tRP'] + t['tRCD'] + 8) + t['tRFC']
    reset = [(0, 3, 0, 0, 0)]
    def txn(w, b, a, d=0):
        return (2, w, b, a, d)
    zero = reset + [txn(0, b, a) for b in range(banks) for a in range(16)] + [(1, drain, 0, 0, 0)]
    directed = reset + [v for b in range(banks) for a in (0, 1, 4, 0, 15)
                        for v in (txn(1, b, a, 0x12340000 + b * 256 + a), txn(0, b, a))]
    directed += [(1, drain, 0, 0, 0)]
    rng = random.Random(seed)
    pressure = reset + [txn(rng.randrange(2), rng.randrange(banks), rng.randrange(16), rng.getrandbits(32))
                        for _ in range(160)] + [(1, drain, 0, 0, 0)]
    refresh = reset + [txn(1, b, b, 0xabcdef00 + b) for b in range(banks)]
    refresh += [(1, t['tREFI'] - 40, 0, 0, 0)]
    refresh += [txn(i % 2, i % banks, (i * 5) % 16, i + 1) for i in range(80)]
    refresh += [(1, 2 * t['tREFI'] + 2 * t['tRFC'], 0, 0, 0)]
    refresh += [txn(0, b, b) for b in range(banks)] + [(1, drain, 0, 0, 0)]
    interrupted = reset + [txn(1, i % banks, (i * 4) % 16, i + 100) for i in range(6)]
    interrupted += reset + [txn(0, b, a) for b in range(banks) for a in (0, 4, 8, 12)]
    interrupted += [(1, drain, 0, 0, 0)]
    return {'reset_zero': zero, 'fifo_memory': directed, 'queue_pressure': pressure,
            'refresh_traffic': refresh, 'reset_inflight': interrupted}
