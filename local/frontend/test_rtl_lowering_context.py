"""Stage-boundary regressions: policy/facts, unchanged gates, and real SV packing."""
from contextlib import ExitStack, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import dual_llm_rtlGen as gen
from ir_to_llm_context import build_llm_context
from llm_diagnostics import LlmFailure
from prompt_context import expand_context
from rtl_lowering_context import RTL_STAGE_POLICY, scoped_system_prompt
from rtl_recovery import active_recovery, RecoveryBudget
from campaigns.timing import VERSION, cases


def design(lowered=True, explicit=False):
    interfaces={'open_rows':{'type':'shared.types.bank_rows','direction':'input'},
                'open_valid':{'type':'shared.types.bank_valid','direction':'input'},
                'candidate_ok':{'type':'shared.types.bit','direction':'output'}}
    metadata={'behavior':{'future_behavior':{'retain':'exactly'}},
              'implementation_constraints':['Preserve public bank order.']}
    if explicit:metadata['implementation_constraints'].append('In SystemVerilog RTL declare and use typedef user_bank_rows_t.')
    if lowered:
        metadata['behavior'].update(contract={'interfaces':interfaces,'role':'timing_enforcement'},
          specification_context={'package_fingerprint':'fixture-revision',
            'parameters':{'BANK_COUNT':4,'ROW_WIDTH':4},
            'types':{'bank_rows':{'kind':'array','width':16,'count':4,'element_width':4,
                                 'packed_order':'ascending_index_from_lsb'},
                     'bank_valid':{'kind':'array','width':4,'count':4,'element_width':1,
                                  'packed_order':'ascending_index_from_lsb'}},
            'timing_profile':{'selection':'DDR4-2400','unit':'cycles','values':{'tRCD':17,'tRAS':39,'tRRD':4,'tFAW':30}},
            'system_rules':{'R1':{'statement':'Use specification shared names and local timing_cycles bindings.'}}})
    return SimpleNamespace(design_name='stage_fixture',design_type='datapath',
        clock=SimpleNamespace(name='clk',frequency_mhz=None),
        reset=SimpleNamespace(name='rst_n',active_low=True,synchronous=True),fsm=None,
        ports=[SimpleNamespace(name=n,direction=v['direction'],width={'open_rows':16,'open_valid':4,'candidate_ok':1}[n])
               for n,v in interfaces.items()],metadata=metadata)


