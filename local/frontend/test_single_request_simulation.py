"""Independent trace and fault tests for the single-request simulation profile."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simulation.controller import CheckFailure
from simulation import single_request as single
from simulate_handoff import run


def shared():
    types = {name: {'kind':'record','fields':[{'name':n,'width':w,'lsb':b,'msb':b+w-1} for n,w,b in fields]}
             for name,fields in single.LAYOUTS.items()}
    types.update(opcode={'kind':'enum','width':3,'encodings':{'NOP':0,'ACT':1,'PRE':2,'READ':3,'WRITE':4}},
                 bank_rows={'kind':'array','count':4,'element_width':4,'packed_order':'ascending_index_from_lsb'},
                 bank_valid={'kind':'array','count':4,'element_width':1,'packed_order':'ascending_index_from_lsb'})
    return {'parameters':{'BANK_COUNT':4,'DATA_WIDTH':32,'HOST_ADDR_WIDTH':8,'ROW_WIDTH':4,'COLUMN_WIDTH':4,
                'REQUEST_CAPACITY':1,'READ_RESPONSE_LATENCY':1,'COMMAND_OPCODE_WIDTH':3,'TFAW_ACT_LIMIT':4,
                'HOST_BANK_WIDTH':2,'MEMORY_WORDS_PER_BANK':256},'types':types,
            'clock_reset':{'clock':'clk','reset':'rst_n','edge':'rising','active_level':0,'assertion_style':'synchronous'},
            'timing_profile':{'unit':'cycles','values':{'tRCD':3,'tRAS':5,'tRRD':2,'tFAW':8}}}


def fixture():
    ports=[{'name':n,'direction':d,'width':w} for n,(d,w) in single.HOST.items()]
    observations={'candidate_command':45,'candidate_valid':1,'candidate_ok':1,'open_rows':16,'open_valid':4,'engine_idle':1}
    def leaf(n,w=1,top=False):
        return {'endpoint':('top.' if top else 'core.')+n,'dut_path_suffix':n if top else 'core.'+n,
                'width':w,'lsb':0,'msb':w-1}
    def predicate(n,top=False):return dict(leaf(n,top=top),equals=1)
    events={name:{'status':'bound','when':predicate(signal,True),'payload':{}} for name,signal in
            (('command_commit','cmd_valid'),('response_delivery','rsp_valid'),('retirement','cmd_valid'))}
    events['request_admission']={'status':'bound','when':{'all':[predicate('txn_valid',True),predicate('txn_ready',True)]},
                                 'payload':{'request':leaf('incoming',43)}}
    return {'phase':'rtl','shared':shared(),'design':{'top_module':'fixture_top','top_ports':ports,
            'modules':[{'instance':'core','ports':[{'name':n,'width':w} for n,w in {**observations,'incoming':43}.items()]}]},
            'verification_intent':{'bindings':{'sampling':{'phase':'pre_edge','reset_gates_events':True},
               'events':events,'observations':{n:leaf(n,w) for n,w in observations.items()}}}}


def trace():
    """Hand-written write/blocked candidate/read; expectations never consult RTL."""
    rows=[]
    for c in range(14):
        s=dict.fromkeys(('txn_valid','txn_ready','txn_addr','txn_bank','txn_is_write','txn_wdata',
            'cmd_valid','cmd','rsp_valid','rsp_rdata','candidate_valid','candidate_command','candidate_ok',
            'open_rows','open_valid','engine_idle','request_admission','command_commit','response_delivery',
            'retirement','incoming_request'),0)
        s.update(cycle=c,rst_n=int(c>0))
        if c in (1,2,8,9,12,13):s.update(txn_ready=1,engine_idle=1)
        if c>=5:s.update(open_rows=1,open_valid=1)
        if c in (2,9):
            s.update(txn_valid=1,txn_addr=0x12,request_admission=1)
            if c==2:s.update(txn_is_write=1,txn_wdata=0x1234)
            s['incoming_request']=(0x12<<33)|((1<<32)|0x1234 if c==2 else 0)
        if c in (4,6,7,11):
            op=1 if c==4 else 3 if c==11 else 4
            command=(op<<42)|(1<<36)|((2<<32) if op in (3,4) else 0)|(0x1234 if op==4 else 0)
            s.update(candidate_valid=1,candidate_command=command,candidate_ok=int(c!=6))
            if c!=6:s.update(cmd_valid=1,cmd=command,command_commit=1,retirement=int(op in (3,4)))
        if c==6:s.update(txn_valid=1,txn_bank=3,txn_addr=255,txn_wdata=0xdeadbeef)
        if c>=12:s['rsp_rdata']=0x1234
        if c==12:s.update(rsp_valid=1,response_delivery=1)
        rows.append(s)
    return rows


class SingleRequestSimulationTests(unittest.TestCase):
    def replay(self,rows):
        oracle=single.SingleRequestOracle(shared())
        for row in rows:oracle.step(row)
        oracle.finish(rows[-1]['cycle'])
        return oracle

    def test_independent_trace_checks_capture_stall_and_response(self):
        oracle=self.replay(trace())
        self.assertEqual(oracle.counts['admissions'],2)
        self.assertEqual(oracle.counts['retirements'],2)
        self.assertEqual(oracle.counts['responses'],1)
        self.assertEqual(oracle.counts['blocked_candidate_edges'],1)
        self.assertEqual(oracle.counts['tRCD:boundary'],1)

    def test_injected_faults_fail_at_the_first_wrong_edge(self):
        cases=[(12,{'rsp_rdata':0xbad},'read_data'),(11,{'rsp_valid':1,'response_delivery':1},'response_latency'),
               (7,{'candidate_command':0},'candidate_hold'),(5,{'open_valid':0},'row_state'),
               (6,{'txn_ready':1,'txn_valid':0},'single_request_capacity'),
               (3,{'cmd':1},'command_payload'),(0,{'cmd_valid':1},'reset_gating'),
               (2,{'incoming_request':0},'admission_payload')]
        for c,changes,expected in cases:
            rows=trace();rows[c].update(changes)
            with self.subTest(check=expected),self.assertRaises(CheckFailure) as error:self.replay(rows)
            self.assertEqual(error.exception.check,expected)
            self.assertEqual(error.exception.cycle,c)

    def test_early_and_unnecessarily_late_timing_permission_are_both_rejected(self):
        for c,permit in ((6,1),(7,0)):
            rows=trace();r=rows[c]
            r.update(candidate_ok=permit,cmd_valid=permit,command_commit=permit,retirement=permit,
                     cmd=r['candidate_command'] if permit else 0)
            with self.subTest(cycle=c),self.assertRaises(CheckFailure) as error:self.replay(rows)
            self.assertEqual(error.exception.check,'timing_permission')

    def test_wrong_command_sequence_and_unowned_command_are_rejected(self):
        rows=trace();rows[4]['candidate_command']^=1<<40;rows[4]['cmd']=rows[4]['candidate_command']
        with self.assertRaises(CheckFailure) as error:self.replay(rows)
        self.assertEqual(error.exception.check,'captured_request_sequence')
        rows=trace();rows[2].update(txn_valid=0,request_admission=0)
        with self.assertRaises(CheckFailure) as error:self.replay(rows)
        self.assertEqual(error.exception.check,'request_ownership')

    def test_all_four_timing_minima_use_exclusive_window_boundary(self):
        oracle=single.SingleRequestOracle(shared())
        command={'opcode':3,'bank':0,'row':1}
        oracle.rows[0]=1;oracle.acts[0]=2
        self.assertFalse(oracle.allowed(command,4));self.assertTrue(oracle.allowed(command,5))
        command['opcode']=2
        self.assertFalse(oracle.allowed(command,6));self.assertTrue(oracle.allowed(command,7))
        command['opcode']=1;oracle.rows[0]=None;oracle.history=[2]
        self.assertFalse(oracle.allowed(command,3));self.assertTrue(oracle.allowed(command,4))
        oracle.history=[0,2,4,6];oracle.t['tRRD']=1
        self.assertFalse(oracle.allowed(command,7));self.assertTrue(oracle.allowed(command,8))

    def test_reset_cancels_work_and_clears_prior_memory_and_history(self):
        rows=trace()[:7]
        reset=copy.deepcopy(rows[0]);reset['cycle']=7
        rows.append(reset)
        oracle=self.replay(rows)
        self.assertEqual(oracle.counts['reset_canceled'],1)
        self.assertEqual(oracle.memory,{})
        self.assertEqual(oracle.history,[])
        self.assertIsNone(oracle.active)
        with self.assertRaises(CheckFailure) as error:self.replay(trace()[:7])
        self.assertEqual(error.exception.check,'bounded_completion')

    def test_trace_results_require_actual_scenario_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'trace'
            rows=trace();fields=['cycle', *[k for k in rows[0] if k != 'cycle']]
            path.write_text(' '.join(fields)+'\n'+''.join(' '.join(f'{r[k]:x}' for k in fields)+'\n' for r in rows))
            self.assertEqual(single.check_trace(path,shared(),'backpressure_capture')['status'],'passed')
            result=single.check_trace(path,shared(),'timing_row_conflict')
            self.assertEqual(result['status'],'failed')
            self.assertEqual(result['failure']['check'],'scenario_coverage')

    def test_profile_rejects_wrong_config_refresh_layout_reset_and_missing_events(self):
        mutations=[lambda h:h['shared']['parameters'].update(REQUEST_CAPACITY=2),
                   lambda h:h['shared']['timing_profile']['values'].update(tRFC=10),
                   lambda h:h['shared']['types']['opcode']['encodings'].update(REF=5),
                   lambda h:h['shared']['clock_reset'].update(assertion_style='asynchronous'),
                   lambda h:h['shared']['types']['request']['fields'][0].update(lsb=40),
                   lambda h:h['design']['top_ports'].pop(),
                   lambda h:h['verification_intent']['bindings']['events'].pop('request_admission')]
        for mutate in mutations:
            handoff=fixture();mutate(handoff)
            with self.subTest(mutation=mutate),self.assertRaises(ValueError):single.profile_check(handoff)
        self.assertEqual(single.profile_check(fixture()),single.HOST)

    def test_generated_driver_uses_8bit_address_and_declared_port_bindings(self):
        handoff=fixture();tb=single.testbench(handoff)
        self.assertIn("txn_addr = 8'(addr_value)",tb)
        self.assertIn('dut.core.candidate_command',tb)
        self.assertIn('SIMULATION_CYCLE_LIMIT',tb)
        handoff['verification_intent']['bindings']['observations']['open_rows']['dut_path_suffix']='core.private'
        with self.assertRaises(ValueError):single.testbench(handoff)

    def test_seeded_scenarios_preserve_config_and_include_full_address_space(self):
        config=shared();before=copy.deepcopy(config)
        cases=single.scenarios(config,4)
        self.assertEqual(cases,single.scenarios(config,4))
        self.assertNotEqual(cases['backpressure_capture'],single.scenarios(config,5)['backpressure_capture'])
        reads=[(b,a) for op,w,b,a,data in cases['reset_zero'] if op==2]
        self.assertEqual(set(reads),{(b,a) for b in range(4) for a in range(256)})
        memory=cases['memory_row_paths']
        first_read=next(i for i,(op,w,b,a,d) in enumerate(memory) if op==2 and not w)
        self.assertEqual({b for op,w,b,a,d in memory[:first_read] if op==2 and w},{0,1,2,3})
        self.assertEqual(config,before)

    def test_runner_requires_explicit_profile_and_validated_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'handoff.json';path.write_text(json.dumps(fixture()))
            with patch('simulate_handoff.validate_handoff',side_effect=ValueError('tampered handoff')) as validate, \
                 patch('simulate_handoff.subprocess.run') as process:
                result=run(path,root/'out',profile=single.PROFILE)
            validate.assert_called_once();process.assert_not_called()
            self.assertEqual(result['status'],'invalid_input')
            self.assertIn('tampered',result['error'])
            with patch('simulate_handoff.validate_handoff'):
                result=run(path,root/'default')
            self.assertEqual(result['profile'],'ddr4-fifo-v1')
            self.assertEqual(result['status'],'invalid_input')


if __name__=='__main__':unittest.main()
