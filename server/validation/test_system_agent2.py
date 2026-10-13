"""Phase 3 regressions: executable monitor replay, evidence and orchestration.

VCS responses are mocked in unit tests. These tests do not claim the SV/UVM
environment compiled or the real controller passed a functional simulation.
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent1_rtl_validator as agent1
import agent2_uvm_tb as agent2
import run_pipeline
import test_system_discovery as discovery_tests
from controller_verification import ControllerReferenceModel, extract_controller_contract, parse_system_events
from requirement_verification import build_system_requirement_catalog
from system_rtl_validation import validate_system
from system_uvm_verification import (input_fingerprints, render_system_environment,
    run_system_uvm, summarize_system_simulation, verify_fingerprints, system_workspace)
from system_uvm_generation import (BEGIN, END, COMPONENTS, audit_environment, inspect_environment,
                                   compile_failure_classification, validate_saved_environment)


def system_llm_reply(system, prompt, agent):
    """Stand-in for a model authoring the explicitly permitted UVM topology."""
    region = BEGIN + prompt.split(BEGIN, 1)[1].split(END, 1)[0] + END
    generated = region.replace('sequencer=dut_sequencer::type_id::create("sequencer",this);',
                               'sequencer=new("sequencer",this);')
    return json.dumps({'edits': [{'file': 'dut_pkg.sv', 'old': region, 'new': generated}]})


def event(kind, identity=1, bank=0, address=987654, write=1, data=0x1234, cycle=1, **extra):
    return dict(event=kind, id=identity, bank=bank, address=address, write=write,
                data=data, cycle=cycle, scenario='directed', valid=1, ready=1, **extra)


class SystemIntegrityTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parent / 'templates/controller_system'
        self.trusted = {name: (root / name).read_text() for name in
                        ('dut_if.sv', 'dut_pkg.sv', 'dut_tb_top.sv')}
        self.region = self.trusted['dut_pkg.sv'].split(BEGIN)[1].split(END)[0]

    def inspect(self, region):
        files = dict(self.trusted)
        files['dut_pkg.sv'] = files['dut_pkg.sv'].replace(self.region, region)
        return inspect_environment(agent2, files, self.trusted)

    def test_valid_generated_uvm_passes(self):
        self.assertTrue(self.inspect(self.region)['passed'])

    def test_missing_agent_is_specific_failure(self):
        changed = re.sub(r'class dut_agent\b.*?endclass', '', self.region, flags=re.S)
        audit = self.inspect(changed)
        self.assertFalse(audit['passed'])
        self.assertIn('dut_agent', '\n'.join(audit['failures']['required_classes']))

    def test_environment_bypassing_agent_fails_architecture(self):
        changed = self.region.replace('dut_agent agent;', 'dut_driver agent;').replace(
            'agent=dut_agent::type_id::create', 'agent=dut_driver::type_id::create')
        audit = self.inspect(changed)
        self.assertEqual(audit['checks']['topology'], 'FAIL')
        self.assertIn('dut_agent', '\n'.join(audit['failures']['topology']))

    def test_missing_sequencer_connection_fails_topology(self):
        audit = self.inspect(self.region.replace('driver.seq_item_port.connect(sequencer.seq_item_export);', ''))
        self.assertEqual(audit['checks']['topology'], 'FAIL')
        self.assertIn('seq_item_port', '\n'.join(audit['failures']['topology']))

    def test_invalid_lifecycle_rejected(self):
        for old, new in (('phase.raise_objection(this);', ''),
                         ('sequence_instance.start(env.agent.sequencer);', ''),
                         ('env.scoreboard.finalize();', ''),
                         ('phase.drop_objection(this);', ''),
                         ('sequence_instance.start(env.agent.sequencer);', 'sequence_instance.start(null);'),
                         ('env.scoreboard.fail_count>0', 'env.scoreboard.fail_count>100'),
                         ('`uvm_fatal("SYSTEM_FAILED"', '`uvm_error("SYSTEM_FAILED"')):
            with self.subTest(old=old):
                audit = self.inspect(self.region.replace(old, new))
                self.assertEqual(audit['checks']['lifecycle'], 'FAIL')

    def test_equivalent_real_run_variations_are_valid(self):
        changed = self.region.replace('function void connect_phase(uvm_phase phase);',
            'function void connect_phase(uvm_phase phase); super.connect_phase(phase);')
        changed = changed.replace('dut_sequence sequence_instance=dut_sequence::type_id::create("system_sequence");',
            'dut_sequence sequence_instance; phase.raise_objection(this);\n'
            'sequence_instance=dut_sequence::type_id::create("system_sequence");').replace(
            'phase.raise_objection(this); sequence_instance.vif=vif;', 'sequence_instance.vif=vif;')
        get = 'if (!uvm_config_db#(virtual dut_if)::get(this,"","vif",vif))\n                `uvm_fatal("SYSTEM_VIF","Missing test interface")'
        changed = changed.replace('env=dut_env::type_id::create("env",this);', '').replace(
            get, get + '\n env=dut_env::type_id::create("env",this);')
        changed = changed.replace('if (env.scoreboard.fail_count>0)', 'if (((env.scoreboard.fail_count>0)))')
        self.assertTrue(self.inspect(changed)['passed'], self.inspect(changed)['failures'])

    def test_renamed_handles_phases_and_this_connection_are_valid(self):
        changed = self.region
        for old, new in (('driver', 'producer'), ('sequencer', 'sqr'), ('monitor', 'observer'),
                         ('agent', 'a'), ('scoreboard', 'checker'), ('env', 'environment'),
                         ('sequence_instance', 'stream'), ('phase', 'p')):
            changed = re.sub(r'\b' + old + r'\b', new, changed)
        changed = changed.replace('producer.seq_item_port.connect(sqr.seq_item_export);',
            'this.producer.seq_item_port.connect((this.sqr.seq_item_export));')
        self.assertTrue(self.inspect(changed)['passed'], self.inspect(changed)['failures'])

    def test_explicit_helpers_and_typed_new_are_valid(self):
        calls = ('driver=dut_driver::type_id::create("driver",this);\n'
                 '            sequencer=dut_sequencer::type_id::create("sequencer",this);\n'
                 '            monitor=dut_monitor::type_id::create("monitor",this);')
        changed = self.region.replace(calls, 'make_children();')
        changed = changed.replace('class dut_agent extends uvm_agent;',
            'class dut_agent extends uvm_agent;\nfunction void make_children();\n' +
            calls.replace('sequencer=dut_sequencer::type_id::create', 'sequencer=new') + '\nendfunction')
        changed = changed.replace('agent.monitor.ap.connect(scoreboard.analysis_export);', 'connect_observer(phase);')
        changed = changed.replace('class dut_env extends uvm_env;',
            'class dut_env extends uvm_env;\nfunction void connect_observer(uvm_phase p);\n'
            'this.agent.monitor.ap.connect(this.scoreboard.analysis_export);\nendfunction')
        self.assertTrue(self.inspect(changed)['passed'], self.inspect(changed)['failures'])

    def test_extra_helper_behavior_cannot_suppress_checks(self):
        changed = self.region.replace('super.build_phase(phase);',
            'super.build_phase(phase); set_report_severity_action(UVM_ERROR,UVM_NO_ACTION);', 1)
        self.assertFalse(self.inspect(changed)['passed'])

    def test_trusted_bytes_remain_immutable(self):
        for name in self.trusted:
            with self.subTest(name=name):
                files = dict(self.trusted)
                files[name] += '\n// even an innocent edit outside the mutable region is blocked\n'
                audit = inspect_environment(agent2, files, self.trusted)
                self.assertFalse(audit['passed'])


class ControllerModelTests(unittest.TestCase):
    def model(self, events):
        model=ControllerReferenceModel()
        for item in events: model.observe(item)
        return model

    def test_write_commits_at_completion_and_arbitrary_addresses(self):
        model=self.model([event('accepted'), event('dispatched')])
        self.assertEqual(model.memory, {})
        model.observe(event('completed'))
        self.assertEqual(model.memory[(0,987654)],0x1234)
        self.assertTrue(model.finish(True)['accounting_proven'])

    def test_backpressure_is_not_acceptance(self):
        request=event('accepted'); request['ready']=0
        model=self.model([request])
        self.assertEqual(model.accepted,0)
        self.assertTrue(any('handshake' in f['reason'] for f in model.failures))

    def test_reordering_multiple_outstanding_same_address_and_read_association(self):
        model=self.model([event('accepted',1,data=0x1111), event('accepted',2,data=0x2222),
                          event('accepted',3,write=0,data=0), event('dispatched',2,data=0x2222),
                          event('completed',2,data=0x2222), event('dispatched',3,write=0,data=0),
                          event('completed',3,write=0,data=0), event('response',3,write=0,data=0x2222),
                          event('dispatched',1,data=0x1111),event('completed',1,data=0x1111)])
        self.assertEqual(model.responses_checked,1)
        self.assertEqual(model.memory[(0,987654)],0x1111)
        self.assertEqual(model.failures,[])
        self.assertEqual(model.finish(True)['outstanding'],0)

    def test_lost_request_at_drain(self):
        model=self.model([event('accepted')])
        result=model.finish(True)
        self.assertEqual(result['lost'],1)
        self.assertFalse(result['accounting_proven'])

    def test_duplicates_at_dispatch_completion_and_response(self):
        streams=([
            event('accepted'),event('dispatched'),event('dispatched')], [
            event('accepted'),event('dispatched'),event('completed'),event('completed')], [
            event('accepted',write=0,data=0),event('dispatched',write=0,data=0),event('completed',write=0,data=0),
            event('response',write=0,data=0),event('response',write=0,data=0)])
        for stream in streams:
            with self.subTest(stream=stream):
                model=self.model(stream)
                self.assertGreater(model.duplicated,0)
                self.assertTrue(model.failures)

    def test_bank_isolation_and_response_mismatch(self):
        stream=[]
        for identity,bank,data in ((1,0,0xabcd),(2,7,0xef01)):
            stream.extend(event(kind,identity,bank=bank,data=data) for kind in ('accepted','dispatched','completed'))
        stream.extend(event(kind,3,bank=7,write=0,data=0) for kind in ('accepted','dispatched','completed'))
        model=self.model(stream)
        model.observe(event('response',3,bank=7,write=0,data=0xabcd))
        self.assertEqual(model.memory[(0,987654)],0xabcd)
        self.assertEqual(model.memory[(7,987654)],0xef01)
        self.assertEqual(model.failures[-1]['expected'],0xef01)
        self.assertEqual(model.failures[-1]['observed'],0xabcd)

    def test_reset_cancels_waiting_read_response_and_clears_memory(self):
        model=self.model([event('accepted'),event('dispatched'),event('completed'),
                          event('accepted',2,write=0,data=0),event('dispatched',2,write=0,data=0),event('completed',2,write=0,data=0)])
        self.assertEqual(model.completed,1)
        model.observe({'event':'reset','cycle':9,'scenario':'reset_outstanding'})
        result=model.finish(True)
        self.assertEqual(result['cancelled_by_reset'],1)
        self.assertEqual(result['completed'],1)
        self.assertTrue(result['accounting_proven'])
        self.assertEqual(model.memory,{})
        model.observe(event('response',2,write=0,data=0x1234))
        self.assertGreater(model.duplicated,0)

    def test_no_end_marker_does_not_claim_zero_loss(self):
        result=self.model([event('accepted')]).finish(False)
        self.assertIsNone(result['lost'])
        self.assertIsNone(result['duplicated'])
        self.assertFalse(result['accounting_proven'])


def simulation_fixture(requirements, bad_response=False, missing_evidence=None):
    lines=[]
    for req in requirements:
        identity=req['id']
        lines.extend(['AGENT2_REQ_COUNTS|{}|1|0'.format(identity),
                      'AGENT2_REQ|{}|PASS|executed checker'.format(identity)])
        if identity!=missing_evidence:
            lines.append('AGENT2_REQ_EVIDENCE|{}|directed|cycle=7|expected=1234|observed=1234'.format(identity))
    stream=[event(kind,1,address=9,data=0x1234,cycle=i+1) for i,kind in enumerate(('presented','accepted','dispatched','completed'))]
    stream.extend(event(kind,2,address=9,write=0,data=0,cycle=i+5) for i,kind in enumerate(('presented','accepted','dispatched','completed')))
    stream.append(event('response',2,address=9,write=0,data=0xffff if bad_response else 0x1234,cycle=9))
    for item in stream:
        fields=[]
        for key,value in item.items(): fields.append(key+'='+ (format(value,'x') if key=='data' else str(value)))
        lines.append('AGENT2_SYSTEM_EVENT|'+'|'.join(fields))
    lines.extend(['SCOREBOARD PASS=15 FAIL=0','UVM_ERROR : 0','UVM_FATAL : 0',
                  'AGENT2_SYSTEM_TRAFFIC|presented=2|accepted=2|dispatched=2|completed=2|cancelled_by_reset=0|outstanding=0|duplicated=0|responses_checked=1|data_mismatches=0',
                  'AGENT2_SYSTEM_COVERAGE|consecutive_accepts=2|backpressure_cycles=5|saturation_cycles=2|max_outstanding=4|max_queued=3|completion_and_accept=1|trrd_boundary_hits=1',
                  'AGENT2_SYSTEM_END|cycle=10'])
    return '\n'.join(lines)


class EvidenceTests(unittest.TestCase):
    def test_system_and_legacy_requirement_ids_remain_distinct(self):
        text='AGENT2_REQ|REQ-001|PASS|module\nAGENT2_REQ|SYS-REQ-001|FAIL|system\nAGENT2_REQ|SYS-REQ-001|PASS|must not erase failure'
        result=agent2.parse_requirement_results(text)
        self.assertEqual(result['REQ-001']['status'],'PASS')
        self.assertEqual(result['SYS-REQ-001']['status'],'FAIL')

    def test_pass_requires_positive_count_and_real_evidence(self):
        requirements=[{'id':'SYS-REQ-001','category':'reset','source':'system contract','text':'reset'}]
        base='SCOREBOARD PASS=1 FAIL=0\nAGENT2_REQ|SYS-REQ-001|PASS|test\n'
        report=agent2.build_requirement_report(requirements,base)
        self.assertEqual(report['requirements'][0]['status'],'NOT_TESTED')
        report=agent2.build_requirement_report(requirements,base+'AGENT2_REQ_COUNTS|SYS-REQ-001|1|0\n')
        self.assertEqual(report['requirements'][0]['status'],'NOT_TESTED')
        report=agent2.build_requirement_report(requirements,base+'AGENT2_REQ_COUNTS|SYS-REQ-001|1|0\nAGENT2_REQ_EVIDENCE|SYS-REQ-001|reset_idle|cycle=2|expected=idle|observed=idle')
        self.assertEqual(report['requirements'][0]['status'],'PASS')

    def test_failure_count_cannot_be_hidden_by_pass_marker(self):
        requirements=[{'id':'SYS-REQ-006','category':'read','source':'system contract','text':'readback'}]
        text='SCOREBOARD PASS=2 FAIL=0\nAGENT2_REQ|SYS-REQ-006|PASS|claimed\nAGENT2_REQ_COUNTS|SYS-REQ-006|3|1'
        report=agent2.build_requirement_report(requirements,text)
        self.assertEqual(report['requirements'][0]['status'],'FAIL')
        self.assertEqual(report['overall_status'],'FAIL')

    def test_unobservable_comparison_is_not_tested(self):
        reqs=[{'id':'SYS-REQ-014','category':'timing','source':'system contract','text':'tFAW'}]
        text='SCOREBOARD PASS=1 FAIL=0\nAGENT2_REQ|SYS-REQ-014|NOT_TESTED|no ACT observation'
        self.assertEqual(agent2.build_requirement_report(reqs,text)['requirements'][0]['status'],'NOT_TESTED')


class SystemAgent2Tests(unittest.TestCase):
    setUp=discovery_tests.SystemDiscoveryTests.setUp
    write_manifest=discovery_tests.SystemDiscoveryTests.write_manifest
    rebuild=discovery_tests.SystemDiscoveryTests.rebuild

    def approve(self):
        self.agent1_path=Path(self.temp.name)/'approved_agent1.json'
        with patch.object(agent1,'compile_rtl',return_value=('vcs',True,'mocked system elaboration')):
            self.approved=validate_system(agent1,self.manifest_path,self.agent1_path)
        self.assertTrue(self.approved['is_valid'],self.approved['errors'])
        self.contract=extract_controller_contract(self.manifest_path,self.approved)
        self.requirements=build_system_requirement_catalog(self.contract['configuration'],self.contract)

    def report(self):
        directory=run_pipeline.create_report_run([str(self.manifest_path)],requested=('agent2',),metadata_extra={'mode':'system'})
        return run_pipeline.AgentReport('agent2',run_dir=directory)

    def run_agent2(self, bad_response=False, reuse=None, compile_ok=True, compile_results=None,
                   llm_reply=system_llm_reply, simulation_text=None, simulation_ok=True):
        report=self.report()
        simulation=simulation_fixture(self.requirements,bad_response) if simulation_text is None else simulation_text
        with patch.object(agent2,'call_llm',side_effect=llm_reply) as llm_call:
            compile_output='mocked compile' if compile_ok else 'Error-[SE] Syntax error\n"tb_auto/dut_pkg.sv", 503: unexpected token'
            with patch.object(agent2,'vcs_compile',return_value=(compile_ok,'compile.log',compile_output),
                              side_effect=compile_results) as compile_call:
                with patch.object(agent2,'vcs_run',return_value=(simulation_ok,'simulation.log',simulation)) as run_call:
                    with contextlib.redirect_stdout(io.StringIO()) as console:
                        ready=run_system_uvm(agent2,report,self.manifest_path,self.agent1_path,reuse)
        self.llm_calls=llm_call.call_count
        self.llm_call_args=llm_call.call_args_list
        self.console=console.getvalue()
        result=json.loads((Path(report.directory)/'system_report.json').read_text())
        return ready,result,report,compile_call,run_call

    def test_unseen_controller_invokes_llm_with_approved_inputs(self):
        self.approve()
        _,result,report,_,_=self.run_agent2()
        self.assertEqual(self.llm_calls,1)
        self.assertEqual(result['generation']['mode'],'LLM_GENERATED')
        prompt=self.llm_call_args[0][0][1]
        for content in ('module_definitions','txn_ready','controller_contract','SYS-REQ-015',
                        'configuration','clock','reset','rtl_files','trusted_rules','vcs_uvm_flags'):
            self.assertIn(content,prompt)
        self.assertTrue(result['checker_compile_valid'])
        self.assertTrue(result['checker_reuse_eligible'])
        self.assertTrue((Path(report.directory)/'generation/raw_0.txt').is_file())

    def test_automatic_compatible_reuse_bypasses_llm_and_copies_exact_bytes(self):
        self.approve()
        _,first,_,_,_=self.run_agent2()
        prior=json.loads(Path(first['uvm_environment_file']).read_text())
        before={Path(row['path']).name:Path(row['path']).read_bytes() for row in prior['testbench_files']}
        _,second,_,_,_=self.run_agent2(llm_reply=AssertionError('Compatible rerun must not call LLM'))
        self.assertEqual(self.llm_calls,0)
        self.assertEqual(second['generation']['mode'],'REUSED')
        current=json.loads(Path(second['uvm_environment_file']).read_text())
        after={Path(row['path']).name:Path(row['path']).read_bytes() for row in current['testbench_files']}
        self.assertEqual(before,after)

    def test_changed_rtl_hash_triggers_new_generation_after_agent1(self):
        self.approve(); self.run_agent2()
        path=Path(self.approved['rtl_files'][0]); path.write_text(path.read_text()+'\n// frontend revision\n')
        self.rebuild(); self.approve()
        _,result,_,_,_=self.run_agent2()
        self.assertEqual(self.llm_calls,1)
        self.assertFalse(result['testbench_reused'])

    def test_manifest_configuration_contract_requirements_and_checker_changes_regenerate(self):
        self.approve(); _,first,_,_,_=self.run_agent2()
        saved_path=Path(first['uvm_environment_file'])
        original=saved_path.read_text(); metadata=json.loads(original)
        for key,new in (('source_manifest_sha256','changed'),('configuration_hash','changed'),
                        ('contract_hash','changed'),('requirement_hash','changed'),('checker_identity','changed')):
            with self.subTest(key=key):
                saved_path.write_text(json.dumps(dict(metadata,**{key:new})))
                _,result,_,_,_=self.run_agent2(reuse=str(saved_path))
                self.assertEqual(self.llm_calls,1)
                self.assertEqual(result['generation']['mode'],'LLM_GENERATED')
        saved_path.write_text(original)

    def test_saved_contract_and_requirement_content_changes_regenerate(self):
        self.approve(); _,first,_,_,_=self.run_agent2()
        saved=json.loads(Path(first['uvm_environment_file']).read_text())
        for key in ('controller_verification_contract','requirement_catalog'):
            path=Path(saved[key]); original=path.read_text()
            with self.subTest(key=key):
                path.write_text('{}' if key=='controller_verification_contract' else '[]')
                _,result,_,_,_=self.run_agent2(reuse=first['uvm_environment_file'])
                self.assertEqual(self.llm_calls,1)
                self.assertFalse(result['testbench_reused'])
            path.write_text(original)

    def test_actual_configuration_contract_and_requirement_revisions_regenerate(self):
        self.approve(); _,first,_,_,_=self.run_agent2()
        for field in ('configuration','contract','requirements'):
            with self.subTest(field=field):
                contract=dict(self.contract)
                catalog=[dict(row) for row in self.requirements]
                if field=='configuration':
                    contract['configuration']=dict(contract['configuration'],scheduler='reviewed_revision')
                elif field=='contract':
                    contract['observability_limitations']=list(contract.get('observability_limitations',[]))+['reviewed contract revision']
                else:
                    catalog[0]['text']+=' (approved clarification)'
                with patch('system_uvm_verification.extract_controller_contract',return_value=contract), \
                     patch('system_uvm_verification.build_system_requirement_catalog',return_value=catalog):
                    _,result,_,_,_=self.run_agent2(reuse=first['uvm_environment_file'])
                self.assertEqual(self.llm_calls,1)
                self.assertFalse(result['testbench_reused'])

    def test_new_manifest_revision_hash_regenerates(self):
        self.approve(); _,first,_,_,_=self.run_agent2()
        # Whitespace alone is a new manifest fingerprint without inventing a
        # configuration that contradicts frontend source metadata.
        self.manifest_path.write_text(self.manifest_path.read_text()+'\n')
        self.approve()
        _,result,_,_,_=self.run_agent2(reuse=first['uvm_environment_file'])
        self.assertEqual(self.llm_calls,1)
        self.assertFalse(result['testbench_reused'])

    def test_compile_failed_checker_is_saved_but_not_reuse_eligible(self):
        self.approve()
        _,failed,report,compiler,runner=self.run_agent2(compile_ok=False)
        self.assertEqual(compiler.call_count,agent2.MAX_ITERS)
        self.assertEqual(self.llm_calls,agent2.MAX_ITERS)
        runner.assert_not_called()
        self.assertTrue(failed['environment_saved'])
        self.assertFalse(failed['checker_compile_valid'])
        self.assertFalse(failed['checker_reuse_eligible'])
        saved=json.loads(Path(failed['uvm_environment_file']).read_text())
        self.assertFalse(saved['checker_reuse_eligible'])
        self.assertEqual(failed['failure']['disposition'],'AGENT2_TB_REPAIR_EXHAUSTED')
        self.assertIsNone(failed['simulation']['uvm_error'])
        _,result,_,_,_=self.run_agent2()
        self.assertEqual(self.llm_calls,1)
        self.assertEqual(result['generation']['mode'],'LLM_GENERATED')

    def test_generated_tb_compile_failure_repairs_before_simulation(self):
        self.approve()
        outcomes=[(False,'compile.log','Error-[SE] Syntax error\n"dut_pkg.sv", 505: bad token'),
                  (True,'compile.log','compile passed')]
        _,result,report,compiler,runner=self.run_agent2(compile_results=outcomes)
        self.assertEqual(compiler.call_count,2)
        self.assertEqual(self.llm_calls,2)
        self.assertEqual(runner.call_count,1)
        self.assertEqual(result['generation']['mode'],'LLM_REPAIRED')
        self.assertIn('bad token',self.llm_call_args[1][0][1])
        self.assertTrue((Path(report.directory)/'compile_iter_0.log').is_file())
        self.assertTrue((Path(report.directory)/'compile_iter_1.log').is_file())

    def test_rtl_compile_and_tool_failure_do_not_enter_tb_repair(self):
        self.approve()
        for output,classification in (
                ('Error-[SE] Syntax error\n"ddr4_controller_top.sv", 20: bad token','RTL_COMPILE_FAILURE'),
                ('VCS license checkout failed','VERIFICATION_TOOL_FAILURE')):
            with self.subTest(classification=classification):
                _,result,_,compiler,runner=self.run_agent2(compile_results=[(False,'compile.log',output)])
                self.assertEqual(compiler.call_count,1)
                self.assertEqual(self.llm_calls,1)
                self.assertEqual(result['failure']['classification'],classification)
                runner.assert_not_called()

    def test_dut_failure_returns_frontend_evidence_without_llm_repair(self):
        self.approve()
        _,result,report,compiler,runner=self.run_agent2(bad_response=True)
        self.assertEqual(self.llm_calls,1)
        self.assertEqual(compiler.call_count,1)
        self.assertEqual(runner.call_count,1)
        self.assertFalse(result['ready_for_agent3'])
        self.assertFalse(result['ready_for_backend'])
        self.assertTrue(result['checker_reuse_eligible'])
        self.assertEqual(result['failure']['classification'],'RTL_FUNCTIONAL_FAILURE')
        self.assertEqual(result['failure']['disposition'],'RETURN_TO_FRONTEND_RTL_FIX')
        feedback=json.loads(Path(result['failure']['frontend_feedback']).read_text())
        failure=next(f for f in feedback['failures'] if f['requirement']=='SYS-REQ-006')
        self.assertEqual(failure['expected'],0x1234)
        self.assertEqual(failure['actual'],0xffff)
        self.assertEqual(failure['acceptance_cycle'],6)
        self.assertEqual(failure['dispatch_cycle'],7)
        self.assertEqual(failure['completion_cycle'],8)
        self.assertEqual(failure['cycle'],9)
        self.assertEqual(feedback['suspected_subsystems'],[])
        self.assertEqual(feedback['restart_from'],'Agent 1 SYSTEM')
        self.assertTrue(Path(feedback['paths']['compile_log']).is_file())
        self.assertTrue(Path(feedback['paths']['system_report']).is_file())
        saved=json.loads(Path(result['uvm_environment_file']).read_text())
        self.assertEqual(saved['agent2_result'],'FAIL')
        self.assertTrue(saved['checker_compile_valid'])
        self.assertTrue(saved['checker_reuse_eligible'])

    def test_failed_run_artifacts_survive_identical_rerun(self):
        self.approve(); _,failed,report,_,_=self.run_agent2(bad_response=True)
        originals={p:p.read_bytes() for p in Path(report.directory).rglob('*') if p.is_file()}
        _,result,new,_,_=self.run_agent2()
        self.assertEqual(self.llm_calls,0)
        self.assertNotEqual(new.directory,report.directory)
        for path,content in originals.items():
            self.assertEqual(path.read_bytes(),content,str(path))

    def test_uvm_severities_and_scoreboard_counts_visible_and_gate_pass_with_gaps(self):
        self.approve()
        normal=simulation_fixture(self.requirements,missing_evidence='SYS-REQ-014')
        cases=[('UVM_ERROR : 0','UVM_ERROR : 1','uvm_error',1),
               ('UVM_FATAL : 0','UVM_FATAL : 1','uvm_fatal',1),
               ('SCOREBOARD PASS=15 FAIL=0','SCOREBOARD PASS=0 FAIL=0','scoreboard_pass',0),
               ('SCOREBOARD PASS=15 FAIL=0','SCOREBOARD PASS=15 FAIL=1','scoreboard_fail',1)]
        for before,after,key,value in cases:
            with self.subTest(key=key):
                _,result,_,_,_=self.run_agent2(simulation_text=normal.replace(before,after))
                self.assertEqual(result['status'],'FAIL')
                self.assertFalse(result['ready_for_agent3'])
                self.assertEqual(result['simulation'][key],value)
                for label in ('UVM_ERROR','UVM_FATAL','Scoreboard PASS','Scoreboard FAIL'):
                    self.assertIn(label,self.console)

    def test_saved_agent2_result_matches_real_coverage_gap_result(self):
        self.approve()
        text=simulation_fixture(self.requirements).replace('backpressure_cycles=5','backpressure_cycles=0')
        _,result,_,_,_=self.run_agent2(simulation_text=text)
        self.assertEqual(result['status'],'PASS_WITH_GAPS')
        saved=json.loads(Path(result['uvm_environment_file']).read_text())
        self.assertEqual(saved['agent2_result'],result['status'])

    def test_uvm_structure_and_trusted_semantics_cannot_be_weakened(self):
        self.approve()
        trusted=render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports'])
        audit=audit_environment(agent2,trusted,trusted)
        self.assertEqual(set(audit['components']),set(COMPONENTS))
        for name in COMPONENTS:
            self.assertIn('class '+name+' extends',trusted['dut_pkg.sv'])
        for old,new in (('phase.raise_objection(this);',''),
                        ('env.scoreboard.finalize();',''),
                        ('sample.ready===1','1'),
                        ('reference_memory[key]=active.data','reference_memory[key]=sample.response_data')):
            source=trusted['dut_pkg.sv']
            if old not in source:
                continue
            with self.subTest(old=old),self.assertRaises(ValueError):
                audit_environment(agent2,dict(trusted,**{'dut_pkg.sv':source.replace(old,new)}),trusted)
        changed=dict(trusted,**{'dut_pkg.sv':trusted['dut_pkg.sv'].replace(' && ',' & & ',1)})
        with self.assertRaises(ValueError):
            audit_environment(agent2,changed,trusted)

    def test_mixed_or_unlocalized_compile_errors_do_not_authorize_repairs(self):
        self.assertEqual(compile_failure_classification(
            'Error-[SE] Syntax error\n"dut_pkg.sv", 5\nError-[SE] Syntax error\n"rtl.sv", 8',
            ['dut_pkg.sv'],['rtl.sv']),'RTL_COMPILE_FAILURE')
        self.assertEqual(compile_failure_classification('Error-[SE] Syntax error\nunknown scope',
            ['dut_pkg.sv'],['rtl.sv']),'VERIFICATION_TOOL_FAILURE')

    def test_integrity_rejection_is_bounded_and_never_compiles_weakened_checker(self):
        self.approve()
        def weaken(system,prompt,agent):
            reply=json.loads(system_llm_reply(system,prompt,agent))
            reply['edits'][0]['new']=reply['edits'][0]['new'].replace('env.scoreboard.finalize();','')
            return json.dumps(reply)
        _,result,report,compiler,runner=self.run_agent2(llm_reply=weaken)
        compiler.assert_not_called(); runner.assert_not_called()
        self.assertEqual(self.llm_calls,agent2.MAX_ITERS)
        self.assertFalse(result['checker_reuse_eligible'])
        self.assertEqual(result['failure']['classification'],'TESTBENCH_GENERATION_FAILURE')

    def test_audit_exhaustion_history_stages_and_exact_previous_prompt(self):
        self.approve()
        def remove_connection(system, prompt, agent):
            region = BEGIN + prompt.split(BEGIN, 1)[1].split(END, 1)[0] + END
            replacement = region.replace('driver.seq_item_port.connect(sequencer.seq_item_export);', '')
            # Make each candidate distinguishable and ensure the next request
            # includes these bytes rather than restarting from the seed.
            replacement = replacement.replace(END, '// candidate revision\n' + END)
            return json.dumps({'edits': [{'file': 'dut_pkg.sv', 'old': region, 'new': replacement}]})
        _, result, report, compiler, runner = self.run_agent2(llm_reply=remove_connection)
        compiler.assert_not_called(); runner.assert_not_called()
        self.assertEqual(self.llm_calls, 5)
        self.assertIsNone(result['simulation']['compile_passed'])
        self.assertIsNone(result['simulation']['simulation_passed'])
        self.assertEqual(result['failure']['disposition'], 'AGENT2_TB_REPAIR_EXHAUSTED')
        self.assertEqual(result['verification_readiness'], 'BLOCKED_SYSTEM_AGENT_2')
        self.assertIsNone(result['failure']['frontend_feedback'])
        self.assertIn('SYSTEM_UVM_GENERATION_FAILED', result['failure_detail'])
        self.assertNotIn('SYSTEM_UVM_COMPILE_FAILED', result['failure_detail'])
        directory = Path(report.directory)
        self.assertFalse((directory / 'compile.log').exists())
        self.assertFalse((directory / 'frontend_feedback.json').exists())
        validation = json.loads((directory / 'checker_compile_validation.json').read_text())
        self.assertFalse(validation['compile_invoked'])
        self.assertEqual(validation['compile_result'], 'NOT_RUN')
        history = json.loads((directory / 'iteration_history.json').read_text())
        self.assertEqual(len(history), 5)
        for index, row in enumerate(history):
            self.assertFalse(row['vcs_invoked'])
            self.assertIsNone(row['vcs_result'])
            self.assertIn('seq_item_port', row['diagnostic'])
            folder = directory / 'generation' / ('attempt_' + str(index + 1))
            for name in ('dut_if.sv', 'dut_pkg.sv', 'dut_tb_top.sv', 'llm_raw.txt', 'audit.json', 'diagnostics.txt', 'prompt.txt'):
                self.assertTrue((folder / name).is_file(), name)
            self.assertFalse((folder / 'compile.log').exists())
            if index:
                prompt = self.llm_call_args[index][0][1]
                self.assertIn(history[index - 1]['diagnostic'], prompt)
                previous = directory / 'generation' / ('attempt_' + str(index)) / 'dut_pkg.sv'
                self.assertIn(previous.read_text(), prompt)
            self.assertIn('AGENT 2 UVM ATTEMPT {}/5'.format(index + 1), self.console)
        self.assertEqual(history[-1]['repair_decision'], 'REPAIR_EXHAUSTED')

    def test_successful_integrity_repair_reports_repaired_and_only_compiles_accepted_candidate(self):
        self.approve()
        call_count = [0]
        def reply(system, prompt, agent):
            region = BEGIN + prompt.split(BEGIN, 1)[1].split(END, 1)[0] + END
            call_count[0] += 1
            if call_count[0] == 1:
                replacement = region.replace('env.scoreboard.finalize();', '// missing finalize')
            else:
                self.assertIn('dut_test.run_phase', prompt)
                self.assertIn('// missing finalize', prompt)
                replacement = region.replace('// missing finalize', 'env.scoreboard.finalize();')
            return json.dumps({'edits': [{'file': 'dut_pkg.sv', 'old': region, 'new': replacement}]})
        _, result, report, compiler, runner = self.run_agent2(llm_reply=reply)
        self.assertEqual(self.llm_calls, 2)
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result['generation']['mode'], 'LLM_REPAIRED')
        history = result['generation']['iteration_history']
        self.assertFalse(history[0]['vcs_invoked'])
        self.assertEqual(history[1]['vcs_result'], 'PASS')
        self.assertEqual(history[1]['repair_decision'], 'ACCEPTED')
        self.assertFalse((Path(report.directory)/'generation/attempt_1/compile.log').exists())
        self.assertTrue((Path(report.directory)/'generation/attempt_2/compile.log').is_file())

    def test_final_attempt_repair_succeeds_and_never_exceeds_five_calls(self):
        self.approve()
        calls = [0]
        def reply(system, prompt, agent):
            region = BEGIN + prompt.split(BEGIN, 1)[1].split(END, 1)[0] + END
            calls[0] += 1
            if calls[0] < 5:
                replacement = region.replace('env.scoreboard.finalize();', '// missing finalize')
            else:
                replacement = region.replace('// missing finalize', 'env.scoreboard.finalize();')
            return json.dumps({'edits': [{'file': 'dut_pkg.sv', 'old': region, 'new': replacement}]})
        with patch.object(agent2, 'MAX_ITERS', 9):
            _, result, _, compiler, runner = self.run_agent2(llm_reply=reply)
        self.assertEqual(self.llm_calls, 5)
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result['generation']['mode'], 'LLM_REPAIRED')
        self.assertFalse(result['generation']['repair_exhausted'])

    def test_malformed_response_cannot_compile_unchanged_valid_seed(self):
        self.approve()
        _, result, _, compiler, runner = self.run_agent2(llm_reply=lambda *args: '{broken json}')
        compiler.assert_not_called(); runner.assert_not_called()
        self.assertEqual(self.llm_calls, 5)
        self.assertEqual(result['failure']['disposition'], 'AGENT2_TB_REPAIR_EXHAUSTED')
        self.assertIn('GENERATION_RESPONSE', self.llm_call_args[1][0][1])

    def test_specific_compile_diagnostic_retained_in_history_and_next_prompt(self):
        self.approve()
        diagnostic = 'Error-[SE] Syntax error\n"dut_pkg.sv", 550: exact offending token\ncompiler context'
        _, result, report, compiler, _ = self.run_agent2(compile_results=[
            (False, 'compile.log', diagnostic), (True, 'compile.log', 'compiled')])
        self.assertEqual(compiler.call_count, 2)
        history = result['generation']['iteration_history']
        self.assertEqual(history[0]['diagnostic'], diagnostic)
        self.assertIn(diagnostic, history[0]['exact_failures'])
        self.assertIn(diagnostic, self.llm_call_args[1][0][1])
        previous = Path(report.directory)/'generation/attempt_1/dut_pkg.sv'
        self.assertIn(previous.read_text(), self.llm_call_args[1][0][1])

    def test_rtl_compile_failure_has_no_functional_frontend_disposition(self):
        self.approve()
        _, result, report, _, runner = self.run_agent2(compile_results=[
            (False, 'compile.log', 'Error-[SE] Syntax error\n"ddr4_controller_top.sv", 20: bad token')])
        runner.assert_not_called()
        self.assertEqual(result['failure']['disposition'], 'REVALIDATE_AGENT1_RTL')
        self.assertIsNone(result['failure']['frontend_feedback'])
        self.assertFalse((Path(report.directory)/'frontend_feedback.json').exists())

    def test_failure_without_valid_runtime_startup_is_not_frontend_rtl_feedback(self):
        self.approve()
        _, result, _, _, _ = self.run_agent2(simulation_ok=False,
            simulation_text='AGENT2_REQ|SYS-REQ-006|FAIL|unstarted\nUVM_FATAL dut_pkg.sv(4) [SYSTEM_VIF] missing vif')
        self.assertFalse(result['simulation']['startup_valid'])
        self.assertEqual(result['failure']['disposition'], 'INVESTIGATE_VERIFICATION_ENVIRONMENT')
        self.assertIsNone(result['failure']['frontend_feedback'])

    def test_reused_checker_compile_failure_never_changes_checker_or_calls_llm(self):
        self.approve(); self.run_agent2()
        _,result,_,compiler,runner=self.run_agent2(compile_ok=False,
            llm_reply=AssertionError('Saved checker must remain immutable'))
        self.assertEqual(self.llm_calls,0)
        self.assertEqual(compiler.call_count,1)
        runner.assert_not_called()
        self.assertFalse(result['checker_reuse_eligible'])

    def test_uvm_fatal_with_dut_evidence_is_returned_even_if_simulator_exits_nonzero(self):
        self.approve()
        text=simulation_fixture(self.requirements,bad_response=True)+'\nUVM_FATAL dut_pkg.sv(540) [SYSTEM_FAILED] comparison failed'
        _,result,_,compiler,runner=self.run_agent2(simulation_text=text,simulation_ok=False)
        self.assertEqual(result['failure']['classification'],'RTL_FUNCTIONAL_FAILURE')
        self.assertEqual(result['simulation']['uvm_fatal'],1)
        self.assertEqual(compiler.call_count,1)
        self.assertEqual(self.llm_calls,1)

    def test_missing_runtime_evidence_is_not_mislabeled_as_dut_failure(self):
        self.approve()
        _,result,report,_,_=self.run_agent2(simulation_text='UVM_FATAL dut_pkg.sv(99) [SYSTEM_VIF] missing interface',
                                         simulation_ok=False)
        self.assertEqual(result['failure']['classification'],'VERIFICATION_SIMULATION_FAILURE')
        self.assertIsNone(result['failure']['frontend_feedback'])
        self.assertFalse((Path(report.directory)/'frontend_feedback.json').exists())

    def test_early_abort_trusted_failure_is_fail_without_final_requirement_counts(self):
        self.approve()
        text='\n'.join(line for line in simulation_fixture(self.requirements).splitlines()
                       if not line.startswith(('AGENT2_REQ_COUNTS|','AGENT2_REQ|')))
        text+='\nAGENT2_SYSTEM_FAILURE|scenario=timing|cycle=7|requirement=SYS-REQ-013|expected=spacing>=6|observed=spacing=2'
        _,result,report,_,_=self.run_agent2(simulation_text=text)
        self.assertEqual(result['failure']['disposition'],'RETURN_TO_FRONTEND_RTL_FIX')
        trace=json.loads((Path(report.directory)/'requirement_traceability.json').read_text())
        self.assertEqual(next(row for row in trace['requirements'] if row['id']=='SYS-REQ-013')['status'],'FAIL')

    def test_progress_watchdog_reports_failed_requirement_and_frontend_disposition(self):
        self.approve()
        item=event('accepted')
        text='AGENT2_SYSTEM_EVENT|'+'|'.join(key+'='+str(value) for key,value in item.items())
        text+='\nUVM_FATAL dut_pkg.sv(84) @ 200000: [SYSTEM_DRIVER_TIMEOUT] Request drain timed out'
        _,result,report,_,_=self.run_agent2(simulation_text=text,simulation_ok=False)
        self.assertEqual(result['failure']['disposition'],'RETURN_TO_FRONTEND_RTL_FIX')
        self.assertEqual(self.llm_calls,1)
        trace=json.loads((Path(report.directory)/'requirement_traceability.json').read_text())
        self.assertEqual(next(row for row in trace['requirements'] if row['id']=='SYS-REQ-015')['status'],'FAIL')

    def test_module_entrypoint_still_uses_existing_llm_flow(self):
        with patch.object(sys,'argv',['agent2_uvm_tb.py','module.sv']), \
             patch.object(agent2,'run_reported_agent') as reported:
            agent2.main()
        self.assertEqual(reported.call_args[0][1],agent2._main)
        self.assertIn('call_llm',agent2.run_uvm_agent.__code__.co_names)

    def test_contract_uses_real_events_and_weak_ordering(self):
        self.approve()
        self.assertTrue(self.contract['supported'],self.contract['limitations'])
        self.assertIn('txn_valid && txn_ready',self.contract['request_acceptance'])
        self.assertEqual(self.contract['observability']['dispatch'],'dispatch_fire')
        self.assertIn('completion_fire',self.contract['write_completion'])
        self.assertIn('no FIFO',self.contract['ordering']['same_address'])
        self.assertIn('dispatch',self.contract['read_response']['association'])
        self.assertEqual(self.contract['constants']['req_queue_depth'],4)

    def test_requires_agent1_passing_system_result(self):
        self.approve()
        for change in ({'is_valid':False},{'mode':'module'},{'compile_passed':False},{'rtl_files':[]}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                extract_controller_contract(self.manifest_path,dict(self.approved,**change))

    def test_dynamic_rtl_count_and_saved_handoff(self):
        baseline=len(self.manifest['rtl_files'])
        for index in range(24):
            (self.manifest_path.parent/('arbitrary_container_{}.sv'.format(index))).write_text('module optional_{}(input logic clk); endmodule'.format(index))
        self.rebuild(); self.approve()
        ready,result,report,compile_call,run_call=self.run_agent2()
        self.assertTrue(ready,result)
        self.assertEqual(result['rtl_file_count'],baseline+24)
        self.assertEqual(len(compile_call.call_args[0][0]),baseline+24)
        self.assertEqual(compile_call.call_count,1)
        self.assertEqual(run_call.call_count,1)
        saved=json.loads(Path(result['uvm_environment_file']).read_text())
        self.assertEqual(saved['dut_top_module'],self.manifest['top_module'])
        self.assertEqual(len(saved['rtl_files']),baseline+24)
        self.assertIsNone(saved['approved_yaml'])
        self.assertEqual(len(saved['approved_module_specs']),len(self.manifest['module_specs']))
        self.assertFalse(saved['agent3_executed'])
        self.assertEqual(saved['checker_identity'],result['checker_identity'])
        self.assertEqual(saved['compile_order'],['dut_if.sv','dut_pkg.sv','dut_tb_top.sv'])
        self.assertTrue(Path(saved['controller_verification_contract']).is_file())

    def test_optional_requirements_follow_configuration(self):
        self.approve()
        config=dict(self.contract['configuration'],bank_count=1,page_policy='closed_page',refresh_enabled=False,trrd_enabled=False,tfaw_enabled=False)
        requirements=build_system_requirement_catalog(config,self.contract)
        identities={r['id'] for r in requirements}
        for identity in ('SYS-REQ-008','SYS-REQ-009','SYS-REQ-010','SYS-REQ-012','SYS-REQ-013','SYS-REQ-014'):
            self.assertNotIn(identity,identities)
        self.assertIn('SYS-REQ-011',identities)
        contract=dict(self.contract,configuration=config)
        rendered=render_system_environment(contract,requirements,self.approved['dut_spec']['ports'])
        self.assertIn('BANKS=1',rendered['dut_pkg.sv'])
        self.assertIn('OPEN_PAGE=0',rendered['dut_pkg.sv'])

    def test_single_bank_adapter_handles_omitted_bank_port(self):
        self.approve()
        config=dict(self.contract['configuration'],bank_count=1)
        ports=dict(self.contract['host_ports']); ports.pop('txn_bank')
        contract=dict(self.contract,configuration=config,host_ports=ports,
                      widths=dict(self.contract['widths'],bank=1))
        requirements=build_system_requirement_catalog(config,contract)
        dut_ports=[port for port in self.approved['dut_spec']['ports'] if port['name']!='txn_bank']
        rendered=render_system_environment(contract,requirements,dut_ports)
        self.assertNotIn('.txn_bank(',rendered['dut_tb_top.sv'])
        self.assertIn('BANKS=1',rendered['dut_pkg.sv'])
        self.assertIn('BANK_W = 1',rendered['dut_if.sv'])

    def test_runtime_failure_evidence_is_retained_with_failed_scenario(self):
        self.approve()
        text=simulation_fixture(self.requirements)
        text=text.replace('AGENT2_REQ_COUNTS|SYS-REQ-013|1|0','AGENT2_REQ_COUNTS|SYS-REQ-013|1|1')
        text+='\nAGENT2_SYSTEM_FAILURE|scenario=directed|cycle=7|requirement=SYS-REQ-013|expected=spacing >=6|observed=spacing=2|outstanding=4'
        result,report,_=summarize_system_simulation(agent2,self.requirements,self.contract,text,True)
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(result['scenarios']['directed'],'FAIL')
        self.assertEqual(result['scenarios']['trrd_boundary'],'FAIL')
        self.assertTrue(any(f['expected']=='spacing >=6' and f['cycle']==7 for f in result['failure_evidence']))
        self.assertFalse(result['ready_for_agent3'])
        self.assertFalse(result['traffic']['accounting_proven'])
        self.assertIsNone(result['traffic']['lost'])

    def test_scenario_without_executable_comparison_is_not_tested(self):
        self.approve()
        text=simulation_fixture(self.requirements).replace('scenario=directed','scenario=no_comparison',1)
        result,_,_=summarize_system_simulation(agent2,self.requirements,self.contract,text,True)
        self.assertEqual(result['scenarios']['no_comparison'],'NOT_TESTED')

    def test_missing_simulation_evidence_does_not_invent_dut_violations(self):
        self.approve()
        result,report,_=summarize_system_simulation(agent2,self.requirements,self.contract,'',False)
        self.assertEqual(result['status'],'FAIL')
        self.assertEqual(result['requirements']['failed'],0)
        self.assertEqual(result['requirements']['not_tested'],len(self.requirements))
        self.assertIsNone(result['traffic']['lost'])
        self.assertIsNone(result['traffic']['duplicated'])
        self.assertFalse(result['ready_for_agent3'])

    def test_existing_module_handoff_remains_loadable_by_agent3(self):
        import agent3_testvectors as agent3
        self.approve()
        module_spec=self.manifest_path.parent/self.manifest['module_specs'][0]['path']
        import yaml
        spec=yaml.safe_load(module_spec.read_text())
        dut_spec={'module_name':spec['design_name']}
        with system_workspace(agent2,Path(self.temp.name)/'module_handoff'):
            for name in agent2.COMPILE_ORDER:
                (Path(agent2.TB_ROOT)/name).write_text('// existing module environment fixture\n')
            saved=agent2.save_uvm_environment({'overall_status':'PASS'},[],self.approved['rtl_files'],dut_spec,spec,'compile','simulation')
            manifest,files,loaded=agent3.load_uvm_handoff(saved,self.approved['rtl_files'],dut_spec,str(module_spec))
        self.assertEqual(manifest['schema_version'],1)
        self.assertTrue(Path(manifest['approved_yaml']).is_file())
        self.assertEqual(loaded,spec)
        self.assertEqual({name for name,_ in files},{'dut_if.sv','dut_pkg.sv','dut_tb_top.sv'})

    def test_monitor_replay_can_reject_false_simulation_pass(self):
        self.approve()
        ready,result,report,compile_call,run_call=self.run_agent2(bad_response=True)
        self.assertFalse(ready)
        self.assertEqual(result['status'],'FAIL')
        self.assertGreater(result['reads']['data_mismatches'],0)
        self.assertTrue(any(f['expected']==0x1234 and f['observed']==0xffff for f in result['failure_evidence']))
        trace=json.loads((Path(report.directory)/'requirement_traceability.json').read_text())
        read=next(row for row in trace['requirements'] if row['id']=='SYS-REQ-006')
        self.assertEqual(read['simulation']['status'],'FAIL')
        self.assertEqual(read['uvm_simulation']['status'],'PASS')
        self.assertEqual(compile_call.call_count,1)
        self.assertEqual(run_call.call_count,1)
        self.assertTrue(Path(result['uvm_environment_file']).is_file())

    def test_reuse_is_byte_identical_and_preserves_module_globals(self):
        self.approve()
        originals=(agent2.TB_ROOT,agent2.BUILD_DIR,list(agent2.COMPILE_ORDER))
        _,first,_,_,_=self.run_agent2()
        saved=json.loads(Path(first['uvm_environment_file']).read_text())
        contents={Path(r['path']).name:Path(r['path']).read_bytes() for r in saved['testbench_files']}
        ready,second,_,_,_=self.run_agent2(reuse=first['uvm_environment_file'])
        self.assertTrue(ready,second)
        self.assertTrue(second['testbench_reused'])
        self.assertEqual(second['checker_identity'],first['checker_identity'])
        current=json.loads(Path(second['uvm_environment_file']).read_text())
        self.assertEqual(contents,{Path(r['path']).name:Path(r['path']).read_bytes() for r in current['testbench_files']})
        self.assertEqual((agent2.TB_ROOT,agent2.BUILD_DIR,list(agent2.COMPILE_ORDER)),originals)

    def test_incompatible_explicit_reuse_regenerates_rtl_checker_or_specs(self):
        self.approve(); _,first,_,_,_=self.run_agent2()
        saved=json.loads(Path(first['uvm_environment_file']).read_text())
        for path in (Path(saved['rtl_files'][0]['path']),Path(saved['testbench_files'][0]['path']),
                     Path(saved['approved_module_specs'][0]['source'])):
            contents=path.read_bytes()
            path.write_bytes(contents+(b'\n# modified\n' if path.suffix=='.yaml' else b'\n// modified\n'))
            try:
                _,result,_,_,_=self.run_agent2(reuse=first['uvm_environment_file'])
                self.assertFalse(result['testbench_reused'])
                self.assertGreater(self.llm_calls,0)
            finally:path.write_bytes(contents)

    def test_compile_failure_preserves_sources_and_never_simulates(self):
        self.approve()
        ready,result,report,compile_call,run_call=self.run_agent2(compile_ok=False)
        self.assertFalse(ready)
        self.assertEqual(result['status'],'FAIL')
        run_call.assert_not_called()
        self.assertIn('SYSTEM_UVM_COMPILE_FAILED',result['failure_detail'])
        self.assertEqual(result['requirements'],{'passed':0,'failed':0,'not_tested':len(self.requirements)})
        self.assertTrue(Path(result['uvm_environment_file']).is_file())
        self.assertIsNone(result['traffic']['lost'])

    def test_unobservable_contract_is_not_tested_without_compile(self):
        self.approve()
        top=Path(self.contract['sources']['controller_rtl'])
        top.write_text(top.read_text().replace('completion_fire','hidden_completion'))
        with patch.object(agent2,'vcs_compile') as compiler:
            with contextlib.redirect_stdout(io.StringIO()):
                report=self.report()
                ready=run_system_uvm(agent2,report,self.manifest_path,self.agent1_path)
        compiler.assert_not_called()
        result=json.loads((Path(report.directory)/'system_report.json').read_text())
        self.assertFalse(ready)
        self.assertEqual(result['status'],'NOT_TESTED')
        self.assertEqual(result['requirements']['not_tested'],len(self.requirements))

    def test_input_hashes_detect_drift_after_agent1(self):
        fingerprints=input_fingerprints(self.manifest_path)
        verify_fingerprints(agent2,fingerprints)
        path=self.manifest_path.parent/self.manifest['rtl_files'][0]
        path.write_text(path.read_text()+'\n// drift\n')
        with self.assertRaisesRegex(ValueError,'changed after Agent 1'):
            verify_fingerprints(agent2,fingerprints)

    def test_template_uses_only_centralized_internal_observability(self):
        self.approve()
        files=render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports'])
        self.assertNotIn('dut.',files['dut_pkg.sv'])
        self.assertNotIn('bank_mem',files['dut_pkg.sv'])
        self.assertIn('reference_memory[location_t]',files['dut_pkg.sv'])
        self.assertIn('try_next_item',files['dut_pkg.sv'])
        self.assertIn('sample.valid===1 && sample.ready===1',files['dut_pkg.sv'])
        self.assertIn('default input #1step',files['dut_if.sv'])
        self.assertIn('dut.completion_fire',files['dut_tb_top.sv'])

    def test_vcs_dump_arguments_are_signals_and_config_db_uses_virtual_handle(self):
        self.approve()
        import re
        files=render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports'])
        top=files['dut_tb_top.sv']
        dump=re.search(r'\$dumpvars\s*\((.*?)\);',top,re.S).group(1)
        arguments=[part.strip() for part in dump.split(',')]
        self.assertEqual(arguments[0],'0')
        self.assertNotIn('bus',arguments[1:])
        self.assertIn('bus.txn_valid',arguments)
        self.assertIn('bus.dut_port_cmd_ready',arguments)
        self.assertIn('virtual dut_if vif;',top)
        self.assertLess(top.index('vif = bus;'),top.index('uvm_config_db#(virtual dut_if)::set(null, "*", "vif", vif);'))

    def test_every_actual_top_port_is_connected_including_cmd_ready(self):
        self.approve()
        import re
        files=render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports'])
        instance=files['dut_tb_top.sv'].split('ddr4_controller_top dut (',1)[1].split(');',1)[0]
        names=re.findall(r'\.([a-zA-Z_][a-zA-Z_0-9$]*)\s*\(',instance)
        self.assertCountEqual(names,[port['name'] for port in self.approved['dut_spec']['ports']])
        self.assertIn('.cmd_ready(bus.dut_port_cmd_ready)',instance)
        self.assertIn('wire dut_port_cmd_ready;',files['dut_if.sv'])
        self.assertIn('input dut_port_cmd_ready;',files['dut_if.sv'])

    def test_future_generated_output_is_discovered_with_resolved_width(self):
        top=self.manifest_path.parent/'ddr4_controller_top.sv'
        source=top.read_text().replace('output logic cmd_ready,','output logic cmd_ready,\n    output logic [6:0] debug_credit,')
        top.write_text(source.replace('endmodule',"assign debug_credit = 7'd0;\nendmodule"))
        self.rebuild(); self.approve()
        files=render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports'])
        self.assertIn('.debug_credit(bus.dut_port_debug_credit)',files['dut_tb_top.sv'])
        self.assertIn('wire [6:0] dut_port_debug_credit;',files['dut_if.sv'])
        self.assertIn('input dut_port_debug_credit;',files['dut_if.sv'])
        ready,result,report,_,_=self.run_agent2()
        self.assertTrue(ready)
        inventory=json.loads((Path(report.directory)/'dut_port_connectivity.json').read_text())
        observed=next(row for row in inventory if row['name']=='debug_credit')
        self.assertEqual(observed['width'],7)
        self.assertEqual(observed['role'],'observed_output')

    def test_new_input_or_unresolved_output_requires_explicit_review(self):
        self.approve()
        for port,reason in (({'name':'enable_extra','direction':'input','width':1},'drive/tie policy'),
                            ({'name':'bidirectional_extra','direction':'inout','width':8},'drive/tie policy'),
                            ({'name':'debug_extra','direction':'output','width':None},'resolved packed width')):
            with self.subTest(port=port),self.assertRaisesRegex(ValueError,reason):
                render_system_environment(self.contract,self.requirements,self.approved['dut_spec']['ports']+[port])

    def test_clock_reset_connections_use_metadata_and_waveform_has_no_expression(self):
        self.approve()
        contract=dict(self.contract,clock=dict(self.contract['clock'],name='system_clock'),
                      reset=dict(self.contract['reset'],name='reset_high',active_low=False))
        ports=[dict(port,name={'clk':'system_clock','rst_n':'reset_high'}.get(port['name'],port['name']))
               for port in self.approved['dut_spec']['ports']]
        top=render_system_environment(contract,self.requirements,ports)['dut_tb_top.sv']
        self.assertIn('.system_clock(clk)',top)
        self.assertIn('.reset_high(~bus.rst_n)',top)
        dump=top.split('$dumpvars(',1)[1].split(');',1)[0]
        self.assertIn('bus.rst_n',dump)
        self.assertNotIn('~bus.rst_n',dump)

    def test_system_pipeline_stop_after_agent2_runs_once_and_never_agent3(self):
        self.approve()
        for failed_stage in (None,'agent1','agent2'):
            with self.subTest(failed_stage=failed_stage):
                called=[]
                def execute(stage,command,run_dir,environment):
                    called.append(stage); reporter=run_pipeline.AgentReport(stage,run_dir=run_dir)
                    if stage=='agent1':
                        result=dict(self.approved,is_valid=failed_stage!='agent1')
                        run_pipeline.write_report_json(environment['RTL_VALIDATION_RESULT_FILE'],result)
                    else:
                        self.assertEqual(stage,'agent2'); self.assertIn('--system',command)
                        self.assertIn('--agent1-result',command); self.assertIn('--agent1-inputs',command)
                        result={'mode':'system','ready_for_agent3':failed_stage!='agent2',
                                'verification_readiness':'READY_FOR_SYSTEM_AGENT_3' if failed_stage!='agent2' else 'BLOCKED_SYSTEM_AGENT_2',
                                'failure':{'classification':'RTL_FUNCTIONAL_FAILURE' if failed_stage=='agent2' else None,
                                           'disposition':'RETURN_TO_FRONTEND_RTL_FIX' if failed_stage=='agent2' else None,
                                           'frontend_feedback':'feedback.json' if failed_stage=='agent2' else None},
                                'artifacts':{'frontend_feedback.json':'agent2/frontend_feedback.json'} if failed_stage=='agent2' else {}}
                        reporter.artifact('system_report.json',data=result)
                    reporter.update(verification_status='FAIL' if stage==failed_stage else 'PASS')
                    reporter.finish(1 if stage==failed_stage else 0)
                    return 1 if stage==failed_stage else 0
                with patch.object(run_pipeline,'execute_reported_process',side_effect=execute):
                    with contextlib.redirect_stdout(io.StringIO()):
                        if failed_stage:
                            with self.assertRaises(SystemExit):run_pipeline.run_system_pipeline(self.manifest_path,{'manifest':self.manifest},stop_after_agent2=True)
                        else:run_pipeline.run_system_pipeline(self.manifest_path,{'manifest':self.manifest},stop_after_agent2=True)
                self.assertEqual(called,['agent1'] if failed_stage=='agent1' else ['agent1','agent2'])
                summary=json.loads((self.manifest_path.parent/'verification_reports'/'latest.json').read_text())
                self.assertEqual(summary['stages']['agent3']['status'],'SKIPPED')
                self.assertFalse(summary['verification_complete'])
                self.assertFalse(summary['ready_for_backend'])
                if failed_stage=='agent2':
                    self.assertEqual(summary['disposition'],'RETURN_TO_FRONTEND_RTL_FIX')
                    self.assertEqual(summary['frontend_feedback_artifact'],'agent2/frontend_feedback.json')


if __name__=='__main__': unittest.main()
