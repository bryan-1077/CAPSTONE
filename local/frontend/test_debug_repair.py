"""Regression coverage for debug intake, investigation, experiments, and repair."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import debug_agent as agent
import debug_focus as focus
import debug_investigation as investigation
from debug_experiment import run_experiment
from verif_debug_intake import capture_verif_run


def assessment(actions=None, next_inspection=""):
    return json.dumps({
        "decision": "sufficient_evidence",
        "hypotheses": [{"summary": "Scheduler cannot issue", "cause": "Input may be undriven",
                        "uncertainty": "Need to check wiring", "next_inspection": next_inspection,
                        "supporting_evidence": ["E1", "E2", "E3"], "contradictory_evidence": []}],
        "next_actions": actions or [], "unresolved_questions": [],
    })


def record():
    return {"evidence": [{"id": f"E{i}", "kind": kind, "text": "source"}
                         for i, kind in enumerate(("rtl", "observation", "checker"), 1)],
            "actions": [], "pending": [], "decision": "insufficient_evidence"}


class EvidenceTests(unittest.TestCase):
    def test_requested_wiring_inspection_blocks_patching(self):
        data = record()
        action = {"tool": "search", "terms": ["timing_ok"], "paths": ["tb/top.sv"]}
        investigation.accept_assessment(data, assessment([action]))
        self.assertEqual(data["decision"], "insufficient_evidence")
        self.assertEqual(data["pending"][0], action)
        self.assertEqual(agent.route_after_assessment({"investigation": data, "repair": True}), "investigate")

    def test_primary_hypothesis_inspection_blocks_patching(self):
        data = record()
        investigation.accept_assessment(data, assessment(next_inspection="Check the input connection"))
        self.assertEqual(data["decision"], "insufficient_evidence")

    def test_independent_question_does_not_block_supported_repair(self):
        response = json.loads(assessment())
        response["unresolved_questions"] = ["A separate row-hit failure remains unexplained"]
        data = record()
        investigation.accept_assessment(data, json.dumps(response))
        self.assertEqual(data["decision"], "sufficient_evidence")

    def test_triage_includes_connection_and_all_failure_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl_output").mkdir()
            (root / "rtl_output/scheduler.sv").write_text("module scheduler; endmodule")
            (root / "tb").mkdir()
            lines = ["// padding"] * 170
            lines[114] = ".timing_ok(1'b1),"
            lines += ["task automatic checks;"]
            lines += [f'`CHECK(0, "failure {i}")' for i in range(4)]
            lines += ["endtask"]
            (root / "tb/top.sv").write_text("\n".join(lines))
            failure = root / "debug/failure"
            (failure / "intake").mkdir(parents=True)
            (failure / "intake/raw.log").write_text("\n".join(f"[ERROR] failure {i}" for i in range(4)))
            data = investigation.new_investigation(root, failure, [{"file": "rtl_output/scheduler.sv"}], [],
                8, 64000, {"testbench_errors": [{"message": f"failure {i}", "log_line": i + 1} for i in range(4)]})
            investigation.inspect(data, root)
            evidence = "\n".join(item["text"] for item in data["evidence"])
            self.assertIn(".timing_ok(1'b1)", evidence)
            for i in range(4):
                self.assertIn(f"failure {i}", evidence)


class RetryTests(unittest.TestCase):
    def test_failed_checks_and_empty_patch_route_to_reassessment(self):
        failed = {"stop_reason": "checks_failed", "status": {
            "status": "target_failed", "rtl_modified": False, "rollback": {"passed": True}}}
        self.assertEqual(agent.route_after_apply(failed), "reassess_repair")
        self.assertEqual(agent.route_after_validate({"validation": {"status": "patch_rejected"}}), "reassess_repair")
        failed["status"]["rollback"]["passed"] = False
        self.assertEqual(agent.route_after_apply(failed), "record_result")

    def test_previous_attempts_include_response_and_diagnostics_after_long_build(self):
        with tempfile.TemporaryDirectory() as directory:
            failure = Path(directory)
            attempt = failure / "attempts/attempt_01"
            attempt.mkdir(parents=True)
            (attempt / "patch.diff").write_text("")
            (attempt / "response.md").write_text("NO_PATCH: need timing_ok wiring")
            (attempt / "target.log").write_text("g++ compiling\n" * 2000 +
                "[ERROR] Scheduler should select row-hit req[1]\nTEST FAIL\nErrors: 1\n")
            context = agent.collect_previous_attempts_context(failure)
            self.assertIn("need timing_ok wiring", context[0]["response_excerpt"])
            self.assertIn("row-hit req[1]", context[0]["check_diagnostics"]["target"])
            self.assertNotIn("g++", context[0]["check_diagnostics"]["target"])

    def test_reassessment_passes_failed_patch_feedback_and_clears_readiness(self):
        data = record()
        data.update(decision="sufficient_evidence", stop_reason="old")
        feedback = [{"attempt": "attempt_01", "check_diagnostics": {"target": "TEST FAIL"}}]
        with patch.object(agent, "graph_failure_dir", return_value=Path("/tmp/failure")), \
             patch.object(agent, "investigation_record", return_value=data), \
             patch.object(agent, "collect_previous_attempts_context", return_value=feedback), \
             patch.object(agent, "numbered_attempt_dirs", return_value=[Path("attempt_01")]), \
             patch.object(agent, "save_investigation", return_value={"stop_reason": ""}), \
             patch.object(agent, "log_debug"), \
             patch.object(agent, "graph_assess_evidence_node", return_value={"assessed": True}) as assess:
            result = agent.graph_reassess_repair_node({"max_attempts": 3})
        self.assertEqual(data["repair_feedback"], feedback)
        self.assertEqual(data["decision"], "insufficient_evidence")
        self.assertNotIn("stop_reason", data)
        self.assertTrue(result["assessed"])
        assess.assert_called_once()

    def test_exhausted_repair_budget_stops_before_another_model_call(self):
        data = record()
        data["unresolved_questions"] = []
        with patch.object(agent, "graph_failure_dir", return_value=Path("/tmp/failure")), \
             patch.object(agent, "investigation_record", return_value=data), \
             patch.object(agent, "collect_previous_attempts_context", return_value=[]), \
             patch.object(agent, "numbered_attempt_dirs", return_value=[Path("attempt_01")]), \
             patch.object(agent, "save_investigation", return_value={"stop_reason": "repair_attempts_exhausted"}), \
             patch.object(agent, "graph_assess_evidence_node") as assess:
            result = agent.graph_reassess_repair_node({"max_attempts": 1})
        self.assertEqual(result["stop_reason"], "repair_attempts_exhausted")
        assess.assert_not_called()

    def test_langgraph_conditional_destinations_exist(self):
        # Validate graph wiring without installing LangGraph or calling a model.
        import sys
        import types
        from typing import get_args, get_type_hints
        class Graph:
            def __init__(self, schema):
                self.nodes = set()
                self.routes = []
            def add_node(self, name, function):
                self.nodes.add(name)
            def add_edge(self, source, destination):
                pass
            def add_conditional_edges(self, source, function):
                self.routes.append((source, function))
            def compile(self, **kwargs):
                for source, function in self.routes:
                    for destination in get_args(get_type_hints(function)["return"]):
                        if destination not in self.nodes:
                            raise AssertionError(f"Unknown destination {source} -> {destination}")
                return self
        modules = {name: types.ModuleType(name) for name in (
            "langgraph", "langgraph.checkpoint", "langgraph.checkpoint.memory", "langgraph.graph")}
        modules["langgraph.checkpoint.memory"].MemorySaver = lambda: None
        modules["langgraph.graph"].StateGraph = Graph
        modules["langgraph.graph"].START = "START"
        modules["langgraph.graph"].END = "END"
        with patch.dict(sys.modules, modules):
            self.assertTrue(callable(agent.build_langgraph_runner()))

    def test_fallback_reinvestigates_before_second_proposal(self):
        events = []
        def node(name, result):
            def run(state):
                events.append(name)
                return result
            return run
        initial = ["run_or_ingest", "parse_failure", "classify_failure", "localize_rtl", "write_patch_plan"]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in initial:
                stack.enter_context(patch.object(agent, f"graph_{name}_node", node(name, {})))
            stack.enter_context(patch.object(agent, "route_after_patch_plan", return_value="investigate"))
            stack.enter_context(patch.object(agent, "graph_investigate_node", node("inspect", {})))
            stack.enter_context(patch.object(agent, "graph_assess_evidence_node", node("assess", {
                "investigation": {"decision": "sufficient_evidence"}, "stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_build_patch_context_node", node("context", {"stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_propose_patch_node", node("propose", {"attempt_dir": "attempt", "stop_reason": ""})))
            stack.enter_context(patch.object(agent, "graph_validate_patch_node", node("validate", {"validation": {"can_apply": True}})))
            stack.enter_context(patch.object(agent, "graph_apply_patch_node", side_effect=[{
                "stop_reason": "checks_failed", "status": {"status": "target_failed", "rtl_modified": False,
                "rollback": {"passed": True}}}, {"stop_reason": "", "status": {"status": "fixed"}}]))
            stack.enter_context(patch.object(agent, "graph_reassess_repair_node", node("reassess", {
                "stop_reason": "", "investigation": {"decision": "insufficient_evidence"}})))
            stack.enter_context(patch.object(agent, "graph_record_result_node", node("result", {})))
            agent.run_fallback_graph({"repair": True})
        first = events.index("propose")
        second = events.index("propose", first + 1)
        self.assertEqual(events[first + 1:second], ["validate", "reassess", "inspect", "assess", "context"])


def _focus_evidence(number,kind='rtl',size=100):
    lines=[f'{i}: source_{number} signal = value; // '+ 'x'*40 for i in range(1,size+1)]
    return dict(id=f'E{number}',kind=kind,path=f'rtl_output/source_{number}.sv',
                start_line=1,end_line=size,sha256=f'hash{number}',text='\n'.join(lines))


def _focus_record():
    items=[_focus_evidence(1),_focus_evidence(2,'observation'),_focus_evidence(3,'checker')]
    return dict(focused_mode=True,evidence=items,actions=[],pending=[],hypotheses=[],
                catalog={e['path']:e['kind'] for e in items},max_chars=128000,chars_used=0,
                max_actions=12,assessments=0,omitted_context=[],decision='insufficient_evidence')


def _focus_assessment(identity='H1',result='not_run',refs=None,revision=''):
    return dict(decision='insufficient_evidence',
        hypotheses=[dict(summary='Payload corruption',cause='Queue storage flips a bit',
                         uncertainty='Checker sampling also possible',next_inspection='Compare stored and sampled fields',
                         supporting_evidence=['E1','E2'],contradictory_evidence=[])],
        focus=dict(failure='Request data changed at dispatch',hypothesis_id=identity,
                   alternative='Checker samples after queue slot reuse',
                   discriminating_check='Compare stored payload and checker sampling at the same edge',
                   primary_prediction='Stored payload already differs',
                   alternative_prediction='Stored payload is correct but the checker samples another slot',
                   check_result=result,result_evidence=refs or [],counterevidence=[],revision_reason=revision),
        unresolved_questions=[],next_actions=[])


class FocusTests(unittest.TestCase):
    def test_small_queue_module_keeps_the_update_near_its_end(self):
        item=_focus_evidence(1,size=87)
        lines=[f'{i}: // queue field and state logic' for i in range(1,88)]
        lines[80]='81: entries[slot] <= incoming ^ reuse_slot;'
        item['text']='\n'.join(lines)
        self.assertLessEqual(len(item['text']),6000)
        text,partial=focus.excerpt(item,[])
        self.assertIn('81: entries[slot] <= incoming ^ reuse_slot;',text)
        self.assertFalse(partial)

    def test_working_set_is_bounded_and_keeps_counterevidence(self):
        data=_focus_record()
        data['evidence'] += [_focus_evidence(i,size=300) for i in range(4,22)]
        data['catalog'].update({e['path']:e['kind'] for e in data['evidence']})
        data['hypotheses']=[dict(cause='signal storage error',supporting_evidence=['E21'],contradictory_evidence=['E4'])]
        before=copy.deepcopy(data['evidence'])
        context=focus.prompt_context(data)
        self.assertLessEqual(context['source_characters'],focus.SOURCE_BUDGET)
        self.assertIn('E4',[e['id'] for e in context['evidence']])
        self.assertTrue(any(not e['presented'] for e in context['evidence_archive']))
        self.assertEqual(data['evidence'],before)
        self.assertTrue(any(e['partial'] for e in context['evidence']))

    def test_explicit_recall_can_revisit_archived_lines(self):
        data=_focus_record()
        data['evidence'] += [_focus_evidence(i,size=300) for i in range(4,22)]
        data['catalog'].update({e['path']:e['kind'] for e in data['evidence']})
        data['actions']=[dict(request=dict(tool='read',path='rtl_output/source_1.sv',start_line=80,end_line=88),
                              evidence_ids=[],status='no_new_evidence')]
        context=focus.prompt_context(data)
        recalled=next(e for e in context['evidence'] if e['id']=='E1')
        self.assertIn('80: source_1',recalled['text'])

    def test_competing_predictions_required(self):
        data=_focus_record();response=_focus_assessment();response['focus']['alternative_prediction']=response['focus']['primary_prediction']
        with self.assertRaisesRegex(ValueError,'different predictions'):
            investigation.accept_assessment(data,json.dumps(response))

    def test_refuted_cause_cannot_silently_return(self):
        data=_focus_record()
        investigation.accept_assessment(data,json.dumps(_focus_assessment(result='supports_alternative',refs=['E2'])))
        with self.assertRaisesRegex(ValueError,'Previously refuted'):
            investigation.accept_assessment(data,json.dumps(_focus_assessment(result='supports_primary',refs=['E2'])))
        data['evidence'].append(_focus_evidence(4,'observation'))
        revived=_focus_assessment(result='supports_primary',refs=['E4'],revision='New trace shows the earlier sampling assumption was wrong')
        investigation.accept_assessment(data,json.dumps(revived))
        self.assertEqual(data['focus']['check_result'],'supports_primary')

    def test_no_progress_requires_disconfirmation_or_new_diagnosis(self):
        data=_focus_record()
        investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        with self.assertRaisesRegex(ValueError,'No new evidence'):
            investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        updated=_focus_assessment();updated['next_actions']=[dict(tool='search',terms=['sampling'],purpose='disconfirm')]
        investigation.accept_assessment(data,json.dumps(updated))
        self.assertTrue(data['reconsideration_required'])
        investigation.accept_assessment(data,json.dumps(_focus_assessment('H2',revision='Sampling evidence points to the checker instead')))
        self.assertEqual(data['focus']['hypothesis_id'],'H2')

    def test_diagnosis_change_defers_old_confirmation_plan(self):
        data=_focus_record()
        old_action=dict(tool='search',terms=['confirm queue'])
        initial=_focus_assessment();initial['next_actions']=[old_action]
        investigation.accept_assessment(data,json.dumps(initial))
        changed=_focus_assessment('H2',revision='New observation points to checker sampling')
        new_action=dict(tool='search',terms=['sample edge'],purpose='disconfirm')
        changed['next_actions']=[new_action]
        investigation.accept_assessment(data,json.dumps(changed))
        self.assertEqual(data['pending'],[new_action])
        self.assertEqual(data['deferred_plans'][-1]['actions'],[old_action])

    def test_archived_range_can_be_requested_again_without_queue_duplication(self):
        data=_focus_record()
        request=dict(tool='read',path='rtl_output/source_1.sv',start_line=80,end_line=88)
        data['actions']=[dict(request=request,status='ok',evidence_ids=['E1'])]
        data['presented_lines']={}
        response=_focus_assessment();response['next_actions']=[request,dict(request,reason='Same recall')]
        investigation.accept_assessment(data,json.dumps(response))
        self.assertEqual(data['pending'],[request])

    def test_recalling_different_lines_counts_as_progress(self):
        data=_focus_record();data['working_context_signature']='first range'
        investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        data['working_context_signature']='different recalled range'
        investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        self.assertEqual(data['focus_stagnation'],0)

    def test_failure_overview_omits_duplicate_inventory_not_diagnostics(self):
        original=dict(flow_errors=[dict(message='payload changed',file='queue.sv',
                                       rtl_files=['many_files.sv']*100,raw='duplicate payload changed')])
        result=focus.failure_overview(original)
        self.assertEqual(result['flow_errors'][0]['message'],'payload changed')
        self.assertEqual(result['flow_errors'][0]['file'],'queue.sv')
        self.assertNotIn('rtl_files',result['flow_errors'][0])
        self.assertIn('rtl_files',original['flow_errors'][0])

    def test_no_patch_readiness_until_check_distinguishes_causes(self):
        data=_focus_record();response=_focus_assessment()
        response.update(decision='sufficient_evidence')
        response['hypotheses'][0].update(next_inspection='',supporting_evidence=['E1','E2','E3'])
        with self.assertRaisesRegex(ValueError,'discriminating check'):
            investigation.accept_assessment(data,json.dumps(response))
        response['focus'].update(check_result='supports_primary',result_evidence=['E1','E2','E3'])
        investigation.accept_assessment(data,json.dumps(response))
        self.assertEqual(data['decision'],'sufficient_evidence')

    def test_failed_repair_needs_explicit_reassessment_without_forcing_wrong_cause(self):
        data=_focus_record();data['focus_review_required']=True
        with self.assertRaisesRegex(ValueError,'failed repair feedback'):
            investigation.accept_assessment(data,json.dumps(_focus_assessment()))
        response=_focus_assessment(revision='The patch fixed one payload field but left a separate timing failure; inspect that next')
        investigation.accept_assessment(data,json.dumps(response))
        self.assertNotIn('focus_review_required',data)

    def test_unpresented_archive_cannot_authorize_a_patch(self):
        data=_focus_record();data['presented_evidence_ids']=['E1','E2']
        response=_focus_assessment(result='supports_primary',refs=['E1','E2','E3'])
        response['decision']='sufficient_evidence'
        response['hypotheses'][0].update(next_inspection='',supporting_evidence=['E1','E2','E3'])
        with self.assertRaisesRegex(ValueError,'Recall archived evidence'):
            investigation.accept_assessment(data,json.dumps(response))


def response(actions):
    return json.dumps(dict(decision='insufficient_evidence', hypotheses=[],
                           unresolved_questions=['Trace the data path'], next_actions=actions))


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.failure = self.root / 'debug/failure'
        (self.failure / 'intake').mkdir(parents=True)
        (self.failure / 'intake/raw.log').write_text('FAIL data mismatch')
        (self.root / 'rtl_output').mkdir()
        for name in ('controller', 'queue', 'timing'):
            (self.root / f'rtl_output/{name}.sv').write_text(
                '\n'.join(f'// {name} line {i}' for i in range(1, 543)))
        self.data = investigation.new_investigation(self.root, self.failure, [], [], 8, 64000)
        investigation.inspect_batch(self.data, self.root)

    def test_requested_queue_cannot_be_starved_by_new_controller_pages(self):
        actions = [dict(tool='read', path=f'rtl_output/{name}.sv', start_line=1, end_line=200)
                   for name in ('controller', 'queue', 'timing')]
        actions += [dict(tool='read', path='rtl_output/controller.sv', start_line=i, end_line=i+20)
                    for i in (210, 250, 290, 330)]
        investigation.accept_assessment(self.data, response(actions))
        self.assertEqual(len(self.data['pending']), 7)  # no actions[:4] truncation
        investigation.inspect_batch(self.data, self.root)
        self.assertEqual([a['request']['path'] for a in self.data['actions'][1:4]],
                         [a['path'] for a in actions[:3]])
        later = dict(tool='read', path='rtl_output/controller.sv', start_line=400, end_line=500)
        duplicate = dict(actions[3], reason='Different prose, same requested read')
        investigation.accept_assessment(self.data, response([later, duplicate]))
        self.assertEqual(self.data['pending'], actions[3:] + [later])

    def test_coherent_read_reaches_controller_instance_near_line_535(self):
        self.data['pending'] = [dict(tool='read', path='rtl_output/controller.sv', start_line=1, end_line=600)]
        investigation.inspect_batch(self.data, self.root)
        evidence = '\n'.join(e['text'] for e in self.data['evidence'])
        self.assertIn('535: // controller line 535', evidence)
        self.assertLessEqual(self.data['chars_used'], self.data['max_chars'])

    def test_batch_respects_action_budget(self):
        self.data['max_actions'] = 2
        self.data['pending'] = [dict(tool='read', path=f'rtl_output/{name}.sv')
                                for name in ('controller', 'queue', 'timing')]
        investigation.inspect_batch(self.data, self.root)
        self.assertEqual(len(self.data['actions']), 2)
        self.assertEqual(len(self.data['pending']), 2)

    def test_cli_defaults_match_full_controller_investigation(self):
        for argv in (['debug_agent.py','auto','verif','--log','saved','--repair'],
                     ['debug_agent.py','graph','--failure-dir','saved','--repair']):
            with patch.object(sys, 'argv', argv):
                args = agent.parse_args()
            self.assertEqual(args.investigation_steps, investigation.DEFAULT_MAX_ACTIONS)
            self.assertEqual(args.investigation_context_chars, investigation.DEFAULT_MAX_CHARS)

    def test_controller_context_does_not_hide_queue_assignment_or_checker_tail(self):
        # Match run 0010's shape: ~27k initial context, a complete controller,
        # refresh logic and checker reads before the small faulty queue file.
        background = self.root/'rtl_output/background.sv'
        background.write_text('\n'.join('// prior collected context '+str(i) for i in range(800)))
        controller = self.root/'rtl_output/controller.sv'
        controller.write_text('\n'.join('// controller declarations and sequential logic '+str(i) for i in range(542)))
        queue = self.root/'rtl_output/queue.sv'
        queue_lines = ['// queue declarations and packing logic '+str(i) for i in range(87)]
        queue_lines[80] = 'entries[slot] <= incoming ^ reuse_slot;'
        queue.write_text('\n'.join(queue_lines))
        checker = self.root/'tb/checker.sv'
        checker.parent.mkdir()
        checker.write_text('\n'.join('// checker acceptance dispatch and clock sampling '+str(i) for i in range(407)))
        data = investigation.new_investigation(self.root, self.failure, [], [],
                    investigation.DEFAULT_MAX_ACTIONS, investigation.DEFAULT_MAX_CHARS)
        data['pending'] = [dict(tool='triage', inspections=[dict(path='rtl_output/background.sv',
                                         whole_file=True, limit=30000)])]
        investigation.inspect_batch(data,self.root)
        first = [dict(tool='read',path='rtl_output/controller.sv',start_line=1,end_line=600),
                 dict(tool='read',path='rtl_output/timing.sv',start_line=1,end_line=600),
                 dict(tool='read',path='tb/checker.sv',start_line=196,end_line=407)]
        investigation.accept_assessment(data,response(first))
        investigation.inspect_batch(data,self.root)
        second = [dict(tool='read',path='rtl_output/controller.sv',start_line=305,end_line=600),
                  dict(tool='read',path='rtl_output/queue.sv',start_line=1,end_line=600),
                  dict(tool='read',path='tb/checker.sv',start_line=298,end_line=407)]
        investigation.accept_assessment(data,response(second))
        investigation.inspect_batch(data,self.root)
        evidence = '\n'.join(e['text'] for e in data['evidence'])
        self.assertIn('81: entries[slot] <= incoming ^ reuse_slot;',evidence)
        self.assertTrue(any(e['path']=='tb/checker.sv' and e['end_line']==407 for e in data['evidence']))
        self.assertGreater(data['chars_used'],64000)
        self.assertFalse(investigation.context_exhausted(data))
        self.assertTrue(investigation.can_inspect(data))

    def test_inventory_is_not_diagnostic_localization(self):
        top, queue = self.root/'rtl_output/controller.sv', self.root/'rtl_output/queue.sv'
        index = dict(rtl_files=[top, queue], file_text={top:'', queue:''},
                     module_to_file={'controller': top, 'queue': queue},
                     file_to_modules={top:['controller'], queue:['queue']})
        with patch.object(agent, 'collect_rtl_index', return_value=index), \
             patch.object(agent, 'extract_failure_terms', return_value=[]), \
             patch.object(agent, 'is_read_data_mismatch', return_value=False):
            parsed = agent.ParsedFailure(source='verif', returncode=None, command_passed=None,
                summary='', counts={}, status_markers={}, testbench_errors=[],
                assert_failures=[], verilator_diagnostics=[],
                flow_errors=[{'rtl_files': [str(top),str(queue)]}])
            self.assertEqual(agent.localize_rtl('', parsed).suspects, [])
            parsed.flow_errors.append({'file': str(queue)})
            localized = agent.localize_rtl('', parsed)
            self.assertEqual(len(localized.suspects), 1)
            self.assertIn('queue.sv', localized.suspects[0]['file'])


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rtl = self.root/'rtl_output/top.sv'
        self.rtl.parent.mkdir()
        self.rtl.write_text('module top(output logic q); assign q = 1; endmodule')
        self.failure = self.root/'debug/failure'
        (self.failure/'intake').mkdir(parents=True)
        (self.failure/'intake/raw.log').write_text('FAIL q expected 0 observed 1')
        self.action = dict(tool='simulate', rtl_paths=['rtl_output/top.sv'], testbench='''
module debug_repro;
wire q; top dut(q);
initial begin
 #1; $display("expected=0 observed=%0d",q);
 if (q !== 0) $fatal(1,"reproduced mismatch");
 $finish;
end
endmodule
''')

    def test_external_access_and_non_catalog_sources_are_rejected(self):
        for text in ('$system("touch /tmp/no");', '$fopen("outside");', 'import "DPI-C" function int f();', '`include "outside.sv"'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                run_experiment(dict(self.action, testbench='module debug_repro; '+text+' endmodule'),
                               self.root, self.failure, 1, {'rtl_output/top.sv':'rtl'})
        with self.assertRaises(ValueError):
            run_experiment(dict(self.action, rtl_paths=['outside.sv']), self.root, self.failure, 1, {})

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator unavailable')
    def test_real_reproducer_records_failure_without_editing_rtl_or_authorizing_patch(self):
        before = self.rtl.read_bytes()
        data = investigation.new_investigation(self.root,self.failure,[],[],8,64000)
        data['pending'] = [self.action]
        investigation.inspect_batch(data, self.root)
        action = data['actions'][0]
        self.assertEqual(action['status'],'ok',action)
        result = action['experiment']
        self.assertEqual(result['compile']['exit_code'],0)
        self.assertNotEqual(result['simulation']['exit_code'],0)
        self.assertEqual(result['source_hashes']['rtl_output/top.sv'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.rtl.read_bytes(),before)
        self.assertEqual(data['decision'],'insufficient_evidence')
        self.assertEqual(data['reruns_used'],1)
        self.assertTrue(any('reproduced mismatch' in e['text'] for e in data['evidence']))
        self.assertFalse(any(e['kind']=='checker' for e in data['evidence']))

    def test_timeout_kills_process_group(self):
        from debug_experiment import run_process
        import sys
        result = run_process([sys.executable,'-c','import time; time.sleep(10)'],
                             self.root,self.root/'timeout.log',1)
        self.assertTrue(result['timed_out'])


class SystemEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'reports/run_one'
        self.intake = self.root / 'debug/failure/intake'
        self.rtl = self.root / 'rtl_output'
        self.rtl.mkdir()
        self.intake.mkdir(parents=True)
        (self.source / 'agent2/work/tb_auto').mkdir(parents=True)
        (self.rtl / 'top.sv').write_text('module top; endmodule')
        manifest = {'rtl_files': ['top.sv']}
        (self.rtl / 'manifest.json').write_text(json.dumps(manifest))
        (self.source / 'agent2/source_manifest.json').write_text(json.dumps(manifest))
        self.checker = self.source / 'agent2/work/tb_auto/dut_pkg.sv'
        self.checker.write_text('function void check(); `uvm_error("ID", "Mismatch"); endfunction')
        self.remote = '/remote/input/rtl'
        self.summary = dict(overall_status='FAIL', input_directory=self.remote,
                            stages={'agent1': {'status': 'PASS'}, 'agent2': {'status': 'FAIL'}})
        (self.source / 'summary.json').write_text(json.dumps(self.summary))
        self.environment = dict(testbench_files=[dict(
            path=self.remote + '/verification_reports/run_one/agent2/work/tb_auto/dut_pkg.sv',
            sha256=hashlib.sha256(self.checker.read_bytes()).hexdigest())],
            rtl_files=[dict(path=self.remote + '/top.sv',
                            sha256=hashlib.sha256((self.rtl / 'top.sv').read_bytes()).hexdigest())])
        self.write_environment()

    def write_environment(self):
        (self.source / 'agent2/uvm_environment.json').write_text(json.dumps(self.environment))

    def test_imports_compiled_checker_and_proves_current_rtl_identity(self):
        (self.source / 'agent2/rtl_snapshot').mkdir()
        (self.source / 'agent2/rtl_snapshot/top.sv').write_text('historical DUT')
        report = json.loads(capture_verif_run(self.source, self.intake, self.rtl))
        self.assertTrue(report['rtl_identity'][0]['matches_current_rtl'])
        self.assertIn('matches current', report['rtl_provenance'])
        evidence = self.intake / 'evidence/verif_run'
        self.assertTrue((evidence / 'agent2/work/tb_auto/dut_pkg.sv').is_file())
        self.assertFalse((evidence / 'agent2/rtl_snapshot/top.sv').exists())
        catalog = investigation.source_catalog(self.root, self.intake.parent)
        self.assertEqual(catalog[str((evidence / 'agent2/work/tb_auto/dut_pkg.sv').relative_to(self.root))], 'checker')

    def test_changed_or_incomplete_rtl_inventory_cannot_claim_identity(self):
        for change in ('changed', 'incomplete'):
            with self.subTest(change=change):
                if change == 'changed':
                    (self.rtl / 'top.sv').write_text('module top; wire changed; endmodule')
                else:
                    self.environment['rtl_files'] = []
                    self.write_environment()
                report = json.loads(capture_verif_run(self.source, self.intake, self.rtl))
                self.assertFalse(report['rtl_identity'][0]['matches_current_rtl'])
                self.assertIn('differs', report['rtl_provenance'])

    def test_checker_mismatch_and_escaping_paths_are_rejected(self):
        self.checker.write_text('changed checker')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            capture_verif_run(self.source, self.intake, self.rtl)
        self.environment['testbench_files'][0]['path'] = '../outside.sv'
        self.write_environment()
        with self.assertRaisesRegex(ValueError, 'inside the verification run'):
            capture_verif_run(self.source, self.intake, self.rtl)

    def test_legacy_candidate_intake_retains_unknown_provenance(self):
        (self.source / 'agent2/uvm_environment.json').unlink()
        candidate = self.source / 'agent2/candidates/iter_0/dut_pkg.sv'
        candidate.parent.mkdir(parents=True)
        candidate.write_text('legacy checker')
        report = json.loads(capture_verif_run(self.source, self.intake, self.rtl))
        self.assertNotIn('rtl_identity', report)
        self.assertIn('not established', report['rtl_provenance'])
        self.assertTrue((self.intake / 'evidence/verif_run/agent2/candidates/iter_0/dut_pkg.sv').is_file())

    def test_triage_puts_failed_requirement_before_large_helper_tasks(self):
        lines = ['function void generic_check();'] + ['expected = observed;'] * 600 + ['endfunction']
        lines += ['task monitor();', 'check_requirement("SYS-REQ-007", expected == observed);', 'endtask']
        ranges = investigation.checker_ranges(lines, {'terms': ['SYS-REQ-007', 'expected', 'check']})
        self.assertTrue(ranges[0][0] <= 604 <= ranges[0][1])

    def test_large_log_search_reads_and_deduplication_preserve_line_numbers(self):
        log = self.intake / 'evidence/verif_run/agent2/simulation.log'
        log.parent.mkdir(parents=True)
        padding = 'routine simulation output ' + 'x' * 100 + '\n'
        log.write_text(padding * 18000 + 'UVM_ERROR mismatch expected=0 observed=1\ncontext after failure\n')
        (self.intake / 'raw.log').write_text('FAIL')
        data = investigation.new_investigation(self.root, self.intake.parent, [], [], 8, 64000)
        name = str(log.relative_to(self.root))
        data['pending'] = [{'tool': 'search', 'paths': [name], 'terms': ['UVM_ERROR', 'mismatch']}]
        investigation.inspect(data, self.root)
        self.assertEqual(data['actions'][0]['status'], 'ok')
        evidence = data['evidence'][0]
        self.assertEqual(evidence['sha256'], hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertIn('18001: UVM_ERROR', evidence['text'])
        used = data['chars_used']
        data['pending'] = [{'tool': 'search', 'paths': [name], 'terms': ['mismatch']}]
        investigation.inspect(data, self.root)
        self.assertEqual(data['chars_used'], used)
        data['pending'] = [{'tool': 'read', 'path': name, 'start_line': 17990, 'end_line': 17993}]
        investigation.inspect(data, self.root)
        self.assertIn('17990:', data['evidence'][-1]['text'])
        self.assertLess(data['chars_used'], 4000)


if __name__ == "__main__":
    unittest.main()
