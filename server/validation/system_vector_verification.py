"""System Agent 3: authenticated immutable Agent 2 checker, host vectors, VCS.

Functional evidence is evaluated by the existing Agent 2 monitor replay. This
module adds stimulus/coverage and cycle-level timing checks, not another memory.
"""
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

from controller_verification import extract_controller_contract
from controller_stress_vectors import discover_item_contract, build_stress_plan, vector_text
from controller_timing_verification import discover_timing_inventory, trace_records, check_timing
from requirement_verification import build_system_requirement_catalog
from system_uvm_generation import validate_saved_environment
from system_uvm_verification import (render_system_environment, summarize_system_simulation,
    classify_simulation_failure, input_fingerprints, verify_fingerprints, valid_simulation_startup)
from system_vector_sources import build_extension
from run_pipeline import read_report_json, write_report_json, run_reported_agent


class SystemAPI:
    """Reuse Agent 3 stimulus discovery and Agent 2 verification helpers."""
    def __init__(self, vectors):
        import agent2_uvm_tb
        self.vectors, self.uvm = vectors, agent2_uvm_tb

    def __getattr__(self, name):
        if hasattr(self.vectors, name): return getattr(self.vectors, name)
        return getattr(self.uvm, name)


def functional_clean(result, requirements, simulation_ok):
    traffic = result['traffic']
    return bool(simulation_ok and result['status'] in ('PASS', 'PASS_WITH_GAPS') and
        requirements.get('uvm_errors') == 0 and requirements.get('uvm_fatals') == 0 and
        requirements.get('scoreboard_failed_checks') == 0 and
        (requirements.get('scoreboard_passed_checks') or 0) > 0 and
        traffic.get('accounting_proven') is True and traffic.get('accepted', 0) > 0 and
        traffic.get('accepted') == traffic.get('completed', 0) + traffic.get('cancelled_by_reset', 0) and
        all(traffic.get(k) == 0 for k in ('outstanding', 'lost', 'duplicated', 'data_mismatches')))


def require_severity_summaries(requirements, text):
    """Missing simulator severity evidence is unknown, never an implicit zero."""
    for name,key in (('ERROR','uvm_errors'),('FATAL','uvm_fatals')):
        if not re.search(r'^\s*UVM_'+name+r'\s*:\s*\d+\s*$',text,re.M):
            requirements[key]=None


def authenticate_handoff(api, manifest_path, agent1_path, agent2_path, environment_path, fingerprints_path=None):
    agent1 = read_report_json(str(agent1_path))
    if not isinstance(agent1,dict): raise ValueError('Missing Agent 1 result.')
    contract = extract_controller_contract(Path(manifest_path), agent1)
    if not contract['supported']:
        raise ValueError('Unsupported approved controller contract: ' + '; '.join(contract['limitations']))
    contract['_rtl_files'] = agent1['rtl_files']
    requirements = build_system_requirement_catalog(contract['configuration'], contract)
    trusted = render_system_environment(contract, requirements, agent1['dut_spec']['ports'])
    saved = read_report_json(str(environment_path))
    validate_saved_environment(api, saved, contract, requirements, trusted)
    baseline = read_report_json(str(agent2_path))
    if not isinstance(baseline,dict): raise ValueError('Missing Agent 2 system result.')
    if (baseline.get('mode') != 'system' or baseline.get('top_module') != contract['top_module'] or
            baseline.get('status') not in ('PASS', 'PASS_WITH_GAPS') or
            any(baseline.get(key) is not True for key in ('ready_for_agent3', 'checker_compile_valid', 'simulation_completed')) or
            saved.get('ready_for_agent3') is not True or saved.get('simulation_completed') is not True or
            saved.get('agent2_result') not in ('PASS', 'PASS_WITH_GAPS') or
            baseline.get('checker_identity') != saved['checker_identity'] or
            baseline.get('configuration') != contract['configuration']):
        raise ValueError('Agent 2 handoff lacks compatible successful simulation evidence.')
    if saved.get('testbench_top') != 'dut_tb_top' or saved.get('default_test') != 'dut_test':
        raise ValueError('Unsupported saved UVM test/top lifecycle.')
    if saved.get('uvm_flags')!=list(api.VCS_UVM_FLAGS) or saved.get('timescale')!='1ns/1ps':
        raise ValueError('Saved UVM compile configuration differs from the reviewed flow.')
    # Replay the saved log rather than trusting a status string or edited count.
    text = Path(saved['simulation_log']).read_text()
    public = {key: value for key, value in contract.items() if not key.startswith('_')}
    replay, baseline_requirements, _ = summarize_system_simulation(api, requirements, public, text, True)
    require_severity_summaries(baseline_requirements,text)
    if not functional_clean(replay, baseline_requirements, True) or not replay['ready_for_agent3']:
        raise ValueError('Saved Agent 2 log does not prove a clean baseline.')
    for key in ('accepted', 'completed', 'cancelled_by_reset', 'outstanding', 'lost', 'duplicated', 'data_mismatches'):
        if baseline.get('traffic', {}).get(key) != replay['traffic'][key]:
            raise ValueError('Agent 2 report/log accounting differs: ' + key)
    simulation = baseline.get('simulation', {})
    for key, field in (('uvm_error','uvm_errors'), ('uvm_fatal','uvm_fatals'),
                       ('scoreboard_pass','scoreboard_passed_checks'), ('scoreboard_fail','scoreboard_failed_checks')):
        if simulation.get(key) != baseline_requirements[field]:
            raise ValueError('Agent 2 report/log severity or scoreboard differs: ' + key)
    fingerprints = input_fingerprints(manifest_path)
    if fingerprints_path:
        verify_fingerprints(api, read_report_json(str(fingerprints_path)))
    files = {Path(row['path']).name: Path(row['path']).read_text() for row in saved['testbench_files']}
    item = discover_item_contract(api, files, contract)
    return dict(agent1=agent1, contract=contract, public_contract=public, requirements=requirements,
                saved=saved, baseline=baseline, baseline_requirements=baseline_requirements,
                fingerprints=fingerprints, files=files, item=item)


