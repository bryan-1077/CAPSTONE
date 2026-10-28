"""Agent 3 host-plan, authentication, timing, fault and pipeline regressions.

VCS is mocked. Passing these tests is not a real SystemVerilog compile/sim.
"""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent3_testvectors as vectors
import run_pipeline as pipeline
import test_system_agent2 as agent2_tests
from test_system_agent2 import simulation_fixture
from controller_stress_vectors import build_stress_plan, discover_item_contract, vector_text
from controller_timing_verification import discover_timing_inventory, check_timing, trace_records
from system_vector_sources import build_extension
from system_vector_verification import (SystemAPI, authenticate_handoff, evaluate_group,
    functional_clean, coverage_evidence, aggregate_results, validate_plan_budget, run_system_vectors,
    frontend_feedback, run_command)


def controller(banks=4, depth=4, policy='open_page', refresh=True):
    return {'configuration':{'bank_count':banks,'page_policy':policy,'refresh_enabled':refresh,
            'trrd_enabled':True,'tfaw_enabled':True}, 'top_module':'ddr4_controller_top',
            'constants':{'req_queue_depth':depth,'row_width':2,'col_width':2,'slow_service_cycles':3},
            'widths':{'bank':max(1,(banks-1).bit_length()),'address':4,'data':32},
            'timing_cycles':{'tRRD':6,'tFAW':40,'tRCD':22,'tRP':22,'tRAS':52,'tREFI':12480},
            'tfaw_limit':4,'component_metadata':[], 'observability':{'act':'act_pulse','refresh':'ref_req'},
            'sources':{'module_specs':[]}}


def item(contract):
    return {'roles':{'bank':'bank','address':'address','data':'data','write':'is_write',
                    'kind':'kind','cycles':'cycles','scenario':'scenario'},
            'control_encoding':{'request':0,'idle':1,'reset':2,'drain':3},
            'widths':contract['widths'],'item_type':'dut_item','sequence_parent':'dut_sequence'}


def rule(name='tRRD', required=6, kind='spacing', mandatory=True, classification='APPLICABLE'):
    return {'rule':name,'required_cycles':required,'kind':kind,'mandatory':mandatory,
            'classification':classification,'scope':'logical_ACT_admission','activation_limit':4,
            'observers':[],'reason':None}


def acts(*cycles): return [dict(event='act',cycle=c,bank=i%4) for i,c in enumerate(cycles)]