class RtlLoweringContextTests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ,{'CAPSTONE_RTL_CACHE_DIR':str(self.root/'cache'),
                                                      'CAPSTONE_TPM_LIMIT':'0'}))
        self.stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('Network forbidden')))

    def test_facts_match_elaboration_and_do_not_rewrite_original_requirements(self):
        spec=design();before=copy.deepcopy(spec.metadata)
        context=build_llm_context(spec);facts=context['rtl_lowering']
        self.assertEqual(facts['resolved_timing_profile']['values'],{'tRCD':17,'tRAS':39,'tRRD':4,'tFAW':30})
        self.assertEqual(facts['source_type_layouts']['bank_rows']['width'],16)
        ports={p['name']:p for p in facts['public_ports']}
        self.assertEqual(set(ports),{'clk','rst_n','open_rows','open_valid','candidate_ok'})
        self.assertEqual(ports['open_valid']['width'],4)
        self.assertFalse(facts['schema_alone_requires_sv_typedef_spelling'])
        self.assertEqual(facts['scope'],'concrete_compiler_elaboration_not_candidate_verification')
        self.assertEqual(spec.metadata,before)
        self.assertEqual(context['metadata']['behavior'],before['behavior'])
        facts['source_type_layouts']['bank_rows']['width']=15
        self.assertEqual(spec.metadata['behavior']['specification_context']['types']['bank_rows']['width'],16)

    def test_full_and_compact_context_preserve_original_data_and_fact_values(self):
        spec=design();bundle=gen._design_context_bundle(spec)
        self.assertEqual(expand_context(json.loads(bundle['ctx_str'])),build_llm_context(spec))
        self.assertEqual(bundle['prompt_text'],gen._compact_review_context(spec))
        self.assertIn('Use specification shared names',bundle['prompt_text'])
        self.assertEqual(bundle['ctx']['rtl_lowering']['public_ports'],bundle['ctx']['module']['ports'])

    def test_all_four_model_stages_receive_identical_scope_policy(self):
        spec=design();rtl='module stage_fixture; endmodule'
        good=json.dumps({'status':'VALID','issues':[],'corrected_rtl':rtl})
        review=json.dumps({'status':'VALID','issues':[]})
        with patch.object(gen,'_call_llm',side_effect=[good,review,review,good]) as call:
            gen._llm1_generate(spec,'',[])
            gen._llm2_review(spec,'',rtl,[])
            gen._llm2_review_compact(spec,rtl)
            gen._llm1_merge(spec,'',rtl,rtl,{'issues':[]},[])
        self.assertEqual(call.call_count,4)
        for request in call.call_args_list:
            system=request.args[1]
            self.assertEqual(system.count(RTL_STAGE_POLICY),1)
            self.assertIn('rtl_lowering',request.args[2])
            self.assertIn('do not require YAML lookup syntax',system)
            self.assertIn('Honor any EXPLICIT RTL',system)

    def test_payload_recovery_keeps_the_same_scoped_system_prompt(self):
        spec=design();token=active_recovery.set(RecoveryBudget())
        try:
            good=json.dumps({'status':'VALID','issues':[],'corrected_rtl':'module stage_fixture; endmodule'})
            with patch.object(gen,'_call_llm',side_effect=['bad JSON',good]) as call:
                result=gen._llm1_generate(spec,'',[])
            self.assertFalse(result['parse_failed'])
            self.assertEqual(call.call_count,2)
            self.assertEqual(call.call_args_list[0].args[1],call.call_args_list[1].args[1])
            self.assertIn(RTL_STAGE_POLICY,call.call_args.args[1])
        finally:active_recovery.reset(token)

    def test_legacy_prompts_and_contexts_remain_unscoped_after_lowered_calls(self):
        original=(gen._SYS_GENERATOR,gen._SYS_REVIEWER,gen._SYS_REVIEWER_COMPACT,gen._SYS_MERGER)
        gen._design_context_bundle(design())
        spec=design(lowered=False);bundle=gen._design_context_bundle(spec)
        self.assertNotIn('rtl_lowering',bundle['ctx'])
        for system in original:self.assertEqual(scoped_system_prompt(system,bundle),system)
        good=json.dumps({'status':'VALID','issues':[],'corrected_rtl':'module stage_fixture; endmodule'})
        with patch.object(gen,'_call_llm',side_effect=[good,'{"status":"VALID","issues":[]}',
                     '{"status":"VALID","issues":[]}',good]) as call:
            gen._llm1_generate(spec,'',[]);gen._llm2_review(spec,'','module stage_fixture; endmodule',[])
            gen._llm2_review_compact(spec,'module stage_fixture; endmodule')
            gen._llm1_merge(spec,'','module stage_fixture; endmodule','',{'issues':[]},[])
        self.assertEqual(tuple(c.args[1] for c in call.call_args_list),original)
        self.assertEqual((gen._SYS_GENERATOR,gen._SYS_REVIEWER,gen._SYS_REVIEWER_COMPACT,gen._SYS_MERGER),original)

    def test_explicit_sv_naming_constraints_are_retained(self):
        spec=design(explicit=True);context=build_llm_context(spec)
        directive='In SystemVerilog RTL declare and use typedef user_bank_rows_t.'
        self.assertIn(directive,context['implementation_constraints'])
        self.assertIn(directive,gen._design_context_bundle(spec)['prompt_text'])
        self.assertIn('these\nstage rules do not waive it',RTL_STAGE_POLICY)

    def test_genuine_behavior_width_and_explicit_style_findings_still_block_acceptance(self):
        clean={'ok':True,'blocking':[],'nonblocking':[],'ignored':[]}
        good={'status':'VALID','issues':[],'corrected_rtl':'module stage_fixture; endmodule'}
        for description in ('tRCD permits at E+16 instead of required E+17.',
                            'open_rows has width 15 instead of elaborated width 16.',
                            'Bank zero maps to the most significant nibble instead of bits 3:0.',
                            'Explicit SystemVerilog typedef user_bank_rows_t is absent.'):
            issue={'type':'SPEC_VIOLATION','description':description,'location':'candidate'}
            with self.subTest(issue=description),patch.object(gen,'MAX_ROUNDS',1), \
                 patch.object(gen,'_llm1_generate',return_value=good), \
                 patch.object(gen,'lint_candidate',return_value=clean), \
                 patch.object(gen,'_llm2_review',return_value={**good,'status':'INVALID','issues':[issue]}), \
                 patch.object(gen,'_write_reviewed_cache') as cache:
                with self.assertRaises(LlmFailure):gen.run_dual_llm_rtlgen(design(explicit=True),'',recovery=True)
                cache.assert_not_called()

    def test_real_ambiguity_is_not_converted_into_valid_or_recovery(self):
        good={'status':'VALID','issues':[],'corrected_rtl':'module stage_fixture; endmodule'}
        review={**good,'status':'AMBIGUOUS','issues':[{'type':'AMBIGUITY',
                'description':'Explicit RTL directive conflicts with the elaborated port ABI.','location':'interface'}]}
        with patch.object(gen,'_llm1_generate',return_value=good), \
             patch.object(gen,'lint_candidate',return_value={'ok':True,'blocking':[],'nonblocking':[],'ignored':[]}), \
             patch.object(gen,'_llm2_review',return_value=review),patch.object(gen,'_write_reviewed_cache') as cache:
            with self.assertRaises(LlmFailure) as error:gen.run_dual_llm_rtlgen(design(),' ',recovery=True)
        self.assertEqual(error.exception.kind,'spec_clarification');cache.assert_not_called()

    def test_new_campaign_wording_scopes_paths_and_names_without_mutating_v1(self):
        self.assertEqual(VERSION,2)
        for case in cases()[:2]:
            request=case['config']['requirements'][0]
            self.assertIn('In shared.timing_profile',request)
            self.assertIn('RTL may use the compiler-resolved numeric constants',request)
            self.assertIn('Preserve the exact public widths',request)


