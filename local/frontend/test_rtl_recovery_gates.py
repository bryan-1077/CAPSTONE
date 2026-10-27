"""Recovery acceptance gates using mocked HTTP and real isolated RTL/cache files."""
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import design
import dual_llm_rtlGen as gen
import run_flow
from generate_specification import propose_specification
from llm_diagnostics import LlmFailure
from rtl_recovery import active_recovery
from specification_lowering import compile_package, create_approval, emit_review
from test_generate_specification import register_fixture
from test_llm_diagnostics import response
from token_usage import ModuleUsage, UsageRun
from verification_handoff import validate_handoff


RTL_A = 'module sample(output logic q); assign q = 1\'b0; endmodule'
RTL_B = 'module sample(output logic q); assign q = 1\'b1; endmodule'
BAD_RTL = 'module sample; assign = ; endmodule'
VIOLATION = {'type': 'SPEC_VIOLATION', 'description': 'Output must be zero.', 'location': 'sample.q'}
AMBIGUITY = {'type': 'AMBIGUITY', 'description': 'Required timing is unspecified.', 'location': 'sample.q'}


def payload(code=None, *, status='VALID', issues=None):
    value = {'status': status, 'issues': [] if issues is None else issues}
    if code is not None:
        value['corrected_rtl'] = code
    return json.dumps(value)


