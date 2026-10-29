"""Agent 4 lineage, preparation and formal-result regressions.

Most formal subprocesses are mocked; real-tool tests use small temporary designs.
PASS here is not a real controller LEC proof.
"""
import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

import agent4_lec
import run_pipeline as pipeline
import test_system_agent2 as agent2_tests
from system_uvm_verification import input_fingerprints
from agent4_lec import (LECInputError, authenticate_validation, load_backend_handoff,
                               file_rows, read_json, inspect_sources)
from agent4_lec import (SystemLECAgent, classify_proof, reset_wrapper,
                       validate_ports, validate_state_clock, system_main,
                       validate_llm_repair_integrity, is_recoverable_setup_error)
from types import SimpleNamespace


class ExperimentalSetupFixTests(unittest.TestCase):
    def test_system_slang_paths_are_literal_unquoted_tokens(self):
        agent = SystemLECAgent.__new__(SystemLECAgent)
        agent.read_verilog_defer = False
        for frontend in ('slang', 'slang_builtin'):
            lines = agent.read_commands(['/tmp/reference/0_top.sv'], 'top', ['/tmp/includes'], frontend)
            self.assertEqual(lines[-1], 'read_slang -I/tmp/includes /tmp/reference/0_top.sv --top top')
            self.assertEqual('plugin -i slang' in lines, frontend == 'slang')

    def test_slang_rejects_unsafe_tokens_and_native_verilog_keeps_quoting(self):
        agent = SystemLECAgent.__new__(SystemLECAgent)
        agent.read_verilog_defer = False
        for path in ('/tmp/design with spaces/top.sv', '/tmp/a;exec.sv', '/tmp/a"b.sv', '/tmp/a\nb.sv'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'single-token'):
                agent.read_commands([path], 'top', [], 'slang')
        native = agent.read_commands(['/tmp/design with spaces/top.sv'], 'top', [], 'verilog')
        self.assertIn('"/tmp/design with spaces/top.sv"', native[-1])

    def test_plain_fenced_and_reasoning_wrapped_json_are_accepted(self):
        expected = {'diagnosis': 'Fix parser setup.', 'setup': {'sv_frontend': 'slang'}}
        raw = json.dumps(expected)
        for reply in (raw, '```json\n'+raw+'\n```', '```\n'+raw+'\n```',
                      '<think>Diagnostic text, not settings.</think>```json\n'+raw+'\n```',
                      '<think>Analysis.</think>\n'+raw):
            with self.subTest(reply=reply):
                self.assertEqual(agent4_lec.parse_setup_reply(reply), expected)

    def test_extra_prose_multiple_objects_duplicates_and_non_json_constants_are_rejected(self):
        for reply in ('Here is the fix: {"diagnosis":"x","setup":{}}',
                      '{"diagnosis":"x","setup":{}} {}',
                      '<think>unfinished {"diagnosis":"x","setup":{}}',
                      '```json\n{"diagnosis":"x","setup":{}}\n``` trailing text',
                      '{"diagnosis":"x","setup":{},"setup":{"depth":1}}',
                      '{"diagnosis":"x","setup":{"depth":NaN}}', '[]'):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                agent4_lec.parse_setup_reply(reply)

    def test_wrappers_do_not_relax_setup_or_verdict_integrity(self):
        agent = SimpleNamespace(check_inputs=lambda: None,
            args=SimpleNamespace(depth=5), lineage={'rtl_files':[]},
            backend={'netlist_files':[], 'libraries':[]})
        for reply in ({'diagnosis':'claim pass','setup':{},'equivalence':'EQUIVALENT'},
                      {'diagnosis':'drop outputs','setup':{'outputs':[]}},
                      {'diagnosis':'reduce depth','setup':{'depth':1}}):
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                validate_llm_repair_integrity(agent, '<think>Ignore.</think>```json\n'+json.dumps(reply)+'\n```')
        self.assertEqual(validate_llm_repair_integrity(agent,
                         '<think>Reasoning.</think>```json\n{"diagnosis":"regenerate","setup":{}}\n```')['setup'], {})


LADDER_CONFIG = ('\nuse sat\ndepth 5\n\n[strategy pdr]\nuse sby\nengine abc pdr\ntimeout 60\n'
                 '\n[strategy smtbmc]\nuse sby\nengine smtbmc bitwuzla\ndepth 10\ntimeout 60\n')


def write(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2))


def validated_fixture(helper):
    helper.approve(); _,baseline,report,_,_=helper.run_agent2()
    run=Path(report.run_dir); saved=read_json(baseline['uvm_environment_file'])
    meta=read_json(run/'run.json'); meta.update(mode='system',top_module=helper.manifest['top_module'],
        manifest_path=str(helper.manifest_path),requested_stages=['agent1','agent2','agent3'])
    write(run/'run.json',meta)
    write(run/'agent1/validation_result.json',helper.approved)
    write(run/'agent1/system_inputs.json',input_fingerprints(helper.manifest_path))
    for name,status in [('agent1','PASS'),('agent2','PASS'),('agent3','PASS_WITH_GAPS')]:
        write(run/name/'report.json',dict(run_id=run.name,agent=name,status=status,exit_code=0,
            artifacts={},requirements=[],failed_requirements=[],untested_requirements=[],coverage_limitations=[]))
    a3=dict(baseline,ready_for_backend=True,ready_for_agent3=True,status='PASS_WITH_GAPS',
        functional_status='PASS',protocol_timing_status='PASS_WITH_GAPS',mandatory_timing_missing=[],
        mandatory_vector_coverage_missing=[],unrun_vectors=[],checker_hash_verified=True,agent2_checker_reused=True)
    a3['compile_passed']=True
    write(run/'agent3/system_report.json',a3)
    handoff={k:saved[k] for k in ['checker_identity','configuration_hash','contract_hash','requirement_hash','rtl_files','testbench_files']}
    handoff.update(environment=baseline['uvm_environment_file'],manifest_sha256=saved['source_manifest_sha256'])
    write(run/'agent3/authenticated_handoff.json',handoff)
    write(run/'final_system_report.json',dict(mode='system',top_module=helper.manifest['top_module'],
        status='PASS_WITH_GAPS',ready_for_backend=True,functional_rtl='PASS',protocol_timing_compliance='PASS_WITH_GAPS',
        agents=dict(agent1='PASS',agent2='PASS',agent3='PASS_WITH_GAPS')))
    return run


def yosys_ports(approved):
    result={}; bit=2
    for port in approved:
        result[port['name']]={'direction':port['direction'],'bits':list(range(bit,bit+port['width']))}
        bit+=port['width']
    return result


def proof_tree(work,status='PASS',global_status=None,base_fail=False):
    work=Path(work); strategy=work/'strategies/output/induction'; strategy.mkdir(parents=True,exist_ok=True)
    (work/'partition.list').write_text('agent4_formal_top output : rsp_rdata\n')
    (strategy/'status').write_text(status+'\n')
    (strategy/'run.log').write_text('SAT temporal induction proof finished - model found for base case: FAIL!\n' if base_fail else
                                 'Induction step proven: SUCCESS!\n' if status=='PASS' else 'Reached maximum number of time steps -> proof failed.\n')
    if base_fail: (strategy/'trace.vcd').write_text('$comment regression trace $end\n')
    (work/(global_status or ('PASS' if status=='PASS' else 'FAIL'))).touch()
    return work


class ProcessCleanupTests(unittest.TestCase):
    """Real isolated process trees, including EQY/SBY-style nested groups."""
    SCRIPT = '''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
root=Path(__file__).parent
mode=(root/'mode').read_text()
role=sys.argv[1] if len(sys.argv)>1 and sys.argv[1] in ('child','grand') else 'root'
if role!='root': os.setpgrp()
work=root/'eqy_work'
def finish(number,frame):
    (root/('term_'+role)).write_text('SIGTERM')
    if role=='root' and mode=='stalled':
        (work/'strategies/top.y/smtbmc/status').write_text('ERROR\\n')
    sys.exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN if role=='grand' and mode=='stubborn' else finish)
(root/('pid_'+role)).write_text(json.dumps(dict(pid=os.getpid(),group=os.getpgrp(),session=os.getsid(0),makeflags=os.environ.get('MAKEFLAGS'))))
if role!='grand':
    child=subprocess.Popen([sys.executable,__file__,'child' if role=='root' else 'grand'])
    while not (root/'pid_grand').exists(): time.sleep(.01)
if role=='root':
    work.mkdir(exist_ok=True)
    (work/'partition.list').write_text('top top.y : y\\n')
    def record(strategy,status,nested=False):
        p=work/'strategies/top.y'/strategy
        if nested: p=p/'top.y'
        p.mkdir(parents=True,exist_ok=True); (p/'status').write_text(status+'\\n')
    if mode.startswith('parallel'):
        parts=['top.A','top.B','top.C']
        (work/'partition.list').write_text(''.join('top '+p+' : y\\n' for p in parts))
        (work/'summary_targets.list').write_text(''.join('strategies/'+p+'/smtbmc/status\\n' for p in parts))
        jobs=[subprocess.Popen([sys.executable,str(root/'parallel_worker.py'),name]) for name in ('A','B','C')]
        if mode=='parallel_normal':
            for job in jobs: job.wait()
            (work/'FAIL').touch(); (root/'finish').touch(); child.wait(); sys.exit(1)
    else: record('quick_sat','UNKNOWN')
    if mode in ('stalled','summary','nonfinal'):
        record('pdr','TIMEOUT 0 1' if mode=='nonfinal' else 'TIMEOUT',mode=='nonfinal')
        if mode!='nonfinal': record('smtbmc','TIMEOUT 0 3' if mode=='stalled' else 'TIMEOUT',mode=='stalled')
        (work/'summary_targets.list').write_text('strategies/top.y/smtbmc/status\\n')
    if mode=='normal':
        (root/'finish').touch(); child.wait(); sys.exit(0)
    if mode=='leave': sys.exit(0)
while not (root/'finish').exists(): time.sleep(.02)
if role=='child': child.wait()
'''

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.script=self.root/'fake_eqy'; self.script.write_text(self.SCRIPT); self.script.chmod(0o700)
        (self.root/'parallel_worker.py').write_text('''import json, os, sys, time
from pathlib import Path
root=Path(__file__).parent; name=sys.argv[1]; os.setpgrp()
(root/('pid_'+name)).write_text(json.dumps(dict(pid=os.getpid(),session=os.getsid(0))))
strategy=root/'eqy_work/strategies'/('top.'+name)/'smtbmc'
job=strategy/('top.'+name); job.mkdir(parents=True)
(job/'logfile.txt').write_text('Started SBY proof engine')
if name=='A': time.sleep(.05)
else:
    while not (root/'engine_done_A').exists(): time.sleep(.01)
    time.sleep(.7 if name=='B' else 1.1)
status='UNKNOWN' if name=='B' else 'TIMEOUT'
(job/'status').write_text(status+' 0 1\\n')
(root/('engine_done_'+name)).write_text(str(time.monotonic()))
if name=='A' and (root/'mode').read_text()=='parallel_stuck':
    while True: time.sleep(.1)
time.sleep(.15)
(strategy/'status').write_text(status+'\\n')
(root/('wrapper_done_'+name)).touch()
''')
        self.addCleanup(self.cleanup_tree)

    @staticmethod
    def alive(pid):
        try: return Path('/proc/{}/stat'.format(pid)).read_text().rsplit(')',1)[1].split()[0] not in ('Z','X')
        except FileNotFoundError: return False

    def wait_for(self,predicate):
        deadline=time.monotonic()+30
        while not predicate():
            if time.monotonic()>deadline: self.fail('Timed out waiting for process fixture')
            time.sleep(.02)

    def cleanup_tree(self):
        path=self.root/'pid_root'
        if path.exists(): agent4_lec.terminate_eqy_session(read_json(path)['session'],grace=.1)

    def assert_tree_stopped(self):
        records=[read_json(self.root/('pid_'+role)) for role in ('root','child','grand')]
        self.wait_for(lambda: all(not self.alive(r['pid']) for r in records))
        self.assertEqual({r['session'] for r in records},{records[0]['pid']})
        self.assertEqual(len({r['group'] for r in records}),3)

    def run_agent(self,mode,timeout=2):
        (self.root/'mode').write_text(mode)
        agent=agent4_lec.LEC_Agent(str(self.root/'rtl.v'),str(self.root/'mapped.v'),top_module='top',
                                  eqy=str(self.script),strategy='sat',timeout=timeout)
        agent.config=str(self.root/'formal.eqy')
        agent.report=SimpleNamespace(directory=str(self.root),artifact=lambda *a,**k: None)
        with patch('agent4_lec.shutil.which',return_value=sys.executable),patch('agent4_lec.WRAPPER_GRACE',.3):
            agent.run_eqy()
        return agent

    def test_normal_completion_leaves_no_descendants(self):
        with patch.dict(os.environ,{'MAKEFLAGS':'-j4'}): agent=self.run_agent('normal')
        self.assertEqual(agent.execution['return_code'],0)
        self.assertEqual(agent.execution['process_cleanup']['sigkill_groups'],[])
        self.assertEqual(read_json(self.root/'pid_root')['makeflags'],'-j4')
        self.assert_tree_stopped()

    def test_cleanup_refuses_agent_session(self):
        with patch('agent4_lec.os.killpg') as kill:
            with self.assertRaisesRegex(RuntimeError,'Agent 4 session'):
                agent4_lec.terminate_eqy_session(os.getsid(0),grace=0)
        kill.assert_not_called()

    def test_reused_leader_pid_cannot_target_another_session(self):
        with patch('agent4_lec.process_identity',return_value='new_start_time'),patch('agent4_lec.os.killpg') as kill:
            result=agent4_lec.terminate_eqy_session(999999,identity='original_start_time')
        kill.assert_not_called()
        self.assertEqual(result['remaining_groups'],[])
        for error in (FileNotFoundError,ProcessLookupError):
            with patch('agent4_lec.Path.read_text',side_effect=error):
                self.assertIsNone(agent4_lec.process_identity(999999))

    def test_normal_exit_also_cleans_abandoned_children(self):
        agent=self.run_agent('leave')
        self.assertEqual(agent.execution['return_code'],0)
        self.assertTrue(agent.execution['process_cleanup']['sigterm_groups'])
        self.assert_tree_stopped()

    def test_outer_timeout_kills_descendants_and_is_never_nonequivalent(self):
        agent=self.run_agent('timeout')
        self.assertTrue(agent.execution['timed_out'])
        agent.parse_results()
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertEqual(agent.formal['result'],'TIMEOUT')
        self.assertEqual(agent.formal['counterexamples'],[])
        self.assert_tree_stopped()

    def test_stubborn_grandchild_requires_sigkill(self):
        agent=self.run_agent('stubborn')
        self.assertTrue(agent.execution['process_cleanup']['sigkill_groups'])
        self.assertTrue((self.root/'term_child').is_file())
        self.assertFalse((self.root/'term_grand').exists())
        self.assert_tree_stopped()

    def test_cleanup_does_not_signal_unrelated_session(self):
        other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            self.run_agent('timeout')
            self.assertIsNone(other.poll())
            self.assert_tree_stopped()
        finally: other.kill(); other.wait()

    def interrupted_controller(self,number=None,abnormal=False,unlimited=False):
        (self.root/'mode').write_text('stubborn')
        controller=self.root/'controller.py'
        controller.write_text('''import os, sys, time, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agent4_lec import LEC_Agent
root=Path(__file__).parent
agent=LEC_Agent(str(root/'rtl.v'),str(root/'mapped.v'),top_module='top',eqy=str(root/'fake_eqy'),strategy='sat',timeout=30)
agent.config=str(root/'formal.eqy'); agent.report=SimpleNamespace(directory=str(root),artifact=lambda *a,**k:None)
if len(sys.argv)>1:
    def abort():
        while not (root/'pid_grand').exists(): time.sleep(.02)
        os._exit(7)
    threading.Thread(target=abort,daemon=True).start()
with patch('agent4_lec.shutil.which',return_value=sys.executable): agent.run_eqy()
''')
        if unlimited:controller.write_text(controller.read_text().replace('timeout=30','timeout=0'))
        with (self.root/'controller.log').open('w') as log:
            runner=subprocess.Popen([sys.executable,str(controller)]+(['abort'] if abnormal else []),
                                    stdout=log,stderr=log,start_new_session=True)
            try:
                self.wait_for(lambda:(self.root/'pid_grand').exists())
                if number is not None: runner.send_signal(number)
                runner.wait(timeout=15)
            finally:
                if runner.poll() is None: runner.kill(); runner.wait()
        self.assert_tree_stopped()
        return runner.returncode

    def test_ctrl_c_cleans_all_descendant_groups(self):
        previous=signal.getsignal(signal.SIGINT)
        self.assertNotEqual(self.interrupted_controller(signal.SIGINT),0)
        self.assertEqual(signal.getsignal(signal.SIGINT),previous)

    def test_zero_global_timeout_allows_normal_completion(self):
        agent=self.run_agent('normal',timeout=0)
        self.assertFalse(agent.execution['timed_out']);self.assert_tree_stopped()

    def test_zero_global_timeout_keeps_ctrl_c_cleanup(self):
        self.assertNotEqual(self.interrupted_controller(signal.SIGINT,unlimited=True),0)

    def test_zero_global_timeout_keeps_sigterm_cleanup(self):
        self.assertEqual(self.interrupted_controller(signal.SIGTERM,unlimited=True),128+signal.SIGTERM)

    def test_sigterm_cleans_all_descendant_groups(self):
        self.assertEqual(self.interrupted_controller(signal.SIGTERM),128+signal.SIGTERM)

    def test_abnormal_agent_exit_is_cleaned_by_pipe_watcher(self):
        self.assertEqual(self.interrupted_controller(abnormal=True),7)

    def test_agent_sigkill_is_cleaned_by_pipe_watcher(self):
        self.assertEqual(self.interrupted_controller(signal.SIGKILL),-signal.SIGKILL)

    def test_unexpected_exception_cleans_tree_and_restores_handlers(self):
        original=agent4_lec.EQYProcessSession.start
        handlers={n:signal.getsignal(n) for n in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
        def launch(session,*args,**kwargs):
            original(session,*args,**kwargs)
            self.wait_for(lambda:(self.root/'pid_grand').exists())
            raise RuntimeError('unexpected supervisor exception')
        # Mandatory supervision failures still clean the complete tree. Optional
        # status-reader exceptions now have separate nonfatal-worker coverage.
        with patch.object(agent4_lec.EQYProcessSession,'start',new=launch):
            with self.assertRaisesRegex(RuntimeError,'unexpected supervisor exception'):
                self.run_agent('timeout',timeout=10)
        self.assertEqual(handlers,{n:signal.getsignal(n) for n in handlers})
        self.assert_tree_stopped()

    def test_terminal_sby_timeout_does_not_wait_for_outer_timeout(self):
        agent=self.run_agent('stalled',timeout=20)
        agent.parse_results()
        self.assertFalse(agent.execution['timed_out'])
        self.assertEqual(agent.execution['stop_reason'],'stalled_sby_wrapper')
        self.assertLess(agent.execution['runtime_seconds'],10)
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertEqual(agent.failure_classification,'AGENT4_PROOF_INCOMPLETE')
        rows=agent.formal['partition_statuses']['top.y']
        self.assertTrue(any(r['strategy']=='smtbmc' and r['status']=='TIMEOUT' for r in rows))
        self.assert_tree_stopped()

    def test_exhausted_eqy_summary_cleans_stuck_make(self):
        agent=self.run_agent('summary',timeout=20)
        agent.parse_results()
        self.assertFalse(agent.execution['timed_out'])
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertEqual(agent.formal['counterexamples'],[])
        self.assertEqual(agent.execution['stop_reason'],'stalled_eqy_summary')
        self.assert_tree_stopped()

    def test_stuck_nonfinal_sby_waits_for_outer_timeout_without_inventing_later_proof(self):
        agent=self.run_agent('nonfinal',timeout=2)
        agent.parse_results()
        self.assertTrue(agent.execution['timed_out'])
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertEqual([r['strategy'] for r in agent.formal['partition_statuses']['top.y']],['pdr','quick_sat'])
        self.assert_tree_stopped()

    def assert_parallel_results(self,agent):
        agent.parse_results()
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertFalse(agent.execution['timed_out'])
        for name,status in [('A','TIMEOUT'),('B','UNKNOWN'),('C','TIMEOUT')]:
            self.assertEqual([r['status'] for r in agent.formal['partition_statuses']['top.'+name]],[status])
            record=read_json(self.root/('pid_'+name))
            self.assertEqual(record['session'],read_json(self.root/'pid_root')['session'])
            self.wait_for(lambda: not self.alive(record['pid']))
        self.assertTrue((self.root/'wrapper_done_B').is_file())
        self.assertTrue((self.root/'wrapper_done_C').is_file())
        first=float((self.root/'engine_done_A').read_text())
        self.assertGreater(float((self.root/'engine_done_B').read_text())-first,.3)
        self.assertGreater(float((self.root/'engine_done_C').read_text())-first,.3)
        self.assert_tree_stopped()

    def test_parallel_timeout_waits_for_other_strategies_and_normal_eqy_exit(self):
        agent=self.run_agent('parallel_normal',timeout=20)
        self.assert_parallel_results(agent)
        self.assertEqual(agent.execution['stop_reason'],'eqy_exit')
        self.assertTrue((self.root/'wrapper_done_A').is_file())

    def test_parallel_stuck_sby_and_make_clean_up_only_after_other_jobs_finish(self):
        other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            agent=self.run_agent('parallel_stuck',timeout=20)
            self.assert_parallel_results(agent)
            self.assertEqual(agent.execution['stop_reason'],'stalled_sby_wrapper')
            self.assertIsNone(other.poll())
            rows=agent.formal['partition_statuses']['top.A']
            self.assertEqual(rows[0]['status_source'],'sby_terminal')
            self.assertIsNone(rows[0]['wrapper_status'])
        finally: other.kill(); other.wait()


class CompletionWatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name)
        (self.work/'partition.list').write_text(''.join('top top.'+p+' : y\n' for p in ('A','B','C')))
        self.targets=['strategies/top.'+p+'/smtbmc/status' for p in ('A','B','C')]
        (self.work/'summary_targets.list').write_text('\n'.join(self.targets)+'\n')

    def record(self,part,status,nested=False,strategy='smtbmc'):
        path=self.work/'strategies'/('top.'+part)/strategy
        if nested: path=path/('top.'+part)
        path.mkdir(parents=True,exist_ok=True); (path/'status').write_text(status+'\n')
        return path

    def test_one_terminal_strategy_never_releases_parallel_completion_barrier(self):
        for status in ('PASS','FAIL','UNKNOWN','TIMEOUT'):
            with self.subTest(status=status):
                self.record('A',status,nested=True)
                self.assertEqual(agent4_lec.pending_eqy_wrappers(self.work),{})

    def test_partial_summary_does_not_hide_unfinished_partitions(self):
        (self.work/'summary_targets.list').write_text(self.targets[0]+'\n')
        self.record('A','TIMEOUT',nested=True)
        self.assertEqual(agent4_lec.pending_eqy_wrappers(self.work),{})

    def test_all_final_targets_and_all_started_jobs_must_be_terminal(self):
        for part in ('A','B','C'): self.record(part,'TIMEOUT')
        running=self.work/'strategies/top.B/pdr/top.B'
        running.mkdir(parents=True); (running/'logfile.txt').write_text('still running')
        self.assertEqual(agent4_lec.pending_eqy_wrappers(self.work),{})
        (running/'status').write_text('TIMEOUT 0 1\n')
        self.assertIn('summary',agent4_lec.pending_eqy_wrappers(self.work))

    def test_unrun_strategy_directories_do_not_count_as_started(self):
        for part in ('A','B','C'): self.record(part,'UNKNOWN')
        (self.work/'strategies/top.B/unrun').mkdir()
        self.assertIn('summary',agent4_lec.pending_eqy_wrappers(self.work))
        result=classify_proof(self.work,dict(return_code=1,timed_out=False))
        self.assertNotIn('unrun',[r['strategy'] for rows in result['partition_statuses'].values() for r in rows])

    def test_native_terminal_statuses_are_reported_without_inventing_equivalence(self):
        self.record('B','TIMEOUT'); self.record('C','UNKNOWN')
        for status in ('PASS','FAIL','UNKNOWN','TIMEOUT'):
            with self.subTest(status=status):
                self.record('A',status,nested=True)
                result=classify_proof(self.work,dict(return_code=1,timed_out=False))
                row=result['partition_statuses']['top.A'][0]
                self.assertEqual(row['status'],status)
                self.assertEqual(row['status_source'],'sby_terminal')
                self.assertEqual(result['result'],'INCONCLUSIVE')
                self.assertEqual(result['counterexamples'],[])

    def test_native_fail_with_counterexample_remains_nonequivalent(self):
        self.record('B','TIMEOUT'); self.record('C','UNKNOWN')
        job=self.record('A','FAIL',nested=True)
        engine=job/'engine_0'; engine.mkdir()
        (engine/'trace.vcd').write_text('$comment assertion counterexample $end\n')
        (engine/'logfile.txt').write_text('Assert failed in miter: equivalence assertion\n')
        result=classify_proof(self.work,dict(return_code=1,timed_out=False))
        self.assertEqual(result['result'],'NOT_EQUIVALENT')
        self.assertTrue(result['counterexamples'])

    def test_started_unfinished_sby_is_visible_and_unrun_strategies_stay_absent(self):
        job=self.work/'strategies/top.A/smtbmc/top.A'; job.mkdir(parents=True)
        (job/'logfile.txt').write_text('Started SBY engine; no terminal result')
        (self.work/'strategies/top.B/unrun').mkdir(parents=True)
        agent=agent4_lec.LEC_Agent(str(self.work/'rtl.v'),str(self.work/'mapped.v'),top_module='top')
        agent.work=str(self.work); agent.execution=dict(return_code=-9,timed_out=True)
        agent.parse_results()
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertEqual(agent.formal['result'],'TIMEOUT')
        self.assertEqual(set(agent.formal['partition_statuses']),{'top.A'})
        row=agent.formal['partition_statuses']['top.A'][0]
        self.assertEqual(row['status'],'UNKNOWN')
        self.assertEqual(row['status_source'],'unfinished_strategy')
        self.assertIsNone(row['status_file'])
        self.assertTrue(Path(row['activity_path']).is_file())
        self.assertEqual(list(agent.partitions.values()),['UNKNOWN'])

    def test_started_sat_log_without_terminal_status_blocks_completion(self):
        for part in ('A','B','C'): self.record(part,'TIMEOUT')
        active=self.work/'strategies/top.B/quick_sat'; active.mkdir()
        (active/'run.log').write_text('SAT proof still running')
        self.assertEqual(agent4_lec.pending_eqy_wrappers(self.work),{})
        result=classify_proof(self.work,dict(return_code=-9,timed_out=True))
        self.assertTrue(any(r['strategy']=='quick_sat' and r['status']=='UNKNOWN'
                            for r in result['partition_statuses']['top.B']))


