"""Bounded recovery fault injection. Every provider response is local/mocked."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import requests

import design
import run_flow
import configure_microarch_from_text as intake
import dual_llm_rtlGen as gen
from llm_diagnostics import LlmFailure
from rtl_recovery import RecoveryBudget, active_recovery, retry_delay
from token_usage import ModuleUsage, UsageRun
from test_llm_diagnostics import response, malformed_rtl

RTL = 'module sample(input logic clk, output logic q); always_ff @(posedge clk) q <= 1\'b0; endmodule'
GOOD = json.dumps({'status':'VALID', 'issues':[], 'corrected_rtl':RTL})
REVIEW = json.dumps({'status':'VALID', 'issues':[]})
AMBIG = {'status':'AMBIGUOUS', 'issues':[{'type':'AMBIGUITY','description':'Missing required timing','location':'timing'}]}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {'CAPSTONE_TPM_LIMIT':'0', 'TAMUS_AI_CHAT_API_KEY':'test-secret',
            'DUAL_LLM_MAX_TOKENS':'4096','DUAL_LLM_MAX_RETRY_TOKENS':'16384'}))
        self.stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('Network forbidden')))
        self.sleep = self.stack.enter_context(patch('rtl_recovery.time.sleep'))
        self.stack.enter_context(patch.object(gen,'_design_context_bundle',return_value={'prompt_text':'authoritative fixture','invariants':[]}))
        self.stack.enter_context(patch.object(gen,'_write_reviewed_cache',side_effect=lambda name,rtl:rtl))
        self.spec = SimpleNamespace(design_name='sample')
        self.clean = {'ok':True,'blocking':[],'nonblocking':[],'ignored':[]}

    def budget(self):
        budget = RecoveryBudget()
        token = active_recovery.set(budget)
        self.addCleanup(active_recovery.reset,token)
        return budget

    def call(self):
        return gen._call_llm('fixture','system','user','LLM1-Generate')

    def generate(self):
        return gen._llm1_generate(self.spec,'',[])

    def loop(self, replies, **kwargs):
        with patch('requests.post',side_effect=replies) as post, \
             patch.object(gen,'lint_candidate',return_value=self.clean), \
             patch.object(gen,'lint_rtl_single_file',return_value=(True,[])):
            value = gen.run_dual_llm_rtlgen(self.spec,'',recovery=True,**kwargs)
        return value,post

    def test_budget_caps_and_retry_after(self):
        budget = RecoveryBudget()
        self.assertTrue(budget.claim('payload','gen',{}))
        self.assertFalse(budget.claim('payload','merge',{}))
        self.assertTrue(budget.claim('transport','review',{},delay=2))
        self.assertFalse(budget.claim('truncation','merge',{}))
        self.assertEqual(budget.report()['extra_attempts_reserved'],2)
        self.assertEqual(retry_delay('Thu, 01 Jan 1970 00:00:15 GMT',now=5),10)
        self.assertEqual(retry_delay('3'),3)
        self.assertEqual(retry_delay('invalid'),2)
        for delay in ('31','nan','inf','-1'):
            self.assertFalse(RecoveryBudget().claim('transport','gen',{},delay=retry_delay(delay)))

    def test_malformed_schema_missing_and_empty_recover_once(self):
        for raw in (malformed_rtl(), '[]', '{"status":"VALID","issues":[]}', '',
                    '{"status":"VALID","issues":"bad","corrected_rtl":"rtl"}'):
            with self.subTest(raw=raw[:25]):
                b=self.budget()
                with patch('requests.post',side_effect=[response(raw),response(GOOD)]) as post:
                    self.assertEqual(self.generate()['corrected_rtl'],RTL)
                self.assertEqual(post.call_count,2)
                prompts=[c.kwargs['json']['messages'] for c in post.call_args_list]
                self.assertEqual(prompts[0][0],prompts[1][0])
                self.assertIn('authoritative fixture',prompts[1][1]['content'])
                if raw in ('','{"status":"VALID","issues":[]}'):
                    self.assertEqual(prompts[0],prompts[1])
                else:
                    self.assertIn('VALIDATION ERROR',prompts[1][1]['content'])
                self.assertEqual(b.used['payload'],1)

    def test_no_recursive_payload_repair(self):
        b=self.budget()
        with patch('requests.post',return_value=response('bad')) as post:
            with self.assertRaises(LlmFailure):self.generate()
        self.assertEqual(post.call_count,2)
        self.assertEqual(b.used['payload'],1)

    def test_terminal_generation_failures_never_retry(self):
        cases=[(response(status=x),'http_error') for x in (400,401,403,404,422)]
        cases += [(response(reason='content_filter'),'filtered_response'),
                  (response(data={'choices':[{'message':{'refusal':'no'},'finish_reason':'stop'}]}),'refused_response'),
                  (response(data={'choices':[]}), 'provider_response'),
                  (response(content=[]),'non_text_response')]
        for reply,kind in cases:
            with self.subTest(kind=kind):
                self.budget()
                with patch('requests.post',return_value=reply) as post:
                    with self.assertRaises(LlmFailure) as exc:self.generate()
                self.assertEqual(exc.exception.kind,kind)
                self.assertEqual(post.call_count,1)
        for code in ('',RTL):
            self.budget()
            with patch('requests.post',return_value=response(json.dumps({**AMBIG,'corrected_rtl':code}))) as post:
                with self.assertRaises(LlmFailure) as exc:self.generate()
            self.assertEqual(exc.exception.kind,'spec_clarification')
            post.assert_called_once()

    def test_transient_statuses_and_exceptions_retry_same_request_once(self):
        for failure in ([response(status=x) for x in (408,429,500,502,503,504,524)] +
                        [requests.exceptions.Timeout('timeout'),requests.exceptions.ConnectionError('connection')]):
            with self.subTest(failure=str(failure)):
                b=self.budget()
                with patch('requests.post',side_effect=[failure,response(GOOD)]) as post:
                    self.assertEqual(self.call(),GOOD)
                self.assertEqual(post.call_args_list[0].kwargs['json'],post.call_args_list[1].kwargs['json'])
                self.assertEqual(b.used['transport'],1)
        self.budget()
        with patch('requests.post',return_value=response(status=524)) as post:
            with self.assertRaises(LlmFailure):self.call()
        self.assertEqual(post.call_count,2)
        self.budget()
        with patch('requests.post',side_effect=requests.exceptions.SSLError('certificate')) as post:
            with self.assertRaises(LlmFailure):self.call()
        post.assert_called_once()

    def test_retry_after_over_budget_stops_without_sleep(self):
        self.budget(); reply=response(status=429);reply.headers['Retry-After']='60'
        with patch('requests.post',return_value=reply) as post:
            with self.assertRaises(LlmFailure):self.call()
        post.assert_called_once();self.sleep.assert_not_called()

    def test_nested_payload_transport_and_truncation_share_global_limit(self):
        for nested in (response(status=524),response('partial',reason='length')):
            b=self.budget()
            with patch('requests.post',side_effect=[response('bad'),nested,response(GOOD)]) as post:
                self.assertEqual(self.generate()['corrected_rtl'],RTL)
            self.assertEqual(post.call_count,3)
            self.assertEqual(sum(b.used.values()),2)
        self.budget()
        with patch('requests.post',side_effect=[response('bad'),response(status=524),response('partial',reason='length')]) as post:
            with self.assertRaises(LlmFailure):self.generate()
        self.assertEqual(post.call_count,3)

    def test_truncation_is_one_retry_even_below_ceiling(self):
        b=self.budget()
        with patch('requests.post',return_value=response('partial',reason='length')) as post:
            with self.assertRaises(LlmFailure):self.call()
        self.assertEqual(post.call_count,2)
        self.assertEqual(b.used['truncation'],1)
        self.budget()
        with patch.dict(os.environ,{'DUAL_LLM_MAX_RETRY_TOKENS':'4096'}),patch('requests.post',return_value=response('partial',reason='length')) as post:
            with self.assertRaises(LlmFailure):self.call()
        post.assert_called_once()

    def test_successful_recovery_goes_through_real_lint_and_reviewer(self):
        with patch('requests.post',side_effect=[response('bad'),response(GOOD),response(REVIEW)]) as post, \
             patch.object(gen,'lint_candidate',wraps=gen.lint_candidate) as lint, \
             patch.object(gen,'_llm2_review',wraps=gen._llm2_review) as review:
            value=gen.run_dual_llm_rtlgen(self.spec,'',recovery=True)
        self.assertIn('endmodule',value)
        self.assertEqual(post.call_count,3)
        self.assertTrue(lint.called);review.assert_called_once()
        self.assertIn(RTL,post.call_args_list[-1].kwargs['json']['messages'][1]['content'])

    def test_reviewer_compact_uses_payload_allowance_and_never_cache_on_failure(self):
        for broken in ('bad',''):
            value,post=self.loop([response(GOOD),response(broken),response(REVIEW)])
            self.assertEqual(post.call_count,3)
            self.assertIn('endmodule',value)
        with patch.object(gen,'_reviewer_failed_fallback') as fallback:
            for replies,expected in (([response(GOOD),response('bad'),response('bad')],3),
                                     ([response('bad'),response(GOOD),response('bad')],3),
                                     ([response(GOOD),response(status=401)],2)):
                with patch('requests.post',side_effect=replies) as post,patch.object(gen,'lint_candidate',return_value=self.clean):
                    with self.assertRaises(LlmFailure):gen.run_dual_llm_rtlgen(self.spec,'',recovery=True)
                self.assertEqual(post.call_count,expected)
            fallback.assert_not_called()

    def test_payload_allowance_shared_with_merge_and_lint_repair(self):
        b=self.budget()
        with patch('requests.post',side_effect=[response('bad'),response(GOOD),response('bad')]) as post:
            self.generate()
            with self.assertRaises(LlmFailure):
                gen._llm1_merge(self.spec,'',RTL,RTL,{},[])
        self.assertEqual(post.call_count,3);self.assertEqual(b.used['payload'],1)
        b=self.budget()
        with patch('requests.post',side_effect=[response('bad'),response(GOOD),response('bad')]) as post:
            self.generate()
            with self.assertRaises(LlmFailure):
                gen._llm1_generate(self.spec,'',[],lint_issues=['syntax'],previous_rtl=RTL)
        self.assertEqual(post.call_count,3)

    def test_disabled_mode_and_budget_reset(self):
        with patch('requests.post',return_value=response('bad')) as post:
            with self.assertRaises(LlmFailure):gen.run_dual_llm_rtlgen(self.spec,'')
        post.assert_called_once()
        for _ in range(2):
            self.loop([response('bad'),response(GOOD),response(REVIEW)])
            self.assertIsNone(active_recovery.get())

    def test_diagnostics_capture_attempts_budget_and_usage(self):
        with UsageRun(self.root/'usage') as usage, ModuleUsage('sample',self.root):
            self.loop([response('bad test-secret'),response(status=524),response(GOOD),response(REVIEW)])
            report=json.loads(next(usage.directory.rglob('rtl_review.json')).read_text())
            self.assertEqual(report['recovery']['extra_attempts_reserved'],2)
            artifacts=list(usage.directory.rglob('attempt_*.json'))
            self.assertEqual(len(artifacts),4)
            text=''.join(p.read_text() for p in artifacts)
            self.assertNotIn('test-secret',text)
            self.assertIn('[redacted]',text)
            self.assertEqual(report['recovery']['used'],{'payload':1,'transport':1,'truncation':0})

    def test_recovered_merge_is_re_reviewed_and_later_round_cannot_get_new_payload_budget(self):
        violation = {'status':'INVALID','issues':[{'type':'SPEC_VIOLATION',
                     'description':'Fix reset','location':'sample'}]}
        replies=[response(GOOD),response(json.dumps(violation)),response('bad'),response(GOOD),response(REVIEW)]
        value,post=self.loop(replies)
        self.assertEqual(post.call_count,5)
        self.assertIn('endmodule',value)
        with patch('requests.post',side_effect=replies[:-1]+[response('bad')]) as post, \
             patch.object(gen,'lint_candidate',return_value=self.clean), \
             patch.object(gen,'lint_rtl_single_file',return_value=(True,[])):
            with self.assertRaises(LlmFailure):gen.run_dual_llm_rtlgen(self.spec,'',recovery=True)
        self.assertEqual(post.call_count,5)

    def test_resume_hit_uses_no_recovery_or_http_and_failure_reports_are_preserved(self):
        with UsageRun(self.root/'usage') as usage, ModuleUsage('sample',self.root), \
             patch.object(gen,'_load_reviewed_cache',return_value=RTL), \
             patch.object(gen,'lint_rtl_single_file',return_value=(True,[])), \
             patch('requests.post') as post:
            self.assertIn('endmodule',gen.run_dual_llm_rtlgen(self.spec,'',resume=True,recovery=True))
            post.assert_not_called()
            report=json.loads(next(usage.directory.rglob('rtl_review.json')).read_text())
            self.assertEqual(report['recovery']['extra_attempts_reserved'],0)
        with UsageRun(self.root/'failure') as usage, ModuleUsage('sample',self.root):
            with patch('requests.post',return_value=response('bad')) as post:
                with self.assertRaises(LlmFailure):gen.run_dual_llm_rtlgen(self.spec,'',recovery=True)
            report=json.loads(next(usage.directory.rglob('rtl_review.json')).read_text())
            self.assertEqual(report['outcome'],'failed')
            self.assertEqual(report['failure']['kind'],'malformed_json')
            self.assertEqual(report['recovery']['extra_attempts_reserved'],1)
            self.assertEqual(len(list(usage.directory.rglob('attempt_*.json'))),2)

    def test_actual_http_accounting_keeps_unknown_usage_unknown(self):
        with UsageRun(self.root/'usage') as usage:
            with ModuleUsage('sample',self.root):
                self.loop([response('bad'),response(status=524,data={'error':'timeout'}),response(GOOD),response(REVIEW)])
        totals=json.loads((usage.directory/'usage.json').read_text())['totals']
        self.assertEqual(totals['calls'],4)
        self.assertIsNone(totals['input_tokens'])
        self.assertEqual(totals['known_input_tokens'],51)
        self.assertEqual(totals['calls_missing_input_tokens'],1)

    def test_configuration_failure_is_terminal_and_lint_is_required(self):
        self.budget()
        with patch.dict(os.environ,{'TAMUS_AI_CHAT_API_KEY':''}),patch('requests.post') as post:
            with self.assertRaises(LlmFailure) as exc:self.generate()
            self.assertEqual(exc.exception.kind,'configuration')
            post.assert_not_called()
        with self.assertRaises(ValueError):gen.run_dual_llm_rtlgen(self.spec,'',lint_enabled=False,recovery=True)

    def test_design_routing_and_intake_forwarding(self):
        from dataclasses import dataclass
        @dataclass
        class Spec:
            design_name: str = 'sample'
            design_type: str = 'datapath'
        source=self.root/'spec.yaml';source.write_text('fixture: true')
        for kind,enabled in (('datapath',True),('datapath',False),('fsm',True)):
            spec=Spec(design_type=kind)
            args=SimpleNamespace(yaml_path=str(source),output_root=str(self.root),no_lint=False,
                                 cache=False,resume=False,rtl_generator_model=None,rtl_recovery=enabled)
            dual=SimpleNamespace(run_dual_llm_rtlgen=unittest.mock.Mock(return_value=RTL))
            modules={'input_validator':SimpleNamespace(validate_spec=lambda path:spec),'rtlgen':object(),
                     'dual_rtlgen':dual,'fsm_generator':SimpleNamespace(generate_fsm_rtl=lambda spec:RTL)}
            with patch.dict(os.environ),patch.object(design,'load_module_from_path',side_effect=lambda name,path:modules[name]):
                self.assertTrue(design._run_design(args)['passed'])
            if kind=='fsm':dual.run_dual_llm_rtlgen.assert_not_called()
            elif enabled:self.assertTrue(dual.run_dual_llm_rtlgen.call_args.kwargs['recovery'])
            else:self.assertNotIn('recovery',dual.run_dual_llm_rtlgen.call_args.kwargs)
        args=intake.parse_args(['--rtl-recovery'])
        self.assertIn('--rtl-recovery',intake.flow_command(args,self.root))

    def test_architecture_defaults_and_explicit_opt_out(self):
        for flags, expected in (([],True),(['--resume'],True),(['--no-rtl-recovery'],False),
                                (['--rtl-recovery'],True),(['--microarch-only'],False),(['--no-lint'],False)):
            with self.subTest(flags=flags),patch.object(sys,'argv',['flow','architecture.yaml']+flags):
                args=run_flow.parse_args()
                self.assertEqual(run_flow.architecture_rtl_recovery(args),expected)
                command=run_flow.design_command(Path('spec'),no_lint=args.no_lint,cache=False,
                                               rtl_recovery=run_flow.architecture_rtl_recovery(args))
                self.assertIn('--rtl-recovery' if expected else '--no-rtl-recovery',command)
        for flags in (['--microarch-only'],['--no-lint']):
            with patch.object(sys,'argv',['flow','architecture.yaml','--rtl-recovery']+flags):args=run_flow.parse_args()
            with self.assertRaises(ValueError):run_flow.architecture_rtl_recovery(args)

    def test_intake_preserves_default_and_explicit_disable(self):
        args=intake.parse_args([])
        self.assertIsNone(args.rtl_recovery)
        for flags in (['--prepare-only'],['--microarch-only'],['--no-lint']):
            self.assertIsNone(intake.parse_args(flags).rtl_recovery)
        args=intake.parse_args(['--no-rtl-recovery'])
        self.assertFalse(args.rtl_recovery)
        self.assertIn('--no-rtl-recovery',intake.flow_command(args,self.root))
        for parser,argv in ((run_flow.parse_args,['flow','spec']), (design.parse_args,['design','spec'])):
            with patch.object(sys,'argv',argv+['--rtl-recovery','--no-rtl-recovery']),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                parser()
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            intake.parse_args(['--rtl-recovery','--no-rtl-recovery'])
        with patch.object(sys,'argv',['design','spec','--no-rtl-recovery']):
            self.assertFalse(design.parse_args().rtl_recovery)

    def test_cli_forwarding_and_invalid_legacy_modes(self):
        self.assertNotIn('--rtl-recovery',run_flow.design_command(Path('spec'),no_lint=False,cache=False))
        self.assertIn('--rtl-recovery',run_flow.design_command(Path('spec'),no_lint=False,cache=False,rtl_recovery=True))
        with patch.object(sys,'argv',['flow','legacy.yaml','--rtl-recovery']):args=run_flow.parse_args()
        with patch.object(run_flow,'maybe_expand_user_config') as expand:
            self.assertEqual(run_flow._run_flow(args),1)
        expand.assert_not_called()
        for argv in (['design','spec','--rtl-recovery'],['design','spec','--rtl-recovery','--output-root','/tmp/build','--no-lint']):
            with patch.object(sys,'argv',argv),redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):design.parse_args()
        for flags in (['--prepare-only'],['--microarch-only'],['--no-lint']):
            with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):intake.parse_args(['--rtl-recovery']+flags)
        with patch.object(sys,'argv',['design','spec','--rtl-recovery','--output-root','/tmp/build']):
            self.assertTrue(design.parse_args().rtl_recovery)


if __name__=='__main__':unittest.main()
