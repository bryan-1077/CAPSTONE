"""Agent 3 cycle timing inventory and executable event-spacing checks.

Only approved, implemented and observable rules are checked. Bank-FSM timers
are distinguished from logical ACT admission and physical DRAM command pins.
No functional reference memory is implemented here.
"""
import re
from bisect import bisect_left
from pathlib import Path
from system_uvm_generation import digest
from controller_manifest import read_mapping


def instance_paths(agent1, top, bank_commands='bank_cmd_valid'):
    definitions = {m['name']: m for m in agent1['module_definitions']}
    result, pending = [], [(top, 'dut_tb_top.dut', None, ())]
    while pending:
        module, path, bank, ancestors = pending.pop()
        if module in ancestors or module not in definitions: continue
        definition = definitions[module]
        result.append((definition, path, bank))
        for child in definition.get('instantiated_modules', []):
            if child.get('array'): continue  # needs reviewed generate/index elaboration
            bank_match = re.search(re.escape(bank_commands)+r'\s*\[\s*(\d+)\s*\]', child.get('connections', ''))
            pending.append((child['module'], path+'.'+child['name'],
                            int(bank_match.group(1)) if bank_match else bank, ancestors+(module,)))
    return result


def discover_timing_inventory(manifest, controller, agent1, catalog=()):
    profile = manifest.get('timing', {}).get('profile', {})
    cycles = dict(controller['timing_cycles'])
    # Parameter/config/spec/IR provenance is saved with every implemented rule.
    metadata = controller.get('component_metadata', [])
    parameters = manifest.get('timing', {}).get('module_parameters', {})
    names = set(cycles) | {'tRCD','tRP','tRAS','tWR','tCCD_S','tCCD_L','tCL','tRRD','tFAW','tREFI','tRFC1'}
    source_cycles = {}
    def inspect_mapping(data, source):
        if not isinstance(data,dict): return
        for key,value in data.items():
            if key=='timing_cycles' and isinstance(value,dict):
                for rule, configured in value.items():
                    if type(configured) is int:
                        if rule in cycles and cycles[rule]!=configured:
                            raise ValueError('Timing cycle metadata disagrees for '+rule+': '+source)
                        cycles.setdefault(rule,configured); names.add(rule); source_cycles[rule]=source
            elif isinstance(value,dict): inspect_mapping(value,source)
    for entry in metadata:
        inspect_mapping(read_mapping(entry['path']),entry['path'])
    for entries in parameters.values():
        for key, value in entries.items():
            matched = False
            for name in list(names):
                if re.sub(r'[^a-z0-9]', '', key.lower()).startswith(re.sub(r'[^a-z0-9]','',name.lower())):
                    if key.endswith('_CYCLES'):
                        if name in cycles and cycles[name]!=value: raise ValueError('Timing parameter/profile differs: '+key)
                        cycles.setdefault(name, value); matched=True
            if key.endswith('_CYCLES') and not matched:
                name=key[:-7]; names.add(name); cycles.setdefault(name,value)
    paths = instance_paths(agent1, controller['top_module'], controller['observability'].get('bank_commands','bank_cmd_valid'))
    result = []
    for name in sorted(names):
        value = cycles.get(name)
        row = {'rule': name, 'required_cycles': value, 'classification': 'NOT_APPLICABLE',
               'scope': None, 'kind': None, 'mandatory': False, 'observers': [],
               'configured_source': 'manifest.timing.profile.timing_cycles.'+name if name in profile.get('timing_cycles', {}) else 'manifest.timing.module_parameters',
               'metadata_sources': [m['path'] for m in metadata if name.lower() in str(m).lower()],
               'module_parameters': parameters, 'reason': 'No implemented timing rule/observation contract found.'}
        if name in source_cycles: row['configured_source']=source_cycles[name]+' / timing_cycles.'+name
        row['requirement_catalog_entries']=[r for r in catalog if name.lower() in str(r).lower()]
        row['agent1_modules']=[d['name'] for d,_,_ in paths if name.lower() in d['name'].lower()]
        enabled = controller['configuration'].get({'tRRD':'trrd_enabled','tFAW':'tfaw_enabled','tREFI':'refresh_enabled','tRFC1':'refresh_enabled'}.get(name), True)
        row['mandatory'] = bool(enabled and name in ('tRRD','tFAW'))
        if not enabled:
            row['reason'] = 'Disabled by normalized controller configuration.'
        elif name in ('tRRD','tFAW') and value:
            row['mandatory'] = True
            implements = any(any(p['name'].upper().startswith(name.upper()) for p in d.get('parameters', [])) or
                             name.lower() in Path(d['source_file']).read_text().lower() for d, _, _ in paths)
            if implements and 'act' in controller['observability']:
                row.update(classification='APPLICABLE', scope='logical_ACT_admission', mandatory=True,
                           kind='spacing' if name=='tRRD' else 'window',
                           activation_limit=controller['tfaw_limit'],
                           observers=[{'signal': 'dut_tb_top.bus.mon.act', 'event':'act'}], reason=None)
            else:
                row.update(classification='NOT_OBSERVABLE', reason='Enabled ACT rule lacks implemented observable ACT admission.')
        elif value:
            observers = []
            for definition, path, bank in paths:
                ports = {p['name'].lower(): p['name'] for p in definition['ports']}
                done = ports.get(name.lower()+'_done')
                if not done or 'start' not in ports: continue
                text = Path(definition['source_file']).read_text()
                match = re.search(r'\b'+re.escape(name)+r'_counter\s*>=\s*(?:\d+\s*\x27[dD])?(\d+)', text, re.I)
                # Configuration defines the expectation. A buggy RTL threshold
                # must be exercised and measured, not rejected before simulation
                # or substituted into the expected minimum.
                observers.append({'start':path+'.'+ports['start'], 'end':path+'.'+done,
                                  'bank':bank, 'source':definition['source_file'],
                                  'rtl_literal_threshold':int(match.group(1)) if match else None})
            if observers:
                row.update(classification='APPLICABLE', scope='bank_FSM_start_to_done_minimum',
                           kind='delay', mandatory=True, observers=observers, reason=None,
                           unverified_scope='No physical ACT/column/PRE command pair is exposed; this checks the implemented bank timer only.')
            else:
                row.update(classification='NOT_OBSERVABLE', reason='Profile value exists but no reviewed command/event pair implements this requirement. Fixed host response latency is not assumed.')
        if name=='tCL' and not value and profile.get('cas_latency_options'):
            row.update(classification='NOT_OBSERVABLE', reason='CAS options are profile metadata, not a fixed host response-latency contract.')
        if row['mandatory'] and not value:
            row.update(classification='NOT_OBSERVABLE',reason='Enabled timing rule lacks a resolved configured cycle value.')
        result.append(row)
    if controller['configuration']['refresh_enabled']:
        result.append({'rule':'refresh_blocking','required_cycles':0,'classification':'APPLICABLE' if 'refresh' in controller['observability'] else 'NOT_OBSERVABLE',
            'scope':'controller_dispatch_exclusion','kind':'exclusion','mandatory':True,
            'observers':[{'signal':'dut_tb_top.bus.mon.refresh','event':'refresh'}, {'signal':'dut_tb_top.bus.mon.dispatch','event':'dispatch'}],
            'configured_source':'controller contract / SYS-REQ-012','metadata_sources':controller['sources']['module_specs'],
            'reason':None})
    channel=0
    for row in result:
        if row['kind']=='delay':
            for observer in row['observers']:
                observer['channel']=channel; channel+=1
        row['inventory_hash'] = digest(row)
    return result


