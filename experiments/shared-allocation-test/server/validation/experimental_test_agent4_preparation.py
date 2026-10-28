"""Stage-aware supervision, including cache-seeded but unpublished proof plans."""
import io
import json
import os
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import agent4_lec as lec
import agent4_orchestration as safety
from agent4_progress import Progress
import test_agent4_reliability as fixtures

class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReliabilityTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.agent,self.work=self.fixture.parent();self.root=self.work.parent
        self.runtime=lec.read_json(self.root/'runtime_report.json')
        (self.root/'runtime_report.json').unlink()
        self.agent.runtime_payload=self.root/'runtime_payload.json'
        self.agent.finish_runtime=lambda:None
        self.stream=io.StringIO();self.observer=Progress(self.agent,stream=self.stream)
        self.agent._live_progress=self.observer
        self.addCleanup(lambda:self.observer.finish({'equivalence':'INCONCLUSIVE'}))
    def child(self,duration,publish=True):
        names=list(self.runtime['identity']['design']['partitions'])
        # Reproduce native make-script generation and cache seeding BEFORE the
        # expensive admission/context work publishes the verified plan.
        for name in names:(self.work/'strategies'/name/'prove/status').write_text('PASS (persistent cache)\n')
        (self.work/'summary_targets.list').write_text(''.join('strategies/'+n+'/prove/status\n' for n in names))
        (self.root/'published.json').write_text(json.dumps(self.runtime))
        Path(self.agent.eqy).write_text('#!'+sys.executable+'\nimport time,os\nfrom pathlib import Path\n'+
            'time.sleep('+repr(duration)+')\n'+
            ('os.replace('+repr(str(self.root/'published.json'))+','+repr(str(self.root/'runtime_report.json'))+')\ntime.sleep(.25)\n' if publish else ''))
        self.agent.runtime_options.update(io_seconds=1.,orchestration_idle_seconds=.15,preparation_seconds=5.)
    def test_quiet_three_second_preparation_exceeds_one_second_io_budget(self):
        self.child(3.)
        with patch('agent4_lec.WRAPPER_GRACE',.1):execution=self.agent.run_eqy()
        self.assertEqual(execution['return_code'],0);self.assertFalse(execution['termination'])
        text=self.stream.getvalue()
        sequence=['Stage 3/8: PASS','Stage 4/8: Partition','Stage 4/8: PASS','Stage 5/8: PASS-cache']
        positions=[text.index(s) for s in sequence];self.assertEqual(positions,sorted(positions))
    def test_seeded_cache_summary_cannot_end_runtime_preparation(self):
        self.child(.6);self.agent.runtime_options['io_seconds']=.2
        with patch('agent4_lec.WRAPPER_GRACE',.1):execution=self.agent.run_eqy()
        self.assertFalse(execution['stalled_strategy_directories']);self.assertEqual(execution['stop_reason'],'eqy_exit')
    def test_scheduler_idle_does_not_apply_at_59_seconds_of_stage4(self):
        now=[0.];guard=safety.SafetyState(120.,clock=lambda:now[0])
        snapshot=dict(preparing=True,scheduler_ready=False,transition='preparing',active_preparation=1,active_solver=0,ready=0,blocked=0,unresolved=0)
        guard.update(snapshot);now[0]=59.;self.assertIsNone(guard.update(snapshot)[1])
        now[0]=150.;self.assertIsNone(guard.update(snapshot)[1])
        snapshot.update(preparing=False,scheduler_ready=True,transition='ready',unresolved=1,ready=1,active_preparation=0)
        self.assertIsNone(guard.update(snapshot)[1]);now[0]=271.
        self.assertEqual(guard.update(snapshot)[1],'ORCHESTRATION_STALL')
    def test_active_preparation_and_solver_are_distinct(self):
        self.child(.1)
        snapshot=safety.orchestration_snapshot(self.work,require_runtime=True)
        self.assertTrue(snapshot['preparing']);self.assertFalse(snapshot['scheduler_ready'])
        self.assertNotIn('summary',snapshot.get('pending',{}))
    def test_preparation_deadline_has_explicit_stage4_terminal_failure(self):
        self.child(3.,publish=False);self.agent.runtime_options['preparation_seconds']=.2
        with patch('agent4_lec.WRAPPER_GRACE',.1):
            with self.assertRaises(safety.InfrastructureError) as raised:self.agent.run_eqy()
        self.assertEqual(raised.exception.state,'PREPARATION_TIMEOUT')
        result=safety.infrastructure_result(self.agent,raised.exception);self.observer.finish(result)
        self.assertEqual(result['failure_stage'],4);self.assertTrue(result['failure_detail'])
        self.assertEqual(result['triggering_supervisor'],'preparation_deadline')
        self.assertEqual(result['signal'],'SIGTERM');self.assertEqual(result['exit_code'],1)
        self.assertFalse(safety.pid_alive(result['execution']['termination']['pid']))
        text=self.stream.getvalue();self.assertLess(text.index('Stage 4/8: FAIL'),text.index('Stage 8/8:'))
    def test_actual_blocked_io_is_io_timeout_with_machine_reason(self):
        fifo=self.root/'blocked_fifo';os.mkfifo(str(fifo));self.agent._active_stage=4
        # A real blocked open/read, rather than shutil's early FIFO rejection.
        with patch('agent4_orchestration.IO_PROGRAM','import sys;sys.stdin.read();open('+repr(str(fifo))+').read()'):
            with self.assertRaises(safety.InfrastructureError) as raised:
                safety.io_operations([dict(kind='copy',source=str(fifo),path=str(self.root/'never_written'))],.2)
        self.assertEqual(raised.exception.state,'IO_TIMEOUT')
        result=safety.infrastructure_result(self.agent,raised.exception)
        self.assertEqual(result['triggering_supervisor'],'bounded_io');self.assertTrue(result['io_timeout'])
        self.assertTrue(result['failure_detail']);self.assertEqual(result['failure_stage'],4)
        self.assertFalse(safety.pid_alive(result['failure_evidence']['helper_pid']))
        self.assertEqual(lec.read_json(self.root/'report.json')['failure_classification'],'AGENT4_IO_TIMEOUT')
    def test_successful_child_without_verified_runtime_plan_is_explained(self):
        self.child(.1,publish=False)
        with self.assertRaises(safety.InfrastructureError) as raised:self.agent.run_eqy()
        self.assertEqual(raised.exception.state,'TOOL_ERROR')
        self.observer.finish(safety.infrastructure_result(self.agent,raised.exception))
        self.assertIn('Stage 4/8: FAIL',self.stream.getvalue())
    def test_native_counterexample_exit_still_reaches_formal_classification(self):
        self.child(.1,publish=False)
        del self.agent.runtime_payload
        # Native EQY can discover a counterexample and exit nonzero before its
        # summary exists. Preparation still completed; the parser decides FAIL.
        (self.work/'summary_targets.list').unlink()
        (self.work/'partition.list').write_text('partition top.result\n')
        Path(self.agent.eqy).write_text('#!'+sys.executable+'\nraise SystemExit(2)\n')
        execution=self.agent.run_eqy()
        self.assertEqual(execution['return_code'],2)
        self.assertIn('Stage 4/8: PASS',self.stream.getvalue())
        self.assertNotIn('infrastructure_status',execution)
    def test_outer_timeout_finalization_cannot_skip_stage4_terminal_result(self):
        self.observer.stage(4)
        self.agent.execution=dict(timed_out=True,termination=dict(supervisor='global_deadline',reason='Global proof deadline exceeded'))
        self.observer.stage(8)
        self.observer.stage(4,True) # A late observer cannot turn failure into PASS.
        text=self.stream.getvalue()
        self.assertLess(text.index('Stage 4/8: FAIL | classification=TIMEOUT'),text.index('Stage 8/8:'))
        self.assertNotIn('Stage 4/8: PASS',text)
    def test_preparation_setting_is_not_part_of_formal_identity(self):
        payload=lec.read_json(self.root/'runtime_payload.json');changed=dict(payload,preparation_seconds=999)
        config=self.runtime['runtime_config']
        self.assertEqual(lec.formal_runtime_identity(self.work,config,payload,{}),lec.formal_runtime_identity(self.work,config,changed,{}))