def validate_plan_budget(plan, saved_package, controller, inventory):
    match = re.search(r'\bMAX_WAIT\s*=\s*(\d+)\s*;', saved_package)
    if not match: raise ValueError('Saved checker watchdog budget is unresolved.')
    maximum = int(match.group(1))
    # Conservative admission/service budget plus explicit waits. This is a
    # preflight bound, not an expected per-request latency or timing assertion.
    bank_timer_budget = sum(r['required_cycles']+4 for r in inventory
                            if r['classification']=='APPLICABLE' and r['kind']=='delay')
    admission_budget = max([r['required_cycles'] for r in inventory
                            if r['classification']=='APPLICABLE' and r['kind'] in ('spacing','window')] or [1])
    per_request = admission_budget + controller['constants']['slow_service_cycles'] + 8
    roles = plan['item_contract']['roles']
    for group in plan['groups']:
        if group['status']=='NOT_APPLICABLE': continue
        bank_counts = {}
        for step in group['steps']:
            if step['op']=='request':
                bank = step['fields'][roles['bank']]; bank_counts[bank]=bank_counts.get(bank,0)+1
        budget = group['transactions']*per_request + max(bank_counts.values())*bank_timer_budget + sum(s['fields'][roles['cycles']]
                 for s in group['steps'] if s['op'] in ('idle','reset','wait_read_completion','wait_active_dispatch')) + 64
        # tRFC profile values without a runtime latency contract must not inflate
        # this estimate. Relevant bank-FSM and ACT timers govern admission.
        group['estimated_cycle_budget'] = budget
        group['checker_watchdog_cycles'] = maximum
        if budget >= maximum:
            raise ValueError('{} plan exceeds saved checker watchdog budget ({} >= {}); reduce --long-count/--random-count.'.format(group['vector_id'],budget,maximum))


def coverage_evidence(group, result, text, timing):
    samples = trace_records(text, 'AGENT3_SAMPLE')
    counts, coverage = result['traffic'], result.get('coverage', {})
    kind = group['expected_observation']
    events = result.get('accepted_request_history', [])
    values = {'traffic': counts.get('accepted',0)>0,
        'reset': 'event=reset|' in text,
        'queue_boundary': counts.get('accepted',0)==group['transactions'],
        'queue_full': coverage.get('saturation_cycles',0)>0,
        'backpressure': coverage.get('backpressure_cycles',0)>0,
        'consecutive': coverage.get('consecutive_accepts',0)>0,
        'outstanding': coverage.get('max_outstanding',0)>1,
        'simultaneous': coverage.get('completion_and_accept',0)>0,
        'full_completion': any(s.get('full')==1 and s.get('completion')==1 for s in samples),
        'same_bank': bool(events) and {e.get('bank') for e in events}=={0},
        'cross_bank': {e.get('bank') for e in events}==set(range(group['applicable_configuration']['bank_count'])),
        'readback': counts.get('responses_checked',0)>0,
        'mixed': {e.get('write') for e in events}=={0,1} and counts.get('responses_checked',0)>0,
        'row_hit': coverage.get('row_hits',0)>0,
        'row_miss': coverage.get('row_misses',0)>1,
        'row_closed': coverage.get('row_closed',0)>0,
        'cancelled': counts.get('cancelled_by_reset',0)>0,
        'pending_reset': 'AGENT3_GATE_HIT|' in text and counts.get('cancelled_by_reset',0)>0,
        'active_reset': any(g.get('gate')=='active_service' for g in trace_records(text,'AGENT3_GATE_HIT')) and counts.get('cancelled_by_reset',0)>0,
        'refresh': len({s['cycle'] for i,s in enumerate(samples) if s.get('refresh')==1 and
                       (i==0 or samples[i-1].get('refresh')!=1 or samples[i-1].get('cycle')!=s['cycle']-1)})>=2
                   and coverage.get('refresh_traffic',0)>0,
        'timing': all(timing.get(name,{}).get('comparisons',0)>0 for name in group['timing_requirements'])}
    exercised = values.get(kind, False)
    return {'expected':kind, 'observed':exercised, 'status':'PASS' if exercised else 'NOT_TESTED',
            'sample_count':len(samples), 'trusted_coverage':coverage,
            'gap':None if exercised else 'Target condition did not occur; planned intent is not proof.'}