class PlanTests(unittest.TestCase):
    def plan(self, **kwargs):
        cfg=controller(**kwargs)
        return build_stress_plan(cfg,item(cfg),[rule(),rule('tFAW',40,'window')],random_count=128,long_count=128)

    def group(self, identity, **kwargs):
        return next(g for g in self.plan(**kwargs)['groups'] if g['vector_id']==identity)

    def requests(self,g): return [s['fields'] for s in g['steps'] if s['op']=='request']

    def test_dynamic_bank_count(self):
        for banks in (1,2,7,16):
            plan=self.plan(banks=banks)
            self.assertEqual(plan['bank_count'],banks)
            self.assertTrue(all(s['fields']['bank']<banks for g in plan['groups'] for s in g['steps']))

    def test_queue_capacity_boundaries(self):
        for depth in (1,3,13):
            plan=self.plan(depth=depth)
            self.assertEqual([g['pressure_transactions'] for g in plan['groups'][:3]],[depth-1,depth,depth+1])

    def test_depth_one_empty_case_has_checker_witness(self):
        group=self.group('QUEUE-001',depth=1)
        self.assertEqual(group['pressure_transactions'],0)
        self.assertEqual(group['transactions'],2)
        self.assertEqual([s['is_write'] for s in self.requests(group)],[1,0])

    def test_consecutive_requests_have_no_completion_waits(self):
        group=self.group('CONSECUTIVE-001')
        pressure=group['steps'][2:2+group['pressure_transactions']]
        self.assertTrue(all(s['op']=='request' and s['issue_relationship']=='next_ready_cycle' for s in pressure))

    def test_multiple_outstanding_pressure_exceeds_capacity(self):
        self.assertGreater(self.group('OUTSTANDING-001')['pressure_transactions'],self.plan()['queue_depth'])

    def test_full_queue_at_completion_has_explicit_coverage(self):
        self.assertEqual(self.group('QUEUE-006')['expected_observation'],'full_completion')

    def test_simultaneous_accept_completion_group(self):
        self.assertEqual(self.group('SIMULTANEOUS-001')['expected_observation'],'simultaneous')

    def test_bank_isolation_barrier_then_readback(self):
        group=self.group('BANK-003',banks=7)
        traffic=self.requests(group)
        self.assertEqual({s['bank'] for s in traffic if s['is_write']},set(range(7)))
        self.assertEqual({s['bank'] for s in traffic if not s['is_write']},set(range(7)))
        self.assertGreater(len({s['data'] for s in traffic if s['is_write']}),7)
        self.assertIn('drain',[s['op'] for s in group['steps'][3:-3]])

    def test_same_bank_pattern(self):
        self.assertEqual({s['bank'] for s in self.requests(self.group('BANK-001'))},{0})

    def test_cross_bank_pattern(self):
        self.assertEqual({s['bank'] for s in self.requests(self.group('BANK-002',banks=7))},set(range(7)))

    def test_single_bank_cross_bank_is_not_applicable(self):
        self.assertEqual(self.group('BANK-002',banks=1)['status'],'NOT_APPLICABLE')

    def test_row_hit_pattern(self):
        self.assertEqual({s['address']>>2 for s in self.requests(self.group('ROW-001'))},{0})

    def test_row_replacement_pattern(self):
        self.assertGreater(len({s['address']>>2 for s in self.requests(self.group('ROW-002'))}),1)

    def test_open_page_selection(self):
        self.assertEqual(self.group('ROW-001')['status'],'PENDING')
        self.assertEqual(self.group('ROW-003')['status'],'NOT_APPLICABLE')

    def test_closed_page_selection(self):
        self.assertEqual(self.group('ROW-001',policy='closed_page')['status'],'NOT_APPLICABLE')
        self.assertEqual(self.group('ROW-003',policy='closed_page')['status'],'PENDING')

    def test_refresh_enabled_has_multiple_opportunities(self):
        group=self.group('REFRESH-001')
        self.assertGreater(sum(s['fields']['cycles'] for s in group['steps'] if s['op']=='idle'),3*12480)

    def test_refresh_disabled_omits_traffic(self):
        group=self.group('REFRESH-001',refresh=False)
        self.assertEqual(group['status'],'NOT_APPLICABLE'); self.assertEqual(group['steps'],[])

    def test_reset_outstanding_then_restart(self):
        group=self.group('RESET-001'); ops=[s['op'] for s in group['steps']]
        second=ops.index('reset',1)
        self.assertIn('request',ops[2:second]); self.assertIn('request',ops[second+1:])

    def test_pending_read_reset_gate(self):
        self.assertIn('wait_read_completion',[s['op'] for s in self.group('RESET-002')['steps']])

    def test_active_service_reset_gate(self):
        self.assertIn('wait_active_dispatch',[s['op'] for s in self.group('RESET-003')['steps']])
        self.assertTrue(self.group('RESET-003')['mandatory_coverage'])

    def test_real_vcs_padded_active_reset_gate_is_coverage(self):
        # Real TAMU VCS output: dispatch at 10, reset at 11, one cancellation.
        text = ('AGENT3_GATE_HIT|vector=RESET-003|gate=       active_service|cycle=10\n'
                'AGENT3_TIMING_EVENT|event=reset|cycle=11|vector=RESET-003\n')
        result = {'traffic': {'cancelled_by_reset': 1}}
        evidence = coverage_evidence(self.group('RESET-003'), result, text, {})
        self.assertTrue(evidence['observed'])
        self.assertEqual(evidence['status'], 'PASS')
        for missing in ('', text.replace('AGENT3_GATE_HIT', 'AGENT3_GATE_MISSED'),
                        text.replace('active_service', 'pending_read_response')):
            self.assertFalse(coverage_evidence(self.group('RESET-003'), result, missing, {})['observed'])
        result['traffic']['cancelled_by_reset'] = 0
        self.assertFalse(coverage_evidence(self.group('RESET-003'), result, text, {})['observed'])

    def test_reproducible_random_seed(self):
        cfg=controller(); a=build_stress_plan(cfg,item(cfg),[],seed=44,random_count=64,long_count=64)
        b=build_stress_plan(cfg,item(cfg),[],seed=44,random_count=64,long_count=64)
        c=build_stress_plan(cfg,item(cfg),[],seed=45,random_count=64,long_count=64)
        self.assertEqual(a,b); self.assertNotEqual(a['groups'][-1]['steps'],c['groups'][-1]['steps'])

    def test_random_and_long_counts_configurable(self):
        cfg=controller(); plan=build_stress_plan(cfg,item(cfg),[],random_count=71,long_count=79)
        self.assertEqual(plan['groups'][-2]['pressure_transactions'],79)
        self.assertEqual(plan['groups'][-1]['pressure_transactions'],71)

    def test_invalid_seed_rejected(self):
        cfg=controller()
        with self.assertRaises(ValueError): build_stress_plan(cfg,item(cfg),[],seed=-1)

    def test_vector_encoding_uses_discovered_fields(self):
        cfg=controller(); discovered=item(cfg)
        discovered['roles']={k:'custom_'+v for k,v in discovered['roles'].items()}
        plan=build_stress_plan(cfg,discovered,[],random_count=2,long_count=2)
        text=vector_text(plan['groups'][0],discovered)
        self.assertIn('QUEUE-001',text); self.assertTrue(all(len(line.split())==7 for line in text.splitlines()))

    def test_extension_inherits_original_test_lifecycle(self):
        cfg=controller(); sources=build_extension(item(cfg),cfg,[])
        package=sources['agent3_system_pkg.sv']
        self.assertIn('extends dut_test',package); self.assertIn('super.build_phase(phase)',package)
        self.assertNotIn('task run_phase',package); self.assertNotIn('class dut_scoreboard',package)
        self.assertIn('set_type_override',package)

    def test_extension_does_not_contain_functional_model(self):
        cfg=controller(); text='\n'.join(build_extension(item(cfg),cfg,[]).values())
        self.assertNotIn('bank_mem',text); self.assertNotIn('expected_data',text)
        self.assertIn('AGENT3_TIMING_EVENT',text)


