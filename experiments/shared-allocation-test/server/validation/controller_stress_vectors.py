"""Agent 3 host stimulus: discovered item bindings, deterministic stress plan.

No expected-memory values are calculated here. The saved Agent 2 checker owns
all functional expectations and dispatch/completion accounting.
"""
import random
import re


def discover_item_contract(api, files, controller):
    discovered = api.vector_transaction_contract(list(files.items()))
    roles = {}
    for role, port in (('bank', 'txn_bank'), ('address', 'txn_addr'),
                       ('data', 'txn_wdata'), ('write', 'txn_is_write')):
        matches = [field for field, assignments in discovered['fields'].items()
                   if any(re.search(r'\.' + port + r'\s*(?:<=|=)', a) for a in assignments)]
        if len(matches) != 1:
            raise ValueError('Cannot uniquely discover saved driver binding for ' + port)
        roles[role] = matches[0]
    package = files['dut_pkg.sv']
    helper = re.search(r'\btask\s+control\s*\((.*?)\)\s*;(.*?)endtask', package, re.S)
    if not helper:
        raise ValueError('Saved sequence lacks the reviewed request/idle/reset/drain control protocol.')
    parameters = [re.findall(r'\w+', p)[-1] for p in helper.group(1).split(',')]
    if len(parameters) != 3:
        raise ValueError('Control protocol needs kind, cycles and scenario parameters.')
    for role, parameter in zip(('kind', 'cycles', 'scenario'), parameters):
        matches = re.findall(r'\b\w+\.(\w+)\s*=\s*' + re.escape(parameter) + r'\s*;', helper.group(2))
        if len(matches) != 1:
            raise ValueError('Cannot discover control item field for ' + role)
        roles[role] = matches[0]
    if len(set(roles.values())) != len(roles):
        raise ValueError('Saved transaction bindings overlap.')
    # Confirm the meanings consumed by the immutable driver, rather than
    # assigning an assumed encoding to an arbitrary generated transaction.
    driver = re.search(r'\bclass\s+dut_driver\b.*?endclass', package, re.S)
    if not driver: raise ValueError('Saved package lacks the reviewed driver.')
    driver=driver.group()
    for kind in (0, 1, 2, 3):
        if not re.search(r'\.' + re.escape(roles['kind']) + r'\s*==\s*' + str(kind) + r'\b', driver):
            raise ValueError('Unsupported saved driver control encoding: ' + str(kind))
    return dict(discovered, roles=roles, widths=controller['widths'],
                control_encoding={'request': 0, 'idle': 1, 'reset': 2, 'drain': 3})