def evaluate_group(api, context, group, text, simulation_ok, inventory):
    result, requirements, events = summarize_system_simulation(api, context['requirements'], context['public_contract'], text, simulation_ok)
    require_severity_summaries(requirements,text)
    records, issues = trace_records(text,'AGENT3_TIMING_EVENT'), trace_records(text,'AGENT3_ISSUE')
    host_attempts=[e for e in trace_records(text,'AGENT2_SYSTEM_EVENT') if e.get('event')=='presented']
    timing = check_timing(inventory, records, host_attempts)
    sequence_complete = bool(trace_records(text,'AGENT3_END'))
    issue_count = len(issues)
    roles=context['item']['roles']
    planned=[{role:step['fields'][roles[role]] for role in ('bank','address','write','data')}
             for step in group['steps'] if step['op']=='request']
    accepted=[{role:event.get(role) for role in ('bank','address','write','data')}
              for event in events if event['event']=='accepted']
    offered=[]
    for issue in issues:
        try:
            offered.append({'bank':issue['bank'],'address':int(issue['address'],16),
                            'write':issue['write'],'data':int(issue['data'],16)})
        except (ValueError, KeyError, TypeError): offered.append(None)
    integrity = ('AGENT3_RUNTIME_START|' in text and sequence_complete and issue_count==group['transactions'] and
                 result['traffic'].get('accepted')==group['transactions'] and accepted==planned and offered==planned)
    clean = functional_clean(result,requirements,simulation_ok)
    classification = None
    original, _ = classify_simulation_failure(result,requirements,text,simulation_ok,context['agent1']['rtl_files']) if not clean else (None,None)
    if any(row['violations'] for row in timing.values()) and valid_simulation_startup(text):
        classification = 'RTL_TIMING_PROTOCOL_FAILURE'
    elif not clean:
        classification = 'RTL_FUNCTIONAL_FAILURE' if original=='RTL_FUNCTIONAL_FAILURE' else 'AGENT3_INFRASTRUCTURE_FAILURE'
    if not integrity and classification is None:
        classification = 'AGENT3_INFRASTRUCTURE_FAILURE'
    coverage = coverage_evidence(group,result,text,timing)
    return {'vector_id':group['vector_id'],'seed':group['seed'],
            'status':'FAIL' if classification else 'PASS' if coverage['observed'] else 'PASS_WITH_GAPS',
            'functional_status':'PASS' if clean and integrity else 'FAIL' if original=='RTL_FUNCTIONAL_FAILURE' else 'NOT_TESTED',
            'simulation_completed':simulation_ok, 'sequence_completed':sequence_complete,
            'planned_transactions':group['transactions'], 'issued_transactions':issue_count,
            'stimulus_payloads_verified':accepted==planned and offered==planned,
            'traffic':result['traffic'],'simulation':{'uvm_error':requirements.get('uvm_errors'),
            'uvm_fatal':requirements.get('uvm_fatals'),'scoreboard_pass':requirements.get('scoreboard_passed_checks'),
            'scoreboard_fail':requirements.get('scoreboard_failed_checks')}, 'coverage':coverage,'timing':timing,
            'failure_classification':classification,'failure_evidence':result['failure_evidence'],
            'outstanding_transactions':result['outstanding_transactions'],
            'pending_read_response_transactions':result['pending_read_response_transactions'],
            'events':events,'timing_events':records,'issue_events':issues,'host_attempt_events':host_attempts,'requirements':requirements}


