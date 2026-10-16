"""Offline regressions for usage aggregation and bounded RTL review."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests
import dual_llm_rtlGen as generator
import generate_microarch
import run_flow
import design
from token_usage import ModuleUsage, UsageRun, record_usage, estimate_cost
from ir_to_llm_context import build_llm_context
from validator import validate_spec

ROOT = Path(__file__).resolve().parent


def response(incoming=100, outgoing=20, finish='stop', status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({'choices': [{'finish_reason': finish, 'message': {'content': '{}'}}],
                                 'usage': {'prompt_tokens': incoming, 'completion_tokens': outgoing,
                                           'total_tokens': incoming + outgoing,
                                           'completion_tokens_details': {'reasoning_tokens': 9}}}).encode()
    return result


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = io.StringIO()
        output = contextlib.redirect_stdout(self.output)
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        env = patch.dict(os.environ, {'TAMUS_AI_CHAT_API_KEY': 'mock-secret'})
        env.start()
        self.addCleanup(env.stop)

    def test_retry_and_subprocess_calls_aggregate_without_double_counting(self):
        with UsageRun(self.root) as run:
            with ModuleUsage('module_a', self.root):
                with patch.object(generator.requests, 'post', side_effect=[response(finish='length'), response(200, 30)]):
                    generator._call_llm('protected.Claude-Opus-5', 'system', 'user')
            subprocess.run([sys.executable, '-c',
                "from token_usage import ModuleUsage,record_usage; "
                "\nwith ModuleUsage('module_b', '.'):\n record_usage('protected.gpt-5.6-terra','review',{'input_tokens':40,'output_tokens':10})"],
                cwd=ROOT, check=True, stdout=subprocess.PIPE)
            run.status = 'completed'
        result = json.loads((run.directory / 'usage.json').read_text())
        self.assertEqual(result['totals']['input_tokens'], 340)
        self.assertEqual(result['totals']['output_tokens'], 60)
        self.assertEqual(result['totals']['total_tokens'], 400)
        self.assertEqual(result['totals']['calls'], 3)
        self.assertEqual(len(result['modules']), 2)
        self.assertEqual(result['modules'][0]['totals']['calls'], 2)
        self.assertEqual(result['totals']['cost']['estimated_total_usd'], 0.002950)
        events = [json.loads(line) for line in (run.directory / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual([event['request_characters'] for event in events], [10, 10, None])
        self.assertNotIn('mock-secret', (run.directory / 'calls.jsonl').read_text())

    def test_cost_uses_provided_rates_and_prints_module_and_flow(self):
        with UsageRun(self.root) as run:
            for name, model in [('opus_module', 'protected.Claude-Opus-5'),
                                ('terra_module', 'protected.gpt-5.6-terra')]:
                with ModuleUsage(name, self.root):
                    record_usage(model, 'generate', {'prompt_tokens': 1_000_000,
                                                    'completion_tokens': 1_000_000})
            run.status = 'completed'
        result = json.loads((run.directory / 'usage.json').read_text())
        self.assertEqual(result['totals']['cost']['estimated_total_usd'], 44)
        self.assertEqual(result['modules'][0]['totals']['cost']['estimated_total_usd'], 30)
        self.assertEqual(result['modules'][1]['totals']['cost']['estimated_total_usd'], 14)
        self.assertEqual(result['pricing']['source'], 'user_supplied')
        self.assertIn('[COST] Module opus_module completed: estimated USD cost=$30.000000', self.output.getvalue())
        self.assertIn('[COST] Flow completed: estimated USD cost=$44.000000', self.output.getvalue())
        self.assertEqual(json.loads(next((run.directory / 'modules').rglob('usage.json')).read_text())
                         ['totals']['cost']['currency'], 'USD')

    def test_missing_usage_preserves_known_cost_on_failed_module_and_flow(self):
        with UsageRun(self.root) as run:
            with self.assertRaises(RuntimeError):
                with ModuleUsage('engine', self.root):
                    record_usage('protected.Claude-Opus-5', 'generate', {'completion_tokens': 8192})
                    record_usage('protected.Claude-Opus-5', 'retry', outcome='http_error')
                    raise RuntimeError('timeout')
        result = json.loads((run.directory / 'usage.json').read_text())
        cost = result['totals']['cost']
        self.assertIsNone(cost['estimated_total_usd'])
        self.assertEqual(cost['known_output_usd'], 0.204800)
        self.assertEqual(cost['known_total_usd'], 0.204800)
        self.assertFalse(cost['complete'])
        self.assertIn('[COST] Module engine failed: estimated USD cost=unknown', self.output.getvalue())
        self.assertIn('[COST] Flow failed: estimated USD cost=unknown (known subtotal $0.204800)', self.output.getvalue())

    def test_unpriced_model_is_unknown_and_empty_cache_cost_is_zero(self):
        cost = estimate_cost([{'model': 'unpriced', 'input_tokens': 100, 'output_tokens': 20},
                              {'model': 'protected.gpt-5.6-terra', 'input_tokens': 100, 'output_tokens': 20}])
        self.assertIsNone(cost['estimated_total_usd'])
        self.assertEqual(cost['unpriced_models'], ['unpriced'])
        self.assertEqual(cost['known_total_usd'], 0.000440)
        self.assertEqual(estimate_cost([])['estimated_total_usd'], 0)

    def test_failed_module_preserves_known_subtotals_and_zero_call_module(self):
        with UsageRun(self.root) as run:
            with ModuleUsage('cached', self.root):
                pass
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with ModuleUsage('failed', self.root):
                    record_usage('model', 'review', {'completion_tokens': 12})
                    raise RuntimeError('failure')
        result = json.loads((run.directory / 'usage.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['totals']['input_tokens'])
        self.assertEqual(result['totals']['output_tokens'], 12)
        self.assertIsNone(result['totals']['total_tokens'])
        self.assertEqual(result['modules'][0]['totals']['total_tokens'], 0)
        self.assertEqual(result['modules'][1]['status'], 'failed')

    def test_specification_failure_counts_response_and_network_attempt(self):
        with UsageRun(self.root) as run:
            with patch.object(generate_microarch.requests, 'post', return_value=response(finish='length')):
                with self.assertRaises(ValueError):
                    generate_microarch.call_llm('prompt', 'spec_model')
            with patch.object(generate_microarch.requests, 'post', side_effect=requests.Timeout()):
                with self.assertRaises(ValueError):
                    generate_microarch.call_llm('prompt', 'spec_model')
        result = json.loads((run.directory / 'usage.json').read_text())['totals']
        self.assertEqual(result['calls'], 2)
        self.assertEqual(result['known_total_tokens'], 120)
        self.assertIsNone(result['total_tokens'])

    def test_cli_flow_failure_still_prints_and_saves_totals(self):
        with patch.object(run_flow, 'SCRIPT_DIR', self.root), patch.object(run_flow, 'parse_args'), \
             patch.object(run_flow, '_run_flow', return_value=1):
            self.assertEqual(run_flow.main(), 1)
        reports = list(self.root.rglob('usage.json'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())['status'], 'failed')

    def test_design_summary_precedes_finished_with_blank_line(self):
        def generate(args):
            print('last generation diagnostic')
            record_usage('protected.gpt-5.6-terra', 'review', {'input_tokens': 100, 'output_tokens': 20})
        args = SimpleNamespace(yaml_path='small.yaml', output_root=str(self.root))
        with patch.object(design, 'parse_args', return_value=args), patch.object(design, '_run_design', side_effect=generate):
            design.main()
        text = self.output.getvalue()
        self.assertIn('last generation diagnostic\n\n[TOKENS] Module small completed:', text)
        self.assertIn('estimated USD cost=$0.000440\n--- FINISHED --- small.sv', text)
        self.assertEqual(text.count('--- FINISHED ---'), 1)
        self.assertLess(text.index('[TOKENS] Module'), text.index('[COST] Module'))
        self.assertLess(text.index('[COST] Module'), text.index('--- FINISHED ---'))
        self.assertLess(text.index('--- FINISHED ---'), text.index('[COST] Flow'))

    def test_failed_design_reports_cost_without_success_banner(self):
        args = SimpleNamespace(yaml_path='small.yaml', output_root=str(self.root))
        with patch.object(design, 'parse_args', return_value=args), \
             patch.object(design, '_run_design', side_effect=RuntimeError('generation failed')):
            with self.assertRaisesRegex(RuntimeError, 'generation failed'):
                design.main()
        self.assertIn('[COST] Module small failed:', self.output.getvalue())
        self.assertNotIn('--- FINISHED ---', self.output.getvalue())

    def test_ambiguity_stops_first_round_and_saves_full_report(self):
        rtl = 'module scheduler; endmodule'
        issue = {'type': 'AMBIGUITY', 'description': 'Conflicting interface declaration'}
        payload = {'corrected_rtl': rtl, 'status': 'AMBIGUOUS', 'issues': [issue]}
        with UsageRun(self.root) as run:
            with self.assertRaisesRegex(RuntimeError, 'specification clarification required'):
                with ModuleUsage('scheduler', self.root):
                    with patch.object(generator, '_llm1_generate', return_value=payload) as generate, \
                         patch.object(generator, '_llm2_review', return_value=payload) as review, \
                         patch.object(generator, '_llm1_merge') as merge:
                        generator.run_dual_llm_rtlgen(SimpleNamespace(design_name='scheduler'), '', False)
            generate.assert_called_once()
            review.assert_called_once()
            merge.assert_not_called()
        trace = json.loads(next(run.directory.rglob('rtl_review.json')).read_text())
        self.assertEqual(trace['outcome'], 'needs_spec_clarification')
        self.assertEqual(trace['rounds'][0]['llm2_report'], [issue])
        self.assertEqual(trace['rounds'][0]['llm1_code'], rtl)

    def test_spec_violation_can_repair_then_receive_clean_review(self):
        rtl = 'module scheduler; endmodule'
        bad = {'corrected_rtl': rtl, 'status': 'INVALID',
               'issues': [{'type': 'SPEC_VIOLATION', 'description': 'Incorrect dispatch'}]}
        good = {'corrected_rtl': rtl, 'status': 'VALID', 'issues': []}
        with UsageRun(self.root) as run:
            with ModuleUsage('scheduler', self.root):
                with patch.object(generator, '_llm1_generate', return_value=bad) as generate, \
                     patch.object(generator, '_llm2_review', side_effect=[bad, good]) as review, \
                     patch.object(generator, '_llm1_merge', return_value=good) as merge, \
                     patch.object(generator, '_write_reviewed_cache', return_value=rtl):
                    self.assertEqual(generator.run_dual_llm_rtlgen(
                        SimpleNamespace(design_name='scheduler'), '', False), rtl)
                generate.assert_called_once()
                merge.assert_called_once()
                self.assertEqual(review.call_count, 2)
            run.status = 'completed'
        trace = json.loads(next(run.directory.rglob('rtl_review.json')).read_text())
        self.assertEqual(len(trace['rounds']), 2)
        self.assertEqual(trace['rounds'][1]['llm2_status'], 'VALID')

    def test_cached_module_has_zero_new_tokens_and_no_provider_calls(self):
        with UsageRun(self.root) as run:
            with ModuleUsage('scheduler', self.root):
                with patch.object(generator, '_load_reviewed_cache', return_value='reviewed rtl'), \
                     patch.object(generator.requests, 'post') as post:
                    self.assertEqual(generator.run_dual_llm_rtlgen(
                        SimpleNamespace(design_name='scheduler'), '', False, True), 'reviewed rtl')
                post.assert_not_called()
            run.status = 'completed'
        result = json.loads((run.directory / 'usage.json').read_text())
        self.assertEqual(result['totals']['total_tokens'], 0)
        self.assertEqual(result['modules'][0]['totals']['calls'], 0)

    def test_final_round_does_not_pay_for_unreviewable_merge(self):
        payload = {'corrected_rtl': 'module scheduler; endmodule', 'status': 'INVALID',
                   'issues': [{'type': 'SPEC_VIOLATION', 'description': 'Incorrect dispatch'}]}
        with patch.object(generator, 'MAX_ROUNDS', 1), \
             patch.object(generator, '_llm1_generate', return_value=payload), \
             patch.object(generator, '_llm2_review', return_value=payload), \
             patch.object(generator, '_llm1_merge') as merge, \
             patch.object(generator, '_reviewer_failed_fallback', side_effect=RuntimeError('no approved RTL')):
            with self.assertRaisesRegex(RuntimeError, 'no approved RTL'):
                generator.run_dual_llm_rtlgen(SimpleNamespace(design_name='scheduler'), '', False)
        merge.assert_not_called()

    def test_context_has_complete_clock_reset_interface_and_allows_helpers(self):
        from specification_lowering import compile_package, emit_review
        manifest = compile_package(ROOT / 'microarch/specification/ddr4_fifo/package.yaml')
        for path in emit_review(manifest, self.root / 'architecture.yaml'):
            design = validate_spec(str(path))
            context = build_llm_context(design)
            ports = context['module']['ports']
            for name in ('clk', 'rst_n'):
                self.assertEqual([p for p in ports if p['name'] == name],
                                 [{'name': name, 'direction': 'input', 'width': 1}])
            text = '\n'.join(context['invariants'])
            self.assertIn('internal helper signals', text)
            self.assertNotIn('no additional states or signals', text)
            self.assertIn('owned behavior', text)


if __name__ == '__main__':
    unittest.main()
