"""Existing controller behavior that architecture exploration must preserve.

Contracts constrain observable behavior, not the choice of module decomposition.
The versioned snapshot comes from generate_wrapper.py and its host handoff.
"""
from __future__ import annotations

import copy

BASELINE_ID = 'current_controller_v2'


def controller_baseline(config):
    """Apply the baseline to legacy controller configs; exploration can opt out."""
    options = config.get('microarchitecture', {})
    if not isinstance(options, dict) or set(options) - {'baseline', 'waiting_capacity'}:
        raise ValueError('microarchitecture supports baseline: current_controller|none and waiting_capacity')
    selection = options.get('baseline')
    if selection not in (None, 'none', 'current_controller'):
        raise ValueError('microarchitecture.baseline must be current_controller or none')
    legacy_config = (isinstance(config.get('profile'), dict)
                     and config['profile'].get('type') == 'basic'
                     and 'features' in config and 'memory' in config)
    if selection == 'none' or (selection is None and not legacy_config):
        if 'waiting_capacity' in options:
            raise ValueError('microarchitecture.waiting_capacity requires the current_controller preset')
        return None
    capacity = options.get('waiting_capacity', 4)
    if type(capacity) is not int or capacity < 1:
        raise ValueError('microarchitecture.waiting_capacity must be a positive integer')
    memory = config.get('memory', {})
    topology = config.get('topology', {})
    features = config.get('features', {})
    if not all(isinstance(v, dict) for v in (memory, topology, features)):
        raise ValueError('Controller baseline requires memory/topology/features mappings')
    banks = topology.get('banks', 1)
    speed = str(memory.get('speed'))
    if memory.get('protocol') != 'DDR4' or speed not in {'2400', '3200'} or type(banks) is not int or banks not in {1, 2, 4}:
        raise ValueError('Current controller baseline supports DDR4-2400/3200 with 1/2/4 banks; use baseline: none for exploration')
    scheduler = features.get('scheduler')
    if scheduler is not None and (not isinstance(scheduler, str) or not scheduler.strip()):
        raise ValueError('features.scheduler must be a nonempty policy name')
    if features.get('page_policy', 'open_page') not in {'open_page', 'close_page'}:
        raise ValueError('Invalid page policy for controller baseline')
    for name in ('refresh', 'tRRD', 'tFAW'):
        if name in features and type(features[name]) is not bool:
            raise ValueError(f'Controller baseline requires boolean features.{name}')
    def port(name, direction, width=1):
        return {'name': name, 'direction': direction, 'width': width}
    ports = [port('clk', 'input'), port('rst_n', 'input'), port('txn_valid', 'input'),
             port('txn_is_write', 'input'), port('txn_addr', 'input', 4), port('txn_wdata', 'input', 32)]
    if banks > 1:
        ports.append(port('txn_bank', 'input', (banks - 1).bit_length()))
    ports.extend([port('txn_ready', 'output'), port('cmd_ready', 'output'),
                  port('rsp_valid', 'output'), port('rsp_rdata', 'output', 32)])
    requirements = [
        ('BASE_HOST', 'Preserve the existing host ports and widths listed in the baseline contract. Accept a request only on a rising clk edge with txn_valid && txn_ready. Hold payload while stalled. Consecutive accepted cycles with txn_valid held high are distinct requests. cmd_ready is execution/timing visibility, not the host acceptance handshake. Deassert txn_ready and rsp_valid during reset.'),
        ('BASE_ADDRESS', 'Preserve the simplified 4-bit txn_addr mapping: row=txn_addr[3:2], column=txn_addr[1:0], zero-extended into the internal 10-bit row and 6-bit column fields. Preserve explicit txn_bank for multi-bank configurations; a single-bank configuration uses bank 0. Internal packed requests retain bank(2), row(10), column(6), write flag(1), write data(32).'),
        ('BASE_QUEUE', f'Provide {capacity} WAITING request slots PLUS one separately stored active-service request: waiting plus active capacity is {capacity + 1}. The active request must not consume a waiting slot. Dispatch captures the complete pre-edge request in active registers and frees its waiting slot without retiring the transaction. A full queue may admit on the same edge as dispatch; retirement alone does not free waiting storage. Accepted request fields survive stalls and each accepted request is serviced exactly once within a reset-free epoch. Keep admission, dispatch, retirement and read response delivery distinct. Captured response storage consumes neither waiting nor active capacity.'),
        ('BASE_DATA', 'Provide functional 32-bit memory storage with 16 addresses per bank, bank isolation and reset-to-zero contents. Accepted writes update only their addressed location at service completion or a committed WRITE. Each accepted read produces exactly one one-cycle rsp_valid pulse with the stored rsp_rdata after read service/READ commit, using the existing one-cycle response stage. Writes produce no host response. Selection and stalled candidates cannot mutate memory; mere command output without storage and read responses is insufficient.'),
        ('BASE_TIMING', 'Retain existing tRCD, tRAS and tRP timing protection from the selected JEDEC profile. ACT-to-READ/WRITE must wait tRCD, ACT-to-PRE must respect tRAS, and PRE-to-next-ACT must respect tRP if explicit commands are used. Aggregate service may retain the existing protected timing envelope instead. Configured tRRD and tFAW gates must use accepted activation events, not host admissions or stalled proposals. Never treat existing basic timing as an unrequested future feature.'),
        ('BASE_SEQUENCE', 'Dispatch captures the complete selected request in separate active-service registers before freeing its waiting queue slot. Hold that active request and payload across setup PRE/ACT and timing stalls until its final READ/WRITE commits. Setup commands never retire the active request. Aggregate service also keeps active-service ownership through completion. Waiting-queue dequeue at dispatch is distinct from active-request retirement. Row state changes only on the responsible committed operation; uncommitted proposals cannot change state.'),
        ('BASE_MODULARITY', 'Keep request storage, request selection, command sequencing, timing enforcement and functional memory in independently verifiable modules with explicit interfaces. No single controller core may own all these functions. Each mandatory role has distinct module owners with functional inter-module connections. Module names and further decomposition remain design choices.'),
        ('BASE_LEGALITY', 'Compute command permission from registered timing/bank history and an independent candidate. An accepted ACT pulse is derived from candidate && permission; permission must not combinationally depend on that same accepted pulse. Scheduler selection/validity must be available independently of timing_ok; timing_ok may gate commit but must not create a selection-validity feedback loop. Declare accurate per-output combinational input dependencies.'),
        ('BASE_RESET', 'Use top-level clk and active-low rst_n. Reset clears pending requests, active service, row validity, memory and responses. Preserve reusable blocks with either synchronous or asynchronous active-low resets driven by the same rst_n; differing assertion styles do not require rebuilding otherwise reusable blocks. Do not change polarity without an explicit adapter.'),
    ]
    policy = features.get('page_policy', 'open_page')
    selection_rule = (f'Honor the requested scheduler policy {scheduler}.' if scheduler else
                      'Choose and document a scheduling policy from the design request; this preset does not select one.')
    if scheduler == 'simple':
        selection_rule += ' Simple scheduling prefers a row hit then the lowest-index valid request; do not claim oldest-first fairness or same-address ordering is already implemented.'
    requirements.append(('BASE_POLICY', f'{selection_rule} Preserve page policy {policy}. Open-page retains a serviced row; close-page closes it before subsequent bank use. Preserve request ownership through any setup sequence. Internal module decomposition and command latency may change while these observable contracts hold.'))
    if features.get('refresh') is True:
        requirements.append(('BASE_REFRESH', 'Retain periodic refresh requests using profile tREFI, refresh priority and request/acknowledgment handling without dropping queued traffic. Refresh must make progress and normal requests must resume. Existing refresh is simplified; full REF/tRFC servicing remains later feature work. If explicit REF is proposed, describe drain/row-state interaction and acknowledge once at commit.'))
    elif features.get('refresh') is False:
        requirements.append(('BASE_REFRESH', 'Refresh is disabled in this configuration; do not add autonomous refresh traffic.'))
    for name in ('tRRD', 'tFAW'):
        if name in features:
            requirements.append(('BASE_' + name.upper(), f'{name} is {"enabled" if features[name] else "disabled"}; honor that setting and resolve enabled timing values from DDR4-{speed}.'))
    return {'id': BASELINE_ID, 'design_name': 'ddr4_controller_top', 'profile': f'DDR4-{speed}',
            'config': copy.deepcopy({k: v for k, v in config.items() if k != 'requirements'}), 'host_ports': ports,
            'requirements': [{'id': rid, 'text': text} for rid, text in requirements]}