@unittest.skipUnless(shutil.which('verilator'),'Verilator required')
class PackedTypeProofTests(unittest.TestCase):
    def compile_and_run(self,root,*,unpacked=False,wrong_order=False):
        typedef='typedef row_t bank_rows_t [3:0];' if unpacked else 'typedef row_t [3:0] bank_rows_t;'
        index=0 if wrong_order else 1
        source=f'''module packed_aliases(rows,valid,row_one);
 typedef logic [3:0] row_t;
 typedef logic bit_t;
 {typedef}
 typedef bit_t [3:0] bank_valid_t;
 input bank_rows_t rows;
 input bank_valid_t valid;
 output row_t row_one;
 assign row_one=rows[{index}];
 endmodule
 module type_probe;
 logic [15:0] rows;
 logic [3:0] valid,row_one;
 packed_aliases dut(.rows(rows),.valid(valid),.row_one(row_one));
 initial begin
  rows=16'hABCD;valid=4'b1010;#1;
  if ($bits(dut.rows)!=16 || $bits(dut.valid)!=4 || $bits(dut.rows[0])!=4) $fatal(1,"PUBLIC_WIDTH");
  if (row_one!=4'hC || dut.rows[0]!=4'hD || dut.rows[3]!=4'hA) $fatal(1,"BANK_ORDER");
  $display("PACKED_ABI_PASS");$finish;
 end
 endmodule'''
        path=root/'types.sv';path.write_text(source)
        proc=subprocess.run(['verilator','--binary','--timing','-Wno-fatal','--top-module','type_probe',
                             '--Mdir',str(root/'obj'),'-j','2',str(path)],capture_output=True,text=True,timeout=60,cwd=root)
        if proc.returncode:return proc,None
        return proc,subprocess.run([str(root/'obj/Vtype_probe')],capture_output=True,text=True,timeout=10,cwd=root)

    def test_live_failure_typedef_spelling_is_packed_and_preserves_flattened_bank_order(self):
        with tempfile.TemporaryDirectory() as directory:
            compile_result,run=self.compile_and_run(Path(directory))
            self.assertEqual(compile_result.returncode,0,compile_result.stdout+compile_result.stderr)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn('PACKED_ABI_PASS',run.stdout)

    def test_truly_unpacked_public_array_cannot_connect_to_flattened_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            compile_result,run=self.compile_and_run(Path(directory),unpacked=True)
            self.assertNotEqual(compile_result.returncode,0)
            self.assertIsNone(run)

    def test_correct_width_with_wrong_bank_order_fails_behavioral_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            compile_result,run=self.compile_and_run(Path(directory),wrong_order=True)
            self.assertEqual(compile_result.returncode,0,compile_result.stdout+compile_result.stderr)
            self.assertNotEqual(run.returncode,0)
            self.assertIn('BANK_ORDER',run.stdout+run.stderr)


if __name__=='__main__':unittest.main()