def aggregate_results(plan, results, inventory):
    rows = {r['vector_id']:r for r in results}
    all_records, all_issues = [], []
    for result in results:
        # Reset history at every simulation boundary; cycles/IDs restart.
        all_records.append({'event':'reset','cycle':0})
        all_records += [dict(e, execution=result['vector_id']) for e in result['timing_events']]
        all_issues += [dict(e, execution=result['vector_id']) for e in result.get('host_attempt_events',result['issue_events'])]
    timing = check_timing(inventory,all_records,all_issues)
    failures = [r for r in results if r['status']=='FAIL']
    incomplete = [g['vector_id'] for g in plan['groups'] if g['status']!='NOT_APPLICABLE' and g['vector_id'] not in rows]
    mandatory_missing = [name for name,r in timing.items() if r['mandatory'] and r['result']!='PASS']
    mandatory_vectors = [g['vector_id'] for g in plan['groups'] if g.get('mandatory_coverage') and
                         g['vector_id'] in rows and not rows[g['vector_id']]['coverage']['observed']]
    gaps = [dict(vector_id=r['vector_id'],reason=r['coverage']['gap']) for r in results if not r['coverage']['observed']]
    gaps += [dict(timing_rule=name,reason=r.get('reason') or 'No measured comparisons') for name,r in timing.items()
             if r['result'] in ('NOT_OBSERVABLE','NOT_TESTED')]
    gaps += [dict(timing_rule=name,reason=r['unverified_scope']) for name,r in timing.items() if r.get('unverified_scope')]
    gaps += [dict(timing_rule=name,reason='Exact legal boundary was not observed; no boundary coverage claimed.')
             for name,r in timing.items() if r['classification']=='APPLICABLE' and r['kind']!='exclusion' and r['boundary_hits']==0]
    classification = failures[0]['failure_classification'] if failures else ('AGENT3_INFRASTRUCTURE_FAILURE' if incomplete or mandatory_missing or mandatory_vectors else None)
    ready = bool(results and not failures and not incomplete and not mandatory_missing and not mandatory_vectors)
    traffic = {}
    for key in ('presented','accepted','dispatched','completed','cancelled_by_reset','outstanding','lost','duplicated','data_mismatches','responses_checked'):
        values = [r['traffic'].get(key) for r in results]
        traffic[key] = sum(values) if values and all(type(v) is int for v in values) else None
    traffic['accounting_proven'] = bool(results and all(r['traffic']['accounting_proven'] for r in results))
    counts = {}
    for key in ('uvm_error','uvm_fatal','scoreboard_pass','scoreboard_fail'):
        values = [r['simulation'].get(key) for r in results]
        counts[key] = sum(values) if values and all(type(v) is int for v in values) else None
    return {'mode':'system','top_module':plan['top_module'],'configuration':plan['configuration'],
        'status':'FAIL' if not ready else 'PASS_WITH_GAPS' if gaps else 'PASS',
        'functional_status':'PASS' if results and not incomplete and all(r['functional_status']=='PASS' for r in results) else 'FAIL' if any(r['functional_status']=='FAIL' for r in results) else 'NOT_TESTED',
        'protocol_timing_status':'FAIL' if any(r['violations'] for r in timing.values()) else 'NOT_TESTED' if not results or incomplete or mandatory_missing else 'PASS_WITH_GAPS' if any(r['result'] in ('NOT_OBSERVABLE','NOT_TESTED') for r in timing.values()) else 'PASS',
        'traffic':traffic,'simulation':counts,'timing':timing,'coverage_limitations':gaps,
        'mandatory_timing_missing':mandatory_missing,'mandatory_vector_coverage_missing':mandatory_vectors,'unrun_vectors':incomplete,
        'ready_for_backend':ready,'agent3_executed':bool(results),'physical_STA_verified':False,
        'verification_readiness':'READY_FOR_BACKEND' if ready else 'BLOCKED_SYSTEM_AGENT_3',
        'failure':{'classification':classification,'disposition':'RETURN_TO_FRONTEND_RTL_FIX' if classification in ('RTL_FUNCTIONAL_FAILURE','RTL_TIMING_PROTOCOL_FAILURE') else 'INVESTIGATE_VERIFICATION_ENVIRONMENT' if classification else None,'frontend_feedback':None}}


def run_command(command, directory, timeout):
    try:
        process = subprocess.run(command,cwd=str(directory),stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                 universal_newlines=True,timeout=timeout)
        return process.returncode==0, process.stdout, None
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ''
        if isinstance(output,bytes): output = output.decode('utf-8','replace')
        return False, output, 'Process timed out after {} seconds'.format(timeout)
    except OSError as error:
        return False, '', str(error)


