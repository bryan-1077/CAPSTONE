"""Resume uses reviewed, specification-keyed caches; all generation is mocked."""
import contextlib
import hashlib
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

import design
import dual_llm_rtlGen as generator
import run_flow
from architecture import ROOT
from generate_specification import propose_specification
from specification_lowering import compile_package, emit_review
from test_generate_specification import register_fixture
from token_usage import ModuleUsage, UsageRun


class ResumeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {'CAPSTONE_RTL_CACHE_DIR': str(self.root / 'cache'),
                                     'CAPSTONE_TPM_LIMIT': '0', 'TAMUS_AI_CHAT_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.module = SimpleNamespace(design_name='sample')
        self.rtl = 'module sample; endmodule'
        self.good = {'status': 'VALID', 'issues': [], 'corrected_rtl': self.rtl}

    def test_reviewed_cache_hit_makes_no_calls_and_reports_zero_usage(self):
        generator._write_reviewed_cache('sample', self.rtl)
        with UsageRun(self.root) as usage:
            with ModuleUsage('sample', self.root):
                with patch.object(generator, '_llm1_generate') as generate, patch.object(generator, '_llm2_review') as review:
                    rtl = generator.run_dual_llm_rtlgen(self.module, '', resume=True)
                generate.assert_not_called()
                review.assert_not_called()
                self.assertIn('module sample', rtl)
        report = json.loads((usage.directory / 'usage.json').read_text())
        self.assertEqual(report['totals']['calls'], 0)
        trace = json.loads(next(usage.directory.rglob('rtl_review.json')).read_text())
        self.assertTrue(trace['cache_hit'])
        self.assertEqual(trace['rounds'], [])

    def test_missing_or_lint_invalid_cache_generates_and_reviews(self):
        for invalid in (False, True):
            path = Path(generator._cache_path('sample'))
            path.unlink(missing_ok=True)
            if invalid:
                generator._write_reviewed_cache('sample', 'invalid cached RTL')
            with self.subTest(invalid=invalid), \
                 patch.object(generator, 'lint_rtl_single_file', side_effect=(
                     [(False, ['invalid syntax']), (True, [])] if invalid else [(True, [])])), \
                 patch.object(generator, '_llm1_generate', return_value=self.good) as generate, \
                 patch.object(generator, '_llm2_review', return_value=self.good) as review:
                rtl = generator.run_dual_llm_rtlgen(self.module, '', resume=True)
                self.assertIn('module sample', rtl)
                generate.assert_called_once()
                review.assert_called_once()

    def test_cache_only_still_fails_on_missing_cache_without_generating(self):
        with patch.object(generator, '_llm1_generate') as generate:
            with self.assertRaisesRegex(RuntimeError, 'no usable cached reviewed RTL'):
                generator.run_dual_llm_rtlgen(self.module, '', use_cache=True)
        generate.assert_not_called()

    def test_regular_generation_does_not_implicitly_resume(self):
        generator._write_reviewed_cache('sample', self.rtl)
        with patch.object(generator, '_llm1_generate', return_value=self.good) as generate, \
             patch.object(generator, '_llm2_review', return_value=self.good):
            generator.run_dual_llm_rtlgen(self.module, '')
        generate.assert_called_once()

    def test_cli_conflicts_and_unkeyed_resume_are_rejected(self):
        for parser, argv in ((run_flow.parse_args, ['run_flow.py', 'manifest.yaml', '--resume', '--cache']),
                             (design.parse_args, ['design.py', 'module.yaml', '--resume', '--cache']),
                             (design.parse_args, ['design.py', 'module.yaml', '--resume'])):
            with self.subTest(argv=argv), patch.object(sys, 'argv', argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser()

    def test_resume_requires_manifest_and_is_forwarded_to_design(self):
        with patch.object(sys, 'argv', ['run_flow.py', 'request.yaml', '--resume']):
            args = run_flow.parse_args()
        with patch('specification.read_document', return_value={'requirements': ['A request']}):
            with self.assertRaisesRegex(ValueError, 'requires an existing architecture manifest'):
                run_flow.run_architecture_flow(Path('request.yaml'), args)
        command = run_flow.design_command(Path('module.yaml'), no_lint=False, cache=False, resume=True)
        self.assertIn('--resume', command)
        self.assertNotIn('--cache', command)

    def test_design_cli_cache_is_keyed_by_exact_specification_and_restores_output(self):
        _, request, responses = register_fixture()
        replies = iter([json.dumps(responses['system']), json.dumps(responses['architecture'])])
        package = propose_specification(request, output_dir=self.root / 'source', caller=lambda *args: next(replies))
        manifest = compile_package(package)
        spec = emit_review(manifest, self.root / 'architecture.yaml')[0]
        name = manifest['modules'][0]['spec']['design_name']
        build = self.root / 'build'
        digest = hashlib.sha256(spec.read_bytes()).hexdigest()
        cache = build / '.llm_cache' / digest
        cache.mkdir(parents=True)
        # Interface-only cache fixture; no target behavior is asserted.
        (cache / (name + '.sv')).write_text(f'module {name}; endmodule\n')
        command = [sys.executable, str(ROOT / 'design.py'), str(spec), '--resume', '--output-root', str(build)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Using cached reviewed RTL', result.stdout)
        self.assertTrue((build / 'rtl_output' / (name + '.sv')).exists())
        self.assertTrue((build / 'microarch/ir' / (name + '_ir.json')).exists())
        spec.write_text(spec.read_text() + '\n# Different exact source identity\n')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no usable matching reviewed cache', result.stdout)
        self.assertIn('TAMUS_AI_CHAT_API_KEY not set', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