class RelationalConsumerContextTests(unittest.TestCase):
    def setUp(self):
        from test_agent4_producers import fixture
        self.gold,self.gate,self.matching,_,_=fixture()
        row=self.matching['candidates'][0]
        row.update(selected_for_config=True,gold_match_wire='payload_d',gate_match_wire='payload_d')
        self.gold['netnames']['payload_d']={'bits':[20,21]};self.gate['netnames']['payload_d']={'bits':[120,121]}
        self.relation=dict(gold_selector=5,gate_selector=105)

    def control(self):
        self.gold['cells']['flag']={'type':'$mux','connections':{'A':[50],'B':['1'],'S':[5],'Y':[55]},
            'port_directions':{'A':'input','B':'input','S':'input','Y':'output'}}
        self.gate['cells']['flag']={'type':'$_OR_','connections':{'A':[150],'B':[105],'Y':[155]},
            'port_directions':{'A':'input','B':'input','Y':'output'}}
        self.gold['netnames']['flag_d']={'bits':[55]};self.gate['netnames']['flag_d']={'bits':[155]}
        self.matching['candidates'].append(dict(selected_for_config=True,partition_name='unseen.flag',
            gold_match_wire='flag_d',gate_match_wire='flag_d'))

    def amendments(self):
        return agent4_lec.relational_consumer_amendments(self.gold,self.gate,self.matching,self.relation,'new_basis')

    def test_payload_consumer_keeps_free_relational_basis(self):self.assertEqual(self.amendments(),[])

    def test_control_consumer_retains_basis_definition(self):
        self.control();rows=self.amendments()
        self.assertEqual([r['partition'] for r in rows],['unseen.flag'])

    def test_scoped_amendment_does_not_select_payload_sibling(self):
        self.control();self.assertNotIn(self.matching['candidates'][0]['partition_name'],[r['partition'] for r in self.amendments()])

    def test_disabled_match_gets_no_extra_context(self):
        self.control();self.matching['candidates'][-1]['selected_for_config']=False
        self.assertEqual(self.amendments(),[])

    def test_basis_definition_is_amended_before_automatic_amend_is_disabled(self):
        self.control();lines=agent4_lec.retain_relational_consumer_context(['noamend new_basis'],self.amendments())
        self.assertEqual(lines,['amend new_basis flag_d','noamend new_basis'])

    def test_missing_basis_boundary_is_rejected(self):
        self.control()
        with self.assertRaises(ValueError):agent4_lec.retain_relational_consumer_context([],self.amendments())

    def test_no_current_design_name_or_index_is_required(self):
        self.control();self.matching['candidates'][-1]['partition_name']='other_layout.pending'
        self.assertEqual(self.amendments()[0]['partition'],'other_layout.pending')


class ScopedPassCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.cache=self.root/'cache'
        self.old=dict(schema_version=1,configuration='configuration',tools={'yosys':'same'},design=dict(
            input_hashes={'immutable.v':'rtl_hash'},reset='sync_active_low',frontend='slang',
            formal_setup='setup',partition_inventory='inventory',accepted_matches='matches',
            partitions={'arbitrary.a':'theorem_a','arbitrary.b':'theorem_b'}))
        self.current=copy.deepcopy(self.old)
        self.current['configuration']='new_configuration';self.current['design']['formal_setup']='new_setup'
        self.current['design']['partitions']['arbitrary.b']='changed_theorem'
        self.current['design']['consumer_context_scopes']={
            'arbitrary.a':dict(configuration='configuration',formal_setup='setup'),
            'arbitrary.b':dict(configuration='new_configuration',formal_setup='new_setup')}
        statuses={}
        for name in ('arbitrary.a','arbitrary.b'):
            path=self.root/(name+'.status');path.write_text('PASS\n')
            statuses[name]=[dict(status='PASS',status_file=str(path))]
        formal=dict(result='INCONCLUSIVE',counterexamples=[],proven_partitions=list(statuses),partition_statuses=statuses)
        agent4_lec.save_cached_passes(self.cache,self.old,formal,dict(timed_out=False))

    def test_unrelated_exact_native_theorem_receipt_is_reused(self):
        row=agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a')
        self.assertTrue(row['identity_verified']);self.assertTrue(row['partition_scope_verified'])

    def test_changed_consumer_native_fingerprint_is_not_reused(self):
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.b'))

    def test_inventory_reordering_does_not_invalidate_an_unchanged_theorem(self):
        self.current['design']['partition_inventory']='same obligations, different order'
        self.assertTrue(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_required_obligation_inventory_stays_exact(self):
        self.current['design']['partitions']['arbitrary.c']='additional obligation'
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_native_change_is_not_hidden_by_scoping(self):
        self.current['design']['partitions']['arbitrary.a']='different'
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_reset_change_is_not_hidden_by_scoping(self):
        self.current['design']['reset']='other'
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_tool_change_is_not_hidden_by_scoping(self):
        self.current['tools']['yosys']='other'
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_setup_or_x_change_is_not_hidden_by_scoping(self):
        self.current['design']['consumer_context_scopes']['arbitrary.a']['formal_setup']='other_x_policy'
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_damaged_evidence_is_not_reused(self):
        (self.root/'arbitrary.a.status').write_text('UNKNOWN\n')
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_no_implicit_scoped_admission(self):
        del self.current['design']['consumer_context_scopes']
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a'))

    def test_legacy_receipts_are_not_modified(self):
        before={str(p):p.read_bytes() for p in self.cache.glob('*.json')}
        agent4_lec.load_cached_pass(self.cache,self.current,'arbitrary.a')
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.cache.glob('*.json')})