@unittest.skipUnless(shutil.which('verilator'), 'Verilator required for acceptance-gate checks')
class RecoveryGateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {
            'CAPSTONE_TPM_LIMIT': '0', 'TAMUS_AI_CHAT_API_KEY': 'offline-test-secret',
            'CAPSTONE_RTL_CACHE_DIR': str(self.root / 'cache'),
            'DUAL_LLM_MAX_TOKENS': '4096', 'DUAL_LLM_MAX_RETRY_TOKENS': '16384',
        }))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden')))
        self.stack.enter_context(patch('rtl_recovery.time.sleep'))
        self.real_context = gen._design_context_bundle
        self.stack.enter_context(patch.object(gen, '_design_context_bundle',
            return_value={'prompt_text': 'Authoritative fixture: output q must be zero.', 'invariants': []}))
        self.spec = SimpleNamespace(design_name='sample')

    def run_module(self, replies, **options):
        with UsageRun(self.root / 'usage') as usage, ModuleUsage('sample', self.root), \
             patch('requests.post', side_effect=replies) as post:
            try:
                value = gen.run_dual_llm_rtlgen(self.spec, '', recovery=True, **options)
            finally:
                self.last_usage = usage.directory
                self.last_post = post
        return value

    def trace(self):
        return json.loads(next(self.last_usage.rglob('rtl_review.json')).read_text())

    def cache(self):
        return Path(gen._cache_path('sample'))

    def test_corrected_behavior_must_be_reviewed_and_rejected_before_merge_approval(self):
        # A format correction may change RTL behavior: generator VALID is not approval.
        with patch.object(gen, '_write_reviewed_cache', wraps=gen._write_reviewed_cache) as cache:
            value = self.run_module([
                response('malformed'), response(payload(RTL_B)),
                response(payload(status='INVALID', issues=[VIOLATION])),
                response(payload(RTL_A)), response(payload()),
            ])
        self.assertIn(RTL_A, value)
        self.assertNotIn(RTL_B, value)
        cache.assert_called_once_with('sample', RTL_A)
        self.assertEqual(self.cache().read_text(), value)
        self.assertEqual(self.last_post.call_count, 5)
        rounds = self.trace()['rounds']
        self.assertEqual([r['llm2_status'] for r in rounds], ['INVALID', 'VALID'])
        self.assertIn(RTL_B, self.last_post.call_args_list[2].kwargs['json']['messages'][1]['content'])
        self.assertIn(RTL_A, self.last_post.call_args_list[4].kwargs['json']['messages'][1]['content'])
        self.assertTrue(all(r['lint_result']['tool'] == 'verilator' for r in rounds))

    def test_syntax_bad_recovery_requires_a_separate_lint_repair(self):
        value = self.run_module([response('malformed'), response(payload(BAD_RTL)),
                                 response(payload(RTL_A)), response(payload())])
        self.assertIn(RTL_A, value)
        self.assertEqual(self.last_post.call_count, 4)
        repair_prompt = self.last_post.call_args_list[2].kwargs['json']['messages'][1]['content']
        self.assertIn(BAD_RTL, repair_prompt)
        self.assertIn('LINTER REPORT', repair_prompt)
        self.assertEqual(self.trace()['recovery']['used']['payload'], 1)

    def test_persistent_syntax_failure_cannot_be_overridden_by_clean_reviewer(self):
        with patch.object(gen, 'MAX_ROUNDS', 1), patch.object(gen, '_write_reviewed_cache', wraps=gen._write_reviewed_cache) as cache:
            with self.assertRaises(LlmFailure):
                self.run_module([response('malformed'), response(payload(BAD_RTL)),
                                 response(payload(BAD_RTL)), response(payload(BAD_RTL)), response(payload())])
        cache.assert_not_called()
        self.assertFalse(self.cache().exists())
        report = self.trace()
        self.assertFalse(report['rounds'][0]['llm1_lint_ok'])
        self.assertEqual(report['rounds'][0]['llm2_status'], 'INVALID')
        self.assertTrue(any(i['location'] == 'local_verilator_lint' for i in report['rounds'][0]['llm2_report']))
        self.assertEqual(report['outcome'], 'failed')

    def test_recovered_merge_cannot_be_cached_before_its_own_review(self):
        for last in (response(payload(status='AMBIGUOUS', issues=[AMBIGUITY])), response(status=401)):
            with self.subTest(last=last.status_code), patch.object(gen, '_write_reviewed_cache', wraps=gen._write_reviewed_cache) as cache:
                with self.assertRaises(LlmFailure):
                    self.run_module([response(payload(RTL_B)), response(payload(status='INVALID', issues=[VIOLATION])),
                                     response('malformed merge'), response(payload(RTL_A)), last])
                cache.assert_not_called()
                self.assertFalse(self.cache().exists())
                self.assertEqual(self.last_post.call_count, 5)

    def test_bad_recovered_merge_falls_back_only_to_candidate_that_is_reviewed_again(self):
        value = self.run_module([response(payload(RTL_B)),
            response(payload(RTL_A, status='INVALID', issues=[VIOLATION])),
            response('malformed merge'), response(payload(BAD_RTL)), response(payload())])
        self.assertIn(RTL_A, value)
        self.assertNotIn(BAD_RTL, value)
        self.assertIn(RTL_A, self.last_post.call_args_list[-1].kwargs['json']['messages'][1]['content'])
        self.assertEqual(self.trace()['rounds'][1]['llm1_status'], 'FALLBACK_REVIEWER_RTL')

    def test_compact_recovery_preserves_blocking_findings_and_requires_merge(self):
        value = self.run_module([response(payload(RTL_B)), response('malformed review'),
            response(payload(status='INVALID', issues=[VIOLATION])), response(payload(RTL_A)), response(payload())])
        self.assertIn(RTL_A, value)
        report = self.trace()
        self.assertEqual(report['rounds'][0]['llm2_report'], [VIOLATION])
        self.assertEqual(report['recovery']['used']['payload'], 1)
        self.assertEqual(self.last_post.call_count, 5)

    def test_terminal_reviewer_failures_preserve_existing_cache_without_accepting_new_candidate(self):
        original = gen._write_reviewed_cache('sample', RTL_A)
        for last, kind in ((response(status=401), 'http_error'),
                           (response(reason='content_filter'), 'filtered_response'),
                           (response(data={'choices': []}), 'provider_response')):
            with self.subTest(kind=kind):
                with self.assertRaises(LlmFailure) as caught:
                    self.run_module([response('malformed'), response(payload(RTL_B)), last])
                self.assertEqual(caught.exception.kind, kind)
                self.assertEqual(self.cache().read_text(), original)
                self.assertEqual(self.trace()['outcome'], 'failed')
                self.assertEqual(self.last_post.call_count, 3)

    def test_lint_repair_payload_failure_cannot_reuse_earlier_rtl_or_write_cache(self):
        with self.assertRaises(LlmFailure):
            self.run_module([response(payload(BAD_RTL)), response('bad repair JSON'), response('bad correction JSON')])
        self.assertFalse(self.cache().exists())
        self.assertEqual(self.last_post.call_count, 3)
        self.assertEqual(self.trace()['failure']['kind'], 'malformed_json')

    def test_spec_clarification_after_recovery_stops_before_lint_review_or_cache(self):
        for raw in (payload(status='AMBIGUOUS', issues=[AMBIGUITY]),
                    payload(RTL_A, status='AMBIGUOUS', issues=[AMBIGUITY])):
            with self.subTest(raw=raw), patch.object(gen, 'lint_candidate', wraps=gen.lint_candidate) as lint:
                with self.assertRaises(LlmFailure) as caught:
                    self.run_module([response('malformed'), response(raw)])
                self.assertEqual(caught.exception.kind, 'spec_clarification')
                lint.assert_not_called()
                self.assertFalse(self.cache().exists())
                self.assertEqual(self.last_post.call_count, 2)

    def design_args(self, source, build, *, recovery=True):
        return SimpleNamespace(yaml_path=str(source), output_root=str(build), no_lint=False,
                               cache=False, resume=False, rtl_generator_model=None, rtl_recovery=recovery)

    def test_design_failure_preserves_prior_output_and_isolates_specification_caches(self):
        @dataclass
        class Spec:
            design_name: str = 'sample'
            design_type: str = 'datapath'
        source = self.root / 'spec.yaml'
        source.write_text('fixture revision: 2\n')
        build = self.root / 'build'
        old_output = build / 'rtl_output/sample.sv'
        old_output.parent.mkdir(parents=True)
        old_output.write_text(RTL_A)
        old_cache = build / '.llm_cache/old-revision/sample.sv'
        old_cache.parent.mkdir(parents=True)
        old_cache.write_text(RTL_A)
        modules = {'input_validator': SimpleNamespace(validate_spec=lambda path: Spec()),
                   'rtlgen': object(), 'dual_rtlgen': gen}
        with patch.dict(os.environ), ModuleUsage('sample', build), \
             patch.object(design, 'load_module_from_path', side_effect=lambda name, path: modules[name]), \
             patch('requests.post', side_effect=[response('bad'), response('bad')]):
            with self.assertRaises(LlmFailure):
                design._run_design(self.design_args(source, build))
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertFalse((build / '.llm_cache' / digest / 'sample.sv').exists())
        self.assertEqual(old_output.read_text(), RTL_A)
        self.assertEqual(old_cache.read_text(), RTL_A)
        self.assertFalse((build / 'verification_handoff.json').exists())

    def test_recovery_context_and_cache_do_not_leak_to_next_legacy_invocation(self):
        self.run_module([response('malformed'), response(payload(RTL_A)), response(payload())])
        self.assertIsNone(active_recovery.get())
        original = self.cache().read_bytes()
        with patch('requests.post', return_value=response('malformed')) as post:
            with self.assertRaises(LlmFailure):
                gen.run_dual_llm_rtlgen(self.spec, '')
        post.assert_called_once()
        self.assertIsNone(active_recovery.get())
        self.assertEqual(self.cache().read_bytes(), original)

    def test_legacy_expand_spec_commands_keep_their_existing_recovery_default(self):
        source = self.root / 'legacy.yaml'
        source.write_text('legacy: fixture')
        expanded = self.root / 'expanded.yaml'
        with patch.object(sys, 'argv', ['run_flow.py', str(source)]):
            args = run_flow.parse_args()
        with patch.object(run_flow, 'maybe_expand_user_config', return_value=(source, None)), \
             patch.object(run_flow, 'discover_targets', return_value=([source], ['sample'])), \
             patch.object(run_flow, 'discover_expanded_yamls', return_value=[expanded]), \
             patch.object(run_flow, 'run_command') as command, \
             patch.object(run_flow, 'generate_testbench'), patch.object(run_flow, 'enforce_timescale'), \
             patch.object(run_flow, 'run_full_system_lint'):
            self.assertEqual(run_flow._run_flow(args), 0)
        self.assertEqual([call.args[0][1] for call in command.call_args_list],
                         ['expand_spec.py', 'design.py', 'generate_wrapper.py'])
        self.assertTrue(all('--rtl-recovery' not in call.args[0] for call in command.call_args_list))

    def test_fsm_and_deterministic_routes_cannot_call_recovery_generator(self):
        @dataclass
        class Spec:
            design_name: str
            design_type: str
        for name, kind in (('sample', 'fsm'), ('ddr4_tFAW_tFAW_tracker', 'datapath')):
            with self.subTest(route=kind):
                spec = Spec(name, kind)
                source = self.root / (name + '.yaml')
                source.write_text('isolated fixture')
                rtl = 'module ' + name + '; endmodule'
                modules = {'input_validator': SimpleNamespace(validate_spec=lambda path: spec),
                           'rtlgen': object(), 'dual_rtlgen': gen,
                           'fsm_generator': SimpleNamespace(generate_fsm_rtl=lambda spec: rtl)}
                with patch.dict(os.environ), patch.object(design, 'load_module_from_path',
                     side_effect=lambda name, path: modules[name]), \
                     patch.object(design, 'generate_tfaw_seed_rtl', return_value=rtl), \
                     patch.object(gen, 'run_dual_llm_rtlgen') as dual, patch('requests.post') as post:
                    result = design._run_design(self.design_args(source, self.root / name))
                self.assertTrue(result['passed'])
                self.assertIn('module ' + name, Path(result['rtl_path']).read_text())
                dual.assert_not_called()
                post.assert_not_called()

    def architecture_fixture(self):
        _, request, replies = register_fixture()
        replay = self.root / 'replay.json'
        replay.write_text(json.dumps(replies))
        package = propose_specification(request, output_dir=self.root / 'source', response_path=replay)
        manifest = compile_package(package)
        exported = self.root / 'review/architecture.yaml'
        emit_review(manifest, exported)
        approval = self.root / 'approval.json'
        approval.write_text(json.dumps(create_approval(manifest, 'offline-checkpoint4-fixture')))
        self.module_name = manifest['modules'][0]['spec']['design_name']
        self.build = self.root / 'build'
        with patch.object(sys, 'argv', ['run_flow.py', str(exported), '--spec-approval', str(approval), '--build-dir', str(self.build)]):
            args = run_flow.parse_args()
        return exported, args

    def architecture_run(self, exported, args, replies):
        # Run the production command's parser/module implementation in-process so
        # the HTTP boundary stays mocked; lowering, validation, lint and IO are real.
        original_loader = design.load_module_from_path
        def load(name, path):
            return gen if name == 'dual_rtlgen' else original_loader(name, path)
        def command(argv):
            with patch.object(sys, 'argv', argv[1:]), patch.dict(os.environ):
                module_args = design.parse_args()
                with ModuleUsage(Path(module_args.yaml_path).stem, module_args.output_root):
                    try:
                        design._run_design(module_args)
                    except RuntimeError as exc:
                        raise subprocess.CalledProcessError(1, argv) from exc
        with patch.object(gen, '_design_context_bundle', side_effect=self.real_context), UsageRun(self.root / 'architecture_usage') as usage, \
             patch.object(design, 'load_module_from_path', side_effect=load), \
             patch.object(run_flow, 'run_command', side_effect=command), \
             patch('requests.post', side_effect=replies) as post:
            try:
                result = run_flow.run_architecture_flow(exported, args)
            finally:
                self.last_usage = usage.directory
                self.last_post = post
        return result

    def register_rtl(self):
        return f'''module {self.module_name} (
          input logic clk, input logic rst_n, input logic in_valid,
          input logic [7:0] in_data, output logic out_valid, output logic [7:0] out_data
        );
          logic valid_q;
          assign out_valid = rst_n && valid_q;
          always_ff @(posedge clk) begin
            if (!rst_n) begin valid_q <= 1'b0; out_data <= '0; end
            else begin valid_q <= in_valid; if (in_valid) out_data <= in_data; end
          end
        endmodule'''

    def test_architecture_default_recovery_reaches_real_integration_lint_and_handoff(self):
        exported, args = self.architecture_fixture()
        self.assertEqual(self.architecture_run(exported, args,
            [response('malformed'), response(payload(self.register_rtl())), response(payload())]), 0)
        status = json.loads((self.build / 'build_status.json').read_text())
        self.assertTrue(status['rtl_recovery'])
        self.assertEqual(status['rtl_generation'], 'passed')
        self.assertEqual(status['integration_lint'], 'passed')
        self.assertEqual(status['behavioral_verification'], 'not_run')
        self.assertEqual(validate_handoff(self.build / 'verification_handoff.json')['handoff_validation'], 'passed')
        self.assertEqual(self.last_post.call_count, 3)
        self.assertEqual(len(list((self.build / '.llm_cache').rglob('*.sv'))), 1)
        self.assertTrue((self.build / 'rtl_output' / (self.module_name + '.sv')).exists())

    def test_integration_rejects_recovered_interface_and_clears_prior_handoff(self):
        exported, args = self.architecture_fixture()
        self.build.mkdir()
        (self.build / 'verification_handoff.json').write_text('{"stale": true}')
        wrong_interface = f'module {self.module_name}(input logic clk, rst_n); endmodule'
        with self.assertRaisesRegex(ValueError, 'integration lint failed'):
            self.architecture_run(exported, args,
                [response('malformed'), response(payload(wrong_interface)), response(payload())])
        status = json.loads((self.build / 'build_status.json').read_text())
        self.assertEqual(status['rtl_generation'], 'passed')
        self.assertEqual(status['integration_lint'], 'failed')
        self.assertEqual(status['result'], 'failed')
        self.assertFalse((self.build / 'verification_handoff.json').exists())

    def test_architecture_payload_exhaustion_stops_build_before_integration_or_handoff(self):
        exported, args = self.architecture_fixture()
        self.build.mkdir()
        (self.build / 'verification_handoff.json').write_text('{"stale": true}')
        with self.assertRaises(subprocess.CalledProcessError):
            self.architecture_run(exported, args, [response('malformed'), response('malformed again')])
        status = json.loads((self.build / 'build_status.json').read_text())
        self.assertEqual(status['rtl_generation'], 'failed')
        self.assertEqual(status['integration_lint'], 'not_run')
        self.assertEqual(status['result'], 'failed')
        self.assertFalse((self.build / 'verification_handoff.json').exists())
        self.assertFalse((self.build / 'rtl_output' / (self.module_name + '.sv')).exists())
        self.assertFalse(list((self.build / '.llm_cache').rglob('*.sv')))

    def test_explicit_disable_keeps_the_architecture_payload_fail_fast(self):
        exported, args = self.architecture_fixture()
        args.rtl_recovery = False
        with self.assertRaises(subprocess.CalledProcessError):
            self.architecture_run(exported, args, [response('malformed')])
        self.assertFalse(json.loads((self.build / 'build_status.json').read_text())['rtl_recovery'])
        self.assertEqual(self.last_post.call_count, 1)
        self.assertFalse((self.build / 'verification_handoff.json').exists())
        self.assertIsNone(active_recovery.get())


if __name__ == '__main__':
    unittest.main()