def frontend_feedback(report, context, result, cases):
    failed = [c for c in cases if c['failure_classification'] in ('RTL_FUNCTIONAL_FAILURE','RTL_TIMING_PROTOCOL_FAILURE')]
    evidence = []
    for case in failed:
        timing = [dict(e,timing_rule=name) for name,row in case['timing'].items() for e in row['evidence'] if e['result']=='FAIL']
        failures=[]
        for failure in case['failure_evidence']:
            if failure.get('verification_gap'): continue
            history=[e for e in case['events'] if failure.get('transaction') is not None and e.get('id')==failure['transaction']]
            request=next((e for e in history if e['event']=='accepted'),{})
            address=request.get('address',failure.get('address'))
            cycles={kind+'_cycle':next((e.get('cycle') for e in history if e['event']==kind),None)
                    for kind in ('accepted','dispatched','completed','response')}
            failures.append(dict(failure,transaction_history=history,operation='write' if request.get('write') else 'read' if request else None,
                bank=request.get('bank',failure.get('bank')),address=address,
                row=address >> context['contract']['constants']['col_width'] if type(address) is int else None,
                column=address & ((1 << context['contract']['constants']['col_width'])-1) if type(address) is int else None,
                write_data=request.get('data') if request.get('write') else None,**cycles))
        evidence.append({'vector_id':case['vector_id'],'seed':case['seed'],'classification':case['failure_classification'],
            'functional_failures':failures,'timing_failures':timing,
            'transaction_history':case['events'],'command_history':case['timing_events'],
            'host_issue_history':case['issue_events'],'traffic':case['traffic'],
            'outstanding_requests':case['outstanding_transactions'],
            'pending_read_responses':case['pending_read_response_transactions'],
            'paths':case['paths']})
    names = [m['name'] for m in context['agent1']['module_definitions']]
    rules = {e['timing_rule'].lower() for case in evidence for e in case['timing_failures']}
    sources={observer['source'] for case in failed for name,row in case['timing'].items()
             if name.lower() in rules for observer in row['observers'] if 'source' in observer}
    tokens = list(rules) if rules else ['queue','sched','row','bank']
    suggestions = [definition['name'] for definition in context['agent1']['module_definitions']
                   if any(token in definition['name'].lower() for token in tokens) or definition['source_file'] in sources]
    package = {'schema_version':1,'failing_agent':'Agent 3','failure_classification':result['failure']['classification'],
        'disposition':'RETURN_TO_FRONTEND_RTL_FIX','failures':evidence,
        'suspected_subsystems':suggestions,'subsystem_attribution':'Diagnostic suggestions; internal culprit is not established.',
        'suggested_module_diagnostics':suggestions,'automatic_module_diagnostics_run':False,
        'restart_from':'Agent 1 SYSTEM','production_rtl_modified':False,
        'checker_identity':context['saved']['checker_identity'], 'configuration':context['public_contract']['configuration'],
        'paths':{'vector_plan':str(Path(report.directory)/'agent3_vector_plan.json'),
                 'compile_log':str(Path(report.directory)/'compile.log'),
                 'rtl_snapshots':{name:path for name,path in report.data['artifacts'].items() if name.startswith('rtl_snapshot/')}}}
    report.artifact('frontend_feedback.json',data=package)
    return str(Path(report.directory)/'frontend_feedback.json')