class FormalResultTests(unittest.TestCase):
    def test_eqy_failed_to_prove_and_exit_two_without_cex_is_inconclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL')
            (work/'logfile.txt').write_text('Failed to prove equivalence for 1/1 partitions.\n')
            result=classify_proof(work,dict(return_code=2,timed_out=False))
            self.assertEqual(result['result'],'INCONCLUSIVE')
            self.assertEqual(result['counterexamples'],[])

    def test_strategy_fail_is_not_a_proven_counterexample(self):
        result=self.classify('FAIL',return_code=2)
        self.assertEqual(result['result'],'INCONCLUSIVE')
        self.assertFalse(any(r['counterexample_proven'] for rows in result['partition_statuses'].values() for r in rows))

    def test_saved_nonequivalent_without_positive_evidence_is_downgraded(self):
        for evidence in ([],[dict(counterexample_proven=False)]):
            with self.subTest(evidence=evidence):
                formal=dict(result='NOT_EQUIVALENT',counterexamples=evidence)
                self.assertEqual(agent4_lec.guard_counterexample_verdict(formal)['result'],'INCONCLUSIVE')

    def test_positive_post_reset_counterexample_is_retained(self):
        formal=dict(result='NOT_EQUIVALENT',counterexamples=[dict(counterexample_proven=True)])
        self.assertEqual(agent4_lec.guard_counterexample_verdict(formal)['result'],'NOT_EQUIVALENT')

    def test_free_internal_match_witness_is_not_reachable_design_cex(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL',base_fail=True)
            write(work/'partitions/output.json',dict(partition=dict(inbits={'agent4_design.symbolic_control':[0],'rst_n':[0]})))
            result=classify_proof(work,dict(return_code=2,timed_out=False))
            self.assertEqual(result['result'],'INCONCLUSIVE')
            self.assertEqual(result['counterexamples'],[])
            row=result['partition_statuses']['output'][0]
            self.assertTrue(row['partition_counterexample'])
            self.assertFalse(row['counterexample_proven'])

    def test_closed_partition_counterexample_still_proves_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL',base_fail=True)
            write(work/'partitions/output.json',dict(partition=dict(inbits={'rst_n':[0],'data':[0]})))
            result=classify_proof(work,dict(return_code=2,timed_out=False))
            self.assertEqual(result['result'],'NOT_EQUIVALENT')

    def test_induction_witness_is_never_a_reachable_cex(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL')
            strategy=work/'strategies/output/induction'
            (strategy/'trace_induct.vcd').write_text('$comment induction witness $end\n')
            result=classify_proof(work,dict(return_code=2,timed_out=False))
            self.assertEqual(result['result'],'INCONCLUSIVE')

    def classify(self,status='PASS',**kwargs):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work',status,kwargs.pop('global_status',None),kwargs.pop('base_fail',False))
            return classify_proof(work,dict(return_code=kwargs.pop('return_code',0 if status=='PASS' else 1),timed_out=kwargs.pop('timed_out',False)))

    def test_equivalent_requires_all_partitions(self): self.assertEqual(self.classify()['result'],'EQUIVALENT')
    def test_real_sat_counterexample_is_nonequivalent(self):
        result=self.classify('FAIL',base_fail=True)
        self.assertEqual(result['result'],'NOT_EQUIVALENT'); self.assertTrue(result['counterexamples']); self.assertTrue(result['formal_traces'])
    def test_fail_without_counterexample_is_not_nonequivalent(self): self.assertEqual(self.classify('FAIL')['result'],'INCONCLUSIVE')
    def test_induction_exhaustion_is_unproven(self): self.assertEqual(self.classify('UNKNOWN')['result'],'INCONCLUSIVE')
    def test_solver_error_is_inconclusive_with_tool_diagnosis(self):
        result=self.classify('ERROR')
        self.assertEqual(result['result'],'INCONCLUSIVE');self.assertEqual(result['failure_classification'],'AGENT4_TOOL_ERROR')
    def test_timeout_is_distinct(self): self.assertEqual(self.classify('UNKNOWN',timed_out=True)['result'],'TIMEOUT')
    def test_proven_mismatch_is_retained_when_another_partition_times_out(self):
        self.assertEqual(self.classify('FAIL',base_fail=True,timed_out=True)['result'],'NOT_EQUIVALENT')
    def test_exit_nonzero_with_pass_is_inconclusive(self): self.assertEqual(self.classify(return_code=1)['result'],'INCONCLUSIVE')
    def test_global_pass_cannot_hide_unknown_partition(self): self.assertNotEqual(self.classify('UNKNOWN',global_status='PASS',return_code=0)['result'],'EQUIVALENT')
    def test_pass_marker_cannot_hide_counterexample(self): self.assertEqual(self.classify('FAIL',global_status='PASS',base_fail=True)['result'],'TOOL_ERROR')
    def test_empty_proof_is_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'PASS').touch()
            self.assertEqual(classify_proof(directory,dict(return_code=0,timed_out=False))['result'],'TOOL_ERROR')
    def test_missing_partition_status_blocks_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work'); (work/'partition.list').write_text('top output : a\ntop unproven : b\n')
            result=classify_proof(work,dict(return_code=0,timed_out=False))
            self.assertIn('unproven',result['unresolved_partitions']); self.assertNotEqual(result['result'],'EQUIVALENT')

    def test_nested_timeout_cannot_override_an_existing_tool_error(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','ERROR')
            nested=work/'strategies/output/induction/output'; nested.mkdir()
            (nested/'status').write_text('TIMEOUT 0 1\n')
            result=classify_proof(work,dict(return_code=1,timed_out=False))
            self.assertEqual(result['result'],'INCONCLUSIVE')
            self.assertEqual(result['failure_classification'],'AGENT4_TOOL_ERROR')
            self.assertEqual(result['partition_statuses']['output'][0]['status'],'ERROR')

    def ladder(self,sat,pdr,cex=False,sat_timeout=False,smtbmc=None,cex_strategy='pdr'):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        work=Path(temp.name)/'work'; work.mkdir()
        (work/'partition.list').write_text('top output : y\n')
        for name,status in [('quick_sat',sat),('pdr',pdr),('smtbmc',smtbmc)]:
            if status is None: continue
            directory=work/'strategies/output'/name; directory.mkdir(parents=True)
            (directory/'status').write_text(status+'\n')
            (directory/'run.log').write_text('Interrupted SAT solver: TIMEOUT!\n' if name=='quick_sat' and sat_timeout else '')
            if name==cex_strategy and cex:
                (directory/'engine_0').mkdir()
                (directory/'engine_0/trace.vcd').write_text('$comment formal counterexample $end\n')
                (directory/'engine_0/logfile.txt').write_text('Assert failed in miter: equivalence assertion\n')
        passed=not cex and any(s and s.split()[0]=='PASS' for s in [sat,pdr,smtbmc])
        (work/('PASS' if passed else 'FAIL')).touch()
        agent=agent4_lec.LEC_Agent(temp.name,str(work/'unused.v'),top_module='top')
        agent.work=str(work); agent.execution={'return_code':0 if passed else 1,'timed_out':False}
        agent.parse_results()
        return agent

    def test_sat_pass_stops_with_cached_pdr_success(self):
        agent=self.ladder('PASS','PASS (cached)')
        self.assertEqual(agent.status,'PASS')
        self.assertEqual(agent.formal['result'],'EQUIVALENT')
        self.assertEqual(agent.formal['proven_partitions'],['output'])

    def test_sat_unknown_falls_through_to_pdr_pass(self):
        agent=self.ladder('UNKNOWN','PASS')
        self.assertEqual(agent.status,'PASS')
        self.assertEqual(agent.formal['result'],'EQUIVALENT')
        self.assertEqual([r['strategy'] for r in agent.formal['partition_statuses']['output']],['pdr','quick_sat'])

    def test_all_strategies_unknown_are_inconclusive_in_both_flows(self):
        agent=self.ladder('UNKNOWN','UNKNOWN')
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertEqual(agent.failure_classification,'AGENT4_PROOF_INCOMPLETE')
        self.assertEqual(agent.formal['counterexamples'],[])

    def test_pdr_counterexample_remains_nonequivalent(self):
        agent=self.ladder('UNKNOWN','FAIL',cex=True)
        self.assertEqual(agent.status,'FAIL')
        self.assertEqual(agent.formal['result'],'NOT_EQUIVALENT')
        self.assertEqual(agent.failure_classification,'LOGIC_EQUIVALENCE_FAILURE')

    def test_pdr_partition_timeout_is_inconclusive(self):
        agent=self.ladder('UNKNOWN','TIMEOUT')
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertEqual(agent.formal['counterexamples'],[])

    def test_earlier_strategy_timeout_cannot_hide_later_proof(self):
        agent=self.ladder('UNKNOWN','PASS',sat_timeout=True)
        self.assertEqual(agent.status,'PASS')
        self.assertEqual(agent.formal['result'],'EQUIVALENT')

    def test_pdr_timeout_falls_through_to_smtbmc_pass(self):
        agent=self.ladder('UNKNOWN','TIMEOUT',smtbmc='PASS')
        self.assertEqual(agent.formal['result'],'EQUIVALENT')
        self.assertEqual(agent.formal['proven_partitions'],['output'])
        self.assertEqual(agent.formal['counterexamples'],[])

    def test_three_exhausted_strategies_remain_inconclusive(self):
        for smtbmc in ('UNKNOWN','TIMEOUT'):
            with self.subTest(smtbmc=smtbmc):
                agent=self.ladder('UNKNOWN','TIMEOUT',smtbmc=smtbmc)
                self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
                self.assertEqual(agent.failure_classification,'AGENT4_PROOF_INCOMPLETE')
                self.assertEqual(agent.formal['counterexamples'],[])
                self.assertFalse(agent.execution['timed_out'])

    def test_confirmed_smtbmc_counterexample_remains_nonequivalent(self):
        agent=self.ladder('UNKNOWN','TIMEOUT',smtbmc='FAIL',cex=True,cex_strategy='smtbmc')
        self.assertEqual(agent.formal['result'],'NOT_EQUIVALENT')
        self.assertEqual(agent.failure_classification,'LOGIC_EQUIVALENCE_FAILURE')
        self.assertTrue(agent.formal['counterexamples'])

    def test_wide_partition_can_pass_without_quick_sat(self):
        agent=self.ladder(None,'TIMEOUT',smtbmc='PASS')
        self.assertEqual(agent.formal['result'],'EQUIVALENT')
        self.assertNotIn('quick_sat',[r['strategy'] for r in agent.formal['partition_statuses']['output']])

    def test_sby_basecase_trace_in_induction_strategy_is_a_counterexample(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL')
            engine=work/'strategies/output/induction/engine_0'; engine.mkdir()
            (engine/'trace.vcd').write_text('$comment assertion counterexample $end\n')
            (engine/'logfile.txt').write_text('Assert failed in miter: equivalence assertion\n')
            result=classify_proof(work,dict(return_code=1,timed_out=False))
            self.assertEqual(result['result'],'NOT_EQUIVALENT')

    def test_induction_only_smtbmc_trace_is_not_a_counterexample(self):
        with tempfile.TemporaryDirectory() as directory:
            work=proof_tree(Path(directory)/'eqy_work','FAIL')
            engine=work/'strategies/output/induction/engine_0'; engine.mkdir()
            (engine/'trace_induct.vcd').write_text('$comment induction hypothesis only $end\n')
            (engine/'logfile.txt').write_text('Assert failed in miter: equivalence assertion\n')
            result=classify_proof(work,dict(return_code=1,timed_out=False))
            self.assertEqual(result['result'],'INCONCLUSIVE')
            self.assertEqual(result['counterexamples'],[])


class StrategySelectionTests(unittest.TestCase):
    def agent(self):
        return agent4_lec.LEC_Agent('unused_gold.v','unused_gate.v',top_module='unseen_controller')

    def test_width_policy_selects_exact_large_output_partition_names(self):
        ports=[dict(name=n,direction=d,width=w) for n,d,w in [
            ('ready','output',1),('valid','output',1),('small','output',15),
            ('payload','output',16),('other_data','output',64),('request','input',128)]]
        with patch('agent4_lec.shutil.which',return_value=sys.executable):
            config='\n'.join(self.agent().eqy_strategies(ports,'agent4_formal_top'))
        self.assertIn('noapply agent4_formal_top.payload\n',config)
        self.assertIn('noapply agent4_formal_top.other_data\n',config)
        self.assertEqual(config.count('noapply '),2)
        self.assertIn('apply agent4_formal_top.payload\n',config)
        self.assertNotIn('apply agent4_formal_top.request\n',config)
        self.assertNotIn('xprop off',config)
        self.assertLess(config.index('[strategy pdr]'),config.index('[strategy smtbmc]'))

    def test_unavailable_bitwuzla_or_smtbmc_is_omitted_safely(self):
        for missing in ('bitwuzla','yosys-smtbmc'):
            with self.subTest(missing=missing),patch('agent4_lec.shutil.which',
                    side_effect=lambda name,**kwargs: None if name==missing else sys.executable):
                agent=self.agent(); agent.check_strategy_tools()
                config='\n'.join(agent.eqy_strategies())
                self.assertNotIn('[strategy smtbmc]',config)
                self.assertIn('fallback omitted',config)
                self.assertIn('engine abc pdr\ntimeout 60',config)
                self.assertEqual(agent.timeout,600)

    def test_solver_lookup_uses_selected_tool_environment(self):
        agent=self.agent(); agent.tool_environment={'PATH':'/selected/suite/bin','MAKEFLAGS':'-j4'}
        with patch('agent4_lec.shutil.which',return_value=sys.executable) as lookup:
            agent.eqy_strategies()
        self.assertEqual([c[1]['path'] for c in lookup.call_args_list],['/selected/suite/bin']*2)
        self.assertEqual(agent.tool_environment['MAKEFLAGS'],'-j4')


class HardPartitionStrategyTests(unittest.TestCase):
    def plan(self, smt=True):
        agent=agent4_lec.LEC_Agent('unused_gold.v','unused_gate.v',top_module='top')
        candidates=[]
        # Arbitrary hierarchy and entry indices: no dependency on the current queue depth.
        for name,method in [('renamed_req_mem_19_payload','elaborated_struct_entry_field_bit'),
                            ('renamed_req_valid_19',None),('renamed_scheduler_locked_idx',None),
                            ('service_pending_q',None),('txn_enqueued_q',None),('ref_req',None),
                            ('open_row_19',None),('rsp_valid_q',None),('schedulerish',None)]:
            candidates.append(dict(gold_candidate=name,normalized_name=name,accepted_as_eqy_match=True,
                                   match_method=method,partition_name='top.state_'+name))
        candidates.append(dict(gold_candidate='service_rejected',accepted_as_eqy_match=False,partition_name='top.state_service_rejected'))
        agent.internal_match_report=dict(candidates=candidates)
        ports=[dict(name=n,direction='output',width=w) for n,w in [('rsp_rdata',32),('cmd_ready',1),('rsp_valid',1)]]
        with patch('agent4_lec.shutil.which',side_effect=lambda name,**kw: sys.executable if smt or name!='bitwuzla' else None):
            config='\n'.join(agent.eqy_strategies(ports,'top'))
        return agent,config

    def test_hard_categories_expand_to_exact_existing_names(self):
        agent,config=self.plan(); hard=agent.proof_strategy_plan['hard_partitions']
        self.assertEqual(len(hard),8)
        self.assertIn('top.state_renamed_req_mem_19_payload',hard)
        for name in ('state_open_row_19','state_rsp_valid_q','state_schedulerish','state_service_rejected','rsp_valid'):
            self.assertNotIn('top.'+name,hard)
            self.assertNotIn('apply top.'+name+'\n',config)
        self.assertNotIn('apply *',config)
        self.assertEqual([s['strategy'] for s in agent.proof_strategy_plan['strategies']],
            ['quick_sat','pdr','smtbmc','deep_sat','pdr_extended','smtbmc_extended'])

    def test_storage_category_excludes_deep_sat_but_retains_extended_engines(self):
        agent,_=self.plan(); specs={s['strategy']:s for s in agent.proof_strategy_plan['strategies']}
        memory='top.state_renamed_req_mem_19_payload'
        self.assertNotIn(memory,specs['deep_sat']['apply'])
        self.assertIn(memory,specs['pdr_extended']['apply'])
        self.assertIn(memory,specs['smtbmc_extended']['apply'])
        self.assertEqual(specs['deep_sat']['depth'],20)
        self.assertEqual(specs['pdr_extended']['timeout'],180)
        self.assertEqual((specs['smtbmc_extended']['depth'],specs['smtbmc_extended']['timeout']),(30,180))
        self.assertEqual(agent.timeout,600)

    def test_bitwuzla_missing_omits_both_smt_stages(self):
        agent,config=self.plan(smt=False)
        self.assertNotIn('engine smtbmc',config)
        self.assertIn('[strategy pdr_extended]',config)
        self.assertFalse(agent.proof_strategy_plan['bitwuzla_available'])

    def statuses(self, final, counterexample=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);work=Path(temp.name)
        (work/'partition.list').write_text('top top.cmd_ready : cmd_ready\n')
        sequence=[('quick_sat','UNKNOWN'),('pdr','TIMEOUT'),('smtbmc','UNKNOWN'),
                  ('deep_sat','UNKNOWN'),('pdr_extended','TIMEOUT'),('smtbmc_extended',final)]
        for name,status in sequence:
            directory=work/'strategies/top.cmd_ready'/name;directory.mkdir(parents=True)
            (directory/'status').write_text(status);(directory/'run.log').write_text('')
            if name=='smtbmc_extended' and counterexample:
                engine=directory/'engine_0';engine.mkdir()
                (engine/'trace.vcd').write_text('$comment real assertion trace $end')
                (engine/'logfile.txt').write_text('Assert failed in miter: equivalence assertion\n')
        (work/('PASS' if final=='PASS' else 'FAIL')).touch()
        return classify_proof(work,dict(return_code=0 if final=='PASS' else 1,timed_out=False))

    def test_extended_pass_proves_partition_after_unknown_and_timeout(self):
        result=self.statuses('PASS')
        self.assertEqual(result['result'],'EQUIVALENT')
        self.assertEqual(result['proven_partitions'],['top.cmd_ready'])

    def test_extended_confirmed_counterexample_is_nonequivalent(self):
        result=self.statuses('FAIL',counterexample=True)
        self.assertEqual(result['result'],'NOT_EQUIVALENT')
        self.assertTrue(result['counterexamples'])

    def test_all_stages_exhausted_are_inconclusive(self):
        for status in ('UNKNOWN','TIMEOUT','FAIL'):
            # A bare FAIL without an assertion counterexample is not a logic verdict.
            with self.subTest(status=status):
                result=self.statuses(status)
                self.assertNotEqual(result['result'],'NOT_EQUIVALENT')
                self.assertEqual(result['counterexamples'],[])
                if status!='FAIL':self.assertEqual(result['result'],'INCONCLUSIVE')

    def test_cost_report_distinguishes_cached_results_selection_and_unrun(self):
        agent,_=self.plan()
        with tempfile.TemporaryDirectory() as root:
            work=Path(root); name='top.cmd_ready'
            for strategy,status in [('quick_sat','UNKNOWN'),('pdr','PASS'),('smtbmc','PASS (cached)'),('deep_sat','PASS (cached)'),('pdr_extended','PASS (cached)'),('smtbmc_extended','PASS (cached)')]:
                directory=work/'strategies'/name/strategy;directory.mkdir(parents=True)
                (directory/'status').write_text(status)
                if '(cached)' not in status:(directory/'run.log').write_text('')
            (work/'logfile.txt').write_text("EQY 12:00:00 run: Running strategy 'quick_sat' on 'top.cmd_ready'..\nEQY 12:00:02 run: Running strategy 'pdr' on 'top.cmd_ready'..\nEQY 12:00:05 Proved equivalence of partition 'top.cmd_ready' using strategy 'pdr'\n")
            report=agent4_lec.partition_strategy_costs(work,[name,'top.rsp_valid','top.rsp_rdata'],agent.proof_strategy_plan)
            rows={r['partition']:r for r in report['partitions']}; strategies={r['strategy']:r for r in rows[name]['strategies']}
            self.assertEqual(strategies['quick_sat']['start_time'],'12:00:00')
            self.assertEqual(strategies['quick_sat']['elapsed_seconds'],2)
            self.assertEqual(strategies['pdr']['elapsed_seconds'],3)
            self.assertFalse(strategies['pdr_extended']['ran'])
            self.assertTrue(strategies['pdr_extended']['cached'])
            self.assertIsNone(strategies['pdr_extended']['elapsed_seconds'])
            self.assertTrue(rows[name]['hard_reason'])
            self.assertTrue(rows['top.rsp_rdata']['strategies'][0]['skipped_by_apply_noapply'])
            self.assertTrue(all(s['skipped_by_apply_noapply'] for s in rows['top.rsp_valid']['strategies'][3:]))

    def test_native_memory_guard_is_reported_without_claiming_execution(self):
        agent,_=self.plan()
        with tempfile.TemporaryDirectory() as root:
            work=Path(root);name='top.cmd_ready'
            (work/'logfile.txt').write_text("Skipping strategy 'deep_sat' for partition 'top.cmd_ready' as it contains memory.\n")
            rows=agent4_lec.partition_strategy_costs(work,[name],agent.proof_strategy_plan)['partitions'][0]['strategies']
            deep=next(r for r in rows if r['strategy']=='deep_sat')
            self.assertEqual(deep['status'],'SKIPPED');self.assertFalse(deep['ran'])
            self.assertEqual(deep['skip_reason'],'EQY SAT memory-support guard')
            self.assertFalse(deep['skipped_by_apply_noapply'])


class TargetedDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'native';self.output=self.root/'diagnostics'
        (self.work/'partitions').mkdir(parents=True)
        self.names=['top.request_valid_0','top.request_valid_1','top.already_proven']
        (self.work/'partition.list').write_text('\n'.join('top '+n+' : q' for n in self.names))
        for name in self.names:
            strategy=self.work/'strategies'/name/'pdr';strategy.mkdir(parents=True)
            (strategy/(name+'.sby')).write_text('[options]\nmode prove\n[script]\nread_verilog -sv old.sv\nread_rtlil old.il\nformalff -setundef -clk2ff\nxprop -formal -split-ports\n')
            for suffix in ('sv','il'):(self.work/'partitions'/(name+'.'+suffix)).write_text('native reset and X semantics '+suffix)
        (self.work/'strategies'/self.names[-1]/'pdr/status').write_text('PASS\n')
        self.cache=self.root/'cache/receipt.json';write(self.cache,{'status':'PASS','identity':'immutable'})
        self.before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def fake_command(self,statuses,return_code=None,outer_timeout=False):
        pending=iter(statuses)
        def run(command,directory,seconds,environment):
            status=next(pending);job=Path(directory)/'job';job.mkdir()
            (job/'status').write_text(status+'\n');(Path(directory)/'stdout.log').write_text(status)
            return dict(command=command,return_code=(0 if status=='PASS' else 8) if return_code is None else return_code,
                outer_timeout=outer_timeout,runtime_seconds=1.25,log=str(Path(directory)/'stdout.log'))
        return run

    def run_selected(self,statuses,selected=None,engines=None,**kwargs):
        with patch('agent4_lec.shutil.which',return_value='/tool'),patch('agent4_lec.bounded_formal_command',
                side_effect=self.fake_command(statuses,**kwargs)) as command:
            report=agent4_lec.run_targeted_diagnostics(self.work,selected or self.names[:1],self.output,
                engines or ['aiger suprove'],20,{'PATH':'/tools','MAKEFLAGS':'-j6'})
        return report,command

    def test_only_explicit_native_subset_is_run(self):
        report,command=self.run_selected(['PASS'])
        self.assertEqual(command.call_count,1)
        self.assertEqual(report['selected_partitions'],self.names[:1])
        self.assertEqual(report['unselected_partitions'],self.names[1:])
        self.assertEqual(report['required_partitions'],3)

    def test_unknown_duplicate_and_empty_selections_fail_before_execution(self):
        for selected in ([],['top.missing'],[self.names[0]]*2,[self.names[0],'top.missing']):
            with self.subTest(selected=selected),patch('agent4_lec.bounded_formal_command') as command:
                with self.assertRaises(ValueError):
                    agent4_lec.run_targeted_diagnostics(self.work,selected,self.output,['aiger suprove'])
                command.assert_not_called()

    def test_no_template_is_setup_error_before_execution(self):
        (self.work/'strategies'/self.names[1]/'pdr'/(self.names[1]+'.sby')).unlink()
        with patch('agent4_lec.bounded_formal_command') as command,self.assertRaises(ValueError):
            agent4_lec.run_targeted_diagnostics(self.work,self.names[:2],self.output,['aiger suprove'])
        command.assert_not_called()

    def test_all_selected_pass_still_never_claims_controller_equivalence(self):
        report,_=self.run_selected(['PASS']*3,selected=self.names)
        self.assertEqual(report['result'],'INCONCLUSIVE');self.assertTrue(report['diagnostic_only'])
        self.assertFalse(report['affects_readiness']);self.assertEqual(len(report['recommended_strategies']),3)

    def test_unknown_and_timeout_fall_through_to_confirmed_pass(self):
        engines=['aiger avy','abc pdr -g','aiger suprove']
        report,_=self.run_selected(['UNKNOWN','TIMEOUT','PASS'],engines=engines)
        self.assertEqual([r['status'] for r in report['results']],['UNKNOWN','TIMEOUT','PASS'])
        self.assertEqual(report['recommended_strategies'][self.names[0]]['engine'],'aiger suprove')

    def test_pass_stops_further_engines(self):
        _,command=self.run_selected(['PASS'],engines=['aiger suprove','aiger avy'])
        self.assertEqual(command.call_count,1)

    def test_counterexample_stops_and_is_never_recommended_or_cached(self):
        report,command=self.run_selected(['FAIL'],engines=['aiger suprove','aiger avy'])
        self.assertEqual(command.call_count,1);self.assertFalse(report['recommended_strategies'])
        self.assertEqual(report['results'][0]['status'],'FAIL')
        self.assertEqual(self.cache.read_bytes(),self.before[str(self.cache)])

    def test_timeout_cannot_be_promoted_by_late_pass_file(self):
        report,_=self.run_selected(['PASS'],outer_timeout=True)
        self.assertEqual(report['results'][0]['status'],'TIMEOUT');self.assertFalse(report['recommended_strategies'])

    def test_nonzero_exit_with_pass_is_not_accepted(self):
        report,_=self.run_selected(['PASS'],return_code=1)
        self.assertEqual(report['results'][0]['status'],'ERROR');self.assertFalse(report['recommended_strategies'])

    def test_unavailable_solver_is_recorded_without_execution(self):
        with patch('agent4_lec.shutil.which',side_effect=lambda tool,**kw:'/sby' if tool=='sby' else None),patch('agent4_lec.bounded_formal_command') as command:
            report=agent4_lec.run_targeted_diagnostics(self.work,self.names[:1],self.output,['aiger rIC3'])
        command.assert_not_called();self.assertEqual(report['results'][0]['status'],'UNAVAILABLE')

    def test_native_reset_x_scripts_inputs_and_receipts_unchanged(self):
        report,_=self.run_selected(['PASS'])
        config=Path(report['results'][0]['configuration']).read_text()
        self.assertIn('formalff -setundef -clk2ff\nxprop -formal -split-ports',config)
        self.assertIn('mode prove\ndepth 30',config)
        for path,data in self.before.items():self.assertEqual(Path(path).read_bytes(),data)
        self.assertTrue(report['source_hashes_unchanged'])

    def test_suproves_are_serial_and_makeflags_is_preserved(self):
        report,command=self.run_selected(['PASS','PASS'],selected=self.names[:2])
        self.assertEqual(command.call_count,2);self.assertEqual(report['concurrent_proof_jobs'],1)
        for call in command.call_args_list:self.assertEqual(call[0][3]['MAKEFLAGS'],'-j6')
        self.assertTrue(all(r['run_alone'] for r in report['results']))

    def test_bad_budget_engine_or_output_is_rejected(self):
        for engines,seconds,output in [(['aiger suprove'],0,self.output),(['unsafe engine'],1,self.output),
                (['aiger suprove'],1,self.work/'child'),(['aiger suprove'],1,self.root)]:
            with self.subTest(engines=engines,output=output),self.assertRaises(ValueError):
                agent4_lec.run_targeted_diagnostics(self.work,self.names[:1],output,engines,seconds)

    def test_cli_requires_explicit_selection_and_never_enters_full_flow(self):
        args=['agent4_lec.py','--diagnostic-work',str(self.work),'--diagnostic-output',str(self.output),
            '--partition',self.names[0],'--engine','aiger suprove','--diagnostic-seconds','25']
        with patch.object(sys,'argv',args),patch('agent4_lec.run_targeted_diagnostics') as diagnostic,patch('agent4_lec.LEC_Agent') as full:
            self.assertEqual(agent4_lec.main(),0)
        diagnostic.assert_called_once_with(str(self.work),self.names[:1],str(self.output),['aiger suprove'],25)
        full.assert_not_called()


class RuntimeOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.identity=dict(schema_version=1,design=dict(input_hashes={'rtl':'gold-hash','netlist':'gate-hash','liberty':'model-hash'},
            top='top',reset=dict(clock='clk',reset='rst_n',type='synchronous',active=0,cycles=1),frontend='slang',
            accepted_matches='match-hash',formal_setup='xprop+formalff+reset-hash',partition_inventory='inventory',partitions={'top.y':'partition-hash'}),
            configuration='strategy-hash',tools={'yosys':'version1','sby':'version2','solver':'version3'})
        self.status=self.root/'native/status';self.status.parent.mkdir();self.status.write_text('PASS\n')
        self.formal=dict(result='EQUIVALENT',counterexamples=[],proven_partitions=['top.y'],partition_statuses={
            'top.y':[dict(status='PASS',status_file=str(self.status),log=None)]})
        self.cache=self.root/'cache'

    def test_persistent_pass_requires_exact_complete_identity(self):
        self.assertEqual(agent4_lec.save_cached_passes(self.cache,self.identity,self.formal,{}),['top.y'])
        receipt=agent4_lec.load_cached_pass(self.cache,self.identity,'top.y');self.assertTrue(receipt['identity_verified'])
        mutations=[('RTL',lambda d:d['design']['input_hashes'].update(rtl='changed')),
            ('netlist',lambda d:d['design']['input_hashes'].update(netlist='changed')),
            ('Liberty',lambda d:d['design']['input_hashes'].update(liberty='changed')),
            ('reset',lambda d:d['design']['reset'].update(cycles=2)),
            ('X',lambda d:d['design'].update(formal_setup='changed')),
            ('partition',lambda d:d['design']['partitions'].update({'top.y':'changed'})),
            ('matches',lambda d:d['design'].update(accepted_matches='changed')),
            ('frontend',lambda d:d['design'].update(frontend='verilog')),
            ('strategy',lambda d:d.update(configuration='changed')),
            ('versions',lambda d:d['tools'].update(solver='changed'))]
        for name,mutate in mutations:
            with self.subTest(name=name):
                other=copy.deepcopy(self.identity);mutate(other)
                self.assertIsNone(agent4_lec.load_cached_pass(self.cache,other,'top.y'))
        self.status.write_text('UNKNOWN\n')
        self.assertIsNone(agent4_lec.load_cached_pass(self.cache,self.identity,'top.y'))

    def test_unknown_timeout_counterexample_and_incomplete_results_are_not_cached(self):
        for result in ('TIMEOUT','NOT_EQUIVALENT','TOOL_ERROR'):
            formal=dict(self.formal,result=result)
            self.assertEqual(agent4_lec.save_cached_passes(self.cache,self.identity,formal,{}),[])
        for status in ('UNKNOWN','TIMEOUT','FAIL','PASS_CACHED'):
            formal=copy.deepcopy(self.formal);formal['partition_statuses']['top.y'][0]['status']=status
            self.assertEqual(agent4_lec.save_cached_passes(self.cache,self.identity,formal,{}),[])
        self.assertEqual(agent4_lec.save_cached_passes(self.cache,self.identity,self.formal,{'timed_out':True}),[])
        self.assertFalse(self.cache.exists())

    def test_cached_and_fresh_obligations_can_pass_only_with_all_required_proofs(self):
        work=proof_tree(self.root/'work','PASS')
        status=work/'strategies/output/induction/status';status.write_text('PASS (cached)')
        execution=dict(return_code=0,timed_out=False,verified_pass_cache={'output':{'identity_verified':True}})
        result=classify_proof(work,execution)
        self.assertEqual(result['result'],'EQUIVALENT')
        self.assertEqual(result['partition_statuses']['output'][0]['status'],'PASS_CACHED')
        (work/'partition.list').write_text('top output : y\ntop producer : p\n')
        self.assertEqual(classify_proof(work,execution)['result'],'INCONCLUSIVE')
        producer=work/'strategies/producer/quick_sat';producer.mkdir(parents=True)
        (producer/'status').write_text('PASS')
        (producer/'run.log').write_text('Induction step proven: SUCCESS!')
        self.assertEqual(classify_proof(work,execution)['result'],'EQUIVALENT')
        status.write_text('PASS (persistent cache)')
        self.assertNotEqual(classify_proof(work,dict(return_code=0,timed_out=False))['result'],'EQUIVALENT')
        status.write_text('PASS_CACHED')
        self.assertNotEqual(classify_proof(work,dict(return_code=0,timed_out=False))['result'],'EQUIVALENT')

    def test_dependency_deferral_cannot_produce_overall_pass(self):
        work=proof_tree(self.root/'work','DEFERRED_DEPENDENCY')
        result=classify_proof(work,dict(return_code=1,timed_out=False))
        self.assertEqual(result['result'],'INCONCLUSIVE');self.assertFalse(result['counterexamples'])
        self.assertEqual(result['partition_statuses']['output'][0]['status'],'DEFERRED_DEPENDENCY')

    def test_real_counterexample_remains_nonequivalent_with_runtime_metadata(self):
        work=proof_tree(self.root/'work','FAIL');strategy=work/'strategies/output/induction'
        (strategy/'run.log').write_text('SAT temporal induction proof finished - model found for base case: FAIL!')
        result=classify_proof(work,dict(return_code=1,timed_out=False,verified_pass_cache={}))
        self.assertEqual(result['result'],'NOT_EQUIVALENT')

    def test_history_hard_ladder_removes_duplicate_normal_engine_restarts(self):
        agent,config=HardPartitionStrategyTests().plan();name='top.cmd_ready'
        plan=agent4_lec.optimized_runtime_plan(agent.proof_strategy_plan,{name:{'reason':'native history'}})
        applicable=[s['strategy'] for s in plan['strategies'] if name not in s.get('noapply',[]) and (s.get('apply') is None or name in s['apply'])]
        self.assertEqual(applicable,['deep_sat','pdr_extended','smtbmc_extended'])
        easy='top.rsp_valid'
        self.assertEqual([s['strategy'] for s in plan['strategies'] if easy not in s.get('noapply',[]) and (s.get('apply') is None or easy in s['apply'])],['quick_sat','pdr','smtbmc'])

    def test_only_identity_bound_successful_category_winner_is_selected(self):
        agent,_=HardPartitionStrategyTests().plan();name='top.cmd_ready'
        profile=dict(diagnostic_only=True,identity=self.identity,fastest_successful_by_category={
            'command_handshake':dict(engine='aiger rIC3',status='PASS',confirmation_status='PASS',depth=30,runtime_seconds=2)})
        with patch('agent4_lec.shutil.which',return_value='/tool'):
            plan=agent4_lec.optimized_runtime_plan(agent.proof_strategy_plan,{name:{}},profile,self.identity)
        stages=[s for s in plan['strategies'] if name not in s.get('noapply',[]) and (s.get('apply') is None or name in s['apply'])]
        self.assertEqual([s['strategy'] for s in stages],['deep_sat','tuned_0'])
        self.assertEqual(stages[-1]['engine'],'aiger rIC3')
        profile['identity']=dict(self.identity,configuration='irrelevant-strategy-change')
        with patch('agent4_lec.shutil.which',return_value=None):
            plan=agent4_lec.optimized_runtime_plan(agent.proof_strategy_plan,{name:{}},profile,self.identity)
        self.assertFalse(plan['category_winners'])
        profile['identity']=dict(self.identity,design={})
        with patch('agent4_lec.shutil.which',return_value='/tool'):
            self.assertFalse(agent4_lec.optimized_runtime_plan(agent.proof_strategy_plan,{name:{}},profile,self.identity)['category_winners'])

    def test_unconfirmed_autotune_pass_keeps_existing_engine_fallback(self):
        agent,_=HardPartitionStrategyTests().plan();name='top.cmd_ready'
        profile=dict(diagnostic_only=True,identity=self.identity,fastest_successful_by_category={
            'command_handshake':dict(engine='aiger suprove',status='PASS',confirmation_status='TIMEOUT',depth=30)})
        with patch('agent4_lec.shutil.which',return_value='/tool'):
            plan=agent4_lec.optimized_runtime_plan(agent.proof_strategy_plan,{name:{}},profile,self.identity)
        self.assertFalse(plan['category_winners'])
        self.assertIn('pdr_extended',[s['strategy'] for s in plan['strategies']])

    def schedule_fixture(self,cycle=False):
        work=self.root/'schedule';(work/'partitions').mkdir(parents=True)
        producer='top.state_queue';consumer='top.rsp_rdata';names=[producer,consumer]
        (work/'partition.list').write_text('\n'.join('top '+n+' : y' for n in names))
        (work/'summary_targets.list').write_text('\n'.join('strategies/'+n+'/s/status' for n in names)+'\n')
        (work/'strategies.mk').write_text('all: '+' '.join('strategies/'+n+'/s/status' for n in reversed(names))+'\n'+
            '\n'.join('strategies/'+n+'/s/status:\n\t@true' for n in names)+'\n')
        for n in names:
            d=work/'strategies'/n/'s';d.mkdir(parents=True);(d/'run.sh').write_text('echo PASS > status\n')
            write(work/'partitions'/(n+'.json'),dict(partition=dict(outbits={'q' if n==producer else 'y':[0]},
                inbits={'q':[0]} if n==consumer else {'y':[0]} if cycle else {},crossbits={}),gold_module={},gate_module={}))
        matching=dict(candidates=[dict(partition_name=producer,accepted_as_eqy_match=True,gold_candidate='queue')])
        return work,matching,producer,consumer

    def test_producers_are_prioritized_and_full_mode_does_not_defer(self):
        work,matching,producer,consumer=self.schedule_fixture()
        schedule=agent4_lec.partition_runtime_schedule(work,matching)
        self.assertEqual(schedule['priority_order'],[producer,consumer])
        self.assertEqual(schedule['waits'][consumer],[producer])
        self.assertIn(' | strategies/'+producer+'/s/status',(work/'strategies.mk').read_text())
        self.assertNotIn('DEFERRED_DEPENDENCY',(work/'strategies'/consumer/'s/run.sh').read_text())

    def test_cyclic_graph_keeps_producers_parallel_and_can_defer_outputs(self):
        work,matching,producer,consumer=self.schedule_fixture(cycle=True)
        schedule=agent4_lec.partition_runtime_schedule(work,matching,'development')
        self.assertEqual(schedule['cyclic_components'],1)
        self.assertEqual(schedule['waits'][producer],[])
        self.assertEqual(schedule['waits'][consumer],[producer])
        (work/'strategies'/producer/'s/status').write_text('UNKNOWN')
        directory=work/'strategies'/consumer/'s'
        result=subprocess.run(['bash','run.sh'],cwd=str(directory))
        self.assertEqual(result.returncode,0)
        self.assertEqual((directory/'status').read_text().strip(),'DEFERRED_DEPENDENCY')

    def test_history_is_bound_to_exact_inputs_contract_and_native_partitions(self):
        work=proof_tree(self.root/'history/eqy_work','UNKNOWN');(work/'partitions').mkdir()
        for suffix in ('il','sv','json'):(work/'partitions'/('output.'+suffix)).write_text('exact partition '+suffix)
        config=work.parent/'generated_config.eqy';config.write_text('[gold]\nread_rtlil model.il\n[strategy quick_sat]\nuse sat\n')
        payload=dict(source_hashes={'rtl':'hash','netlist':'gate'},top='top',reset_context={'cycles':1},frontend='slang',matching={'candidates':[]})
        identity=agent4_lec.formal_runtime_identity(work,config,payload)
        for name,status in [('quick_sat','UNKNOWN'),('pdr','TIMEOUT'),('smtbmc','UNKNOWN')]:
            directory=work/'strategies/output'/name;directory.mkdir();(directory/'status').write_text(status)
        report=dict(eqy_work_directory=str(work),input_sha256=payload['source_hashes'],clock_reset_assumptions=payload['reset_context'],
                    execution={'return_code':1,'timed_out':False},internal_matching=payload['matching'])
        write(work.parent/'lec_report.json',report)
        hard,_=agent4_lec.history_hard_partitions(work.parent,work,identity);self.assertEqual(set(hard),{'output'})
        changed=copy.deepcopy(identity);changed['design']['input_hashes']['rtl']='changed'
        self.assertFalse(agent4_lec.history_hard_partitions(work.parent,work,changed)[0])
        changed=copy.deepcopy(identity);changed['design']['reset']['cycles']=2
        self.assertFalse(agent4_lec.history_hard_partitions(work.parent,work,changed)[0])
        (work/'partitions/output.il').write_text('changed formal partition')
        self.assertFalse(agent4_lec.history_hard_partitions(work.parent,work,identity)[0])

    def test_diagnostic_template_preserves_native_xprop_and_formalff(self):
        work=self.root/'native';(work/'partitions').mkdir(parents=True)
        for suffix in ('sv','il'):(work/'partitions'/('top.y.'+suffix)).write_text('native proof input')
        template='[options]\nmode prove\n[engines]\nabc pdr\n[script]\nread_verilog -sv ../../top.y.sv\nread_rtlil ../../top.y.il\nformalff -clk2ff -ff2anyinit gate.top.y\nxprop -formal -split-ports -assume-def-inputs miter\n'
        config=agent4_lec.diagnostic_sby(template,work,'top.y',self.root/'diag','abc pdr',10,True).read_text()
        self.assertIn('mode prove',config);self.assertIn('parallel 6',config)
        self.assertIn('formalff -clk2ff -ff2anyinit gate.top.y',config)
        self.assertIn('xprop -formal -split-ports -assume-def-inputs miter',config)
        self.assertNotIn('expect timeout',config)
        (work/'partition.list').write_text('top top.cmd_ready : cmd_ready\n')
        status=work/'strategies/top.cmd_ready/pdr/status';status.parent.mkdir(parents=True)
        status.write_text('PASS')
        self.assertEqual(agent4_lec.representative_partitions(work,{'candidates':[]}),{})
        status.write_text('UNKNOWN')
        self.assertEqual(agent4_lec.representative_partitions(work,{'candidates':[]}),{'command_handshake':'top.cmd_ready'})


class ProductionCompletionTests(unittest.TestCase):
    def setUp(self):
        import agent4_completion
        self.completion=agent4_completion
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.rtl=self.root/'source.v';self.rtl.write_text('module chip; endmodule')
        self.spec=self.root/'config.yaml';self.spec.write_text('latency: 4\n')
        self.cache=self.root/'cache'

    def fixture(self, directory='run', names=('chip.A','chip.B','chip.C'),width=1):
        run=self.root/directory;work=run/'eqy_work';(work/'partitions').mkdir(parents=True)
        (work/'partition.list').write_text(''.join('chip '+n+' : out\n' for n in names))
        payload=dict(source_hashes={str(p):agent4_lec.file_digest(p) for p in (self.rtl,self.spec)},top='chip',
            reset_context=dict(clock='tick',reset='clear',active=1,type='synchronous',cycles=2),frontend='slang',matching={'candidates':[]},
            pass_cache=str(self.cache),report_directory=str(run))
        config=run/'runtime_config.eqy';config.write_text('[gold]\nread_verilog source.v\n[gate]\nread_verilog gate.v\n[strategy prove]\nuse sby\nengine abc pdr\ntimeout 60\n')
        make=['all: '+' '.join('strategies/'+n+'/prove/status' for n in names)]
        for n in names:
            write(work/'partitions'/(n+'.json'),dict(partition=dict(inbits={},outbits={'result':list(range(width))},crossbits={}),
                gold_module={'cellcount':3},gate_module={'cellcount':4}))
            (work/'partitions'/(n+'.sv')).write_text('module miter; endmodule\n')
            (work/'partitions'/(n+'.il')).write_text('module \\gold.'+n+'\n  cell $ff \\state\n    parameter \\WIDTH 2\n  end\nend\nmodule \\gate.'+n+'\nend\n')
            strategy=work/'strategies'/n/'prove';strategy.mkdir(parents=True)
            make += ['strategies/'+n+'/prove/status:', '\t@touch launched_'+n+'; echo PASS > $@']
        (work/'strategies.mk').write_text('\n'.join(make)+'\n')
        identity=agent4_lec.formal_runtime_identity(work,config,payload,{})
        runtime=dict(identity=identity,runtime_config=str(config),cache_hits={},payload=str(run/'runtime_payload.json'),work_directory=str(work))
        write(run/'runtime_report.json',runtime);write(run/'runtime_payload.json',payload)
        return work,payload,runtime

    def complete(self,work,name):
        (work/'strategies'/name/'prove/status').write_text('PASS\n')

    def test_checkpoint_partial_run_and_resume_launches_only_missing_obligation(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.complete(work,'chip.B')
        checkpoint=self.completion.PassCheckpointer(work,runtime,payload);checkpoint.flush()
        self.assertEqual(checkpoint.saved,{'chip.A','chip.B'})
        before={str(p):p.read_bytes() for p in self.cache.glob('*.json')}
        # C is interrupted without a complete status; no global PASS is present.
        other,new_payload,new_runtime=self.fixture('resumed')
        hits=self.completion.seed_cached_passes(other,new_runtime['identity'],self.completion.PassAdmission(self.cache))
        self.assertEqual(set(hits),{'chip.A','chip.B'})
        subprocess.run(['make','-s','-f','strategies.mk'],cwd=str(other),check=True,stdout=subprocess.PIPE)
        self.assertEqual([p.name for p in other.glob('launched_*')],['launched_chip.C'])
        self.assertTrue(all(Path(p).read_bytes()==data for p,data in before.items()))

    def test_monitor_checkpoints_before_run_finishes(self):
        work,payload,runtime=self.fixture();checkpoint=self.completion.PassCheckpointer(work,runtime,payload);checkpoint.start()
        try:
            self.complete(work,'chip.A');deadline=time.monotonic()+3
            while not list(self.cache.glob('*.json')) and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(len(list(self.cache.glob('*.json'))),1)
            self.assertFalse((work/'strategies/chip.C/prove/status').exists())
        finally:checkpoint.close()

    def test_incomplete_or_forwarded_status_is_never_checkpointed(self):
        work,payload,runtime=self.fixture();checkpoint=self.completion.PassCheckpointer(work,runtime,payload)
        for status in ('P','UNKNOWN','TIMEOUT','FAIL','ERROR','PASS (cached)','PASS (persistent cache)'):
            (work/'strategies/chip.A/prove/status').write_text(status);checkpoint.flush();self.assertFalse(self.cache.exists())

    def test_nested_nonpass_cannot_be_checkpointed(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A')
        write(work/'strategies/chip.A/prove/chip.A/status','TIMEOUT')
        self.completion.PassCheckpointer(work,runtime,payload).flush();self.assertFalse(self.cache.exists())

    def test_changed_source_blocks_checkpoint(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.rtl.write_text('changed RTL')
        with self.assertRaisesRegex(RuntimeError,'source identity'):self.completion.PassCheckpointer(work,runtime,payload).flush()

    def test_changed_partition_blocks_checkpoint(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');(work/'partitions/chip.A.il').write_text('changed')
        with self.assertRaisesRegex(RuntimeError,'partition identity'):self.completion.PassCheckpointer(work,runtime,payload).flush()

    def test_changed_strategy_blocks_checkpoint(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');Path(runtime['runtime_config']).write_text('changed X semantics')
        with self.assertRaisesRegex(RuntimeError,'configuration'):self.completion.PassCheckpointer(work,runtime,payload).flush()

    def test_other_partitions_strategy_change_preserves_exact_theorem(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.completion.PassCheckpointer(work,runtime,payload).flush()
        config=Path(runtime['runtime_config']);config.write_text(config.read_text()+'[strategy expensive]\nuse sby\nengine aiger avy\ntimeout 1800\napply chip.C\n')
        identity=agent4_lec.formal_runtime_identity(work,config,payload,{})
        self.assertIsNotNone(self.completion.PassAdmission(self.cache).load(identity,'chip.A'))

    def test_verified_pass_outranks_hard_size_classification_without_reproof(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.completion.PassCheckpointer(work,runtime,payload).flush()
        winners=self.completion.PassAdmission(self.cache).winners(runtime['identity'])
        self.assertEqual(set(winners),{'chip.A'})
        with patch('agent4_completion.shutil.which',return_value='/available'):
            plan=self.completion.completion_plan({'strategies':[]},{'chip.A':self.profile(cells=100000,wide_output=True)}, {},winners=winners)
        self.assertEqual(plan['completion_hard'],{})
        self.assertEqual(len(plan['strategies']),1);self.assertEqual(plan['strategies'][0]['engine'],'abc pdr')

    def test_incompatible_engine_configuration_is_not_reused(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.completion.PassCheckpointer(work,runtime,payload).flush()
        config=Path(runtime['runtime_config']);config.write_text(config.read_text().replace('timeout 60','timeout 600'))
        identity=agent4_lec.formal_runtime_identity(work,config,payload,{})
        self.assertIsNone(self.completion.PassAdmission(self.cache).load(identity,'chip.A'))

    def test_build_A_B_C_changed_inputs_cannot_cross_cache(self):
        work,payload,runtime=self.fixture();self.complete(work,'chip.A');self.completion.PassCheckpointer(work,runtime,payload).flush()
        changes=[lambda i:i['design'].update(top='different_hierarchy'),
                 lambda i:i['design']['input_hashes'].update({str(self.spec):'changed timing/spec'}),
                 lambda i:i['design']['input_hashes'].update({str(self.rtl):'changed content same filename'}),
                 lambda i:i['design']['input_hashes'].update(mapped='changed netlist'),
                 lambda i:i['design']['reset'].update(cycles=3),lambda i:i['design'].update(frontend='verilog'),
                 lambda i:i['design'].update(accepted_matches='different cutpoints'),
                 lambda i:i['design'].update(relational_proof='different relation'),
                 lambda i:i['design'].update(build_context={'elaboration':{'include_dirs':['changed setting']}}),
                 lambda i:i['tools'].update(solver='changed solver')]
        for mutate in changes:
            identity=copy.deepcopy(runtime['identity']);mutate(identity)
            self.assertIsNone(self.completion.PassAdmission(self.cache).load(identity,'chip.A'))

    def test_multiple_filenames_modules_and_widths_are_discovered(self):
        for label,count,width in [('A',3,4),('B',7,19),('C',2,2)]:
            directory=self.root/label;directory.mkdir()
            for i in range(count):(directory/('source_'+str(i)+'.sv')).write_text('module m'+str(i)+'; endmodule')
            mapped=directory/'implementation.v';mapped.write_text('module other; endmodule')
            agent=agent4_lec.LEC_Agent(str(directory),str(mapped),top_module='other',strategy='production')
            self.assertEqual(len(agent.discover_rtl_files()),count)
            work,_,_=self.fixture('shape_'+label,('different.level_'+label,),width)
            profile=self.completion.native_profile(work,{'candidates':[]})
            self.assertEqual(profile['different.level_'+label]['output_width'],width)

    def test_source_identity_contains_include_closure_and_spec(self):
        inc=self.root/'types.svh';inc.write_text('`define LATENCY 7\n')
        self.rtl.write_text('`include "types.svh"\nmodule chip; endmodule')
        gate=self.root/'mapped.v';gate.write_text('module chip; endmodule')
        agent=agent4_lec.LEC_Agent(str(self.rtl),str(gate),top_module='chip',specification=str(self.spec))
        agent.discover_rtl_files()
        actual=set(self.completion.formal_sources(agent,self.root/'reports'))
        self.assertEqual(actual,{str(p) for p in (inc,self.rtl,self.spec,gate)})

    def test_interrupted_history_retains_completed_unknown_timeout_evidence(self):
        work,payload,runtime=self.fixture();runtime['execution']=dict(return_code=-15,timed_out=True)
        (work/'strategies/chip.C/prove/status').write_text('TIMEOUT')
        other=work/'strategies/chip.C/second';other.mkdir();(other/'status').write_text('UNKNOWN')
        write(work.parent/'runtime_report.json',runtime)
        hard,_=agent4_lec.history_hard_partitions(work.parent/'runtime_report.json',work,runtime['identity'])
        self.assertIn('chip.C',hard)

    def test_shared_sequential_scc_is_retained_instead_of_repeated_lane_split(self):
        work,_,_=self.fixture(width=32)
        for a,b in [('chip.A','chip.B'),('chip.B','chip.A')]:
            p=work/'partitions'/(a+'.json');d=read_json(p)
            d['partition']['outbits']={a:[0]};d['partition']['crossbits']={b:[0]};write(p,d)
        d=read_json(work/'partitions/chip.C.json');d['partition']['crossbits']={'chip.A':[0]};write(work/'partitions/chip.C.json',d)
        profile=self.completion.native_profile(work,{'candidates':[]})
        self.assertTrue(profile['chip.C']['shared_sequential_context'])
        self.assertIn('no cone/dependency reduction',profile['chip.C']['decomposition'])

    def profile(self,**changes):
        row=dict(cells=50,state_bits=2,output_width=1,wide_output=False,internal_state=True,memory=False)
        row.update(changes);return row

    def plan(self,profile,history=None):
        with patch('agent4_completion.shutil.which',return_value='/available'):
            return self.completion.completion_plan({'strategies':[]},profile,{},history=history)

    def test_renamed_structures_get_same_plan_and_finite_budgets(self):
        for prefix in ('arbitrary.frontend','totally.other_hierarchy'):
            profile={prefix+'.narrow':self.profile(),prefix+'.wide':self.profile(cells=4000,output_width=64,wide_output=True),
                     prefix+'.state':self.profile(cells=5000)}
            plan=self.plan(profile,{prefix+'.state':{'timed_out':True}})
            selected={n:[s for s in plan['strategies'] if self.completion.applicable(s,n)] for n in profile}
            self.assertEqual([s['engine'] for s in selected[prefix+'.narrow']],['aiger suprove','aiger avy'])
            for n in (prefix+'.state',prefix+'.wide'):
                self.assertEqual([s['timeout'] for s in selected[n]],[900,1800])
                self.assertEqual([s['engine'] for s in selected[n]],['aiger suprove','aiger avy'])

    def test_strategy_selection_matches_native_ordered_apply_noapply_default(self):
        # Native EQY's final unmatched rule sets the default; a trailing
        # noapply must not erase a legitimate historical PASS for other names.
        text='[strategy portfolio]\nuse sby\nengine aiger suprove\ndepth 30\ntimeout 180\napply state.record\nnoapply independent.basis\n'
        scopes=self.completion.strategy_scopes(text,['state.record','independent.basis','arbitrary.output'])
        self.assertIn('portfolio',scopes['state.record']);self.assertIn('portfolio',scopes['arbitrary.output'])
        self.assertNotIn('portfolio',scopes['independent.basis'])
        explicit=text.replace('noapply independent.basis\n','')
        self.assertNotIn('portfolio',self.completion.strategy_scopes(explicit,['arbitrary.output'])['arbitrary.output'])

    def test_unavailable_alternate_is_omitted_safely(self):
        with patch('agent4_completion.shutil.which',side_effect=lambda n,**k:'/tool' if n=='suprove' else None):
            plan=self.completion.completion_plan({'strategies':[]},{'m.unusual':self.profile(cells=30000)}, {})
        self.assertEqual([s['engine'] for s in plan['strategies']],['aiger suprove'])

    def test_memory_never_receives_native_sat_in_production(self):
        plan=self.plan({'m.store':self.profile(cells=30000,memory=True)})
        self.assertNotIn('sat',[s['engine'] for s in plan['strategies']])

    def test_no_native_generator_or_timeout_is_called_by_classification(self):
        work,_,_=self.fixture()
        with patch('agent4_completion.shutil.which',return_value='/tool'),patch('subprocess.run',side_effect=AssertionError('no benchmarking')):
            profile=self.completion.native_profile(work,{'candidates':[]});self.plan(profile)

    def test_sat_wall_limit_survives_disabled_global_timeout(self):
        work=self.root/'sat';strategy=work/'strategies/any/quick';strategy.mkdir(parents=True)
        (strategy/'run.ys').write_text('native SAT, assumptions and reset untouched')
        original='sleep 10\necho PASS > status\n';(strategy/'run.sh').write_text(original)
        self.completion.bound_native_sat(work,.1)
        subprocess.run(['bash','run.sh'],cwd=str(strategy),check=True,timeout=5)
        self.assertEqual((strategy/'status').read_text().strip(),'TIMEOUT')
        self.assertEqual((strategy/'run_engine.sh').read_text(),original)

    def test_sby_wrapper_stall_is_bounded_without_killing_unrelated_work(self):
        strategy=self.root/'strategies/arbitrary.output/prove';strategy.mkdir(parents=True)
        job=strategy/'arbitrary.output';job.mkdir();(job/'status').write_text('TIMEOUT\n')
        (strategy/'run_engine.sh').write_text('sleep 30\n')
        unrelated=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            self.assertEqual(self.completion.strategy_worker(strategy,.1),0)
            self.assertEqual((strategy/'status').read_text().strip(),'TIMEOUT')
            cleanup=read_json(strategy/'wrapper_cleanup.json')
            self.assertEqual(cleanup['reason'],'sby_wrapper_stuck_after_terminal_engine')
            self.assertEqual(cleanup['cleanup']['remaining_groups'],[])
            self.assertIsNone(unrelated.poll())
        finally:unrelated.kill();unrelated.wait()

    def test_stale_strategy_session_registration_cannot_kill_unrelated_process(self):
        other=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True)
        try:
            path=self.root/'strategies/any/prove/wrapper_session.json'
            write(path,dict(owner_session=123,owner_identity='old-run',session=other.pid,identity=agent4_lec.process_identity(other.pid)))
            self.completion.cleanup_registered_sessions(self.root,123,'current-run')
            self.assertIsNone(other.poll())
        finally:other.kill();other.wait()

    def test_external_termination_reports_inconclusive(self):
        agent=agent4_lec.LEC_Agent(str(self.rtl),str(self.rtl),top_module='chip',timeout=0)
        mapped=self.root/'gate.v';mapped.write_text(self.rtl.read_text());agent.netlist=str(mapped)
        with patch.object(agent,'generate_eqy_config'),patch.object(agent,'run_eqy',side_effect=SystemExit(143)):
            result=agent.run()
        self.assertEqual(result['status'],'INCONCLUSIVE');self.assertFalse(result['formal']['counterexamples'])

    def test_certificate_cache_reuses_only_exact_complete_engine_proof(self):
        folder=self.root/'cert';folder.mkdir();model=folder/'model.json';write(model,{'logic':'reset and X unchanged'})
        config=folder/'proof.sby';config.write_text('[options]\nmode prove\ntimeout 10\n[engines]\naiger suprove\n[script]\nread_json "'+str(model)+'"\n')
        command=['sby','-f','-d',str(folder/'job'),str(config)];status=folder/'job/status';status.parent.mkdir();status.write_text('PASS\n')
        log=folder/'stdout.log';log.write_text('actual completed formal proof')
        execution=dict(return_code=0,outer_timeout=False,log=str(log))
        cache=self.completion.CertificateCache(self.cache,{'build':'content+reset+tools'},[str(folder)])
        cache.save(command,execution)
        cached=cache.load(command,self.root/'reused');self.assertIsNotNone(cached);self.assertIn('cached_from',cached)
        self.assertIsNone(self.completion.CertificateCache(self.cache,{'build':'changed spec'},[str(folder)]).load(command,self.root/'different'))
        model.write_text('changed formal model');self.assertIsNone(cache.load(command,self.root/'changed'))

    def test_certificate_unknown_or_interrupted_is_not_saved(self):
        folder=self.root/'cert';folder.mkdir();model=folder/'model.json';model.write_text('{}')
        config=folder/'proof.sby';config.write_text('[script]\nread_json "'+str(model)+'"\n');command=['sby','-d',str(folder/'job'),str(config)]
        status=folder/'job/status';status.parent.mkdir();status.write_text('UNKNOWN');log=folder/'stdout.log';log.write_text('incomplete')
        cache=self.completion.CertificateCache(self.cache,{},[str(folder)]);cache.save(command,dict(return_code=0,outer_timeout=False,log=str(log)))
        self.assertIsNone(cache.load(command,self.root/'resume'));self.assertFalse(cache.directory.exists())


class InternalMatchingTests(unittest.TestCase):
    @staticmethod
    def module(names):
        module=dict(ports={},netnames={},cells={}); bit=2
        for name,width in names.items():
            bits=list(range(bit,bit+width)); bit+=width
            module['netnames'][name]=dict(bits=bits,attributes={},hide_name=0)
            module['cells']['ff_'+name]=dict(type='$dff',connections={'Q':bits})
        return module

    def discover(self,gold,gate):
        return agent4_lec.discover_internal_matches(self.module(gold),self.module(gate))

    def test_exact_sequential_name_is_an_accepted_candidate(self):
        report=self.discover({'pipeline':8},{'pipeline':8})
        self.assertEqual(report['accepted_internal_matches'],1)
        self.assertEqual(report['candidates'][0]['match_method'],'exact_name')

    def test_flattened_synthesis_name_is_normalized(self):
        report=self.discover({'u_stage.counter':4},{'u_stage_counter_reg.Q':4})
        row=report['candidates'][0]
        self.assertTrue(row['accepted_as_eqy_match'])
        self.assertEqual(row['match_method'],'normalized_synthesis_name')

    def test_indexed_array_and_struct_field_provenance(self):
        report=self.discover({'u_request_queue.req_mem[1].bank[0]':1},
                             {'u_request_queue_req_mem_reg[1][bank][0].Q':1})
        self.assertEqual(report['accepted_internal_matches'],1)
        lines,sections=agent4_lec.internal_match_sections(report)
        self.assertIn('\\[1\\]',lines[0])
        self.assertTrue(any(line.startswith('solo-join ') for line in sections))
        self.assertTrue(any(line.startswith('name state_u_request_queue_req_mem_1_bank_0 ') for line in sections))
        self.assertNotIn('assume','\n'.join(lines+sections))

    def test_width_mismatch_is_rejected(self):
        report=self.discover({'counter':4},{'counter':3})
        self.assertEqual(report['accepted_internal_matches'],0)
        self.assertEqual(report['candidates'][0]['rejection_reason'],'width_or_index_direction_mismatch')

    def test_bit_direction_mismatch_is_rejected(self):
        gold=self.module({'counter':4}); gate=self.module({'counter':4})
        gate['netnames']['counter']['upto']=1
        self.assertEqual(agent4_lec.discover_internal_matches(gold,gate)['accepted_internal_matches'],0)

    def test_multiple_gate_states_are_ambiguous(self):
        report=self.discover({'u_stage.counter':4},{'u_stage.counter':4,'u_stage_counter_reg.Q':4})
        self.assertEqual(report['accepted_internal_matches'],0)
        self.assertEqual(report['rejected_ambiguous'],1)
        self.assertEqual(report['candidates'][0]['rejection_reason'],'ambiguous_gate_state')

    def test_multiple_gold_states_cannot_share_one_gate_state(self):
        report=self.discover({'u_stage.counter':4,'u_stage_counter':4},{'u_stage_counter_reg.Q':4})
        self.assertEqual(report['accepted_internal_matches'],0)
        self.assertEqual(report['rejected_ambiguous'],2)

    def test_same_physical_state_aliases_are_deduplicated(self):
        gold=self.module({'pipeline':4}); gate=self.module({'pipeline':4})
        gate['netnames']['pipeline_reg.Q']=dict(gate['netnames']['pipeline'])
        report=agent4_lec.discover_internal_matches(gold,gate)
        self.assertEqual(report['accepted_internal_matches'],1)
        self.assertEqual(report['rejected_ambiguous'],0)

    def test_width_alone_never_creates_a_match(self):
        report=self.discover({'alpha':8},{'beta':8})
        self.assertEqual(report['accepted_internal_matches'],0)
        self.assertEqual(report['candidates'][0]['rejection_reason'],'no_named_sequential_gate_match')

    def test_combinational_or_latch_gate_signal_is_not_a_state_match(self):
        for kind in ('$and','$dlatch'):
            gold=self.module({'pipeline':4}); gate=self.module({'pipeline':4})
            gate['cells']['ff_pipeline']['type']=kind
            self.assertEqual(agent4_lec.discover_internal_matches(gold,gate)['accepted_internal_matches'],0)

    def test_explicit_init_and_recoded_state_are_rejected(self):
        for attrs in ({'init':'0000'},{'fsm_encoding':'one-hot'}):
            gold=self.module({'pipeline':4}); gate=self.module({'pipeline':4})
            gold['netnames']['pipeline']['attributes']=attrs
            self.assertEqual(agent4_lec.discover_internal_matches(gold,gate)['accepted_internal_matches'],0)

    def test_coherent_vector_has_one_join_and_one_named_obligation(self):
        report=self.discover({'pipeline':32},{'pipeline':32})
        _,sections=agent4_lec.internal_match_sections(report)
        self.assertEqual(sum(line.startswith('solo-join ') for line in sections),1)
        self.assertEqual(sum(line.startswith('name ') for line in sections),1)
        self.assertEqual(sum(line.startswith('amend ') for line in sections),1)
        self.assertFalse(any(line.startswith(('merge ','bind ','sticky ')) for line in sections))

    def fixture(self,state_status='PASS',missing_producer=False,amended_consumer=False):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); work=root/'work'; work.mkdir()
        agent=agent4_lec.LEC_Agent(str(root/'gold.v'),str(root/'gate.v'),top_module='top')
        agent.report=SimpleNamespace(directory=str(root),artifact=lambda name,**kw:write(root/name,kw['data']))
        ports=[dict(name='clk',direction='input',width=1),dict(name='y',direction='output',width=1)]
        agent.prepare_internal_matching(self.module({'pipeline':4}),self.module({'pipeline':4}),ports)
        row=agent.internal_match_report['candidates'][0]; internal=row['partition_name']
        names=['agent4_formal_top.y']+([] if missing_producer else [internal])
        (work/'partition.list').write_text(''.join('agent4_formal_top '+name+' : y\n' for name in names))
        (work/'matched.ids').write_text('agent4_formal_top clk clk\nagent4_formal_top y y\n'
            'agent4_formal_top agent4_design.pipeline agent4_design.pipeline\n')
        for name in names:
            directory=work/'strategies'/name/'quick_sat'; directory.mkdir(parents=True)
            status='PASS' if name.endswith('.y') else state_status
            (directory/'status').write_text(status+'\n')
            outputs={'y':[0]} if name.endswith('.y') else {'agent4_design.pipeline':list(range(4))}
            inputs={'agent4_design.pipeline':list(range(4))} if name.endswith('.y') else {}
            write(work/'partitions'/(name+'.json'),{'partition':dict(outbits=outputs,
                inbits={} if amended_consumer else inputs,crossbits=inputs if amended_consumer else {})})
        passed=state_status=='PASS'; (work/('PASS' if passed else 'FAIL')).touch()
        agent.work=str(work); agent.execution=dict(return_code=0 if passed else 1,timed_out=False)
        agent.parse_results()
        return agent

    def test_every_required_partition_passes_before_equivalent(self):
        agent=self.fixture()
        self.assertEqual(agent.formal['result'],'EQUIVALENT')
        report=read_json(Path(agent.report.directory)/'internal_match_report.json')
        self.assertTrue(report['decomposition_improved'])
        self.assertEqual(report['generated_partitions'],2)
        self.assertEqual(report['top_level_matches'],2)
        self.assertEqual(report['accepted_internal_matches'],1)

    def test_unresolved_internal_obligation_is_inconclusive(self):
        agent=self.fixture(state_status='UNKNOWN')
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertTrue(agent.formal['unresolved_partitions'])
        self.assertEqual(agent.formal['counterexamples'],[])

    def test_missing_cutpoint_producer_cannot_be_hidden_by_output_pass(self):
        agent=self.fixture(missing_producer=True)
        self.assertEqual(agent.status,'INCONCLUSIVE')
        self.assertFalse(agent.internal_match_report['cutpoint_obligation_coverage_verified'])
        self.assertEqual(agent.internal_match_report['missing_cutpoint_obligations'],['agent4_design.pipeline'])
        self.assertFalse(agent.internal_match_report['decomposition_improved'])
        self.assertIn('Insufficient',agent.internal_match_report['decomposition_limitation'])

    def test_amended_cross_point_requires_its_own_producer_obligation(self):
        agent=self.fixture(missing_producer=True,amended_consumer=True)
        self.assertEqual(agent.status,'INCONCLUSIVE')
        report=agent.internal_match_report
        self.assertFalse(report['cutpoint_obligation_coverage_verified'])
        self.assertEqual(report['missing_cutpoint_obligations'],['agent4_design.pipeline'])
        self.assertEqual(report['candidates'][0]['consumer_partitions'],['agent4_formal_top.y'])

    def test_ambiguous_or_missing_match_is_a_proof_limitation_not_nonequivalence(self):
        for gates,ambiguous in [({'u_stage.counter':4,'u_stage_counter_reg.Q':4},1),({'unrelated':4},0)]:
            with self.subTest(gates=gates),tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                agent=agent4_lec.LEC_Agent(str(root/'gold.v'),str(root/'gate.v'),top_module='top')
                agent.report=SimpleNamespace(directory=str(root),artifact=lambda name,**kw:write(root/name,kw['data']))
                agent.prepare_internal_matching(self.module({'u_stage.counter':4}),self.module(gates),
                    [dict(name='y',direction='output',width=1)])
                agent.work=str(proof_tree(root/'work','UNKNOWN'))
                agent.execution=dict(return_code=1,timed_out=False)
                agent.parse_results()
                self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
                self.assertEqual(agent.formal['counterexamples'],[])
                self.assertEqual(agent.internal_match_report['accepted_internal_matches'],0)
                self.assertEqual(agent.internal_match_report['rejected_ambiguous'],ambiguous)
                self.assertFalse(agent.internal_match_report['decomposition_improved'])


class PackedStateAndDependencyTests(unittest.TestCase):
    module=staticmethod(InternalMatchingTests.module)

    def layout(self):
        return dict(base='u_q.req_mem',entries=[0,0],width=6,fields=[
            dict(name='bank',left=1,right=0,width=2,offset=4,scalar=False),
            dict(name='wdata',left=3,right=0,width=4,offset=0,scalar=False)])

    def designs(self):
        gold=self.module({'u_q.req_mem[0]':6})
        gate=self.module({'u_q_req_mem_reg[0][{}][{}].Q'.format(field,bit):1
                          for field,width in [('bank',2),('wdata',4)] for bit in range(width)})
        return gold,gate

    def test_exact_entry_field_bit_key(self):
        self.assertEqual(agent4_lec.struct_register_key('u_q_req_mem_reg[1][bank][0].Q'),('u_q_req_mem',1,'bank',0))
        self.assertEqual(agent4_lec.struct_register_key('u_q.req_mem_reg[1][is_write].Q'),('u_q_req_mem',1,'is_write',None))

    def test_resolved_slang_type_establishes_packing_offsets(self):
        members=[dict(kind='TypeAlias',name='request_t',addr=7,target='struct packed{logic[1:0] bank;logic[3:0] wdata;}q.s$1'),
                 dict(kind='Variable',name='req_mem',type='7 q.request_t$[0:0]')]
        ast=dict(members=[dict(kind='Instance',name='top',body=dict(members=[dict(kind='Instance',name='u_q',body=dict(members=members))]))])
        layout=agent4_lec.packed_struct_layouts(ast,'top')[0]
        self.assertEqual(layout['base'],'u_q.req_mem')
        self.assertEqual([(f['name'],f['offset'],f['width']) for f in layout['fields']],[('bank',4,2),('wdata',0,4)])
        members[0]['target']='struct packed{logic[N-1:0] bank;}q.s$1'
        self.assertEqual(agent4_lec.packed_struct_layouts(ast,'top'),[])

    def test_field_selectors_preserve_exact_indices_and_group_vectors(self):
        rows=agent4_lec.discover_struct_matches(*self.designs(),layouts=[self.layout()])
        bank=next(r for r in rows if r['field']=='bank')
        self.assertEqual([s['index'] for s in bank['gold_selection']],[4,5])
        self.assertEqual(bank['matched_field_indices'],[0,1])
        self.assertEqual(sum(r['width'] for r in rows),6)
        lines,groups=agent4_lec.internal_match_sections({'candidates':rows})
        self.assertEqual(len(lines),2)
        self.assertEqual(sum(s.startswith('solo-join ') for s in groups),2)
        self.assertEqual(sum(s.startswith('amend ') for s in groups),2)

    def test_ambiguous_physical_gate_bit_is_excluded(self):
        gold,gate=self.designs();extra=self.module({'u_q.req_mem_reg[0][bank][0].Q':1})
        extra['netnames']['u_q.req_mem_reg[0][bank][0].Q']['bits']=[999]
        extra['cells']['extra']=dict(type='$dff',connections={'Q':[999]})
        gate['netnames'].update(extra['netnames']);gate['cells'].update(extra['cells'])
        row=next(r for r in agent4_lec.discover_struct_matches(gold,gate,[self.layout()]) if r['field']=='bank')
        self.assertNotIn(0,row['matched_field_indices'])
        self.assertIn(dict(bit=0,reason='ambiguous_struct_gate_bit'),row['rejected_bits'])

    def test_mismatched_entry_index_is_rejected(self):
        gold,gate=self.designs();gate['netnames']={n.replace('[0][','[2]['):v for n,v in gate['netnames'].items()}
        self.assertFalse(any(r['accepted_as_eqy_match'] for r in agent4_lec.discover_struct_matches(gold,gate,[self.layout()])))

    def test_mismatched_field_is_rejected(self):
        gold,gate=self.designs();gate['netnames']={n.replace('[bank]','[row]'):v for n,v in gate['netnames'].items()}
        bank=next(r for r in agent4_lec.discover_struct_matches(gold,gate,[self.layout()]) if r['field']=='bank')
        self.assertFalse(bank['accepted_as_eqy_match'])

    def test_mismatched_bit_index_is_rejected(self):
        gold,gate=self.designs();gate['netnames']={n.replace('[bank][0]','[bank][7]'):v for n,v in gate['netnames'].items()}
        bank=next(r for r in agent4_lec.discover_struct_matches(gold,gate,[self.layout()]) if r['field']=='bank')
        self.assertEqual(bank['matched_field_indices'],[1])

    def test_nonsequential_gate_bit_is_rejected(self):
        gold,gate=self.designs()
        for cell in gate['cells'].values():cell['type']='$and'
        self.assertFalse(any(r['accepted_as_eqy_match'] for r in agent4_lec.discover_struct_matches(gold,gate,[self.layout()])))

    def test_decimal_reset_values_and_unknowns_are_distinguished(self):
        values=agent4_lec.reset_eval_values("Eval result: \\payload = 0.\nEval result: \\unknown = 32'x.\nEval result: \\small = 2'00.")
        self.assertEqual(values['payload'],'0'*32)
        self.assertEqual(values['unknown'],'x')
        self.assertEqual(values['small'],'00')

    def test_dependency_graph_includes_amended_producer_edges(self):
        agent=InternalMatchingTests.fixture(self,amended_consumer=True)
        dep=read_json(Path(agent.report.directory)/'partition_dependency_report.json')
        edge=next(e for e in dep['dependency_graph']['edges'] if e['target']=='agent4_formal_top.y')
        self.assertEqual(edge['source'],'agent4_formal_top.state_pipeline')
        self.assertEqual(edge['kind'],'gold_amended_crosspoint')
        self.assertEqual(edge['bits'],list(range(4)))

    def test_state_cost_counts_actual_rtlil_cells_and_widths(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'p.il';path.write_text('module \\gold.p\n  cell $anyinit a\n    parameter \\WIDTH 3\n  end\n  cell $_FF_ b\n  end\nend\nmodule \\gate.p\n  cell $and c\n  end\nend\n')
            costs=agent4_lec.partition_state_cost(path)
            self.assertEqual(costs['gold'],dict(sequential_cells=2,sequential_bits=4))
            self.assertEqual(costs['gate'],dict(sequential_cells=0,sequential_bits=0))

    def test_elapsed_clock_observation_handles_midnight_and_missing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            log="EQY 23:59:59 [x] Running strategy 'quick_sat' on 'p'\nEQY 00:00:05 [x] Running strategy 'pdr' on 'p'\n"
            self.assertEqual(agent4_lec.strategy_elapsed(directory,'p','quick_sat',log)['elapsed_seconds'],6)
            self.assertIsNone(agent4_lec.strategy_elapsed(directory,'p','pdr',log)['elapsed_seconds'])
            p=Path(directory)/'strategies/p/pdr/p/logfile.txt';p.parent.mkdir(parents=True)
            p.write_text('summary: Elapsed clock time [H:MM:SS (secs)]: 0:01:01 (61)\n')
            self.assertEqual(agent4_lec.strategy_elapsed(directory,'p','pdr',log)['elapsed_seconds'],61)


class InstalledStrategyTests(unittest.TestCase):
    """Small real EQY/SBY proofs; never use the controller/backend artifacts."""
    def prove(self,gold,gate,**options):
        tools=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin'
        if not all((tools/name).is_file() for name in ('eqy','yosys','sby','yosys-abc')):
            self.skipTest('Local OSS CAD Suite is not installed')
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root=Path(temp.name); reference=root/'gold.v'; mapped=root/'gate.v'
        reference.write_text(gold); mapped.write_text(gate)
        before=[reference.read_bytes(),mapped.read_bytes()]
        engine=options.pop('sby_engine',None)
        agent=agent4_lec.LEC_Agent(str(reference),str(mapped),top_module='top',timeout=options.pop('timeout',300),**options)
        if engine: agent.args=SimpleNamespace(sby_engine=engine)
        with patch.dict(os.environ,{'PATH':str(tools)+os.pathsep+os.environ.get('PATH','')}):
            result=agent.run()
        self.assertEqual(before,[reference.read_bytes(),mapped.read_bytes()])
        return result,agent

    def prove_hard_after_unknown(self, gold, gate, target):
        original=agent4_lec.LEC_Agent.eqy_strategies
        def select(agent,*args,**kwargs):
            lines=original(agent,*args,**kwargs); result=[]; force=False
            for line in lines:
                if line.startswith('[strategy '):
                    name=line[len('[strategy '):-1]
                    force=name!=target
                    result.append(line)
                    if force:result.append('use dummy')
                elif not force or line.startswith(('apply ','noapply ')):
                    result.append(line)
            return result
        with patch.object(agent4_lec.LEC_Agent,'eqy_strategies',select):
            return self.prove(gold,gate)

    def test_real_easy_hard_category_pass_never_executes_extended_engines(self):
        design='module top(input clk,input rst_n,input d,output reg cmd_ready); always @(posedge clk) if (!rst_n) cmd_ready<=0; else cmd_ready<=d; endmodule'
        result,agent=self.prove(design,design)
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        report=read_json(Path(agent.report.directory)/'proof_strategy_report.json')
        extended=[s for r in report['partitions'] for s in r['strategies'] if s['strategy'].endswith('_extended')]
        self.assertTrue(extended)
        self.assertTrue(all(s['cached'] and not s['ran'] for s in extended))
        self.assertFalse(list(Path(agent.work).glob('strategies/*/*_extended/*/status')))

    def test_real_unknown_falls_through_to_deep_sat_and_extended_pdr(self):
        gold='module top(input clk,input rst_n,output cmd_ready); reg [3:0] a; always @(posedge clk) if (!rst_n) a<=0; else a<=a+1; assign cmd_ready=(a==0); endmodule'
        gate='module top(input clk,input rst_n,output cmd_ready); reg [3:0] b; always @(posedge clk) if (!rst_n) b<=1; else b<=b+1; assign cmd_ready=(b==1); endmodule'
        for target in ('deep_sat','pdr_extended','smtbmc_extended'):
            with self.subTest(target=target):
                result,agent=self.prove_hard_after_unknown(gold,gate,target)
                self.assertEqual(result['status'],'PASS',result['details']['errors'])
                rows=[r for rows in agent.formal['partition_statuses'].values() for r in rows]
                self.assertTrue(any(r['strategy']==target and r['status']=='PASS' for r in rows))
                self.assertTrue(any(r['strategy']=='quick_sat' and r['status']=='UNKNOWN' for r in rows))

    def test_real_extended_engine_counterexample_still_fails_after_reset(self):
        gold='module top(input clk,input rst_n,input d,output reg cmd_ready); always @(posedge clk) if (!rst_n) cmd_ready<=0; else cmd_ready<=d; endmodule'
        gate=gold.replace('cmd_ready<=d','cmd_ready<=~d')
        for target in ('pdr_extended','smtbmc_extended'):
            with self.subTest(target=target):
                result,agent=self.prove_hard_after_unknown(gold,gate,target)
                self.assertEqual(result['formal']['result'],'NOT_EQUIVALENT',result['details']['errors'])
                self.assertTrue(agent.formal['counterexamples'])

    def test_real_native_memory_partition_skips_deep_sat(self):
        suite=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin'
        if not (suite/'eqy').is_file():self.skipTest('Local OSS CAD Suite is not installed')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);design=root/'memory.v'
            design.write_text('module top(input clk,input [1:0] a,input [7:0] d,output [7:0] y); reg [7:0] mem[0:3]; always @(posedge clk) mem[a]<=d; assign y=mem[a]; endmodule')
            before=design.read_bytes();config=root/'test.eqy';work=root/'work'
            config.write_text('[gold]\nread_verilog '+str(design)+'\nprep -top top\n[gate]\nread_verilog '+str(design)+'\nprep -top top\n[match top]\nnodefault\ngold-match clk\ngold-match a\ngold-match d\ngold-match y\n[strategy deep_sat]\nuse sat\ndepth 20\napply top.y\n[strategy fallback]\nuse dummy\n')
            env=dict(os.environ,PATH=str(suite)+os.pathsep+os.environ.get('PATH',''))
            process=subprocess.run([str(suite/'eqy'),'-m','-d',str(work),str(config)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,universal_newlines=True,env=env,timeout=60)
            self.assertEqual(process.returncode,0,process.stdout)
            self.assertIn("Skipping strategy 'deep_sat' for partition 'top.y' as it contains memory.",process.stdout)
            self.assertFalse((work/'strategies/top.y/deep_sat/run.sh').exists())
            self.assertEqual(design.read_bytes(),before)

    def test_real_runtime_pass_cache_reuses_native_proofs_on_second_run(self):
        design='module top(input clk,input rst_n,input d,output reg cmd_ready); always @(posedge clk) if (!rst_n) cmd_ready<=0; else cmd_ready<=d; endmodule'
        cache=tempfile.TemporaryDirectory();self.addCleanup(cache.cleanup)
        first,agent=self.prove(design,design,pass_cache=cache.name,schedule_producers=True)
        self.assertEqual(first['status'],'PASS',first['details']['errors'])
        self.assertTrue(first['formal']['pass_cache']['saved'])
        second=agent4_lec.LEC_Agent(agent.rtl_files[0],agent.netlist,top_module='top',timeout=120,
                                  pass_cache=cache.name,schedule_producers=True)
        suite=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin'
        before=file_rows([agent.rtl_files[0],agent.netlist])
        with patch.dict(os.environ,{'PATH':str(suite)+os.pathsep+os.environ.get('PATH',''),'MAKEFLAGS':'-j6'}):
            result=second.run()
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        def differences(a,b,prefix=''):
            if isinstance(a,dict) and isinstance(b,dict):
                return [item for key in set(a)|set(b) for item in differences(a.get(key),b.get(key),prefix+'/'+key)]
            return [] if a==b else [(prefix,str(a)[:180],str(b)[:180])]
        self.assertEqual(result['formal']['pass_cache']['hits'],result['formal']['expected_partitions'],
                         differences(agent.runtime_report['identity'],second.runtime_report['identity']))
        self.assertTrue(all(r['status']=='PASS_CACHED' for rows in result['formal']['partition_statuses'].values() for r in rows))
        self.assertFalse(list(Path(second.work).glob('strategies/*/*/run.log')))
        self.assertEqual(before,file_rows([agent.rtl_files[0],agent.netlist]))
        self.assertEqual(second.runtime_report['cpu_limit'],6)

    def test_real_production_zero_global_timeout_and_fast_pass_skips_recovery(self):
        design='module top(input clk,input rst_n,input d,output reg arbitrary_result); always @(posedge clk) if (!rst_n) arbitrary_result<=0; else arbitrary_result<=d; endmodule'
        result,agent=self.prove(design,design,strategy='production',timeout=0)
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        self.assertFalse(result['execution']['timed_out'])
        self.assertFalse(list(Path(agent.work).glob('strategies/*/completion_recovery/*/status')))
        self.assertIn('native_profile',agent.runtime_report['plan'])

    def test_real_runtime_counterexample_never_enters_pass_cache(self):
        design='module top(input clk,input rst_n,input d,output reg cmd_ready); always @(posedge clk) if (!rst_n) cmd_ready<=0; else cmd_ready<=d; endmodule'
        cache=tempfile.TemporaryDirectory();self.addCleanup(cache.cleanup)
        result,agent=self.prove(design,design.replace('cmd_ready<=d','cmd_ready<=~d'),pass_cache=cache.name)
        self.assertEqual(result['formal']['result'],'NOT_EQUIVALENT',result['details']['errors'])
        self.assertTrue(result['formal']['counterexamples'])
        self.assertFalse(list(Path(cache.name).glob('*.json')))

    def test_real_runtime_autotune_is_diagnostic_and_cannot_mark_design_equivalent(self):
        design='module top(input clk,input rst_n,input d,output reg cmd_ready); always @(posedge clk) if (!rst_n) cmd_ready<=0; else cmd_ready<=d; endmodule'
        result,agent=self.prove(design,design,proof_mode='autotune',autotune_seconds=2,autotune_wall_seconds=15)
        self.assertEqual(result['formal']['result'],'INCONCLUSIVE',result['details']['errors'])
        report=read_json(Path(agent.report.directory)/'proof_autotune_report.json')
        self.assertTrue(report['diagnostic_only']);self.assertFalse(report['affects_readiness'])
        self.assertTrue(report['results'])
        self.assertEqual(len(report['representatives']),1)
        self.assertTrue(report['identity']['design']['partitions'])
        self.assertFalse(list(Path(agent.work).glob('strategies/*/*/status')))

    def test_real_targeted_native_proof_pass_never_promotes_controller_verdict(self):
        gold='module top(input clk,input rst_n,output cmd_ready); reg [3:0] a; always @(posedge clk) if (!rst_n) a<=0; else a<=a+1; assign cmd_ready=(a==0); endmodule'
        gate='module top(input clk,input rst_n,output cmd_ready); reg [3:0] b; always @(posedge clk) if (!rst_n) b<=1; else b<=b+1; assign cmd_ready=(b==1); endmodule'
        result,agent=self.prove(gold,gate)
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        names=result['formal']['expected_partitions'];before=file_rows([agent.rtl_files[0],agent.netlist])
        suite=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin'
        environment=dict(os.environ,PATH=str(suite)+os.pathsep+os.environ.get('PATH',''),MAKEFLAGS='-j6')
        report=agent4_lec.run_targeted_diagnostics(agent.work,names[:1],Path(agent.report.directory)/'targeted',
            ['abc pdr -m'],20,environment)
        self.assertEqual(report['results'][0]['status'],'PASS',report)
        self.assertEqual(report['result'],'INCONCLUSIVE');self.assertFalse(report['affects_readiness'])
        self.assertEqual(before,file_rows([agent.rtl_files[0],agent.netlist]))

    def test_real_sat_pass_does_not_execute_pdr(self):
        design='module top(input clk,input rst_n,input d,output reg y); always @(posedge clk) if (!rst_n) y<=0; else y<=d; endmodule'
        result,agent=self.prove(design,design)
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        statuses=list(Path(agent.work).glob('strategies/*/pdr/status'))
        self.assertTrue(statuses)
        self.assertTrue(all(p.read_text().strip()=='PASS (cached)' for p in statuses))
        self.assertFalse(list(Path(agent.work).glob('strategies/*/pdr/*/status')))

    def test_real_sat_unknown_is_proved_by_abc_pdr(self):
        result,agent=self.prove(
            'module top(input clk,input rst_n,output y); reg [3:0] a; always @(posedge clk) if (!rst_n) a<=0; else a<=a+1; assign y=(a==0); endmodule',
            'module top(input clk,input rst_n,output y); reg [3:0] b; always @(posedge clk) if (!rst_n) b<=1; else b<=b+1; assign y=(b==1); endmodule')
        self.assertEqual(result['status'],'PASS',result['details']['errors'])
        rows=[r for rows in result['formal']['partition_statuses'].values() for r in rows]
        self.assertTrue(any(r['strategy']=='quick_sat' and r['status']=='UNKNOWN' for r in rows))
        self.assertTrue(any(r['strategy']=='pdr' and r['status']=='PASS' for r in rows))
        configs=list(Path(agent.work).glob('strategies/*/pdr/*.sby'))
        self.assertTrue(configs)
        self.assertTrue(all('[engines]\nabc pdr' in p.read_text() and 'timeout 60' in p.read_text() for p in configs))

    def test_real_sat_counterexample_is_nonequivalent(self):
        result,_=self.prove(
            'module top(input clk,input rst_n,input d,output reg y); always @(posedge clk) if (!rst_n) y<=0; else y<=d; endmodule',
            'module top(input clk,input rst_n,input d,output reg y); always @(posedge clk) if (!rst_n) y<=0; else y<=~d; endmodule')
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT',result['formal'])
        self.assertEqual(result['failure_classification'],'LOGIC_EQUIVALENCE_FAILURE')
        self.assertTrue(result['formal']['counterexamples'])

    PIPELINE = ('module top(input clk,input rst_n,input [3:0] d,output reg [3:0] y); '
                'reg [3:0] pipeline; always @(posedge clk) if (!rst_n) begin pipeline<=0; y<=0; end '
                'else begin pipeline<=d; y<=pipeline; end endmodule')

    def test_real_internal_match_creates_a_required_vector_partition(self):
        result,agent=self.prove(self.PIPELINE,self.PIPELINE)
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        report=result['internal_matching']
        self.assertEqual(report['accepted_internal_matches'],1)
        self.assertTrue(report['decomposition_improved'])
        self.assertEqual(report['generated_partitions'],2)
        internal='agent4_formal_top.state_pipeline'
        self.assertIn(internal,result['formal']['proven_partitions'])
        row=next(r for r in report['partitions'] if r['name']==internal)
        self.assertEqual(row['output_width'],4)
        self.assertIn('agent4_design.__agent4_next_pipeline',row['output_names'])
        self.assertIn('gold-match /^agent4_design\\.__agent4_next_pipeline$/',Path(agent.config).read_text())
        self.assertIn('amend /^agent4_design\\.__agent4_next_pipeline$/',Path(agent.config).read_text())
        self.assertFalse(report['next_state_alias_errors'])

    def test_real_normalized_register_name_remains_a_proof_obligation(self):
        result,agent=self.prove(self.PIPELINE,self.PIPELINE.replace('pipeline','pipeline_reg'))
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        self.assertEqual(result['internal_matching']['candidates'][0]['match_method'],'normalized_synthesis_name')
        matched=Path(agent.work,'matched.ids').read_text()
        candidate=result['internal_matching']['candidates'][0]
        self.assertEqual((candidate['gold_candidate'],candidate['gate_candidate']),('pipeline','pipeline_reg'))
        self.assertIn('agent4_design.__agent4_next_pipeline agent4_design.__agent4_next_pipeline',matched)

    def test_real_changed_register_cannot_hide_behind_passing_downstream(self):
        result,_=self.prove(self.PIPELINE,self.PIPELINE.replace('pipeline<=d','pipeline<=~d'))
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT',result['formal'])
        self.assertTrue(result['formal']['counterexamples'])
        self.assertIn('agent4_formal_top.y',result['formal']['proven_partitions'])
        self.assertTrue(any(r['partition']=='agent4_formal_top.state_pipeline' for r in result['formal']['counterexamples']))

    def test_real_pre_reset_initial_mismatch_is_not_an_internal_logic_failure(self):
        gold=self.PIPELINE.replace('reg [3:0] pipeline;','reg [3:0] pipeline=0;')
        gate=self.PIPELINE.replace('reg [3:0] pipeline;','reg [3:0] pipeline=1;')
        result,_=self.prove(gold,gate)
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        self.assertEqual(result['internal_matching']['accepted_internal_matches'],0)
        self.assertFalse(result['internal_matching']['decomposition_improved'])

    def test_real_unreset_next_state_is_not_an_initial_state_obligation(self):
        design=('module top(input clk,input rst_n,input [3:0] d,output reg [3:0] y); '
                'reg [3:0] payload; reg valid; always @(posedge clk) '
                'if (!rst_n) begin valid<=0; y<=0; end '
                'else begin valid<=1; payload<=d; if(valid) y<=payload; else y<=0; end endmodule')
        result,_=self.prove(design,design)
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        candidate=next(r for r in result['internal_matching']['candidates'] if r['gold_candidate']=='payload')
        self.assertFalse(candidate['accepted_as_eqy_match'])
        self.assertEqual(candidate['rejection_reason'],'next_state_not_defined_by_startup_reset')

    @staticmethod
    def packed_queue_designs(corrupt=False):
        fields=[('bank',2),('row',3),('col',2),('is_write',1),('wdata',4)]
        port='(input clk,input rst_n,input enq,input idx,input sel,input [11:0] d,output [11:0] y);'
        gold=('module top'+port+' typedef struct packed {'+
              ''.join('logic '+('[{}:0] '.format(w-1) if w>1 else '')+n+';' for n,w in fields)+
              '} request_t; request_t req_mem[2]; always @(posedge clk) if(!rst_n) begin '
              'req_mem[0]<=0; req_mem[1]<=0; end else if(enq) req_mem[idx]<=d; assign y=req_mem[sel]; endmodule')
        gate=['module top'+port];position=0
        for field,width in reversed(fields):
            for bit in range(width):
                names=[]
                for entry in range(2):
                    name='\\req_mem_reg[{}][{}]{} '.format(entry,field,'[{}]'.format(bit) if width>1 else '')
                    names.append(name)
                    gate.append('reg '+name+'; always @(posedge clk) if(!rst_n) '+name+'<=0; else if(enq && idx==1\'b'+str(entry)+') '+name+'<=d['+str(position)+']'+(" ^ 1'b1" if corrupt and (entry,field,bit)==(0,'wdata',0) else '')+';')
                gate.append('assign y[{}]=sel ? {}: {};'.format(position,names[1],names[0]));position+=1
        return gold,'\n'.join(gate+['endmodule'])

    def test_real_grouped_struct_fields_are_required_producer_obligations(self):
        result,agent=self.prove(*self.packed_queue_designs(),sv_frontend='slang')
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        fields=[c for c in result['internal_matching']['candidates'] if c['match_method']=='elaborated_struct_entry_field_bit']
        self.assertEqual(len(fields),10)
        self.assertTrue(all(c['accepted_as_eqy_match'] and c['obligation_partitions'] for c in fields))
        self.assertEqual(sum(c['width'] for c in fields),24)
        self.assertEqual(result['internal_matching']['generated_partitions'],11)
        self.assertFalse(result['internal_matching']['next_state_alias_errors'])
        self.assertNotRegex(Path(agent.config).read_text(),r'(?m)^\s*(assume|setundef|chformal)\b')
        # A completed output proof cannot authorize an unresolved memory producer.
        producer=next(c['partition_name'] for c in fields if c['entry_index']==0 and c['field']=='wdata')
        for p in Path(agent.work,'strategies',producer).glob('*/status'):p.write_text('UNKNOWN\n')
        Path(agent.work,'PASS').unlink();Path(agent.work,'FAIL').touch();agent.execution['return_code']=1
        agent.parse_results()
        self.assertEqual(agent.formal['result'],'INCONCLUSIVE')
        self.assertIn('agent4_formal_top.y',agent.formal['proven_partitions'])
        self.assertIn(producer,agent.formal['unresolved_partitions'])

    def test_real_corrupt_request_memory_bit_is_nonequivalent(self):
        result,_=self.prove(*self.packed_queue_designs(corrupt=True),sv_frontend='slang')
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT',result['formal'])
        self.assertTrue(any(c['partition']=='agent4_formal_top.state_req_mem_0_wdata' for c in result['formal']['counterexamples']))

    def test_existing_eight_easy_partition_types_still_prove(self):
        design=('module top(input clk,input rst_n,input [26:0] d,output rsp_valid,output [25:0] y); '
                'reg [13:0] \\u_refresh_controller.counter ; reg rsp_valid_q; reg [3:0] row_open_valid; reg [1:0] open_row[4]; '
                'always @(posedge clk) if(!rst_n) begin \\u_refresh_controller.counter <=0; rsp_valid_q<=0;row_open_valid<=0; '
                'open_row[0]<=0;open_row[1]<=0;open_row[2]<=0;open_row[3]<=0; end else begin '
                '\\u_refresh_controller.counter <=d[13:0]; row_open_valid<=d[17:14]; '
                'open_row[0]<=d[19:18];open_row[1]<=d[21:20];open_row[2]<=d[23:22];open_row[3]<=d[25:24];rsp_valid_q<=d[26];end '
                'assign rsp_valid=rsp_valid_q;assign y={open_row[3],open_row[2],open_row[1],open_row[0],row_open_valid,\\u_refresh_controller.counter };endmodule')
        result,_=self.prove(design,design)
        expected={'agent4_formal_top.'+n for n in ['state_u_refresh_controller_counter','state_rsp_valid_q','state_row_open_valid','rsp_valid']+['state_open_row_'+str(i) for i in range(4)]}
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        self.assertTrue(expected.issubset(result['formal']['proven_partitions']))

    def test_real_wide_output_skips_sat_and_uses_pdr(self):
        design='module top(input clk,input rst_n,input [15:0] d,output reg [15:0] y); always @(posedge clk) if (!rst_n) y<=0; else y<=d; endmodule'
        result,agent=self.prove(design,design)
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        self.assertIn('noapply agent4_formal_top.y',Path(agent.config).read_text())
        self.assertFalse(list(Path(agent.work).glob('strategies/*/quick_sat/status')))
        self.assertTrue(list(Path(agent.work).glob('strategies/*/pdr/status')))

    def test_real_bitwuzla_proof_and_post_reset_counterexample(self):
        tools=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin'
        if not all((tools/name).is_file() for name in ('bitwuzla','yosys-smtbmc')):
            self.skipTest('Bitwuzla/SMTBMC is not installed')
        gold='module top(input clk,input rst_n,input d,output reg y); always @(posedge clk) if (!rst_n) y<=0; else y<=d; endmodule'
        for gate,expected in [(gold,'EQUIVALENT'),(gold.replace('y<=d','y<=~d'),'NOT_EQUIVALENT')]:
            with self.subTest(expected=expected):
                result,agent=self.prove(gold,gate,strategy='sby',sby_engine='smtbmc bitwuzla')
                self.assertEqual(result['equivalence'],expected,result['formal'])
                configs=list(Path(agent.work).glob('strategies/*/induction/*.sby'))
                self.assertTrue(configs)
                self.assertTrue(all('smtbmc bitwuzla' in p.read_text() for p in configs))
                self.assertNotIn('xprop off',Path(agent.config).read_text())
                if expected=='NOT_EQUIVALENT': self.assertTrue(result['formal']['counterexamples'])


class ResetAwareLECtests(unittest.TestCase):
    prove=InstalledStrategyTests.prove
    gold='module top(input clk,input rst_n,input d,output y); reg q; always @(posedge clk) if (!rst_n) q<=0; else q<=d; assign y=q; endmodule'

    def test_synchronous_active_low_reset_initializes_arbitrary_independent_registers(self):
        result,agent=self.prove(self.gold,self.gold.replace('q','other_state'))
        self.assertEqual(result['status'],'PASS',result['formal'])
        context=result['clock_reset_assumptions']
        self.assertEqual(context['reset'],dict(name='rst_n',active_low=True,synchronous=True))
        self.assertEqual(context['startup_reset_cycles'],1)
        self.assertEqual(context['comparison_starts_after_cycles'],1)
        text=Path(agent.config).read_text()
        self.assertIn('[match agent4_formal_top]\nnodefault',text)
        self.assertEqual(text.count('formalff -clk2ff -ff2anyinit'),2)
        self.assertNotIn('gold-match q',text)
        wrapper=Path(agent.report.directory,'gold_reset_wrapper.sv').read_text()
        self.assertIn("agent4_phase < 1'd1",wrapper)
        self.assertIn("agent4_phase == 1'd1",wrapper)
        self.assertIn("? 1'b0 : rst_n",wrapper)

    def test_different_pre_reset_initial_values_do_not_produce_nonequivalence(self):
        gold=self.gold.replace('reg q;','reg q=0;')
        gate=self.gold.replace('reg q;','reg q=1;').replace('q','other_state')
        result,_=self.prove(gold,gate)
        self.assertEqual(result['equivalence'],'EQUIVALENT',result['formal'])
        self.assertEqual(result['formal']['counterexamples'],[])

    def test_bad_reset_value_is_compared_immediately_after_first_reset_clock(self):
        result,_=self.prove(self.gold,self.gold.replace('q<=0','q<=1'))
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT',result['formal'])
        self.assertEqual(result['failure_classification'],'LOGIC_EQUIVALENCE_FAILURE')

    def test_post_reset_logic_mismatch_still_has_formal_counterexample(self):
        result,_=self.prove(self.gold,self.gold.replace('q<=d','q<=~d'))
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT',result['formal'])
        self.assertTrue(result['formal']['counterexamples'])

    def test_ambiguous_reset_never_invokes_formal_or_blames_backend(self):
        design='module top(input clk,input d,output reg y); always @(posedge clk) y<=d; endmodule'
        result,_=self.prove(design,design)
        self.assertEqual(result['failure_classification'],'AGENT4_RESET_SETUP_ERROR')
        self.assertEqual(result['execution'],{})
        self.assertNotEqual(result['equivalence'],'NOT_EQUIVALENT')

    def test_explicit_reset_contract_handles_nonstandard_signal_name(self):
        design=self.gold.replace('rst_n','clear_n')
        result,_=self.prove(design,design,clock='clk',clock_edge='posedge',reset='clear_n',
                            reset_active=0,reset_type='synchronous')
        self.assertEqual(result['status'],'PASS',result['formal'])

    def test_active_high_synchronous_reset_is_detected(self):
        design=self.gold.replace('rst_n','rst').replace('if (!rst)','if (rst)')
        result,_=self.prove(design,design)
        self.assertEqual(result['status'],'PASS',result['formal'])
        self.assertFalse(result['clock_reset_assumptions']['reset']['active_low'])

    def test_slang_preserves_struct_array_reset_connections(self):
        tools=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite'
        if not (tools/'share/yosys/plugins/slang.so').is_file(): self.skipTest('Slang plugin is not installed')
        gold='module top(input logic clk,rst_n,d,output logic y); typedef struct packed {logic flag;} item_t; item_t mem[2]; always_ff @(posedge clk) if (!rst_n) begin mem[0]<=0; mem[1]<=0; end else mem[0].flag<=d; assign y=mem[0].flag; endmodule'
        result,agent=self.prove(gold,self.gold,sv_frontend='slang')
        self.assertEqual(result['status'],'PASS',result['formal'])
        self.assertIn('plugin -i slang\nread_slang',Path(agent.config).read_text())
        self.assertEqual(result['clock_reset_assumptions']['sv_frontend'],'slang')

    def test_multiple_clocks_return_setup_error(self):
        design='module top(input clk,input clk2,input rst_n,input d,output reg y,output reg z); always @(posedge clk) if (!rst_n) y<=0; else y<=d; always @(posedge clk2) if (!rst_n) z<=0; else z<=d; endmodule'
        result,_=self.prove(design,design)
        self.assertEqual(result['failure_classification'],'AGENT4_RESET_SETUP_ERROR')
        self.assertNotEqual(result['equivalence'],'NOT_EQUIVALENT')


class SystemLECInputAndRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper=agent2_tests.SystemAgent2Tests('test_unseen_controller_invokes_llm_with_approved_inputs')
        cls.helper.setUp(); cls.validation_run=validated_fixture(cls.helper)
        cls.lineage=authenticate_validation(cls.helper.manifest_path,cls.validation_run)

    @classmethod
    def tearDownClass(cls): cls.helper.doCleanups()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.backend=Path(self.temp.name)/'backend with spaces'; self.backend.mkdir()
        self.netlist=self.backend/'arbitrary implementation name.v'
        self.netlist.write_text('module ddr4_controller_top(input clk, input rst_n); endmodule\n')
        self.report_root=Path(self.temp.name)/'formal reports'

    def load(self,**kwargs): return load_backend_handoff(str(self.backend),self.lineage,**kwargs)

    def error(self,classification,call):
        with self.assertRaises(LECInputError) as raised: call()
        self.assertEqual(raised.exception.classification,classification)
        return raised.exception

    @contextlib.contextmanager
    def changed(self,path,data):
        path=Path(path); original=path.read_bytes()
        path.write_bytes(data.encode() if isinstance(data,str) else json.dumps(data).encode())
        try: yield
        finally: path.write_bytes(original)

    def args(self,**overrides):
        values=dict(input=str(self.helper.manifest_path),netlist=None,timeout=10,depth=5,eqy='eqy',yosys='yosys',
            validation_run=str(self.validation_run),lec_handoff=str(self.backend),library=[],implementation_top=None,
            include_dir=[],port_map=None,report_root=str(self.report_root),reset_cycles=4,sv_frontend='verilog',strategy='sat',sby_engine='abc pdr')
        values.update(overrides); return SimpleNamespace(**values)

    def mock_run(self,proof='PASS',prepare_fail=None,mutation=None,opaque=False,strategy='sat',available_sby=True,
                 skipped=(),usage_types=(),available_bitwuzla=True):
        agent=SystemLECAgent(self.args(strategy=strategy))
        def command(name,command):
            if name.endswith('_version'): return dict(return_code=0,timed_out=False),name+' regression version'
            if name=='liberty_usage':
                write(Path(agent.report.directory)/'liberty_usage.json',{'modules':{'child':{'cells':{
                    str(i):{'type':kind} for i,kind in enumerate(usage_types)}}}})
                return dict(return_code=0,timed_out=False),'mock implementation inventory'
            if name=='libraries':
                if prepare_fail=='libraries': return dict(return_code=1,timed_out=False),'Error: unsupported library function'
                path=Path(agent.report.directory)/'libraries'
                write(path/'prepared.json',{'modules':{name:{'attributes':{'blackbox':'1'} if opaque else {}}
                      for name in agent.backend['required_cells'] if name not in skipped}})
                (path/'prepared.il').write_text('mock functional library RTLIL\n')
                return dict(return_code=0,timed_out=False),'mock library models\n'+''.join(
                    'Ignoring cell {} with missing function on output GCLK.\n'.format(name) for name in skipped)
            side='reference' if name=='reference' else 'implementation'
            if prepare_fail==side: return dict(return_code=1,timed_out=False),'unsupported source construct'
            top=self.lineage['top']
            path=Path(agent.report.directory)/side
            write(path/'prepared.json',{'modules':{top:{'ports':yosys_ports(self.lineage['ports']),'cells':{}}}})
            (path/'prepared.il').write_text('mock RTLIL preparation\n')
            return dict(return_code=0,timed_out=False),'mock preparation'
        def eqy():
            agent.work=str(Path(agent.report.directory)/'eqy_work'); proof_tree(agent.work,proof,base_fail=proof=='FAIL')
            agent.execution=dict(return_code=0 if proof=='PASS' else 1,timed_out=proof=='TIMEOUT')
            if mutation: mutation()
            return agent.execution
        def executable(name,**kwargs):
            if not available_sby and name in ('sby','yosys-abc','abc'): return None
            if not available_bitwuzla and name=='bitwuzla': return None
            return sys.executable
        with patch('agent4_lec.shutil.which',side_effect=executable),patch.object(agent,'command',side_effect=command),patch.object(agent,'run_eqy',side_effect=eqy):
            with contextlib.redirect_stdout(io.StringIO()): result=agent.run()
        return result,agent

    def repair_run(self,fault='eqy',reply=None,persistent=False,final_proof='PASS',on_llm=None,mutation=None):
        agent=SystemLECAgent(self.args())
        def command(name,argv):
            if name.endswith('_version'): return dict(return_code=0,timed_out=False),'mock formal version'
            broken=persistent or not agent.attempts
            if name=='reference' and fault=='yosys' and broken:
                return dict(return_code=1,timed_out=False),'ERROR: No such command: read_systemverilog. Exact setup diagnostic.'
            if name=='libraries' and fault=='library_path' and broken:
                return dict(return_code=1,timed_out=False),"ERROR: Can't open library file /incorrect/generated/path/cells.v"
            if name=='libraries':
                path=Path(agent.report.directory)/'libraries'
                write(path/'prepared.json',{'modules':{name:{'attributes':{}} for name in agent.backend['required_cells']}})
            else:
                path=Path(agent.report.directory)/name
                write(path/'prepared.json',{'modules':{agent.lineage['top']:{'ports':yosys_ports(agent.lineage['ports']),'cells':{}}}})
            (path/'prepared.il').write_text('mock immutable prepared RTLIL\n')
            return dict(return_code=0,timed_out=False),'mock preparation'
        def eqy():
            agent.work=str(Path(agent.report.directory)/'eqy_work')
            broken=persistent or not agent.attempts
            if fault=='eqy' and broken:
                Path(agent.work).mkdir()
                (Path(agent.report.directory)/'stderr.log').write_text('ERROR: Invalid section [broken_configuration]. Exact EQY setup diagnostic.\n')
                agent.execution=dict(return_code=1,timed_out=False)
            else:
                proof_tree(agent.work,final_proof,base_fail=final_proof=='FAIL')
                agent.execution=dict(return_code=0 if final_proof=='PASS' else 1,timed_out=False)
            if mutation: mutation(agent)
            return agent.execution
        def llm(context):
            if on_llm: on_llm(agent,context)
            if callable(reply): return reply(context)
            return reply if reply is not None else {'diagnosis':'Regenerate trusted setup after the exact tool configuration error.',
                                                   'setup':{'sv_frontend':'verilog','eqy_explicit_yosys':True}}
        with patch('agent4_lec.shutil.which',return_value=sys.executable), \
             patch.object(agent,'command',side_effect=command),patch.object(agent,'run_eqy',side_effect=eqy) as formal, \
             patch('agent4_lec.call_llm_for_setup_repair',side_effect=llm) as model, \
             patch.dict('os.environ',{'TAMU_MODEL':'test-lec-model'}),contextlib.redirect_stdout(io.StringIO()):
            result=agent.run()
        return result,agent,model,formal

    def test_deterministic_success_never_calls_llm(self):
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run()
        model.assert_not_called()
        self.assertEqual(result['generation_mode'],'DETERMINISTIC')
        self.assertEqual(result['llm_model'],'NOT_INVOKED')
        self.assertEqual(len(result['attempts']),1)

    def test_stale_reference_copies_never_call_llm(self):
        target=self.backend/Path(self.lineage['rtl_files'][0]).name
        target.write_text('// stale reference')
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run()
        model.assert_not_called()
        self.assertEqual(result['failure_classification'],'AGENT4_REFERENCE_RTL_MISMATCH')
        self.assertEqual(result['llm_model'],'NOT_INVOKED')

    def test_missing_netlist_never_calls_llm(self):
        self.netlist.unlink()
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run()
        model.assert_not_called()
        self.assertEqual(result['equivalence'],'INPUT_NOT_AVAILABLE')

    def test_missing_trusted_models_block_inputs_without_llm(self):
        self.netlist.write_text('module ddr4_controller_top(input clk,input rst_n); SKY_CELL u1(.A(clk)); endmodule')
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run()
        model.assert_not_called()
        self.assertEqual(result['equivalence'],'LIBRARY_SETUP_ERROR')
        self.assertEqual(result['disposition'],'BLOCKED_AGENT4_INPUT')

    def test_eqy_configuration_failure_uses_llm_then_formal_authority(self):
        result,agent,model,formal=self.repair_run()
        self.assertEqual(result['equivalence'],'EQUIVALENT')
        self.assertEqual((model.call_count,formal.call_count),(1,2))
        self.assertIn('Exact EQY setup diagnostic',json.dumps(model.call_args[0][0]))
        self.assertEqual(result['generation_mode'],'LLM_REPAIRED')
        self.assertEqual(result['llm_model'],'test-lec-model')
        self.assertTrue((Path(agent.report.directory)/'attempts/attempt_02/llm_diagnosis.json').is_file())
        self.assertTrue((Path(agent.report.directory)/'attempts/attempt_01/stderr.log').is_file())

    def test_yosys_configuration_failure_uses_llm_then_reruns_preparation(self):
        result,_,model,formal=self.repair_run('yosys',reply={'diagnosis':'Use the native frontend with deferred elaboration.',
            'setup':{'sv_frontend':'verilog','read_verilog_defer':True}})
        self.assertEqual(result['equivalence'],'EQUIVALENT')
        self.assertEqual((model.call_count,formal.call_count),(1,1))
        self.assertIn('read_systemverilog',json.dumps(model.call_args[0][0]))

    def test_existing_trusted_library_wrong_generated_path_can_be_repaired(self):
        self.netlist.write_text('module ddr4_controller_top(input clk,input rst_n); SKY_CELL u1(.A(clk)); endmodule')
        library=self.backend/'trusted_cells.v'; library.write_text('module SKY_CELL(input A); endmodule')
        write(self.backend/'lec_handoff.json',{'synthesis':{'library_files':[str(library)]}})
        result,_,model,_=self.repair_run('library_path')
        self.assertEqual(result['equivalence'],'EQUIVALENT'); self.assertEqual(model.call_count,1)
        self.assertIn(str(library),json.dumps(model.call_args[0][0]['trusted_library_files']))

    def test_setup_repair_preserves_exact_reference_and_implementation_bytes(self):
        before=file_rows(self.lineage['rtl_files']+[str(self.netlist)])
        result,_,_,_=self.repair_run()
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(before,file_rows([r['path'] for r in before]))

    def test_five_attempts_exhausted_are_setup_failure_not_logic_failure(self):
        result,agent,model,formal=self.repair_run(persistent=True)
        self.assertEqual((len(result['attempts']),model.call_count,formal.call_count),(5,4,5))
        self.assertEqual(result['failure_classification'],'AGENT4_LEC_REPAIR_EXHAUSTED')
        self.assertNotEqual(result['disposition'],'RETURN_TO_BACKEND_NETLIST_FIX')
        self.assertFalse(result['ready_for_backend_implementation'])
        self.assertEqual(len(list((Path(agent.report.directory)/'attempts').iterdir())),5)

    def test_proven_nonequivalence_never_invokes_llm(self):
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run('FAIL')
        model.assert_not_called()
        self.assertEqual(result['failure_classification'],'LOGIC_EQUIVALENCE_FAILURE')
        self.assertEqual(result['disposition'],'RETURN_TO_BACKEND_NETLIST_FIX')

    def test_proven_nonequivalence_after_setup_repair_stops_immediately(self):
        result,_,model,formal=self.repair_run(final_proof='FAIL')
        self.assertEqual((model.call_count,formal.call_count),(1,2))
        self.assertEqual(result['disposition'],'RETURN_TO_BACKEND_NETLIST_FIX')
        self.assertEqual(result['equivalence'],'NOT_EQUIVALENT')

    def test_llm_equivalence_claim_cannot_override_formal_unknown(self):
        result,_,model,_=self.repair_run(final_proof='UNKNOWN',reply={'diagnosis':'These designs appear equivalent.','setup':{}})
        self.assertEqual(model.call_count,1)
        self.assertEqual(result['equivalence'],'INCONCLUSIVE')
        self.assertFalse(result['ready_for_backend_implementation'])

    def invalid_repair(self,setup=None,reply=None):
        result,_,model,formal=self.repair_run(reply=reply if reply is not None else {'diagnosis':'invalid repair','setup':setup})
        self.assertEqual(formal.call_count,1)
        self.assertEqual(model.call_count,4)
        self.assertEqual(result['failure_classification'],'AGENT4_LEC_REPAIR_EXHAUSTED')
        self.assertTrue(all(r['failure_classification']=='AGENT4_INVALID_SETUP_REPAIR' for r in result['attempts'][1:]))

    def test_llm_cannot_edit_design_files(self):
        self.invalid_repair(reply={'diagnosis':'edit source','setup':{},'edits':[{'file':str(self.netlist),'new':'module replaced; endmodule'}]})
    def test_llm_cannot_remove_output_comparisons(self): self.invalid_repair({'outputs':['rsp_valid']})
    def test_llm_cannot_remove_partitions(self): self.invalid_repair({'partitions':['only_one']})
    def test_llm_cannot_insert_raw_yosys_blackboxes(self): self.invalid_repair({'yosys_script':'blackbox *'})
    def test_llm_cannot_weaken_library_semantics(self): self.invalid_repair({'library_options':'-lib -ignore_miss_func'})
    def test_llm_cannot_change_clock_reset(self): self.invalid_repair({'reset_cycles':0,'clock':'other_clock'})
    def test_llm_cannot_change_design_parameters(self): self.invalid_repair({'parameters':{'WIDTH':1}})
    def test_llm_cannot_drop_reference_files(self): self.invalid_repair({'reference_file_order':self.lineage['rtl_files'][:-1]})
    def test_llm_cannot_replace_implementation_file(self): self.invalid_repair({'implementation_file_order':['/tmp/fabricated.v']})
    def test_llm_cannot_substitute_library(self): self.invalid_repair({'library_file_order':['/tmp/fabricated.lib']})
    def test_llm_cannot_reduce_required_proof_depth(self): self.invalid_repair({'depth':1})
    def test_llm_cannot_choose_another_implementation_design(self): self.invalid_repair({'implementation_top':'other_top'})
    def test_llm_cannot_change_reset_correspondence(self):
        mapping={p['name']:p['name'] for p in self.lineage['ports']}; mapping['rst_n']='another_reset'
        self.invalid_repair({'port_map':mapping})
    def test_llm_cannot_omit_outputs_from_port_mapping(self):
        self.invalid_repair({'port_map':{'clk':'clk','rst_n':'rst_n'}})

    def test_reference_mutation_during_model_call_blocks_formal_rerun(self):
        path=Path(self.lineage['rtl_files'][0]); original=path.read_bytes()
        try:
            result,_,model,formal=self.repair_run(on_llm=lambda agent,context:path.write_bytes(original+b'\n// concurrent mutation'))
            self.assertEqual((model.call_count,formal.call_count),(1,1))
            self.assertEqual(result['failure_classification'],'AGENT4_REFERENCE_RTL_MISMATCH')
        finally: path.write_bytes(original)

    def test_netlist_mutation_during_model_call_blocks_formal_rerun(self):
        original=self.netlist.read_bytes()
        result,_,model,formal=self.repair_run(on_llm=lambda agent,context:self.netlist.write_bytes(original+b'\n// concurrent mutation'))
        self.assertEqual((model.call_count,formal.call_count),(1,1))
        self.assertEqual(result['failure_classification'],'AGENT4_IMPLEMENTATION_INPUT_CHANGED')

    def test_new_rtl_hash_mismatch_never_calls_llm(self):
        path=Path(self.lineage['rtl_files'][0])
        with self.changed(path,path.read_text()+'\n// new source'),patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run()
        model.assert_not_called(); self.assertEqual(result['failure_classification'],'AGENT4_REFERENCE_RTL_MISMATCH')

    def test_runtime_is_consolidated_without_duplicate_helpers_or_schema(self):
        directory=Path(agent4_lec.__file__).parent
        for name in ('system_lec_handoff.py','system_lec_verification.py','lec_handoff.schema.json'):
            self.assertFalse((directory/name).exists())

    def test_repaired_setups_still_require_all_partition_evidence(self):
        def delete_partition(agent):
            if agent.attempts:
                (Path(agent.work)/'strategies/output/induction/status').unlink()
        result,_,model,_=self.repair_run(mutation=delete_partition)
        self.assertEqual(model.call_count,1)
        self.assertNotEqual(result['equivalence'],'EQUIVALENT')
        self.assertFalse(result['ready_for_backend_implementation'])

    def test_model_cannot_relabel_proof_result(self):
        self.invalid_repair(reply={'diagnosis':'claim equivalence','setup':{},'equivalence':'EQUIVALENT'})

    def test_invalid_repair_does_not_forget_trusted_port_correspondence(self):
        mapping={p['name']:p['name'] for p in self.lineage['ports']}
        mapping['txn_valid'],mapping['txn_is_write']=mapping['txn_is_write'],mapping['txn_valid']
        def response(context):
            return {'diagnosis':'unsafe comparison change','setup':{'outputs':[]} if len(context['previous_attempts'])==1 else {'port_map':mapping}}
        result,_,model,formal=self.repair_run(reply=response)
        self.assertEqual((model.call_count,formal.call_count),(4,1))
        self.assertEqual(result['failure_classification'],'AGENT4_LEC_REPAIR_EXHAUSTED')

    def test_repair_may_permute_complete_plain_rtl_inventory(self):
        reverse=list(reversed(self.lineage['rtl_files']))
        result,agent,model,_=self.repair_run(reply={'diagnosis':'Resolve source ordering without dropping files.',
            'setup':{'reference_file_order':reverse}})
        self.assertEqual(result['equivalence'],'EQUIVALENT')
        self.assertEqual(model.call_count,1)
        self.assertEqual(agent.rtl_files,list(reversed(agent.reference_snapshots)))

    def test_repair_cannot_reorder_macro_dependent_reference(self):
        first=self.backend/'macro_source.sv'; second=self.backend/'plain_source.sv'
        first.write_text('`define ORDER_SENSITIVE 1\nmodule a; endmodule')
        second.write_text('module b; endmodule')
        files=[str(first),str(second)]; before=file_rows(files)
        agent=SimpleNamespace(check_inputs=lambda:self.assertEqual(before,file_rows(files)),
            lineage={'rtl_files':files},backend={'netlist_files':[str(self.netlist)],'libraries':[]},
            args=SimpleNamespace(depth=20))
        with self.assertRaisesRegex(ValueError,'preprocessing semantics'):
            validate_llm_repair_integrity(agent,{'diagnosis':'unsafe order',
                'setup':{'reference_file_order':list(reversed(files))}})

    def test_current_lineage_and_all_hashes_verified(self):
        result=authenticate_validation(self.helper.manifest_path,self.validation_run)
        self.assertEqual(set(result['rtl_hashes']),set(self.helper.approved['rtl_files']))
        self.assertEqual(result['run_id'],self.validation_run.name)

    def test_arbitrary_multi_file_reference_copies(self):
        paths=[]
        for i,path in enumerate(self.lineage['rtl_files']):
            target=self.backend/('unrelated golden name {}.sv'.format(i)); target.write_bytes(Path(path).read_bytes()); paths.append(target.name)
        write(self.backend/'lec_handoff.json',{'reference':{'rtl_files':paths},'implementation':{'netlist':self.netlist.name,'top':self.lineage['top']}})
        result=self.load(); self.assertEqual(len(result['reference_copies']),len(self.lineage['rtl_files']))
        self.assertEqual(result['netlist_files'],[str(self.netlist)])

    def test_dynamic_reference_count_growth(self):
        baseline=len(self.lineage['rtl_files']); lineage=copy.deepcopy(self.lineage)
        for i in range(17):
            p=Path(self.temp.name)/('extra_bundle_{}.sv'.format(i)); p.write_text('module extra_{}; endmodule'.format(i))
            row=file_rows([p])[0]; lineage['rtl_files'].append(str(p)); lineage['rtl_hashes'][str(p)]=row['sha256']
        paths=[]
        for p in lineage['rtl_files']:
            target=self.backend/Path(p).name; target.write_bytes(Path(p).read_bytes()); paths.append(target.name)
        write(self.backend/'lec_handoff.json',{'reference':{'rtl_files':paths},'implementation':{'netlist':self.netlist.name}})
        result=load_backend_handoff(str(self.backend),lineage)
        self.assertEqual(len(result['reference_copies']),baseline+17)

    def test_validated_rtl_change_invalidates_lineage(self):
        path=self.lineage['rtl_files'][0]
        with self.changed(path,Path(path).read_text()+'\n// changed RTL'):
            self.error('AGENT4_REFERENCE_RTL_MISMATCH',lambda:authenticate_validation(self.helper.manifest_path,self.validation_run))

    def test_manifest_hash_change_invalidates_lineage(self):
        with self.changed(self.helper.manifest_path,self.helper.manifest_path.read_text()+'\n'):
            self.error('AGENT4_REFERENCE_RTL_MISMATCH',lambda:authenticate_validation(self.helper.manifest_path,self.validation_run))

    def test_backend_copy_mismatch_stops_before_formal(self):
        for path in self.lineage['rtl_files']: (self.backend/Path(path).name).write_bytes(Path(path).read_bytes())
        p=self.backend/Path(self.lineage['rtl_files'][0]).name; p.write_text(p.read_text()+'\n// unvalidated backend edit')
        result,agent=self.mock_run()
        self.assertEqual(result['failure_classification'],'AGENT4_REFERENCE_RTL_MISMATCH'); self.assertFalse(result['formal_invoked'])
        self.assertEqual(result['disposition'],'BLOCKED_AGENT4_INPUT')

    def test_backend_reference_inventory_incomplete_is_blocked(self):
        p=Path(self.lineage['rtl_files'][0]); (self.backend/p.name).write_bytes(p.read_bytes())
        self.error('AGENT4_REFERENCE_RTL_MISMATCH',self.load)

    def test_configuration_identity_mismatch_is_blocked(self):
        write(self.backend/'lec_handoff.json',{'reference':{'configuration_hash':'changed'},'implementation':{'netlist':self.netlist.name}})
        self.error('AGENT4_REFERENCE_RTL_MISMATCH',self.load)
    def test_invalid_backend_json_is_implementation_setup_error(self):
        (self.backend/'lec_handoff.json').write_text('{broken JSON')
        result,_=self.mock_run()
        self.assertEqual(result['equivalence'],'IMPLEMENTATION_SETUP_ERROR'); self.assertFalse(result['formal_invoked'])

    def test_unique_netlist_discovery(self): self.assertEqual(self.load()['selection'],'unique source discovery')
    def test_explicit_netlist_selection_amid_other_files(self):
        (self.backend/'other.v').write_text('module other; endmodule')
        self.assertEqual(self.load(netlist=str(self.netlist))['selection'],'explicit')
    def test_directory_netlist_discovery_excludes_selected_library(self):
        model=self.backend/'models.v'; model.write_text('module technology_cell; endmodule')
        result=self.load(libraries=[str(model)])
        self.assertEqual(result['netlist_files'],[str(self.netlist)])
    def test_ambiguous_netlist_is_setup_error(self):
        (self.backend/'other.v').write_text('module other; endmodule')
        self.error('AGENT4_IMPLEMENTATION_SETUP_ERROR',self.load)
    def test_missing_netlist_is_input_unavailable(self):
        self.netlist.unlink(); self.error('AGENT4_INPUT_NOT_AVAILABLE',self.load)
    def test_missing_explicit_netlist_is_input_unavailable(self):
        self.error('AGENT4_INPUT_NOT_AVAILABLE',lambda:self.load(netlist=str(self.backend/'missing.v')))
    def test_duplicate_top_is_setup_error(self):
        self.netlist.write_text('module ddr4_controller_top; endmodule\nmodule ddr4_controller_top; endmodule')
        self.error('AGENT4_IMPLEMENTATION_SETUP_ERROR',self.load)
    def test_missing_implementation_top_is_setup_error(self):
        self.error('AGENT4_IMPLEMENTATION_SETUP_ERROR',lambda:self.load(implementation_top='missing_top'))
    def test_explicit_top_rename(self):
        self.netlist.write_text('module renamed_top; endmodule')
        self.error('AGENT4_PORT_MAPPING_ERROR',self.load)
        self.assertEqual(self.load(implementation_top='renamed_top')['top'],'renamed_top')

    def test_technology_cell_discovery_handles_escaped_instance_names(self):
        self.netlist.write_text('module ddr4_controller_top(input A,output Y); technology_cell \\reg[0] (.A(A),.Y(Y)); endmodule')
        error=self.error('AGENT4_LIBRARY_SETUP_ERROR',self.load)
        self.assertEqual(error.evidence['missing_cells'],['technology_cell'])
    def test_functional_verilog_library_resolves_cell(self):
        self.netlist.write_text('module ddr4_controller_top; technology_cell c(); endmodule')
        model=self.backend/'models.v'; model.write_text('module technology_cell(input A,output Y); assign Y=A; endmodule')
        write(self.backend/'lec_handoff.json',{'implementation':{'netlist':self.netlist.name},'synthesis':{'library_files':['models.v']}})
        self.assertEqual(self.load()['required_cells'],['technology_cell'])
    def test_functional_liberty_library_inventory(self):
        self.netlist.write_text('module ddr4_controller_top; technology_cell c(); endmodule')
        model=self.backend/'cells.lib'; model.write_text('library(test) { cell("technology_cell") { pin(Y) { function : "A"; } } }')
        result=self.load(libraries=[str(model)]); self.assertEqual(result['libraries'][0]['cells'],['technology_cell'])
    def library_package(self):
        self.netlist.write_text('module ddr4_controller_top; technology_cell c(); endmodule')
        model=self.backend/'cells.lib'; model.write_text('library(test) { cell("technology_cell") { pin(Y) { function : "A"; } } }')
        write(self.backend/'lec_handoff.json',{'implementation':{'netlist':self.netlist.name},'synthesis':{'library_files':['cells.lib']}})
    def test_library_models_are_compiled_and_not_blackboxed(self):
        self.library_package(); result,agent=self.mock_run()
        self.assertEqual(result['equivalence'],'EQUIVALENT'); self.assertEqual(result['preparation']['libraries']['status'],'PASS')
        script=Path(agent.attempt_directory,'config/libraries.ys').read_text()
        self.assertIn('read_liberty -ignore_miss_func ',script); self.assertNotIn('read_liberty -lib',script)
    def test_unused_missing_function_cells_allow_formal(self):
        self.library_package()
        skipped=['sky130_fd_sc_hd__dlclkp_1','sky130_fd_sc_hd__sdlclkp_1']
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,agent=self.mock_run(skipped=skipped,usage_types=['technology_cell'])
        model.assert_not_called()
        self.assertEqual(result['equivalence'],'EQUIVALENT')
        self.assertTrue(result['formal_invoked'])
        self.assertEqual(result['preparation']['libraries']['skipped_cells'],skipped)
        self.assertEqual(read_json(Path(agent.attempt_directory)/'liberty_cell_check.json'),
                         {'skipped_cells':skipped,'instantiated_skipped_cells':[]})
    def test_instantiated_missing_function_cell_blocks_formal_and_llm(self):
        self.library_package()
        with patch('agent4_lec.call_llm_for_setup_repair') as model:
            result,_=self.mock_run(skipped=['technology_cell'],usage_types=['technology_cell'])
        model.assert_not_called()
        self.assertEqual(result['equivalence'],'LIBRARY_SETUP_ERROR')
        self.assertEqual(result['failure_classification'],'AGENT4_UNSUPPORTED_LIBRARY_CELL')
        self.assertIn('UNSUPPORTED_LIBRARY_CELL',result['failure_detail'])
        self.assertEqual(result['failure_evidence']['instantiated_skipped_cells'],['technology_cell'])
        self.assertFalse(result['formal_invoked'])
        self.assertEqual(len(result['attempts']),1)
    def test_unsupported_library_cell_is_never_recoverable_setup(self):
        self.assertFalse(is_recoverable_setup_error(
            {'result':'LIBRARY_SETUP_ERROR','failure_classification':'AGENT4_UNSUPPORTED_LIBRARY_CELL'},
            'library_preparation',{'stderr':'ERROR: syntax error'},inputs_verified=True))
    def test_unmodeled_library_cell_cannot_produce_pass(self):
        self.library_package(); result,_=self.mock_run(opaque=True)
        self.assertEqual(result['equivalence'],'LIBRARY_SETUP_ERROR'); self.assertFalse(result['formal_invoked'])
    def test_library_parser_failure_has_own_result(self):
        self.library_package(); result,_=self.mock_run(prepare_fail='libraries')
        self.assertEqual(result['equivalence'],'LIBRARY_SETUP_ERROR'); self.assertNotEqual(result['failure_classification'],'LOGIC_EQUIVALENCE_FAILURE')
    def test_bad_netlist_parser_after_valid_library_is_not_library_failure(self):
        self.library_package(); result,_=self.mock_run(prepare_fail='implementation')
        self.assertEqual(result['equivalence'],'UNSUPPORTED_CONSTRUCT')
    def test_missing_library_file_has_distinct_classification(self):
        self.error('AGENT4_LIBRARY_SETUP_ERROR',lambda:self.load(libraries=[str(self.backend/'missing.lib')]))
    def test_builtin_primitives_do_not_need_arbitrary_blackboxes(self):
        self.netlist.write_text('module ddr4_controller_top(input A,output Y); not g(Y,A); endmodule')
        self.assertEqual(self.load()['required_cells'],[])

    def test_mapped_hash_mismatch_is_implementation_setup_error(self):
        write(self.backend/'lec_handoff.json',{'implementation':{'netlist':self.netlist.name,'hash':'wrong'}})
        self.error('AGENT4_IMPLEMENTATION_SETUP_ERROR',self.load)
    def test_synthesis_provenance_is_recorded_and_hashed(self):
        (self.backend/'run_genus_mapped.tcl').write_text('set TOP "ddr4_controller_top"\nset_db syn_map_effort medium\nsyn_map\n')
        (self.backend/'setup_summary.txt').write_text('Top: ddr4_controller_top\nGenus bin: /tools/genus\n')
        result=self.load(); self.assertEqual(len(result['synthesis']['files']),2); self.assertEqual(result['synthesis']['script_top'],self.lineage['top'])

    def test_complete_formal_pass_requires_prep_and_lineage(self):
        result,agent=self.mock_run()
        self.assertEqual(result['equivalence'],'EQUIVALENT'); self.assertTrue(result['ready_for_backend_implementation'])
        self.assertTrue(result['agent123_lineage_verified']); self.assertEqual(set(result['preparation']),{'reference','implementation'})
        self.assertFalse(result['agent5_executed']); self.assertFalse(result['backend_implementation_executed'])
        aggregate=read_json(Path(agent.report.run_dir)/'system_lec_report.json')
        self.assertEqual(aggregate['status'],'PASS_WITH_GAPS'); self.assertEqual(aggregate['agents']['agent1'],'PASS')
        self.assertEqual(aggregate['agents']['agent3'],'PASS_WITH_GAPS'); self.assertEqual(aggregate['agents']['agent4'],'PASS')
        self.assertTrue(aggregate['ready_for_backend_implementation'])
    def test_real_formal_mismatch_returns_to_backend(self):
        result,agent=self.mock_run('FAIL')
        self.assertEqual(result['failure_classification'],'LOGIC_EQUIVALENCE_FAILURE')
        feedback=read_json(Path(agent.report.directory)/'backend_feedback.json')
        self.assertEqual(feedback['disposition'],'RETURN_TO_BACKEND_NETLIST_FIX'); self.assertTrue(feedback['formal']['formal_traces'])
        self.assertIsNone(feedback['suspected_backend_stage'])
    def test_unknown_proof_never_passes_or_blames_frontend(self):
        result,agent=self.mock_run('UNKNOWN')
        self.assertEqual(result['equivalence'],'INCONCLUSIVE'); self.assertNotEqual(result['disposition'],'RETURN_TO_BACKEND_NETLIST_FIX')
    def test_tool_error_is_not_logic_mismatch(self):
        result,_=self.mock_run('ERROR'); self.assertEqual(result['equivalence'],'INCONCLUSIVE')
        self.assertEqual(result['failure_classification'],'AGENT4_TOOL_ERROR')
    def test_timeout_classification(self):
        result,_=self.mock_run('TIMEOUT'); self.assertEqual(result['equivalence'],'TIMEOUT')
    def test_reference_parser_failure_is_not_nonequivalence(self):
        result,_=self.mock_run(prepare_fail='reference'); self.assertEqual(result['equivalence'],'UNSUPPORTED_CONSTRUCT')
    def test_implementation_parser_failure_is_not_nonequivalence(self):
        result,_=self.mock_run(prepare_fail='implementation'); self.assertEqual(result['equivalence'],'UNSUPPORTED_CONSTRUCT')
    def test_input_mutation_during_formal_revokes_pass(self):
        original=self.netlist.read_text()
        result,_=self.mock_run(mutation=lambda:self.netlist.write_text(original+'\n// backend concurrently changed'))
        self.assertEqual(result['failure_classification'],'AGENT4_IMPLEMENTATION_INPUT_CHANGED'); self.assertFalse(result['ready_for_backend_implementation'])
    def test_backend_netlist_rerun_does_not_rerun_agents123(self):
        before=file_rows([self.validation_run/'final_system_report.json',self.validation_run/'agent1/validation_result.json',self.validation_run/'agent2/system_report.json',self.validation_run/'agent3/system_report.json'])
        first,_=self.mock_run('FAIL'); self.netlist.write_text(self.netlist.read_text()+'\n// corrected backend netlist')
        second,_=self.mock_run('PASS')
        self.assertEqual(before,file_rows([r['path'] for r in before])); self.assertFalse(second['agents123_rerun'])
        self.assertNotEqual(first['implementation']['netlist_hashes'],second['implementation']['netlist_hashes'])
        self.assertEqual(len(list(self.report_root.iterdir()))-1,2)  # two run directories plus latest.json
    def test_eqy_config_separates_gold_and_gate(self):
        result,agent=self.mock_run(); config=Path(agent.config).read_text()
        gold,gate=config.split('[gate]'); self.assertIn('reference/prepared.il',gold); self.assertNotIn('implementation/prepared.il',gold)
        self.assertIn('implementation/prepared.il',gate); self.assertIn('nodefault',gate)
        self.assertNotIn('blackbox',config); self.assertNotIn('setundef -zero',config)
    def test_reference_and_implementation_yosys_scripts_are_separate(self):
        _,agent=self.mock_run()
        gold=Path(agent.attempt_directory,'config/reference.ys').read_text(); gate=Path(agent.attempt_directory,'config/implementation.ys').read_text()
        self.assertNotIn(self.netlist.name,gold); self.assertIn(self.netlist.name,gate)
        for text in (gold,gate): self.assertIn('hierarchy -check',text); self.assertIn('memory_map',text)
    def test_auto_engine_uses_ordered_bounded_ladder_when_available(self):
        result,agent=self.mock_run(strategy='auto')
        self.assertEqual(result['clock_reset_assumptions']['strategy'],'ladder')
        config=Path(agent.config).read_text()
        self.assertTrue(config.split('[strategy quick_sat]')[1].startswith(
            LADDER_CONFIG.replace('depth 5\n','depth 5\nnoapply agent4_formal_top.rsp_rdata\n')))
        self.assertIn('[strategy pdr_extended]\nuse sby\nengine abc pdr\ntimeout 180', config)
        self.assertIn('[strategy smtbmc_extended]\nuse sby\nengine smtbmc bitwuzla\ndepth 30\ntimeout 180', config)
    def test_auto_engine_without_bitwuzla_keeps_pdr_and_inconclusive(self):
        result,agent=self.mock_run('UNKNOWN',strategy='auto',available_bitwuzla=False)
        self.assertEqual(result['equivalence'],'INCONCLUSIVE')
        self.assertEqual(result['failure_classification'],'AGENT4_PROOF_INCOMPLETE')
        self.assertNotIn('[strategy smtbmc]',Path(agent.config).read_text())
        self.assertEqual(result['formal']['counterexamples'],[])
    def test_caller_makeflags_preserved_by_system_tools(self):
        with patch.dict(os.environ,{'MAKEFLAGS':'-j4'}):
            _,agent=self.mock_run(strategy='auto')
        self.assertEqual(agent.tool_environment['MAKEFLAGS'],'-j4')
    def test_auto_engine_reports_missing_pdr_tools_without_formal(self):
        result,agent=self.mock_run(strategy='auto',available_sby=False)
        self.assertEqual(result['equivalence'],'TOOL_ERROR')
        self.assertFalse(result['formal_invoked'])
    def test_whole_run_timeout_preserves_implementation_bytes(self):
        before=self.netlist.read_bytes()
        result,_=self.mock_run('TIMEOUT')
        self.assertEqual(result['equivalence'],'TIMEOUT')
        self.assertNotEqual(result['equivalence'],'NOT_EQUIVALENT')
        self.assertEqual(self.netlist.read_bytes(),before)
    def test_legacy_agent4_dispatch_is_preserved(self):
        with patch.object(sys,'argv',['agent4_lec.py','rtl','--netlist','mapped.v','--top','top']),patch.object(agent4_lec,'LEC_Agent') as constructor:
            instance=constructor.return_value; instance.run.return_value={'status':'PASS','details':{'errors':[]}}
            instance.report.directory='saved'
            with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(agent4_lec.main(),0)
            constructor.assert_called_once()
    def test_system_agent4_dispatch(self):
        with patch.object(sys,'argv',['agent4_lec.py','rtl','--system']),patch('agent4_lec.system_main',return_value=2) as call:
            self.assertEqual(agent4_lec.main(),2); call.assert_called_once_with(['rtl','--system'])
    def test_pipeline_lec_only_dispatch_does_not_call_agents123(self):
        with patch.object(sys,'argv',['run_pipeline.py',str(self.helper.manifest_path),'--system','--lec-only','--validation-run',str(self.validation_run),'--lec-handoff',str(self.backend)]),patch('agent4_lec.system_main',return_value=0) as call,patch.object(pipeline,'run_system_pipeline') as upstream:
            pipeline.main(); upstream.assert_not_called(); self.assertIn('--validation-run',call.call_args[0][0])


class LibertyUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rtl=self.root/'gold.sv'; self.rtl.write_text('module top(input A, output Y); assign Y=A; endmodule')
        self.netlist=self.root/'gate.v'; self.netlist.write_text('module top(input A, output Y); usable u(.A(A),.Y(Y)); endmodule')
        self.library=self.root/'cells with spaces.lib'
        self.library.write_text('''library(test) {
          cell(usable) { pin(A) { direction : input; } pin(Y) { direction : output; function : "A"; } }
          cell(skipped) { pin(A) { direction : input; } pin(Y) { direction : output; } }
        }''')
        self.agent=agent4_lec.LEC_Agent(str(self.rtl),str(self.netlist),top_module='top',libraries=[str(self.library)])

    def test_ladder_caps_sat_depth_even_with_older_pipeline_default(self):
        self.agent.depth=20
        with patch('agent4_lec.shutil.which',return_value=sys.executable):
            self.assertEqual('\n'.join(self.agent.eqy_strategies()),'[strategy quick_sat]'+LADDER_CONFIG)

    def legacy_run(self,used=(),return_code=0,timed_out=False):
        def command(argv,**kwargs):
            if timed_out: raise agent4_lec.subprocess.TimeoutExpired(argv,10)
            write(Path(self.agent.report.directory)/'liberty_usage.json',{'modules':{'nested':{'cells':{
                str(i):{'type':kind} for i,kind in enumerate(used)}}}})
            return SimpleNamespace(returncode=return_code,stdout='Ignoring cell skipped with missing function on output Y.\n')
        with patch('agent4_lec.subprocess.run',side_effect=command), \
             patch('agent4_lec.shutil.which',return_value=sys.executable), \
             patch.object(self.agent,'prepare_reset_context'), \
             patch.object(self.agent,'run_eqy') as formal,patch.object(self.agent,'parse_results'), \
             patch('agent4_lec.call_llm_for_setup_repair') as model:
            result=self.agent.run()
        model.assert_not_called()
        return result,formal

    def test_legacy_script_uses_functional_import_and_allows_unused_skip(self):
        result,formal=self.legacy_run(used=['usable'])
        formal.assert_called_once_with()
        script=Path(self.agent.config).read_text()
        self.assertIn('read_liberty -ignore_miss_func ',script)
        self.assertNotIn('read_liberty -lib',script)
        self.assertEqual(script.split('[strategy quick_sat]')[1],
                         LADDER_CONFIG)
        check=read_json(Path(self.agent.report.directory)/'liberty_cell_check.json')
        self.assertEqual(check,{'skipped_cells':['skipped'],'instantiated_skipped_cells':[]})
        self.assertNotEqual(result['status'],'PASS')  # Only the real formal result may pass.

    def test_legacy_skipped_cell_in_nested_module_stops_before_eqy(self):
        result,formal=self.legacy_run(used=['skipped'])
        formal.assert_not_called()
        self.assertEqual(result['status'],'ERROR')
        self.assertEqual(result['failure_classification'],'AGENT4_UNSUPPORTED_LIBRARY_CELL')
        self.assertEqual(result['failure_evidence']['instantiated_skipped_cells'],['skipped'])

    def test_failed_usage_check_cannot_reach_formal(self):
        result,formal=self.legacy_run(return_code=1)
        formal.assert_not_called()
        self.assertEqual(result['failure_classification'],'AGENT4_LIBRARY_SETUP_ERROR')

    def test_timed_out_usage_check_cannot_reach_formal(self):
        result,formal=self.legacy_run(timed_out=True)
        formal.assert_not_called()
        self.assertEqual(result['failure_classification'],'AGENT4_TIMEOUT')

    def test_changed_implementation_cannot_be_reported_as_formal_mismatch(self):
        def mutation(): self.netlist.write_text(self.netlist.read_text()+'\n// concurrent edit')
        with patch.object(self.agent,'check_liberty_usage'),patch.object(self.agent,'prepare_reset_context'), \
             patch.object(self.agent,'run_eqy',side_effect=mutation),patch.object(self.agent,'parse_results') as parse:
            result=self.agent.run()
        parse.assert_not_called()
        self.assertEqual(result['failure_classification'],'AGENT4_IMPLEMENTATION_INPUT_CHANGED')
        self.assertNotEqual(result['equivalence'],'NOT_EQUIVALENT')

    def real_usage_check(self):
        yosys=Path(__file__).resolve().parents[2]/'tools/oss-cad-suite/bin/yosys'
        if not yosys.is_file(): self.skipTest('Local Yosys is not installed')
        self.agent.yosys=str(yosys)
        class Report:
            directory=str(self.root)
            def artifact(report,name,**kwargs):
                if 'text' in kwargs: (self.root/name).write_text(kwargs['text'])
                if 'data' in kwargs: write(self.root/name,kwargs['data'])
        self.agent.report=Report()
        self.agent.check_liberty_usage()
        return read_json(self.root/'liberty_cell_check.json')

    def test_real_yosys_unused_skip_with_comment_and_string_is_safe(self):
        self.netlist.write_text('module top(input A, output Y); usable u(.A(A),.Y(Y)); '
                               '// skipped fake();\n localparam LABEL="skipped"; endmodule')
        before=[p.read_bytes() for p in [self.rtl,self.netlist,self.library]]
        self.assertEqual(self.real_usage_check(),{'skipped_cells':['skipped'],'instantiated_skipped_cells':[]})
        self.assertEqual(before,[p.read_bytes() for p in [self.rtl,self.netlist,self.library]])

    def test_real_yosys_macro_and_escaped_instance_cannot_hide_skipped_cell(self):
        self.netlist.write_text('`define CELL skipped\nmodule top(input A, output Y); '
                               '`CELL \\reg[0] (.A(A),.Y(Y)); endmodule')
        with self.assertRaises(LECInputError) as raised: self.real_usage_check()
        self.assertEqual(raised.exception.classification,'AGENT4_UNSUPPORTED_LIBRARY_CELL')


class PortAndResetTests(unittest.TestCase):
    def setUp(self):
        self.ports=[dict(name='clk',direction='input',width=1),dict(name='rst_n',direction='input',width=1),dict(name='d',direction='input',width=8),dict(name='q',direction='output',width=8)]
        self.reference=yosys_ports(self.ports)
    def test_port_mapping_identity(self): self.assertEqual(validate_ports(self.reference,self.reference,self.ports,{})['q'],'q')
    def test_explicit_renamed_port(self):
        implementation=dict(self.reference); implementation['out_q']=implementation.pop('q')
        self.assertEqual(validate_ports(self.reference,implementation,self.ports,{'q':'out_q'})['q'],'out_q')
    def test_width_mismatch_blocks_formal(self):
        impl=copy.deepcopy(self.reference); impl['q']['bits'].pop()
        with self.assertRaises(LECInputError): validate_ports(self.reference,impl,self.ports,{})
    def test_direction_mismatch_blocks_formal(self):
        impl=copy.deepcopy(self.reference); impl['q']['direction']='input'
        with self.assertRaises(LECInputError): validate_ports(self.reference,impl,self.ports,{})
    def test_no_silent_missing_port(self):
        impl=dict(self.reference); impl.pop('q')
        with self.assertRaises(LECInputError): validate_ports(self.reference,impl,self.ports,{})
    def test_no_silent_extra_port(self):
        impl=dict(self.reference); impl['scan_enable']=impl['clk']
        with self.assertRaises(LECInputError): validate_ports(self.reference,impl,self.ports,{})
    def test_reset_startup_and_unconstrained_later_reset(self):
        text=reset_wrapper('top',self.ports,dict(name='clk',edge='posedge'),dict(name='rst_n',active_low=True,synchronous=True),4)
        self.assertIn("? 1'b0 : rst_n",text); self.assertIn("agent4_phase == 3'd4",text)
        self.assertIn("assign q = agent4_compare ? agent4_output_3 : 8'd0",text)
    def test_active_high_reset_and_falling_clock(self):
        text=reset_wrapper('top',self.ports,dict(name='clk',edge='negedge'),dict(name='rst_n',active_low=False,synchronous=False),4)
        self.assertIn('negedge clk',text); self.assertIn("? 1'b1 : rst_n",text)
    def test_one_reset_edge_is_sufficient_without_masking_first_post_reset_state(self):
        text=reset_wrapper('top',self.ports,dict(name='clk',edge='posedge'),
                           dict(name='rst_n',active_low=True,synchronous=True),1,settle_cycles=0)
        self.assertIn("agent4_phase == 1'd1",text)
        self.assertIn("agent4_phase < 1'd1",text)
        self.assertIn("? 1'b0 : rst_n",text)
    def test_parameterized_module_sources_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'different filename.sv'; path.write_text('module top #(parameter WIDTH=8)(input [WIDTH-1:0] d); endmodule')
            self.assertEqual(inspect_sources([path])['definitions'],['top']); self.assertIn('WIDTH=8',path.read_text())

    def test_clock_polarity_and_domain_are_audited(self):
        module={'ports':{'clk':{'bits':[2]}},'cells':{'state':{'type':'$dff','connections':{'CLK':[2]},'parameters':{'CLK_POLARITY':'1'}}}}
        self.assertEqual(len(validate_state_clock(module,{'edge':'posedge'},'clk')),1)
        for connection,polarity in [([7],'1'),([2],'0')]:
            module['cells']['state']['connections']['CLK']=connection
            module['cells']['state']['parameters']['CLK_POLARITY']=polarity
            with self.assertRaises(LECInputError): validate_state_clock(module,{'edge':'posedge'},'clk')

    def test_unreviewed_latch_model_is_blocked(self):
        module={'ports':{'clk':{'bits':[2]}},'cells':{'latch':{'type':'$dlatch','connections':{'EN':[2]}}}}
        with self.assertRaises(LECInputError): validate_state_clock(module,{'edge':'posedge'},'clk')

    def test_fine_liberty_dffs_use_c_pin_and_encoded_clock_polarity(self):
        module={'ports':{'clk':{'bits':[2]}},'cells':{'state':{'type':'$_DFF_P_','connections':{'C':[2]},'parameters':{}}}}
        for kind in ('$_DFF_P_','$_DFF_PN0_'):
            module['cells']['state']['type']=kind
            self.assertEqual(len(validate_state_clock(module,{'edge':'posedge'},'clk')),1)
        module['cells']['state']['type']='$_DFF_N_'
        with self.assertRaises(LECInputError): validate_state_clock(module,{'edge':'posedge'},'clk')
        module['cells']['state']['connections']['C']=[9]
        with self.assertRaises(LECInputError): validate_state_clock(module,{'edge':'negedge'},'clk')



class LiveProgressTests(unittest.TestCase):
    def setUp(self):
        from agent4_progress import Progress
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.work=self.root/'eqy_work';self.work.mkdir()
        self.agent=SimpleNamespace(report=SimpleNamespace(directory=str(self.root)),work=str(self.work),timeout=0,
                                   runtime_options={'sat_seconds':60})
        self.names=['arbitrary_chip.cached','arbitrary_chip.internal','arbitrary_chip.output']
        self.runtime=dict(identity={'design':{'partitions':dict.fromkeys(self.names,'fingerprint')}},
            cache_hits={self.names[0]:{'identity_verified':True}},cpu_limit=3,
            plan={'completion_hard':{self.names[2]:'sequential cone'},
                  'native_profile':{self.names[1]:{'internal_state':True}},
                  'strategies':[{'strategy':'first','engine':'aiger suprove','timeout':900},
                                {'strategy':'last','engine':'aiger avy','timeout':1800}]})
        write(self.root/'runtime_report.json',self.runtime)
        self.now=[0.];self.stream=io.StringIO()
        self.observer=Progress(self.agent,stream=self.stream,clock=lambda:self.now[0])
        self.addCleanup(self.close)

    def close(self):
        self.observer.stop.set()
        if self.observer.thread:self.observer.thread.join(timeout=3)
        if self.observer.log and not self.observer.log.closed:self.observer.log.close()

    def start_strategy(self,name=None,strategy='first'):
        path=self.work/'strategies'/(name or self.names[2])/strategy
        write(path/'wrapper_session.json',{'session':987654,'identity':'unobserved'})
        self.observer.poll();return path

    def finish_strategy(self,path,status='PASS',elapsed=317.4):
        (path/'wrapper_session.json').unlink()
        write(path/'wrapper_cleanup.json',{'status':status,'runtime_seconds':elapsed})
        (path/'status').write_text(status+'\n')
        self.observer.poll()

    def text(self):return self.stream.getvalue()

    def test_stage_and_dynamic_preflight_summary(self):
        self.observer.stage(1);self.observer.stage(1,True);self.observer.poll()
        for text in ['Stage 1/8: Validating inputs','Stage 1/8: PASS','Required obligations: 3',
                     'PASS_CACHED: 1','Fresh proofs required: 2','Completion-hard obligations: 1',
                     'Global timeout: disabled','CPU budget: 3','CACHE HIT | '+self.names[0]]:
            self.assertIn(text,self.text())

    def test_start_uses_actual_partition_engine_and_budget(self):
        self.start_strategy()
        self.assertIn('START [2/3] '+self.names[2]+' | class=completion-hard | strategy=suprove | budget=900s',self.text())
        self.assertIn('HARD PROOF 1/1',self.text())

    def test_generated_but_unrun_strategy_has_no_start(self):
        path=self.work/'strategies'/self.names[2]/'first';path.mkdir(parents=True)
        (path/'run.sh').write_text('solver command')
        self.observer.poll();self.assertNotIn('START',self.text())

    def test_normal_internal_partition_stage(self):
        self.start_strategy(self.names[1])
        self.assertIn('class=normal',self.text());self.assertIn('Stage 6/8',self.text())

    def test_pass_updates_only_required_counters(self):
        path=self.start_strategy();self.finish_strategy(path)
        self.assertIn('PASS | '+self.names[2]+' | strategy=suprove | elapsed=317.4s',self.text())
        self.assertIn('proven=2/3 | fresh_pass=1 | cached=1 | unresolved=1 | confirmed_cex=0',self.text())

    def test_timeout_then_escalation_preserves_budgets(self):
        path=self.start_strategy();self.finish_strategy(path,'TIMEOUT',900.)
        self.start_strategy(strategy='last')
        self.assertIn('TIMEOUT | '+self.names[2],self.text())
        self.assertIn('Strategy exhausted | '+self.names[2]+' | suprove | status=TIMEOUT',self.text())
        self.assertIn('Escalating | '+self.names[2]+' | next=avy | budget=1800s',self.text())
        self.assertEqual(self.text().count('START [2/3]'),2)

    def test_unknown_is_visible_and_not_counterexample(self):
        path=self.start_strategy();self.finish_strategy(path,'UNKNOWN')
        self.assertIn('UNKNOWN | ',self.text());self.assertNotIn('CONFIRMED_CEX',self.text())

    def test_abstract_fail_is_not_confirmed_cex(self):
        path=self.start_strategy();self.finish_strategy(path,'FAIL')
        self.assertIn('witness=unvalidated',self.text());self.assertNotIn('CONFIRMED_CEX',self.text())

    def test_confirmed_cex_requires_guarded_final_result(self):
        self.observer.poll()
        formal={'result':'NOT_EQUIVALENT','counterexamples':[
            {'partition':self.names[2],'strategy':'last','counterexample_proven':True},
            {'partition':self.names[1],'counterexample_proven':False}]}
        self.observer.finish({'formal':formal,'equivalence':'NOT_EQUIVALENT'})
        self.assertEqual(self.text().count('CONFIRMED_CEX |'),1)
        self.assertIn('Confirmed counterexamples: 1',self.text())

    def test_cache_saved_only_after_committed_checkpoint_record(self):
        path=self.start_strategy();self.finish_strategy(path)
        self.assertNotIn('CACHE SAVED',self.text())
        write(self.root/'pass_checkpoints.json',{'saved':[self.names[2],'diagnostic.helper']})
        self.observer.poll();self.observer.poll()
        self.assertEqual(self.text().count('CACHE SAVED |'),1)
        self.assertIn('CACHE SAVED | '+self.names[2],self.text())

    def test_per_proof_heartbeat_and_no_general_duplicate(self):
        self.start_strategy();self.now[0]=60.;self.observer.poll()
        self.assertIn('HEARTBEAT | partition='+self.names[2]+' | strategy=suprove | elapsed=60s',self.text())
        self.assertNotIn('HEARTBEAT | stage=',self.text())
        self.now[0]=120.;self.observer.poll()
        self.assertEqual(self.text().count('HEARTBEAT | partition='),2)

    def test_no_proof_heartbeat_after_exit(self):
        path=self.start_strategy();self.finish_strategy(path,'TIMEOUT')
        self.now[0]=120.;self.observer.poll()
        self.assertNotIn('HEARTBEAT | partition=',self.text())
        self.assertIn('HEARTBEAT | stage=',self.text())

    def test_general_preparation_heartbeat(self):
        self.agent.work=None;self.observer.stage(2);self.now[0]=60.;self.observer.poll()
        self.assertIn('HEARTBEAT | stage=RTL and mapped-netlist preparation | total_elapsed=60s',self.text())

    def test_log_matches_flushed_terminal_messages(self):
        self.start_strategy();self.now[0]=60.;self.observer.poll()
        self.assertEqual((self.root/'progress.log').read_text(),self.text())

    def test_helper_proofs_do_not_change_counts(self):
        self.start_strategy('diagnostic.helper');self.observer.poll()
        self.assertNotIn('diagnostic.helper',self.text());self.assertIn('proven=1/3',self.observer.counts())

    def test_final_summary_and_unresolved_names(self):
        path=self.start_strategy();self.finish_strategy(path)
        formal={'result':'INCONCLUSIVE','expected_partitions':self.names,
                'proven_partitions':[self.names[0],self.names[2]],'counterexamples':[]}
        before=copy.deepcopy(formal)
        self.observer.finish({'formal':formal,'equivalence':'INCONCLUSIVE'})
        for line in ['FINAL RESULT: INCONCLUSIVE','Fresh PASS: 1','PASS_CACHED: 1','Unresolved: 1',
                     'UNRESOLVED | '+self.names[1],'Report: '+str(self.root)]:self.assertIn(line,self.text())
        self.assertEqual(formal,before)

    def test_runtime_plan_order_identity_and_budgets_unchanged(self):
        before=(self.root/'runtime_report.json').read_bytes()
        path=self.start_strategy();self.finish_strategy(path,'TIMEOUT');self.start_strategy(strategy='last')
        self.assertEqual(before,(self.root/'runtime_report.json').read_bytes())
        self.assertEqual(self.runtime,read_json(self.root/'runtime_report.json'))

    def test_partial_json_is_retried(self):
        (self.root/'runtime_report.json').write_text('{')
        self.observer.poll();self.assertNotIn('Required obligations',self.text())
        write(self.root/'runtime_report.json',self.runtime);self.observer.poll()
        self.assertIn('Required obligations: 3',self.text())

    def test_broken_terminal_does_not_stop_log(self):
        class Broken:
            def write(self,value):raise BrokenPipeError()
            def flush(self):raise BrokenPipeError()
        self.observer.stream=Broken();self.observer.poll()
        self.assertIn('Required obligations: 3',(self.root/'progress.log').read_text())

    def test_final_terminal_target_reports_unproven_progress_once(self):
        path=self.start_strategy(strategy='last')
        (self.work/'summary_targets.list').write_text(str(path.relative_to(self.work)/'status')+'\n')
        self.finish_strategy(path,'TIMEOUT');self.observer.poll()
        self.assertEqual(self.text().count('Progress |'),1)
        self.assertIn('proven=1/3',self.text())

    def test_real_observer_thread_stops_on_all_exit_kinds(self):
        import threading
        from agent4_progress import reported_run
        for error in (None,KeyboardInterrupt(),SystemExit(143),RuntimeError('solver crash')):
            with self.subTest(error=type(error).__name__):
                @reported_run
                def run(agent):
                    agent.observed=agent._live_progress
                    if error is not None:raise error
                    return {'equivalence':'INCONCLUSIVE'}
                with contextlib.redirect_stdout(io.StringIO()):
                    try:run(self.agent)
                    except (KeyboardInterrupt,SystemExit,RuntimeError):pass
                self.assertFalse(self.agent.observed.thread.is_alive())
                self.assertIsNone(self.agent._live_progress)
        self.assertFalse([t for t in threading.enumerate() if t.name=='agent4-progress'])

    def test_reporting_metadata_does_not_change_real_formal_identity(self):
        helper=ProductionCompletionTests();helper.setUp()
        try:
            work,payload,runtime=helper.fixture()
            changed=copy.deepcopy(payload);changed['progress_global_timeout']=0
            self.assertEqual(runtime['identity'],agent4_lec.formal_runtime_identity(work,runtime['runtime_config'],changed,{}))
        finally:helper.doCleanups()

    def test_native_preflight_is_flushed_before_launch_hook_returns(self):
        from agent4_progress import native_preflight
        calls=[]
        with patch('builtins.print',side_effect=lambda *a,**kw:calls.append((a,kw))):
            native_preflight(self.runtime,0)
        self.assertEqual(len(calls),8)
        self.assertTrue(all(kw.get('flush') is True for _,kw in calls))

    def test_real_sigterm_and_ctrl_c_stop_heartbeat_and_owned_solver(self):
        script='''import os,signal,sys,time,threading
from pathlib import Path
from types import SimpleNamespace
from agent4_progress import reported_run
from agent4_lec import EQYProcessSession
root=Path(sys.argv[1]);agent=SimpleNamespace(report=SimpleNamespace(directory=str(root)))
@reported_run
def run(agent):
    with EQYProcessSession() as owned:
        process=owned.start([sys.executable,'-c','import time;time.sleep(60)'])
        (root/'ready').write_text(str(process.pid))
        while True: time.sleep(.05)
try: run(agent)
except (KeyboardInterrupt,SystemExit): pass
(root/'stopped').write_text(str(not any(t.name=='agent4-progress' for t in threading.enumerate())))
'''
        for signum in (signal.SIGINT,signal.SIGTERM):
            with self.subTest(signum=signum):
                root=self.root/str(signum);root.mkdir()
                process=subprocess.Popen([sys.executable,'-B','-c',script,str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                try:
                    deadline=time.monotonic()+10
                    while not (root/'ready').is_file() and time.monotonic()<deadline and process.poll() is None:time.sleep(.02)
                    self.assertTrue((root/'ready').is_file())
                    child=int((root/'ready').read_text());process.send_signal(signum)
                    _,err=process.communicate(timeout=10)
                    self.assertEqual(process.returncode,0,err)
                    self.assertEqual((root/'stopped').read_text(),'True')
                    self.assertIsNone(agent4_lec.process_identity(child))
                    self.assertIn('FINAL RESULT: INCONCLUSIVE',(root/'progress.log').read_text())
                finally:
                    if process.poll() is None:process.kill();process.communicate()


if __name__=='__main__': unittest.main()
