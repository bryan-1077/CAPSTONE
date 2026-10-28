"""Controller contract extraction and independent replay of monitor evidence.

No DUT output supplies expected memory contents. Requests come from observed
host handshakes; observed dispatch establishes service order, not acceptance
order. A changed/unsupported generated interface is an explicit contract gap.
"""
import json
import re
from pathlib import Path

from controller_manifest import discover_system, read_mapping, resolve_path
from system_rtl_validation import mask_source, parameter_environment


HOST_PORTS = {'txn_valid': 'input', 'txn_ready': 'output', 'txn_is_write': 'input',
              'txn_addr': 'input', 'txn_wdata': 'input', 'txn_bank': 'input',
              'rsp_valid': 'output', 'rsp_rdata': 'output'}
OBSERVATIONS = {
    'dispatch': 'dispatch_fire', 'completion': 'completion_fire',
    'slot': 'selected_index', 'selected_bank': 'selected_bank',
    'selected_addr': 'selected_addr', 'selected_write': 'selected_req.is_write',
    'selected_data': 'selected_req.wdata', 'service_pending': 'service_pending_q',
    'service_bank': 'service_bank_q', 'service_addr': 'service_addr_q',
    'service_write': 'service_is_write_q', 'service_data': 'service_wdata_q',
    'row_closed': 'is_row_closed', 'row_hit': 'is_row_hit', 'row_miss': 'is_row_miss',
    'row_open': 'row_open_valid', 'open_rows': 'open_row', 'queue_empty': 'queue_empty',
    'queue_full': 'queue_full', 'bank_commands': 'bank_cmd_valid', 'act': 'act_pulse', 'refresh': 'ref_req'}