def run_system_vectors(api, report, args):
    context, plan, results, inventory = None, None, [], []
    compile_ok, compiled, compile_error = None, False, None
    hash_verified=False
    stage = 'handoff_authentication'
    try:
        report.step(stage)
        context = authenticate_handoff(api,args.manifest,args.agent1_result,args.agent2_result,args.uvm_environment,args.agent1_inputs)
        hash_verified=True
        saved, controller = context['saved'], context['contract']
        report.artifact('authenticated_handoff.json',data={'checker_identity':saved['checker_identity'],
            'rtl_files':saved['rtl_files'],'testbench_files':saved['testbench_files'],
            'manifest_sha256':saved['source_manifest_sha256'],'configuration_hash':saved['configuration_hash'],
            'contract_hash':saved['contract_hash'],'requirement_hash':saved['requirement_hash'],
            'trusted_checker_hash':saved['generation']['audit']['trusted_scaffolding_hash'],
            'agent2_result':str(Path(args.agent2_result).resolve()),'environment':str(Path(args.uvm_environment).resolve())})
        inventory = discover_timing_inventory(read_report_json(args.manifest),controller,context['agent1'],context['requirements'])
        report.artifact('timing_inventory.json',data=inventory)
        stage = 'vector_planning'; report.step(stage)
        plan = build_stress_plan(controller,context['item'],inventory,args.seed,args.random_count,args.long_count)
        validate_plan_budget(plan,context['files']['dut_pkg.sv'],controller,inventory)
        report.artifact('agent3_vector_plan.json',data=plan)
        work = Path(report.directory)/'work'; source_dir = work/'sources'
        source_dir.mkdir(parents=True,exist_ok=True)
        originals = []
        for row in saved['testbench_files']:
            destination = source_dir/Path(row['path']).name
            shutil.copyfile(row['path'],str(destination)); originals.append(str(destination))
            report.artifact('checker_sources/'+destination.name,source=str(destination))
        for name, source in build_extension(context['item'],controller,inventory).items():
            (source_dir/name).write_text(source)
            report.artifact('extension_sources/'+name,text=source)
        for index,path in enumerate(controller['_rtl_files']):
            report.artifact('rtl_snapshot/{}_{}'.format(index,Path(path).name),source=path)
        tracked=[args.agent1_result,args.agent2_result,args.uvm_environment,saved['controller_verification_contract'],
                 saved['requirement_catalog'],saved['simulation_log']]+[r['path'] for r in saved['testbench_files']]
        tracking={path:api.file_sha256(path) for path in tracked}
        tracking.update({str(path):api.file_sha256(str(path)) for path in source_dir.glob('*.sv')})
        report.artifact('compile_source_hashes.json',data=tracking)
        def verify():
            nonlocal hash_verified
            hash_verified=False
            verify_fingerprints(api,context['fingerprints'])
            for path,expected in tracking.items():
                if api.file_sha256(path)!=expected: raise ValueError('Authenticated handoff changed during Agent 3: '+path)
            for row in saved['testbench_files']:
                if api.file_sha256(str(source_dir/Path(row['path']).name))!=row['sha256']:
                    raise ValueError('Agent 3 copied checker changed: '+row['path'])
            hash_verified=True
        verify()
        stage='compile'; report.step(stage)
        vcs = shutil.which('vcs')
        if not vcs: raise ValueError('VCS is unavailable; no RTL simulation was executed.')
        binary=work/'simv'
        incdirs=sorted({str(source_dir)} | {str(Path(p).parent) for p in controller['_rtl_files']})
        command=[vcs,'-full64','-sverilog']+list(saved['uvm_flags'])+['-timescale='+saved['timescale'],
            '-top',saved['testbench_top'],'-top','agent3_system_bootstrap','-Mdir='+str(work/'csrc'),
            '-debug_access+all']+['+incdir+'+p for p in incdirs]+list(controller['_rtl_files'])+originals+[
            str(source_dir/'agent3_system_pkg.sv'),str(source_dir/'agent3_system_bootstrap.sv'),'-o',str(binary)]
        report.artifact('compile_command.json',data=command)
        compiled=True
        compile_ok,output,compile_error=run_command(command,work,args.timeout)
        report.artifact('compile.log',text=output+('\n'+compile_error if compile_error else ''))
        verify()
        if not compile_ok: raise ValueError('Agent 3 extension/hierarchy compile failed. See compile.log; saved checker was not repaired.')
        if not binary.is_file(): raise ValueError('VCS returned success without a simulation executable.')
        tracking[str(binary)]=api.file_sha256(str(binary))
        report.artifact('compiled_image.json',data={'path':str(binary),'sha256':tracking[str(binary)]})
        stage='simulation'; report.step(stage)
        for group in plan['groups']:
            if group['status']=='NOT_APPLICABLE': continue
            print('[AGENT3] Running '+group['vector_id']+' ('+str(group['transactions'])+' transactions)',flush=True)
            directory=work/group['vector_id']; (directory/'build_auto').mkdir(parents=True,exist_ok=True)
            vector_file=directory/'vectors.txt'; vector_file.write_text(vector_text(group,context['item']))
            tracking[str(vector_file)]=api.file_sha256(str(vector_file))
            command=[str(binary),'-no_save','+UVM_TESTNAME=agent3_system_test','+AGENT3_VECTOR_FILE='+str(vector_file),
                     '+ntb_random_seed='+str(args.seed)]
            relative='vectors/'+group['vector_id']+'/'
            report.artifact(relative+'vectors.txt',source=str(vector_file))
            report.artifact(relative+'command.json',data=command)
            verify()
            ok,output,error=run_command(command,directory,args.timeout)
            # Preserve raw simulator output; infrastructure errors are separate.
            report.artifact(relative+'simulation.log',text=output)
            if error: report.artifact(relative+'execution_error.txt',text=error)
            verify()
            case=evaluate_group(api,context,group,output,ok,inventory)
            if error and case['failure_classification'] is None:
                case.update(status='FAIL',failure_classification='AGENT3_INFRASTRUCTURE_FAILURE')
            paths={name:str(Path(report.directory)/relative/filename) for name,filename in (
                ('simulation_log','simulation.log'),('transactions','monitor_events.json'),
                ('timing','timing_evidence.json'),('requirements','requirement_traceability.json'),('vectors','vectors.txt'))}
            waveform=directory/'build_auto/controller.vcd'
            paths['waveform']=str(Path(report.directory)/relative/'controller.vcd') if waveform.is_file() else None
            if waveform.is_file(): report.artifact(relative+'controller.vcd',source=str(waveform))
            case['paths']=paths; results.append(case)
            for filename,data in (('monitor_events.json',case['events']),('timing_evidence.json',case['timing']),
                                  ('requirement_traceability.json',case['requirements']),('result.json',case)):
                report.artifact(relative+filename,data=data)
            group.update(status=case['status'],evidence_paths=paths)
            report.artifact('agent3_vector_plan.json',data=plan)
            if case['status']=='FAIL': break
        result=aggregate_results(plan,results,inventory)
    except Exception as error:
        import traceback
        report.artifact('infrastructure_traceback.txt',text=traceback.format_exc())
        result=aggregate_results(plan,results,inventory) if plan else {
            'mode':'system','status':'FAIL','functional_status':'NOT_TESTED','protocol_timing_status':'NOT_TESTED',
            'ready_for_backend':False,'agent3_executed':False,'traffic':None,'simulation':{},
            'coverage_limitations':[],'verification_readiness':'BLOCKED_SYSTEM_AGENT_3','physical_STA_verified':False}
        result.update(status='FAIL',ready_for_backend=False,verification_readiness='BLOCKED_SYSTEM_AGENT_3',
                      failure_stage=stage,failure_detail=str(error),
                      failure={'classification':'AGENT3_INFRASTRUCTURE_FAILURE','disposition':'INVESTIGATE_VERIFICATION_ENVIRONMENT','frontend_feedback':None})
        report.artifact('infrastructure_failure.json',data={'stage':stage,'reason':str(error)})
    result.update(compile_invoked=compiled,compile_passed=compile_ok,
                  agent2_checker_reused=bool(context and compiled),checker_hash_verified=hash_verified,
                  checker_identity=context['saved']['checker_identity'] if context else None,
                  rtl_file_count=len(context['contract']['_rtl_files']) if context else None,
                  observability_limitations=context['public_contract']['observability_limitations'] if context else [],
                  functional_reference='Exact saved Agent 2 checker and existing monitor replay',
                  failure_policy='Only proven RTL functional/timing violations return to frontend')
    complete=bool(results and plan and len(results)==sum(g['status']!='NOT_APPLICABLE' for g in plan['groups']) and
                  all(case['simulation_completed'] for case in results))
    result['simulation_completed']=complete
    result['simulation'].update(compile_passed=compile_ok,compile_invoked=compiled,
        simulation_invoked=bool(results),simulation_passed=complete if results else None)
    if context and results:
        # Fold per-vector rows without recursive stage_results growth, then use
        # the existing Agent 3 baseline/directed merge once.
        directed=dict(results[0]['requirements']); rows={r['id']:dict(r) for r in directed['requirements']}
        for case in results[1:]:
            for row in case['requirements']['requirements']:
                before=rows[row['id']]; states=(before['status'],row['status'])
                before.update(status='FAIL' if 'FAIL' in states else 'PASS' if 'PASS' in states else 'NOT_TESTED',
                              checks=before['checks']+row['checks'],failures=before['failures']+row['failures'])
        directed['requirements']=list(rows.values())
        for key,field in (('uvm_error','uvm_errors'),('uvm_fatal','uvm_fatals'),
                          ('scoreboard_pass','scoreboard_passed_checks'),('scoreboard_fail','scoreboard_failed_checks')):
            directed[field]=result['simulation'][key]
        directed['overall_status']='FAIL' if result['status']=='FAIL' else 'PASS'
        directed['failure_stage']='simulation' if result['status']=='FAIL' else None
        combined=api.combine_uvm_requirement_reports(context['baseline_requirements'],directed)
        report.artifact('combined_requirement_verification.json',data=combined)
        report.requirements(combined)
        result['requirements']={key:combined[field] for key,field in (
            ('passed','passed_requirements'),('failed','failed_requirements'),('not_tested','not_tested_requirements'))}
        if combined['failed_requirements']:
            result.update(status='FAIL',ready_for_backend=False,verification_readiness='BLOCKED_SYSTEM_AGENT_3')
    if context and result['failure']['disposition']=='RETURN_TO_FRONTEND_RTL_FIX':
        result['failure']['frontend_feedback']=frontend_feedback(report,context,result,results)
    if plan:
        for group in plan['groups']:
            if group['status']=='PENDING': group['status']='NOT_TESTED'
        report.artifact('agent3_vector_plan.json',data=plan)
        result['vector_results']=[{key:group.get(key) for key in ('vector_id','category','seed','status','purpose','evidence_paths')}
                           for group in plan['groups']]
        result['vectors']={'generated':len(plan['groups']),'executed':len(results),
            'passed':sum(g['status']=='PASS' for g in plan['groups']),
            'passed_with_gaps':sum(g['status']=='PASS_WITH_GAPS' for g in plan['groups']),
            'failed':sum(g['status']=='FAIL' for g in plan['groups']),
            'not_tested':sum(g['status']=='NOT_TESTED' for g in plan['groups']),
            'not_applicable':sum(g['status']=='NOT_APPLICABLE' for g in plan['groups'])}
        result['random']={'seed':args.seed,'transactions':args.random_count,'long_transactions':args.long_count}
    if context:
        result['baseline']={'agent2_result':str(args.agent2_result),'status':context['baseline']['status'],
                            'traffic':context['baseline']['traffic'],'simulation':context['baseline']['simulation']}
    result['statistics_scope']='Agent 3 simulations; combined requirement evidence also includes Agent 2 baseline'
    report.update(verification_status=result['status'],coverage_limitations=result['coverage_limitations'],
                  ready_for_backend=result['ready_for_backend'],failure_classification=result['failure']['classification'])
    result['artifacts']=report.data['artifacts']
    report.artifact('system_report.json',data=result)
    print_system_report(result,plan)
    return result['ready_for_backend']