def build_stress_plan(controller, item, inventory, seed=403404, random_count=2048, long_count=2048):
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError('Random seed must be an unsigned 32-bit integer.')
    if any(type(n) is not int or not 1 <= n <= 16384 for n in (random_count, long_count)):
        raise ValueError('Random/long counts must be between 1 and 16384; groups run in isolated bounded simulations.')
    cfg, constants, widths = controller['configuration'], controller['constants'], controller['widths']
    banks, depth = cfg['bank_count'], constants['req_queue_depth']
    if type(depth) is not int or depth < 1 or type(banks) is not int or banks < 1:
        raise ValueError('Unresolved queue capacity/bank count.')
    address_mask, data_mask = (1 << widths['address']) - 1, (1 << widths['data']) - 1
    columns = 1 << constants['col_width']
    rows = 1 << constants['row_width']
    rng = random.Random(seed)
    groups = []
    def step(op, bank=0, address=0, data=0, write=1, cycles=1):
        values = {'bank': bank, 'address': address & address_mask, 'data': data & data_mask,
                  'write': int(write), 'kind': item['control_encoding'].get(op, 1), 'cycles': cycles}
        return {'op': op, 'issue_relationship': 'next_ready_cycle' if op == 'request' else op,
                'fields': {item['roles'][k]: v for k, v in values.items()}}
    def requests(count, mode='cross', pattern=None):
        return [step('request', bank=(i % banks if mode == 'cross' else 0),
                     address=((i // banks) % rows)*columns + i % columns,
                     data=0x403404 + i * 257, write=(pattern[i % len(pattern)] if pattern else 1)) for i in range(count)]
    def add(identity, category, purpose, steps, coverage='traffic', applicable=True, timings=()):
        planned = sum(s['op'] == 'request' for s in steps)
        # The immutable checker requires accepted traffic and an independent
        # read comparison. Read every stimulus location after a drain so writes
        # cannot hide behind correct accounting alone. This is an address set,
        # not a memory model or handcrafted expected-data table.
        if applicable:
            if not planned:
                steps = [step('request'), step('drain')] + steps
            locations={(s['fields'][item['roles']['bank']],s['fields'][item['roles']['address']])
                       for s in steps if s['op']=='request'} | {(0,0)}
            steps = steps + [step('drain')] + [step('request',bank=b,address=a,write=0)
                                              for b,a in sorted(locations)] + [step('drain')]
            settling=sum(r['required_cycles']+4 for r in inventory if r['classification']=='APPLICABLE' and r['kind']=='delay')
            if settling: steps += [step('idle',cycles=settling+32)]
        groups.append({'vector_id': identity, 'category': category, 'purpose': purpose,
            'applicable_configuration': cfg, 'stimulus_description': purpose,
            'seed': seed if category == 'random' else None,
            'expected_observation': coverage, 'timing_requirements': list(timings),
            'mandatory_coverage': coverage not in ('pending_reset','timing'),
            'status': 'PENDING' if applicable else 'NOT_APPLICABLE', 'evidence_paths': [],
            'steps': ([step('reset', cycles=4), step('idle', cycles=3)] + steps + [step('drain')]) if applicable else [],
            'pressure_transactions': planned,
            'transactions': sum(s['op'] == 'request' for s in steps) if applicable else 0})
    for index, count in enumerate((max(0, depth-1), depth, depth+1)):
        add('QUEUE-00'+str(index+1), 'queue_boundary', 'Queue capacity pressure: {} attempts for depth {}'.format(count, depth),
            requests(count), 'queue_boundary' if count else 'reset')
    repeated = []
    for _ in range(4): repeated += requests(depth*3+1) + [step('drain')]
    add('QUEUE-004', 'repeated_fill_drain', 'Repeated fill/drain without per-request completion waits', repeated, 'queue_full')
    add('QUEUE-005', 'long_saturation', 'Maintain queue pressure and handshake backpressure', requests(depth*32+64), 'backpressure')
    add('QUEUE-006', 'full_at_completion', 'Full queue while the active service completes', requests(depth*16+64), 'full_completion')
    add('CONSECUTIVE-001', 'consecutive', 'Offer a fresh request on each ready cycle', requests(depth*8+32), 'consecutive')
    add('OUTSTANDING-001', 'outstanding', 'Queue requests faster than the serialized service slot', requests(depth*8+32), 'outstanding')
    add('SIMULTANEOUS-001', 'simultaneous', 'Acceptance while another service completes', requests(depth*16+64), 'simultaneous')
    add('BANK-001', 'same_bank', 'Hammer one bank with changing rows', requests(depth*16+64, 'same'), 'same_bank')
    add('BANK-002', 'cross_bank', 'Spread back-to-back writes across every configured bank', requests(banks*32), 'cross_bank', banks>1)
    isolate = [step('request', bank=b, address=a, data=(b+1)*0x10001+a) for b in range(banks) for a in range(min(columns, 16))]
    isolate += [step('drain')]
    isolate += [step('request', bank=b, address=a, write=0) for b in range(banks) for a in range(min(columns, 16))]
    add('BANK-003', 'bank_isolation', 'Unique bank/location writes, barrier, then trusted reference readback', isolate, 'readback')
    add('ROW-001', 'row_hit', 'Repeated open-row locality', [step('request', address=i % columns) for i in range(64)], 'row_hit', cfg['page_policy']=='open_page')
    add('ROW-002', 'row_replacement', 'Alternate rows in the same bank', [step('request', address=(i % rows)*columns) for i in range(96)], 'row_miss', rows>1)
    add('ROW-003', 'closed_page', 'Close-after-access pressure for configured closed-page policy', requests(96), 'row_closed', cfg['page_policy']=='closed_page')
    add('WRITE-001', 'back_to_back_writes', 'Same/different bank, row and address write pressure', requests(128, pattern=[1]), 'traffic')
    add('READ-001', 'back_to_back_reads', 'Read-response association follows actual service order', requests(128, pattern=[0]), 'readback')
    mix = []
    for pattern in ([1,1,0,0], [1,0,1,0], [0,1,0,1], [1,1,1,0], [0,0,1,1]):
        mix += requests(64, pattern=pattern)
    add('MIXED-001', 'mixed', 'WWRR / WRWR / RWRW / WWWR / RRWW with legal reordering', mix, 'mixed')
    add('ADDRESS-001', 'same_address', 'Repeated writes/reads to one address; no acceptance FIFO assumption',
        [step('request', address=0, data=i, write=i%3!=0) for i in range(128)], 'mixed')
    # No wait for completion between setup requests. The exact driver cancels
    # accepted queued/active work at reset and the original checker accounts it.
    setup = requests(depth+1, pattern=[1,0])
    reset = setup + [step('reset', cycles=4), step('idle', cycles=3)] + requests(32, pattern=[1,0])
    add('RESET-001', 'reset_outstanding', 'Reset queued/active traffic, then restart fresh traffic', reset, 'cancelled')
    pending = [step('request', write=0), step('wait_read_completion', cycles=4096),
               step('reset', cycles=4), step('idle', cycles=3), step('request', write=0)]
    add('RESET-002', 'reset_pending_response', 'Reset between read completion and response sample when reachable', pending, 'pending_reset')
    active = [step('request'), step('wait_active_dispatch', cycles=4096),
              step('reset', cycles=4), step('idle', cycles=3), step('request', write=0)]
    add('RESET-003', 'reset_active_service', 'Reset immediately after observed dispatch, before service completes', active, 'active_reset')
    refresh = []
    refi = controller['timing_cycles'].get('tREFI', 0)
    if cfg['refresh_enabled'] and refi:
        refresh += [step('idle', cycles=max(1, refi-(depth*8+64)))]
        for _ in range(3):
            refresh += requests(depth*8+96, pattern=[1,0,1]) + [step('drain'), step('idle', cycles=refi)]
    add('REFRESH-001', 'refresh', 'Queued/sustained traffic over multiple refresh opportunities', refresh,
        'refresh', cfg['refresh_enabled'] and bool(refi), ['refresh_blocking'])
    for rule in inventory:
        if rule['classification'] != 'APPLICABLE': continue
        identity = rule['rule'].upper().replace('_', '-') + '-001'
        if rule['rule']=='refresh_blocking': continue
        timings = [rule['rule']]
        pressure = requests(max(128, banks*32), pattern=[1,0,1])
        # Boundary intent and early host offers. Actual command intervals,
        # including exact-boundary hits, are measured independently.
        pressure += [step('drain'), step('idle', cycles=max(1, rule['required_cycles']-1))] + requests(32)
        if rule['kind'] == 'delay':
            settling = sum(r['required_cycles'] for r in inventory if r['classification']=='APPLICABLE' and r['kind']=='delay') + 32
            pressure += [step('drain'), step('idle', cycles=settling)]
        add(identity, 'timing', 'Saturate row-change/activation pressure for '+rule['rule'], pressure, 'timing', True, timings)
    add('LONG-001', 'long', 'Substantial bounded sustained mixed traffic', requests(long_count, pattern=[1,0,1,1,0]), 'mixed')
    random_steps = [step('request', bank=rng.randrange(banks), address=rng.randrange(address_mask+1),
                         data=rng.getrandbits(widths['data']), write=rng.randrange(2)) for _ in range(random_count)]
    add('RANDOM-001', 'random', 'Seeded constrained-random bank/address/operation/data stress', random_steps, 'mixed')
    return {'schema_version': 1, 'mode': 'system', 'top_module': controller['top_module'],
            'configuration': cfg, 'queue_depth': depth, 'bank_count': banks,
            'page_policy': cfg['page_policy'], 'seed': seed, 'random_count': random_count,
            'long_count': long_count, 'item_contract': item, 'groups': groups,
            'transactions': sum(g['transactions'] for g in groups)}


def vector_text(group, item):
    roles = item['roles']
    ops = {'request':0, 'idle':1, 'reset':2, 'drain':3, 'wait_read_completion':4, 'wait_active_dispatch':5}
    return ''.join('{} {} {} {:x} {:x} {:x} {}\n'.format(ops[s['op']],
        s['fields'][roles['cycles']], s['fields'][roles['write']], s['fields'][roles['bank']],
        s['fields'][roles['address']], s['fields'][roles['data']], group['vector_id']) for s in group['steps'])