def extract_controller_contract(manifest_path, agent1):
    manifest_path = Path(manifest_path).resolve()
    manifest = discover_system(manifest_path)['manifest']
    rtl_files = [str(resolve_path(manifest_path, manifest, p)) for p in manifest['rtl_files']]
    if (agent1.get('mode') != 'system' or not agent1.get('is_valid')
            or not agent1.get('compile_passed') or not agent1.get('hierarchy_passed')
            or not agent1.get('integration_passed') or agent1.get('top_module') != manifest['top_module']):
        raise ValueError('Agent 2 requires a passing Agent 1 full-system structural/elaboration result.')
    if set(agent1.get('rtl_files', [])) != set(rtl_files) or agent1.get('configuration') != manifest['configuration']:
        raise ValueError('Agent 1 RTL inventory/configuration differs from the selected frontend handoff.')
    definition = next(m for m in agent1['module_definitions'] if m['name'] == manifest['top_module'])
    top_file = str(resolve_path(manifest_path, manifest, next(
        m['file'] for m in manifest['module_definitions'] if m['name'] == manifest['top_module'])))
    source = mask_source(Path(top_file).read_text())
    ports = {p['name']: p for p in agent1['dut_spec']['ports']}
    environment = parameter_environment(definition)
    gaps = []
    actual_host_ports = dict(HOST_PORTS)
    if manifest['configuration']['bank_count'] == 1 and 'txn_bank' not in ports:
        actual_host_ports.pop('txn_bank')
    for name, direction in dict(actual_host_ports, **{manifest['clock']['name']: 'input', manifest['reset']['name']: 'input'}).items():
        if name not in ports or ports[name].get('direction') != direction or not ports[name].get('width'):
            gaps.append('Unsupported/unresolved host port ' + name)
    available = {role: signal for role, signal in OBSERVATIONS.items()
                 if re.search(r'\b' + re.escape(signal.split('.')[0]) + r'\b', source)}
    for role in set(OBSERVATIONS) - {'refresh'}:
        if role not in available:
            gaps.append('Missing simulation observation: ' + role)
    if manifest['configuration']['refresh_enabled'] and 'refresh' not in available:
        gaps.append('Enabled refresh lacks an observable request.')
    constants = {}
    for key in ('ROW_WIDTH', 'COL_WIDTH', 'REQ_QUEUE_DEPTH', 'HIT_SERVICE_CYCLES', 'SLOW_SERVICE_CYCLES'):
        value = environment.get(key)
        if not isinstance(value, int) or value < 1:
            gaps.append('Unresolved controller constant ' + key)
        constants[key.lower()] = value
    # These guards establish the adapter's event/commit semantics. They do not
    # prove DUT correctness: the executable checker still verifies actual data.
    rules = {
        'acceptance': r'assign\s+enqueue_fire\s*=\s*txn_valid\s*&&\s*txn_ready\s*;',
        'dispatch': r'assign\s+dispatch_fire\s*=\s*accept_txn_fire\s*;',
        'completion': r'assign\s+completion_fire\s*=\s*service_done\s*;',
        'single_service': r'if\s*\(\s*!service_pending_q\s*&&\s*accept_txn_fire\s*\)',
        'write_commit': r'bank_mem\s*\[service_bank_q\]\s*\[service_addr_q\]\s*<=\s*service_wdata_q\s*;',
        'read_response': r'rsp_rdata_q\s*<=\s*bank_mem\s*\[service_bank_q\]\s*\[service_addr_q\]\s*;',
        'memory_reset': r'bank_mem\s*\[bank_mem_bank\]\s*\[bank_mem_addr\]\s*<=\s*\x27[0]\s*;'}
    rules_found = {name: bool(re.search(pattern, source)) for name, pattern in rules.items()}
    gaps.extend('Unsupported RTL transaction semantics: ' + key for key, found in rules_found.items() if not found)
    widths = {'address': ports.get('txn_addr', {}).get('width'),
              'data': ports.get('txn_wdata', {}).get('width'),
              'bank': ports.get('txn_bank', {}).get('width', max(1, (manifest['configuration']['bank_count']-1).bit_length()))}
    if manifest['clock']['edge'] != 'posedge':
        gaps.append('This reviewed monitor adapter requires a rising-edge controller clock.')
    if all(isinstance(x, int) for x in widths.values()):
        if manifest['configuration']['bank_count'] > (1 << widths['bank']):
            gaps.append('Configured bank count exceeds host bank field capacity.')
        if constants['row_width'] and constants['col_width'] and constants['row_width'] + constants['col_width'] != widths['address']:
            gaps.append('Unsupported host row/column address mapping.')
    timings = manifest['timing']['profile']['timing_cycles']
    limit = next((values['TFAW_LIMIT'] for values in manifest['timing']['module_parameters'].values()
                  if 'TFAW_LIMIT' in values), None)
    if manifest['configuration']['tfaw_enabled'] and not isinstance(limit, int):
        gaps.append('Enabled tFAW lacks its configured activation limit.')
    sources = {'manifest': str(manifest_path), 'controller_rtl': top_file,
               'frontend_testbench': str(resolve_path(manifest_path, manifest, manifest['system_testbench'])),
               'module_specs': [str(resolve_path(manifest_path, manifest, s['path'])) for s in manifest['module_specs']],
               'ir_files': [str(resolve_path(manifest_path, manifest, p)) for p in manifest['ir_files']],
               'configuration_sources': [str(resolve_path(manifest_path, manifest, p)) for p in manifest['configuration_sources']]}
    # Preserve metadata provenance rather than inventing a controller YAML.
    component_metadata = []
    for path in sources['module_specs'] + sources['ir_files']:
        data = read_mapping(path)
        behavior = data.get('behavior', data.get('metadata', {}).get('behavior', {}))
        component_metadata.append({'design_name': data.get('design_name'), 'path': path,
                                   'behavior_rules': behavior.get('rules', []),
                                   'correctness_criteria': data.get('correctness_criteria', [])})
    return {'schema_version': 1, 'mode': 'system', 'top_module': manifest['top_module'],
            'configuration': manifest['configuration'], 'host_ports': actual_host_ports,
            'clock': manifest['clock'], 'reset': manifest['reset'], 'widths': widths,
            'constants': constants, 'timing_cycles': timings, 'tfaw_limit': limit or 0,
            'request_acceptance': 'txn_valid && txn_ready at the consuming clock edge',
            'dispatch': 'dispatch_fire at the consuming edge removes selected_index from the request queue',
            'write_completion': 'completion_fire for the active write commits stimulus data to bank/address memory',
            'write_completion_observability': 'simulation-only service/completion monitor; no external write acknowledgement',
            'read_response': {'valid': 'rsp_valid', 'data': 'rsp_rdata',
                              'completion': 'registered response after active read service completion',
                              'association': 'observed dispatch order through a single service slot; no acceptance-order assumption'},
            'ordering': {'global': 'may reorder; no FIFO guarantee',
                         'reads': 'may reorder before dispatch; responses follow serialized service order',
                         'writes': 'may reorder before dispatch; commits follow serialized service order',
                         'same_bank': 'no FIFO guarantee; lowest reusable slot and scheduler policy determine dispatch',
                         'same_address': 'no FIFO guarantee; dependencies require drain barriers in ordering-sensitive stimulus',
                         'service': 'one transaction in service; no simultaneous service dispatch and completion'},
            'max_simultaneous_read_responses': 1,
            'reset_outstanding_policy': 'cancel queued/in-service requests, discard pending responses, clear reference memory and row state',
            'sampling': 'drive on falling edge; monitor consuming rising edge with clocking input #1step; registered responses sampled next rising edge',
            'timing_observation': 'act_pulse is the frontend wrapper logical ACT request event, not a DDR4 PHY command pin',
            'observability_limitations': [
                'There are no external transaction IDs or write acknowledgements; identity/accounting uses independently reconstructed queue slots plus request tuples and serialized service.',
                'Identical request tuples are externally indistinguishable; multiplicity and slot/tuple preservation are checked under the documented queue contract.',
                'Logical row-buffer/page-policy and ACT admission are checked; physical DRAM PRE/ACT/REF pin sequencing is not exposed by this controller interface.'],
            'observability': available, 'semantics_guards': rules_found,
            'sources': sources, 'component_metadata': component_metadata,
            'supported': not gaps, 'limitations': gaps}