def validate_baseline(manifest, baseline):
    """Machine-check observable interface and requirement coverage; not semantics."""
    expected = controller_baseline(baseline.get('config', {})) if isinstance(baseline, dict) else None
    if expected is None or baseline != expected:
        raise ValueError('Unrecognized or edited controller baseline contract')
    if manifest.get('design_name') != baseline['design_name'] or manifest.get('profile') != baseline['profile']:
        raise ValueError('Controller baseline requires its existing top name and timing profile')
    ports = {p['name']: (p['direction'], p.get('width', 1)) for p in manifest['top_ports']}
    for port in baseline['host_ports']:
        if ports.get(port['name']) != (port['direction'], port['width']):
            raise ValueError(f'Controller baseline host port missing or changed: {port["name"]}')
    allowed_inputs = {p['name'] for p in baseline['host_ports'] if p['direction'] == 'input'}
    if any(p['direction'] == 'input' and p['name'] not in allowed_inputs for p in manifest['top_ports']):
        raise ValueError('Controller baseline cannot require additional host inputs')
    supplied = {r['id']: r['text'] for r in manifest['requirements']}
    for requirement in baseline['requirements']:
        if supplied.get(requirement['id']) != requirement['text']:
            raise ValueError(f'Controller baseline requirement missing or rewritten: {requirement["id"]}')