def print_system_report(result,plan=None):
    print('\nAGENT 3 — FULL SYSTEM DIRECTED / RANDOM / TIMING STRESS')
    print('Vectors: {}'.format(len(plan['groups']) if plan else 'NOT_PLANNED'))
    print('VCS compile: '+('PASS' if result.get('compile_passed') else 'FAIL' if result.get('compile_invoked') else 'NOT_RUN'))
    print('VCS simulation: '+('PASS' if result.get('simulation_completed') else 'FAIL' if result['simulation'].get('simulation_invoked') else 'NOT_RUN'))
    for group in result.get('vector_results',[]):
        print('{} {}: {}'.format(group['vector_id'],group['category'],group['status']))
    for label,key in (('UVM_ERROR','uvm_error'),('UVM_FATAL','uvm_fatal'),('Scoreboard PASS','scoreboard_pass'),('Scoreboard FAIL','scoreboard_fail')):
        print('{}: {}'.format(label,result['simulation'].get(key) if result['simulation'].get(key) is not None else 'NOT_TESTED'))
    print('Traffic: '+json.dumps(result.get('traffic'),sort_keys=True))
    for name,row in result.get('timing',{}).items():
        print('{}: {} scope={} required={} comparisons={} min={} max={} boundary_hits={} early_host_attempts={} violations={}'.format(
            name,row['result'],row['scope'],row['required_cycles'],row['comparisons'],row['minimum_observed'],
            row['maximum_observed'],row['boundary_hits'],row['early_attempts'],row['violations']))
    print('AGENT 3 SYSTEM RESULT : '+result['status'])
    print('Readiness: '+result['verification_readiness'])
    if result.get('failure_detail'): print('Failure: '+result['failure_detail'])


def system_main(api,arguments):
    api=SystemAPI(api)
    parser=argparse.ArgumentParser(description='Agent 3 immutable-checker full-system stress verification')
    parser.add_argument('manifest'); parser.add_argument('--system',action='store_true',required=True)
    for name in ('agent1-result','agent2-result','uvm-environment'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--agent1-inputs')
    parser.add_argument('--seed',type=int,default=403404)
    parser.add_argument('--random-count',type=int,default=2048)
    parser.add_argument('--long-count',type=int,default=2048)
    parser.add_argument('--timeout',type=int,default=300)
    args=parser.parse_args(arguments)
    if args.timeout<1: parser.error('--timeout must be positive')
    def entrypoint(report):
        metadata=read_report_json(str(Path(report.run_dir)/'run.json'))
        metadata.update(mode='system',verification_scope='controller_functional_and_protocol_timing_simulation',ready_for_backend=False)
        write_report_json(str(Path(report.run_dir)/'run.json'),metadata)
        if not run_system_vectors(api,report,args): raise SystemExit(1)
    run_reported_agent('agent3',entrypoint,[args.manifest])