def trace_records(text, prefix):
    result, epoch = [], 0
    for line_number, line in enumerate(text.splitlines()):
        if line.startswith('AGENT3_TIMING_EVENT|event=reset|'): epoch += 1
        if not line.startswith(prefix+'|'): continue
        # VCS pads %s when a conditional expression selects unequal-length
        # string literals (for example "       active_service"). Protocol
        # fields are tokens; presentation padding must not hide real evidence.
        row = dict((key.strip(), value.strip()) for key, value in
                   (part.split('=',1) for part in line.split('|')[1:] if '=' in part))
        row.update(epoch=epoch, line_number=line_number)
        for key in ('cycle','bank','channel','value','valid','ready','dispatch','completion','write','row_hit','row_miss','row_closed','full','refresh','outstanding','response','service_pending'):
            if key in row:
                try: row[key] = int(row[key])
                except ValueError: row[key] = None
        result.append(row)
    return result


def check_timing(inventory, records, issues=()):
    output = {}
    for rule in inventory:
        name, required = rule['rule'], rule['required_cycles']
        measurements, history, starts, last_act = [], {}, {}, None
        early, unknown_samples = 0, 0
        row = dict(rule, comparisons=0, minimum_observed=None, maximum_observed=None,
                   boundary_hits=0, early_attempts=0, violations=0, evidence=[])
        if rule['classification'] != 'APPLICABLE':
            row['result'] = rule['classification']
            output[name] = row
            continue
        for event in records:
            if event.get('event') == 'reset':
                history.clear(); starts.clear(); last_act = None
                continue
            if event.get('cycle') is None:
                if event.get('event') in ('act','start','end','refresh_sample'): unknown_samples += 1
                continue
            cycle, bank = event['cycle'], event.get('bank')
            previous = None
            if event.get('event')=='act' and name in ('tRRD','tFAW'):
                if name=='tRRD': previous = last_act
                else:
                    acts = history.setdefault('acts', [])
                    limit = rule['activation_limit']
                    if len(acts)>=limit: previous = acts[-limit]
                    acts.append(cycle)
                last_act = cycle
            elif event.get('rule')==name and rule['kind']=='delay':
                channel = event.get('channel', bank)
                if event.get('event')=='start': starts[channel] = cycle
                elif event.get('event')=='end' and channel in starts: previous = starts.pop(channel)
            elif rule['kind']=='spacing' and event.get('rule')==name:
                channel = bank if rule.get('per_bank') else 'all'
                previous = history.get(channel); history[channel] = cycle
            elif rule['kind']=='exclusion' and event.get('event')=='refresh_sample':
                if event.get('value')==1:
                    if event.get('dispatch') not in (0,1):
                        unknown_samples+=1; continue
                    measurements.append(dict(event, required_cycles=0, measured_cycles=event.get('dispatch',0),
                                             result='FAIL' if event.get('dispatch') else 'PASS'))
            if previous is not None:
                spacing = cycle-previous
                measurements.append(dict(event, timing_rule=name, required_cycles=required,
                    previous_cycle=previous, measured_cycles=spacing, result='PASS' if spacing>=required else 'FAIL'))
        # Early HOST issue pressure is coverage, not an illegal ACT/DDR command.
        if name in ('tRRD','tFAW'):
            by_epoch={}
            for event in records:
                if event.get('event')=='act' and event.get('cycle') is not None:
                    key=(event.get('execution'),event.get('epoch',0))
                    by_epoch.setdefault(key,[]).append((event['cycle'],event.get('line_number',-1)))
            for values in by_epoch.values(): values.sort()
            for issue in issues:
                if issue.get('cycle') is None: continue
                earlier=by_epoch.get((issue.get('execution'),issue.get('epoch',0)),[])
                end=bisect_left(earlier,(issue['cycle'],issue.get('line_number',float('inf'))))
                index = 1 if name=='tRRD' else rule['activation_limit']
                if end>=index and issue['cycle']-earlier[end-index][0]<required: early += 1
        values = [e['measured_cycles'] for e in measurements]
        row.update(comparisons=len(measurements), minimum_observed=min(values) if values else None,
                   maximum_observed=max(values) if values else None,
                   boundary_hits=sum(v==required for v in values), early_attempts=early,
                   violations=sum(e['result']=='FAIL' for e in measurements), evidence=measurements,
                   result='FAIL' if any(e['result']=='FAIL' for e in measurements) else 'PASS' if measurements else 'NOT_TESTED')
        row['unknown_samples']=unknown_samples
        if unknown_samples and not row['violations']: row['result']='NOT_TESTED'
        if rule['kind']=='delay':
            expected={o['channel'] for o in rule['observers'] if 'channel' in o}
            observed={e.get('channel') for e in measurements}
            row['missing_observer_channels']=sorted(expected-observed)
            if expected-observed and not row['violations']: row['result']='NOT_TESTED'
        output[name] = row
    return output
