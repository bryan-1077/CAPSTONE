"""Architecture model selection; all LLM calls are mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import design
import dual_llm_rtlGen as generator
import run_flow
from token_usage import ModuleUsage, UsageRun


class GeneratorModelTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.spec = SimpleNamespace(design_name='sample', design_type='datapath')
        self.rtl = 'module sample; endmodule'
        self.good = {'status': 'VALID', 'issues': [], 'corrected_rtl': self.rtl}

    def test_cli_defaults_and_explicit_forwarding(self):
        with patch.object(sys, 'argv', ['run_flow.py', 'architecture.yaml', '--resume',
                                      '--rtl-generator-model', ' alternate ']):
            self.assertEqual(run_flow.parse_args().rtl_generator_model, 'alternate')
        legacy = run_flow.design_command(Path('spec.yaml'), no_lint=False, cache=False)
        self.assertNotIn('--rtl-generator-model', legacy)
        command = run_flow.design_command(Path('spec.yaml'), no_lint=False, cache=False,
                                         resume=True, rtl_generator_model='alternate')
        self.assertEqual(command[-2:], ['--rtl-generator-model', 'alternate'])
        self.assertIn('--resume', command)
        with patch.object(sys, 'argv', ['design.py', 'spec.yaml']):
            self.assertIsNone(design.parse_args().rtl_generator_model)

    def test_legacy_override_fails_before_expansion_or_generation(self):
        with patch.object(sys, 'argv', ['run_flow.py', 'legacy.yaml', '--rtl-generator-model', 'alternate']):
            args = run_flow.parse_args()
        with patch.object(run_flow, 'maybe_expand_user_config') as expand, \
             patch.object(run_flow, 'run_command') as command:
            self.assertEqual(run_flow._run_flow(args), 1)
        expand.assert_not_called()
        command.assert_not_called()
        self.assertIn('requires an architecture build', self.output.getvalue())

    def test_invalid_or_unisolated_models_fail_at_cli(self):
        for argv, parser in [
            (['run_flow.py', 'manifest.yaml', '--rtl-generator-model', '  '], run_flow.parse_args),
            (['design.py', 'spec.yaml', '--rtl-generator-model', 'alternate'], design.parse_args),
            (['design.py', 'spec.yaml', '--output-root', '/tmp/build', '--rtl-generator-model', ''], design.parse_args)]:
            with self.subTest(argv=argv), patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser()

    def test_api_selection_is_per_invocation_and_reviewer_is_unchanged(self):
        bundle = {'prompt_text': 'fixture context', 'invariants': []}
        default_generator, default_reviewer = generator.LLM1_MODEL, generator.LLM2_MODEL
        with patch.object(generator, '_design_context_bundle', return_value=bundle), \
             patch.object(generator, '_call_llm', return_value='{}') as call, \
             patch.object(generator, '_parse_llm_json_payload', return_value=self.good), \
             patch.object(generator, '_parse_review_json_payload', return_value=self.good):
            generator._llm1_generate(self.spec, '', [], generator_model='alternate')
            generator._llm1_generate(self.spec, '', [], lint_issues=['repair this'],
                                      previous_rtl=self.rtl, generator_model='alternate')
            generator._llm1_merge(self.spec, '', self.rtl, 'different RTL', self.good, [],
                                   generator_model='alternate')
            generator._llm2_review(self.spec, '', self.rtl, [])
            generator._llm1_generate(self.spec, '', [])
        self.assertEqual([c.args[0] for c in call.call_args_list],
                         ['alternate', 'alternate', 'alternate', default_reviewer, default_generator])
        self.assertEqual(generator.LLM1_MODEL, default_generator)
        self.assertEqual(generator.LLM2_MODEL, default_reviewer)

    def test_loop_forwards_override_through_lint_repair_and_merge(self):
        invalid = {**self.good, 'status': 'INVALID', 'corrected_rtl': self.rtl + '\n// suggested',
                   'issues': [{'type': 'SPEC_VIOLATION', 'description': 'Fix fixture', 'location': 'sample'}]}
        clean = {'ok': True, 'blocking': [], 'nonblocking': [], 'ignored': []}
        failed = {**clean, 'ok': False, 'blocking': ['fixture syntax error']}
        with patch.object(generator, '_llm1_generate', return_value=self.good) as generate, \
             patch.object(generator, 'lint_candidate', side_effect=[failed, clean, clean]), \
             patch.object(generator, 'lint_rtl_single_file', return_value=(True, [])), \
             patch.object(generator, '_llm2_review', side_effect=[invalid, self.good]), \
             patch.object(generator, '_llm1_merge', return_value=self.good) as merge, \
             patch.object(generator, '_write_reviewed_cache'):
            generator.run_dual_llm_rtlgen(self.spec, '', generator_model='alternate')
        self.assertEqual(generate.call_count, 2)
        for call in generate.call_args_list + merge.call_args_list:
            self.assertEqual(call.kwargs['generator_model'], 'alternate')

    def test_resume_cache_avoids_calls_and_trace_marks_requested_model(self):
        with tempfile.TemporaryDirectory() as directory, UsageRun(Path(directory)) as usage:
            with ModuleUsage('sample', directory), \
                 patch.object(generator, '_load_reviewed_cache', return_value=self.rtl), \
                 patch.object(generator, '_call_llm') as call:
                self.assertEqual(generator.run_dual_llm_rtlgen(self.spec, '', resume=True,
                                 generator_model='alternate'), self.rtl)
            call.assert_not_called()
            trace = json.loads(next(usage.directory.rglob('rtl_review.json')).read_text())
            self.assertTrue(trace['cache_hit'])
            self.assertEqual(trace['requested_generator_model'], 'alternate')
            self.assertEqual(trace['rounds'], [])

    def test_fsm_path_does_not_invoke_dual_llm_with_override(self):
        from dataclasses import dataclass
        @dataclass
        class FsmSpec:
            design_name: str = 'sample'
            design_type: str = 'fsm'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'spec.yaml'
            source.write_text('fixture: true\n')
            args = SimpleNamespace(yaml_path=str(source), output_root=directory, no_lint=False,
                                   cache=False, resume=False, rtl_generator_model='alternate')
            validator = SimpleNamespace(validate_spec=lambda path: FsmSpec())
            fsm = SimpleNamespace(generate_fsm_rtl=lambda spec: self.rtl)
            dual = SimpleNamespace(run_dual_llm_rtlgen=unittest.mock.Mock())
            modules = {'input_validator': validator, 'rtlgen': object(), 'dual_rtlgen': dual, 'fsm_generator': fsm}
            with patch.dict(os.environ), patch.object(design, 'load_module_from_path',
                                                       side_effect=lambda name, path: modules[name]):
                result = design._run_design(args)
            self.assertTrue(result['passed'])
            dual.run_dual_llm_rtlgen.assert_not_called()

    def test_design_forwards_override_only_when_selected(self):
        from dataclasses import dataclass
        @dataclass
        class DatapathSpec:
            design_name: str = 'sample'
            design_type: str = 'datapath'
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'spec.yaml'
            source.write_text('fixture: true\n')
            for selected in (None, 'alternate'):
                args = SimpleNamespace(yaml_path=str(source), output_root=directory, no_lint=False,
                                       cache=False, resume=False, rtl_generator_model=selected)
                dual = SimpleNamespace(run_dual_llm_rtlgen=unittest.mock.Mock(return_value=self.rtl))
                modules = {'input_validator': SimpleNamespace(validate_spec=lambda path: DatapathSpec()),
                           'rtlgen': object(), 'dual_rtlgen': dual}
                with self.subTest(model=selected), patch.dict(os.environ), \
                     patch.object(design, 'load_module_from_path', side_effect=lambda name, path: modules[name]):
                    self.assertTrue(design._run_design(args)['passed'])
                options = dual.run_dual_llm_rtlgen.call_args.kwargs
                if selected is None:
                    self.assertNotIn('generator_model', options)
                else:
                    self.assertEqual(options['generator_model'], selected)


if __name__ == '__main__':
    unittest.main()