def parse_system_events(text):
    events = []
    for line in (text or '').splitlines():
        if 'AGENT2_SYSTEM_EVENT|' not in line:
            continue
        payload = line.split('AGENT2_SYSTEM_EVENT|', 1)[1]
        fields = dict(part.split('=', 1) for part in payload.split('|') if '=' in part)
        for key in ('cycle', 'id', 'bank', 'address', 'write', 'ready', 'valid'):
            if key in fields:
                try:
                    fields[key] = int(fields[key])
                except ValueError:
                    fields[key] = None
        for key in ('data',):
            if key in fields:
                try:
                    fields[key] = int(fields[key], 16)
                except ValueError:
                    fields[key] = None
        events.append(fields)
    return events


class ControllerReferenceModel:
    """Replay real monitor events to independently check memory and identities."""
    def __init__(self):
        self.memory, self.pending, self.finished = {}, {}, set()
        self.active, self.responses = None, []
        self.accepted = self.dispatched = self.completed = self.cancelled = 0
        self.responses_checked = self.data_mismatches = self.duplicated = 0
        self.failures = []
        self.history = []

    def fail(self, event, reason, expected=None, observed=None):
        self.failures.append({'scenario': event.get('scenario'), 'cycle': event.get('cycle'),
                              'transaction': event.get('id'), 'bank': event.get('bank'),
                              'address': event.get('address'), 'reason': reason,
                              'expected': expected, 'observed': observed,
                              'outstanding_ids': sorted(self.pending)})

    def observe(self, event):
        self.history.append(event)
        kind, identity = event.get('event'), event.get('id')
        if kind == 'reset':
            self.cancelled += len(self.pending)
            self.finished.update(self.pending)
            self.pending.clear()
            self.active, self.responses = None, []
            self.memory.clear()
            return
        if kind == 'accepted':
            if event.get('valid') != 1 or event.get('ready') != 1:
                self.fail(event, 'Acceptance without real ready/valid handshake', 'valid=ready=1', event)
                return
            if identity is None or identity in self.pending or identity in self.finished:
                self.duplicated += 1
                self.fail(event, 'Duplicate/invalid accepted identity')
                return
            if any(event.get(k) is None for k in ('bank', 'address', 'write', 'data')):
                self.fail(event, 'Unknown accepted request payload')
                return
            self.pending[identity] = dict(event)
            self.accepted += 1
        elif kind == 'dispatched':
            if self.active is not None or identity not in self.pending:
                self.duplicated += 1
                self.fail(event, 'Dispatch without a unique pending accepted request')
                return
            request = self.pending[identity]
            if request.get('dispatched'):
                self.duplicated += 1
                self.fail(event, 'Request dispatched twice')
                return
            for key in ('bank', 'address', 'write', 'data'):
                if request[key] != event.get(key):
                    self.fail(event, 'Dispatch changed accepted ' + key, request[key], event.get(key))
            request['dispatched'] = True
            self.active = identity
            self.dispatched += 1
        elif kind == 'completed':
            if identity != self.active or identity not in self.pending:
                self.duplicated += 1
                self.fail(event, 'Completion without a unique active request')
                return
            request = self.pending[identity]
            for key in ('bank', 'address', 'write', 'data'):
                if request[key] != event.get(key):
                    self.fail(event, 'Service changed accepted ' + key, request[key], event.get(key))
            key = (request['bank'], request['address'])
            if request['write']:
                self.memory[key] = request['data']
                self.pending.pop(identity)
                self.finished.add(identity)
                self.completed += 1
            else:
                self.responses.append((identity, self.memory.get(key, 0)))
            self.active = None
        elif kind == 'response':
            if not self.responses:
                self.duplicated += 1
                self.fail(event, 'Unexpected/duplicate read response')
                return
            expected_id, expected = self.responses.pop(0)
            self.responses_checked += 1
            if identity != expected_id:
                self.fail(event, 'Response association differs from dispatch/service order', expected_id, identity)
            if expected != event.get('data'):
                self.data_mismatches += 1
                self.fail(event, 'Response data mismatch', expected, event.get('data'))
            self.pending.pop(expected_id)
            self.finished.add(expected_id)
            self.completed += 1

    def finish(self, drain_seen):
        unaccounted = len(self.pending)
        if drain_seen and (unaccounted or self.responses or self.active is not None):
            self.fail({'event': 'drain'}, 'Final drain left accepted requests or read responses outstanding', 0,
                      {'requests': unaccounted, 'responses': len(self.responses)})
        proven = bool(drain_seen and not self.failures)
        return {'accepted': self.accepted, 'dispatched': self.dispatched, 'completed': self.completed,
                'cancelled_by_reset': self.cancelled, 'outstanding': unaccounted,
                'pending_read_responses': len(self.responses),
                'lost': unaccounted if drain_seen else None,
                'duplicated': self.duplicated if drain_seen else None,
                'accounting_proven': proven, 'responses_checked': self.responses_checked,
                'data_mismatches': self.data_mismatches}