class TimingTests(unittest.TestCase):
    def check(self, specification, records, issues=()):
        return check_timing([specification],records,issues)[specification['rule']]

    def test_trace_records_normalizes_vcs_token_padding(self):
        records = trace_records('AGENT3_GATE_HIT|vector=RESET-003|gate=       active_service|cycle=10',
                                'AGENT3_GATE_HIT')
        self.assertEqual(records[0]['gate'], 'active_service')
        self.assertEqual(records[0]['cycle'], 10)

    def test_trrd_exact_boundary(self):
        row=self.check(rule(),acts(1,7,13))
        self.assertEqual((row['comparisons'],row['boundary_hits'],row['violations']),(2,2,0))

    def test_trrd_violation(self):
        row=self.check(rule(),acts(1,6)); self.assertEqual(row['result'],'FAIL'); self.assertEqual(row['minimum_observed'],5)

    def test_tfaw_rolling_windows(self):
        row=self.check(rule('tFAW',40,'window'),acts(0,6,12,18,40,46,52))
        self.assertEqual(row['comparisons'],3); self.assertEqual(row['boundary_hits'],3)

    def test_tfaw_violation(self):
        self.assertEqual(self.check(rule('tFAW',40,'window'),acts(0,6,12,18,39))['violations'],1)

    def timer(self,name,required):
        specification=rule(name,required,'delay',False)
        rows=[{'event':'start','rule':name,'channel':'bank0','cycle':3},
              {'event':'end','rule':name,'channel':'bank0','cycle':3+required}]
        self.assertEqual(self.check(specification,rows)['result'],'PASS')
        rows[-1]['cycle']-=1
        self.assertEqual(self.check(specification,rows)['violations'],1)

    def test_trcd_when_applicable(self): self.timer('tRCD',22)
    def test_trp_when_applicable(self): self.timer('tRP',22)
    def test_tras_when_applicable(self): self.timer('tRAS',52)
    def test_twr_when_applicable(self): self.timer('tWR',10)

    def test_tccd_when_applicable(self):
        for name in ('tCCD_S','tCCD_L'):
            r=rule(name,4,'spacing',False)
            row=self.check(r,[dict(event='column',rule=name,cycle=c) for c in (0,4,7)])
            self.assertEqual((row['comparisons'],row['violations']),(2,1))

    def test_not_observable_never_passes(self):
        self.assertEqual(self.check(rule(classification='NOT_OBSERVABLE'),acts(0,6))['result'],'NOT_OBSERVABLE')

    def test_one_act_is_not_timing_proof(self):
        self.assertEqual(self.check(rule(),acts(0))['result'],'NOT_TESTED')

    def test_reset_clears_timing_history(self):
        records=acts(10,16)+[dict(event='reset',cycle=17)]+acts(1,7)
        row=self.check(rule(),records); self.assertEqual((row['violations'],row['comparisons']),(0,2))

    def test_bank_timer_channels_independent(self):
        r=rule('tRCD',22,'delay',False)
        events=[dict(rule='tRCD',event='start',cycle=0,channel='a'),dict(rule='tRCD',event='start',cycle=3,channel='b'),
                dict(rule='tRCD',event='end',cycle=22,channel='a'),dict(rule='tRCD',event='end',cycle=25,channel='b')]
        self.assertEqual(self.check(r,events)['boundary_hits'],2)

    def test_early_host_attempt_is_not_violation(self):
        row=self.check(rule(),acts(0,6),[dict(cycle=2)])
        self.assertEqual(row['early_attempts'],1); self.assertEqual(row['violations'],0)

    def test_tfaw_early_attempt_uses_full_window(self):
        row=self.check(rule('tFAW',40,'window'),acts(0,6,12,18,40),[dict(cycle=39),dict(cycle=40)])
        self.assertEqual(row['early_attempts'],2)

    def test_early_coverage_does_not_cross_resets(self):
        text='AGENT3_TIMING_EVENT|event=act|cycle=8\nAGENT3_TIMING_EVENT|event=reset|cycle=10\nAGENT3_ISSUE|cycle=9'
        row=self.check(rule(),trace_records(text,'AGENT3_TIMING_EVENT'),trace_records(text,'AGENT3_ISSUE'))
        self.assertEqual(row['early_attempts'],0)

    def test_refresh_exclusion(self):
        row=self.check(rule('refresh_blocking',0,'exclusion'),[dict(event='refresh_sample',cycle=1,value=1,dispatch=1)])
        self.assertEqual(row['violations'],1)

    def test_unknown_dispatch_during_refresh_is_not_pass(self):
        row=self.check(rule('refresh_blocking',0,'exclusion'),[dict(event='refresh_sample',cycle=1,value=1,dispatch=None)])
        self.assertEqual(row['result'],'NOT_TESTED'); self.assertEqual(row['comparisons'],0)

    def test_generic_timer_discovery_from_new_parameter(self):
        cfg=controller(); cfg['configuration'].update(trrd_enabled=False,tfaw_enabled=False,refresh_enabled=False)
        cfg['timing_cycles']={}
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'timers.sv'; source.write_text('module timer; if (CUSTOM_DELAY_counter >= 9) x=1; endmodule')
            definitions=[dict(name='ddr4_controller_top',source_file=str(source),ports=[],parameters=[],
                              instantiated_modules=[dict(module='timer',name='delay_i',connections='',array=False)]),
                         dict(name='timer',source_file=str(source),ports=[dict(name='start'),dict(name='CUSTOM_DELAY_done')],parameters=[],instantiated_modules=[])]
            manifest={'timing':{'profile':{},'module_parameters':{'timer':{'CUSTOM_DELAY_CYCLES':9}}}}
            rows=discover_timing_inventory(manifest,cfg,{'module_definitions':definitions})
            custom=next(r for r in rows if r['rule']=='CUSTOM_DELAY')
            self.assertEqual(custom['classification'],'APPLICABLE'); self.assertEqual(custom['required_cycles'],9)

    def test_bad_rtl_timer_threshold_does_not_replace_expectation(self):
        cfg=controller(); cfg['configuration'].update(trrd_enabled=False,tfaw_enabled=False,refresh_enabled=False)
        cfg['timing_cycles']={'tRCD':22}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad_timer.sv'; path.write_text('module timer; if (tRCD_counter >= 1) done=1; endmodule')
            definition=dict(name='ddr4_controller_top',source_file=str(path),ports=[dict(name='start'),dict(name='tRCD_done')],parameters=[],instantiated_modules=[])
            rows=discover_timing_inventory({'timing':{'profile':{},'module_parameters':{}}},cfg,{'module_definitions':[definition]})
            discovered=next(r for r in rows if r['rule']=='tRCD')
            self.assertEqual(discovered['required_cycles'],22); self.assertEqual(discovered['classification'],'APPLICABLE')
            self.assertEqual(discovered['observers'][0]['rtl_literal_threshold'],1)

    def test_missing_bank_timer_channel_is_not_pass(self):
        r=rule('tRCD',22,'delay'); r['observers']=[dict(channel=0),dict(channel=1)]
        row=self.check(r,[dict(rule='tRCD',event='start',cycle=0,channel=0),dict(rule='tRCD',event='end',cycle=22,channel=0)])
        self.assertEqual(row['result'],'NOT_TESTED'); self.assertEqual(row['missing_observer_channels'],[1])


class HandoffAndRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper=agent2_tests.SystemAgent2Tests('test_unseen_controller_invokes_llm_with_approved_inputs')
        cls.helper.setUp(); cls.helper.approve()
        _,cls.baseline,cls.baseline_report,_,_=cls.helper.run_agent2()
        cls.api=SystemAPI(vectors)
        cls.paths=(cls.helper.manifest_path,cls.helper.agent1_path,
                   Path(cls.baseline_report.directory)/'system_report.json',Path(cls.baseline['uvm_environment_file']))
        cls.context=authenticate_handoff(cls.api,*cls.paths)

    @classmethod
    def tearDownClass(cls): cls.helper.doCleanups()

    @contextlib.contextmanager
    def changed(self,path,text):
        path=Path(path); original=path.read_text(); path.write_text(text)
        try: yield
        finally: path.write_text(original)

    def auth(self): return authenticate_handoff(self.api,*self.paths)

    def metadata_rejection(self,key,value):
        data=json.loads(self.paths[3].read_text()); data[key]=value
        with self.changed(self.paths[3],json.dumps(data)):
            with self.assertRaises(ValueError): self.auth()

    def test_valid_saved_checker_accepted(self):
        self.assertEqual(self.auth()['saved']['checker_identity'],self.baseline['checker_identity'])

    def test_each_modified_checker_file_rejected(self):
        for row in self.context['saved']['testbench_files']:
            with self.subTest(file=Path(row['path']).name), self.changed(row['path'],Path(row['path']).read_text()+'\n// modified\n'):
                with self.assertRaises(ValueError): self.auth()

    def test_modified_rtl_rejected(self):
        path=Path(self.context['agent1']['rtl_files'][0])
        with self.changed(path,path.read_text()+'\n// RTL revision\n'):
            with self.assertRaises(ValueError): self.auth()

    def test_modified_manifest_rejected(self):
        with self.changed(self.paths[0],self.paths[0].read_text()+'\n '):
            with self.assertRaises(ValueError): self.auth()

    def test_configuration_hash_rejected(self): self.metadata_rejection('configuration_hash','wrong')
    def test_contract_hash_rejected(self): self.metadata_rejection('contract_hash','wrong')
    def test_catalog_hash_rejected(self): self.metadata_rejection('requirement_hash','wrong')
    def test_checker_identity_rejected(self): self.metadata_rejection('checker_identity','wrong')
    def test_rtl_inventory_rejected(self): self.metadata_rejection('rtl_files',self.context['saved']['rtl_files'][:-1])
    def test_saved_compile_invalid_rejected(self): self.metadata_rejection('checker_compile_valid',False)
    def test_agent2_failure_rejected(self): self.metadata_rejection('agent2_result','FAIL')
    def test_saved_simulation_missing_rejected(self): self.metadata_rejection('simulation_completed',False)

    def test_baseline_result_log_mismatch_rejected(self):
        result=copy.deepcopy(self.baseline); result['traffic']['accepted']+=1
        with self.changed(self.paths[2],json.dumps(result)):
            with self.assertRaises(ValueError): self.auth()

    def test_dynamic_timing_inventory(self):
        inventory=discover_timing_inventory(self.helper.manifest,self.context['contract'],self.context['agent1'],self.context['requirements'])
        rows={r['rule']:r for r in inventory}
        for name in ('tRRD','tFAW','tRCD','tRP','tRAS','refresh_blocking'):
            self.assertEqual(rows[name]['classification'],'APPLICABLE')
        for name in ('tRCD','tRP','tRAS'):
            self.assertEqual(len(rows[name]['observers']),self.context['contract']['configuration']['bank_count'])
            self.assertEqual(rows[name]['scope'],'bank_FSM_start_to_done_minimum')
        self.assertEqual(rows['tCL']['classification'],'NOT_OBSERVABLE')

    def test_default_stress_fits_saved_watchdog(self):
        cfg=self.context['contract']; inventory=discover_timing_inventory(self.helper.manifest,cfg,self.context['agent1'])
        plan=build_stress_plan(cfg,self.context['item'],inventory)
        validate_plan_budget(plan,self.context['files']['dut_pkg.sv'],cfg,inventory)
        self.assertTrue(all(g.get('estimated_cycle_budget',0)<g.get('checker_watchdog_cycles',1) for g in plan['groups']))

    def test_overbudget_plan_rejected_before_vcs(self):
        cfg=self.context['contract']; inv=discover_timing_inventory(self.helper.manifest,cfg,self.context['agent1'])
        plan=build_stress_plan(cfg,self.context['item'],inv,long_count=16384)
        with self.assertRaises(ValueError): validate_plan_budget(plan,self.context['files']['dut_pkg.sv'],cfg,inv)

    def test_discovered_item_fields_can_differ(self):
        files={k:v.replace('.bank','.target_bank').replace(' bank;',' target_bank;') for k,v in self.context['files'].items()}
        found=discover_item_contract(self.api,files,self.context['contract'])
        self.assertEqual(found['roles']['bank'],'target_bank')

    def group(self):
        return {'vector_id':'TEST-001','seed':7,'transactions':2,'expected_observation':'mixed',
                'steps':[{'op':'request','fields':{'bank':0,'address':9,'is_write':1,'data':0x1234}},
                         {'op':'request','fields':{'bank':0,'address':9,'is_write':0,'data':0}}],
                'applicable_configuration':self.context['contract']['configuration'],'timing_requirements':[]}

    def text(self,bad=False):
        return simulation_fixture(self.context['requirements'],bad)+'\nAGENT3_RUNTIME_START|file=test\nAGENT3_BEGIN|vector=TEST-001\nAGENT3_ISSUE|cycle=1|bank=0|address=9|write=1|data=1234\nAGENT3_ISSUE|cycle=5|bank=0|address=9|write=0|data=0\nAGENT3_END|vector=TEST-001|steps=6\n'

    def evaluate(self,text,ok=True,inventory=()):
        return evaluate_group(self.api,self.context,self.group(),text,ok,inventory)

    def test_clean_actual_checker_replay_passes(self):
        self.assertEqual(self.evaluate(self.text())['functional_status'],'PASS')

    def test_uvm_error_propagates_failure(self):
        case=self.evaluate(self.text().replace('UVM_ERROR : 0','UVM_ERROR : 1'))
        self.assertEqual(case['status'],'FAIL'); self.assertEqual(case['failure_classification'],'AGENT3_INFRASTRUCTURE_FAILURE')

    def test_uvm_fatal_propagates_failure(self):
        self.assertEqual(self.evaluate(self.text().replace('UVM_FATAL : 0','UVM_FATAL : 1'))['status'],'FAIL')

    def test_scoreboard_failure_propagates(self):
        self.assertEqual(self.evaluate(self.text().replace('SCOREBOARD PASS=15 FAIL=0','SCOREBOARD PASS=15 FAIL=1'))['status'],'FAIL')

    def test_lost_request_propagates(self):
        text='\n'.join(line for line in self.text().splitlines() if not ('event=completed|id=2' in line or 'event=response|id=2' in line))
        self.assertEqual(self.evaluate(text)['status'],'FAIL')

    def test_duplicate_request_propagates(self):
        text=self.text()+'\n'+next(line for line in self.text().splitlines() if 'event=completed|id=1' in line)
        self.assertEqual(self.evaluate(text)['status'],'FAIL')

    def test_data_mismatch_propagates_rtl_fault(self):
        case=self.evaluate(self.text(True))
        self.assertEqual(case['failure_classification'],'RTL_FUNCTIONAL_FAILURE')

    def test_missing_runtime_is_infrastructure(self):
        self.assertEqual(self.evaluate('UVM_ERROR : 0\nUVM_FATAL : 0')['failure_classification'],'AGENT3_INFRASTRUCTURE_FAILURE')

    def test_exit_zero_without_drain_is_not_pass(self):
        text=self.text().replace('AGENT2_SYSTEM_END|','MISSING_END|')
        self.assertEqual(self.evaluate(text)['status'],'FAIL')

    def test_vector_count_mismatch_is_infrastructure(self):
        self.assertEqual(self.evaluate(self.text().replace('AGENT3_ISSUE|cycle=5','MISSING_ISSUE|cycle=5'))['failure_classification'],'AGENT3_INFRASTRUCTURE_FAILURE')

    def test_wrong_stimulus_payload_blocks_pass(self):
        case=self.evaluate(self.text().replace('AGENT3_ISSUE|cycle=5|bank=0|address=9','AGENT3_ISSUE|cycle=5|bank=0|address=a'))
        self.assertFalse(case['stimulus_payloads_verified']); self.assertEqual(case['failure_classification'],'AGENT3_INFRASTRUCTURE_FAILURE')

    def test_unknown_uvm_counts_are_not_zero(self):
        case=self.evaluate(self.text().replace('UVM_ERROR : 0','MISSING_UVM_ERROR : 0'))
        self.assertEqual(case['status'],'FAIL')

    def miniature_plan(self):
        group=self.group()
        group.update(status='PENDING',mandatory_coverage=True,category='mixed',purpose='mocked runner regression',evidence_paths=[])
        for step in group['steps']: step['fields'].update(kind=0,cycles=1)
        return {'groups':[group],'top_module':'ddr4_controller_top',
                'configuration':self.context['contract']['configuration'],'item_contract':self.context['item']}

    def arguments(self):
        return SimpleNamespace(manifest=str(self.paths[0]),agent1_result=str(self.paths[1]),
            agent2_result=str(self.paths[2]),uvm_environment=str(self.paths[3]),agent1_inputs=None,
            seed=7,random_count=8,long_count=8,timeout=30)

    def report(self):
        run=pipeline.create_report_run([str(self.paths[0])],requested=('agent3',),metadata_extra={'mode':'system'})
        return pipeline.AgentReport('agent3',run_dir=run)

    def test_runner_compiles_exact_checker_and_separate_extension_without_llm(self):
        report=self.report(); commands=[]
        before={r['path']:Path(r['path']).read_bytes() for r in self.context['saved']['testbench_files']}
        def execute(command,directory,timeout):
            commands.append(command)
            if '-o' in command:
                Path(command[command.index('-o')+1]).write_text('mocked simulator')
                return True,'mocked compile',None
            return True,self.text(),None
        with patch('system_vector_verification.discover_timing_inventory',return_value=[]), \
             patch('system_vector_verification.build_stress_plan',return_value=self.miniature_plan()), \
             patch('system_vector_verification.shutil.which',return_value='mock-vcs'), \
             patch('system_vector_verification.run_command',side_effect=execute), \
             patch.object(self.api.uvm,'call_llm',side_effect=AssertionError('Agent 3 must not invoke LLM')), \
             contextlib.redirect_stdout(io.StringIO()):
            ready=run_system_vectors(self.api,report,self.arguments())
        self.assertTrue(ready); self.assertEqual(len(commands),2)
        self.assertIn('+UVM_TESTNAME=agent3_system_test',commands[1])
        self.assertIn('agent3_system_bootstrap',commands[0])
        for original,contents in before.items():
            self.assertEqual(Path(original).read_bytes(),contents)
            self.assertEqual((Path(report.directory)/'checker_sources'/Path(original).name).read_bytes(),contents)
        result=json.loads((Path(report.directory)/'system_report.json').read_text())
        self.assertEqual(result['simulation']['uvm_error'],0); self.assertTrue(result['ready_for_backend'])

    def test_compile_failure_is_infrastructure_without_frontend_feedback(self):
        report=self.report()
        with patch('system_vector_verification.discover_timing_inventory',return_value=[]), \
             patch('system_vector_verification.build_stress_plan',return_value=self.miniature_plan()), \
             patch('system_vector_verification.shutil.which',return_value='mock-vcs'), \
             patch('system_vector_verification.run_command',return_value=(False,'extension syntax error',None)), \
             contextlib.redirect_stdout(io.StringIO()):
            ready=run_system_vectors(self.api,report,self.arguments())
        result=json.loads((Path(report.directory)/'system_report.json').read_text())
        self.assertFalse(ready); self.assertEqual(result['failure']['classification'],'AGENT3_INFRASTRUCTURE_FAILURE')
        self.assertIsNone(result['simulation']['uvm_error']); self.assertFalse((Path(report.directory)/'frontend_feedback.json').exists())

    def test_handoff_failure_creates_infrastructure_report_before_vcs(self):
        report=self.report(); args=self.arguments(); args.uvm_environment='/tmp/nonexistent-phase4-environment.json'
        with patch('system_vector_verification.run_command') as execute,contextlib.redirect_stdout(io.StringIO()):
            ready=run_system_vectors(self.api,report,args)
        self.assertFalse(ready); self.assertFalse(execute.called)
        result=json.loads((Path(report.directory)/'system_report.json').read_text())
        self.assertEqual(result['failure_stage'],'handoff_authentication')

    def test_default_pipeline_runs_three_stages_and_stops_before_backend(self):
        called=[]
        def execute(stage,command,run_dir,environment):
            called.append(stage); report=pipeline.AgentReport(stage,run_dir=run_dir)
            if stage=='agent1': pipeline.write_report_json(environment['RTL_VALIDATION_RESULT_FILE'],self.context['agent1'])
            elif stage=='agent2': report.artifact('system_report.json',data=self.baseline)
            else:
                self.assertEqual(stage,'agent3'); self.assertIn('--system',command)
                self.assertEqual(command[command.index('--uvm-environment')+1],str(self.paths[3]))
                self.assertIn('--agent1-inputs',command); self.assertIn('--agent2-result',command)
                report.artifact('system_report.json',data={'mode':'system','ready_for_backend':True,
                    'functional_status':'PASS','protocol_timing_status':'PASS','verification_readiness':'READY_FOR_BACKEND','failure':{}})
            report.update(verification_status='PASS'); report.finish(0); return 0
        with patch.object(pipeline,'execute_reported_process',side_effect=execute),contextlib.redirect_stdout(io.StringIO()):
            pipeline.run_system_pipeline(self.paths[0],{'manifest':self.helper.manifest})
        self.assertEqual(called,['agent1','agent2','agent3'])
        latest=pipeline.read_report_json(self.paths[0].parent/'verification_reports/latest.json')
        self.assertTrue(latest['ready_for_backend']); self.assertTrue(latest['verification_complete'])

    def feedback(self,case):
        with tempfile.TemporaryDirectory() as directory:
            run=pipeline.create_report_run([str(self.paths[0])],requested=('agent3',),run_dir=Path(directory)/'report')
            report=pipeline.AgentReport('agent3',run_dir=run)
            case['paths']={'simulation_log':'saved.log','waveform':'saved.vcd'}
            result={'failure':{'classification':case['failure_classification']}}
            path=frontend_feedback(report,self.context,result,[case])
            return json.loads(Path(path).read_text())

    def test_timing_violation_generates_frontend_feedback(self):
        text=self.text()+'\nAGENT3_TIMING_EVENT|event=act|cycle=10\nAGENT3_TIMING_EVENT|event=act|cycle=15'
        case=self.evaluate(text,inventory=[rule()]); package=self.feedback(case)
        self.assertEqual(case['failure_classification'],'RTL_TIMING_PROTOCOL_FAILURE')
        self.assertEqual(package['disposition'],'RETURN_TO_FRONTEND_RTL_FIX')
        self.assertEqual(package['failures'][0]['timing_failures'][0]['measured_cycles'],5)

    def test_functional_fault_generates_frontend_feedback(self):
        package=self.feedback(self.evaluate(self.text(True)))
        self.assertEqual(package['failure_classification'],'RTL_FUNCTIONAL_FAILURE')
        self.assertTrue(package['failures'][0]['transaction_history'])
        self.assertFalse(package['automatic_module_diagnostics_run'])

    def test_mandatory_unobservable_blocks_backend(self):
        case=self.evaluate(self.text()); plan={'groups':[dict(self.group(),status='PASS')],'top_module':'ddr4_controller_top','configuration':controller()['configuration']}
        result=aggregate_results(plan,[case],[rule(classification='NOT_OBSERVABLE')])
        self.assertFalse(result['ready_for_backend']); self.assertIn('tRRD',result['mandatory_timing_missing'])

    def test_optional_unobservable_is_gap(self):
        case=self.evaluate(self.text()); plan={'groups':[dict(self.group(),status='PASS')],'top_module':'ddr4_controller_top','configuration':controller()['configuration']}
        result=aggregate_results(plan,[case],[rule('tCL',22,mandatory=False,classification='NOT_OBSERVABLE')])
        self.assertEqual(result['status'],'PASS_WITH_GAPS'); self.assertTrue(result['ready_for_backend'])

    def test_mandatory_vector_coverage_gap_blocks_backend(self):
        case=self.evaluate(self.text()); case['coverage'].update(observed=False,gap='unexercised')
        plan={'groups':[dict(self.group(),status='PASS',mandatory_coverage=True)],'top_module':'ddr4_controller_top','configuration':controller()['configuration']}
        result=aggregate_results(plan,[case],[])
        self.assertFalse(result['ready_for_backend']); self.assertEqual(result['mandatory_vector_coverage_missing'],['TEST-001'])

    def test_module_mode_dispatch_remains_compatible(self):
        with patch.object(sys,'argv',['agent3_testvectors.py','sample.sv']),patch.object(vectors,'run_reported_agent') as call:
            vectors.main()
        self.assertEqual(call.call_args[0][0],'agent3'); self.assertEqual(call.call_args[0][1],vectors._main)

    def test_system_dispatch_is_separate(self):
        with patch.object(sys,'argv',['agent3_testvectors.py','manifest.json','--system']),patch('system_vector_verification.system_main') as call:
            vectors.main()
        self.assertEqual(call.call_args[0][1],['manifest.json','--system'])

    def test_dynamic_rtl_inventory_with_additional_modules(self):
        helper=agent2_tests.SystemAgent2Tests('test_unseen_controller_invokes_llm_with_approved_inputs')
        helper.setUp()
        try:
            initial=len(helper.manifest['rtl_files'])
            for i in range(2): (helper.manifest_path.parent/'extra_{}.sv'.format(i)).write_text('module extra_{}; endmodule\n'.format(i))
            helper.rebuild(); helper.approve(); _,result,report,_,_=helper.run_agent2()
            ctx=authenticate_handoff(self.api,helper.manifest_path,helper.agent1_path,Path(report.directory)/'system_report.json',result['uvm_environment_file'])
            self.assertEqual(len(ctx['contract']['_rtl_files']),initial+2)
        finally: helper.doCleanups()


class PipelineTests(unittest.TestCase):
    def test_final_three_agent_aggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pipeline.create_report_run([directory],run_dir=Path(directory)/'run',metadata_extra={'mode':'system','top_module':'ddr4_controller_top'})
            for name in ('agent1','agent2','agent3'):
                report=pipeline.AgentReport(name,run_dir=run); report.update(verification_status='PASS'); report.finish(0)
            pipeline.write_report_json(Path(run)/'agent2/system_report.json',{'ready_for_agent3':True})
            pipeline.write_report_json(Path(run)/'agent3/system_report.json',{'ready_for_backend':True,'functional_status':'PASS','protocol_timing_status':'PASS','failure':{}})
            with contextlib.redirect_stdout(io.StringIO()): result=pipeline.finalize_system_validation(run)
            self.assertTrue(result['ready_for_backend']); self.assertFalse(result['backend_executed'])
            self.assertTrue(pipeline.read_report_json(Path(run)/'summary.json')['verification_complete'])

    def test_partial_run_cannot_be_backend_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pipeline.create_report_run([directory],requested=('agent1','agent2'),run_dir=Path(directory)/'run',metadata_extra={'mode':'system','top_module':'ddr4_controller_top'})
            with contextlib.redirect_stdout(io.StringIO()): result=pipeline.finalize_system_validation(run)
            self.assertFalse(result['ready_for_backend'])

    def test_pipeline_error_revokes_previously_passing_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            run=pipeline.create_report_run([directory],run_dir=Path(directory)/'run',metadata_extra={
                'mode':'system','top_module':'ddr4_controller_top','pipeline_error':'inputs changed',
                'verification_readiness':'READY_FOR_BACKEND'})
            for name in ('agent1','agent2','agent3'):
                report=pipeline.AgentReport(name,run_dir=run); report.update(verification_status='PASS'); report.finish(0)
            pipeline.write_report_json(Path(run)/'agent2/system_report.json',{'ready_for_agent3':True})
            pipeline.write_report_json(Path(run)/'agent3/system_report.json',{'ready_for_backend':True,
                'functional_status':'PASS','protocol_timing_status':'PASS','failure':{}})
            with contextlib.redirect_stdout(io.StringIO()): result=pipeline.finalize_system_validation(run)
            self.assertFalse(result['ready_for_backend']); self.assertEqual(result['status'],'FAIL')
            self.assertEqual(result['verification_readiness'],'BLOCKED_SYSTEM_VALIDATION')

    def test_compile_timeout_is_infrastructure_with_partial_output(self):
        import subprocess
        with patch('system_vector_verification.subprocess.run',side_effect=subprocess.TimeoutExpired('vcs',10,output=b'partial compile')):
            ok,output,error=run_command(['vcs'],'/tmp',10)
        self.assertFalse(ok); self.assertEqual(output,'partial compile'); self.assertIn('timed out',error)


if __name__=='__main__': unittest.main()
