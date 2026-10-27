"""Plain-text entry point and legacy isolation; no real provider calls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import configure_microarch_from_text as intake
from specification_request import build_specification_request
from test_generate_specification import register_fixture


class TextIntakeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        output = contextlib.redirect_stdout(io.StringIO())
        self.output = output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        self.description = 'Use AXI4-Lite, FIFO ordering and an additional tWR constraint.\nKeep byte strobes; do not replace AXI with txn_* ports. λ\n'
        self.text = self.root / 'description.txt'
        self.text.write_text(self.description)

    def args(self, *extra):
        return ['--text-file', str(self.text), '--run-dir', str(self.root / 'run'), *extra]

    def test_arbitrary_requirements_roundtrip_without_legacy_presets(self):
        request = intake.request_from_text(self.description)
        self.assertEqual(yaml.safe_load(intake.request_yaml(request)), request)
        proof = build_specification_request(request)
        self.assertEqual(proof['requirements'][0]['text'], self.description)
        self.assertNotIn('baseline_contract', proof)
        self.assertNotIn('features', request)
        self.assertNotIn('profile', request)
        self.assertEqual(proof['design_kind'], 'memory_controller')

    def test_prepare_only_persists_exact_text_and_never_starts_generation(self):
        with patch.object(intake, 'stream_command') as command:
            self.assertEqual(intake.main(self.args('--yes', '--prepare-only')), 0)
        command.assert_not_called()
        root = self.root / 'run'
        self.assertEqual((root / 'description.txt').read_text(), self.description)
        self.assertEqual(yaml.safe_load((root / 'request.yaml').read_text())['requirements'], [self.description])
        self.assertEqual(json.loads((root / 'intake_status.json').read_text())['status'], 'request_prepared')

    def test_declined_file_request_does_not_write_or_call_any_stage(self):
        with patch('builtins.input', return_value='n'), patch.object(intake, 'stream_command') as command:
            self.assertEqual(intake.main(self.args()), 0)
        command.assert_not_called()
        self.assertFalse((self.root / 'run').exists())

    def test_interactive_revision_is_confirmed_before_generating(self):
        answers = iter(['wrong description', '.', 'n', 'Use FIFO', 'with refresh.', '.', 'y'])
        with patch('builtins.input', side_effect=lambda prompt: next(answers)), \
             patch.object(intake, 'stream_command', return_value=0) as command:
            code = intake.main(['--run-dir', str(self.root / 'run')])
        self.assertEqual(code, 0)
        self.assertEqual(command.call_count, 1)
        request = yaml.safe_load((self.root / 'run/request.yaml').read_text())
        self.assertEqual(request['requirements'], ['Use FIFO\nwith refresh.'])

    def test_full_route_forwards_isolated_paths_models_and_simulation(self):
        with patch.object(intake, 'stream_command', return_value=0) as command:
            self.assertEqual(intake.main(self.args('--yes', '--simulate', '--rtl-generator-model', 'alternate')), 0)
        self.assertEqual(command.call_count, 2)
        generation = command.call_args_list[0].args[0]
        self.assertIn('--specification', generation)
        self.assertIn('--automated-spec-review', generation)
        self.assertIn('--auto-spec-repair', generation)
        self.assertEqual(generation[generation.index('--rtl-generator-model') + 1], 'alternate')
        self.assertNotIn('--microarch', generation)
        self.assertNotIn('--cache', generation)
        simulation = command.call_args_list[1].args[0]
        self.assertTrue(simulation[2].endswith('simulate_handoff.py'))
        self.assertEqual(simulation[3], str(self.root / 'run/build/verification_handoff.json'))
        self.assertEqual(json.loads((self.root / 'run/intake_status.json').read_text())['status'], 'simulation_passed')

    def test_failure_stops_before_simulation_and_returns_original_exit_code(self):
        with patch.object(intake, 'stream_command', return_value=7) as command:
            self.assertEqual(intake.main(self.args('--yes', '--simulate')), 7)
        self.assertEqual(command.call_count, 1)
        status = json.loads((self.root / 'run/intake_status.json').read_text())
        self.assertEqual(status['status'], 'generation_failed')

    def test_invalid_requests_conflicts_and_existing_outputs_fail_without_calls(self):
        for text in ('', ' \n\t'):
            with self.assertRaises(ValueError):
                intake.request_from_text(text)
        with self.assertRaises(ValueError):
            intake.request_from_text('description', 'bad; module')
        for args in (['--cache'], ['--input', 'legacy.yaml'], ['--simulate', '--prepare-only'],
                     ['--simulate', '--no-lint'], ['--specification-response', 'responses.json'],
                     ['--microarch-only', '--rtl-generator-model', 'alternate']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                intake.parse_args(args)
        (self.root / 'run').mkdir()
        with patch.object(intake, 'stream_command') as command:
            self.assertEqual(intake.main(self.args('--yes')), 2)
        command.assert_not_called()

    def test_shell_dispatch_preserves_legacy_default_and_adds_opt_in(self):
        # Record interpreter arguments instead of running either generator.
        script = self.root / 'start_flow.sh'
        shutil.copyfile(intake.SCRIPT_DIR / 'start_flow.sh', script)
        bindir = self.root / 'bin'
        bindir.mkdir()
        interpreter = bindir / 'python3'
        interpreter.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                               'open(os.environ["INTAKE_CAPTURE"],"w").write(json.dumps(sys.argv[1:]))\n')
        interpreter.chmod(0o755)
        capture = self.root / 'capture.json'
        env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'],
                   INTAKE_CAPTURE=str(capture), BASH_ENV='')
        cases = [([], 'configure_from_text.py', '--run-flow'),
                 (['--cache'], 'run_flow.py', '--cache'),
                 (['--specification'], 'run_flow.py', '--specification'),
                 (['--specification', '--no-rtl-recovery'], 'run_flow.py', '--no-rtl-recovery'),
                 (['--specification', '--from-text', '--no-rtl-recovery'], 'configure_microarch_from_text.py', '--no-rtl-recovery'),
                 (['--specification', '--from-text', '--prepare-only'], 'configure_microarch_from_text.py', '--prepare-only')]
        for flags, expected, option in cases:
            with self.subTest(flags=flags):
                result = subprocess.run(['bash', str(script), *flags], cwd=self.root, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                argv = json.loads(capture.read_text())
                self.assertTrue(any(Path(arg).name == expected for arg in argv))
                self.assertIn(option, argv)
                self.assertNotIn('--from-text', argv)

    def test_real_offline_text_to_system_to_architecture_export(self):
        from specification_lowering import compile_package
        config, _, responses = register_fixture()
        text = config['requirements'][0]
        self.text.write_text(text)
        config = intake.request_from_text(text, 'register_top', 'generic')
        request = build_specification_request(config)
        # Adapt only fixture request bookkeeping; its byte-register behavior,
        # graph and verification remain independent of the intake implementation.
        system = responses['system']['system']
        example_obligation = copy.deepcopy(next(iter(system['verification'].values())))
        system['rules'] = {r['id']: {'origin': 'request.' + r['id'], 'statement': r['text'],
                                    'verification': ['V_' + r['id']]} for r in request['requirements']}
        system['verification'] = {'V_' + r['id']: dict(example_obligation, rules=[r['id']]) for r in request['requirements']}
        architecture = responses['architecture']
        architecture['rule_owners'] = {'system.rules.' + r['id']: ['stage'] for r in request['requirements']}
        contract = architecture['modules']['contracts']['stage']
        contract['invariants'] = list(architecture['rule_owners'])
        contract['verification'] = ['system.verification.' + key for key in system['verification']]
        example_check = next(iter(architecture['verification']['checks'].values()))
        architecture['verification']['checks'] = {key: copy.deepcopy(example_check) for key in contract['verification']}
        replay = self.root / 'responses.json'
        replay.write_text(json.dumps(responses))
        guard = self.root / 'sitecustomize.py'
        guard.write_text('import socket\ndef offline(*a,**kw):\n raise RuntimeError("Network disabled in offline test")\n'
                         'socket.socket.connect=offline\nsocket.create_connection=offline\n')
        env = dict(os.environ, TAMUS_AI_CHAT_API_KEY='', PYTHONPATH=str(self.root))
        legacy = intake.SCRIPT_DIR / 'configs/user_input.yaml'
        before = legacy.read_bytes()
        result = subprocess.run([sys.executable, str(intake.SCRIPT_DIR / 'configure_microarch_from_text.py'),
            *self.args('--yes', '--microarch-only', '--design-name', 'register_top', '--design-type', 'generic',
                        '--specification-response', str(replay))], cwd=self.root, env=env,
                        capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        root = self.root / 'run'
        manifest = compile_package(root / 'source/package.yaml')
        self.assertEqual(manifest['provenance']['specification']['snapshot']['package']['generation']['request'], request)
        self.assertTrue((root / 'architecture.yaml').is_file())
        self.assertFalse((root / 'build').exists())
        self.assertIn('calls=0', result.stdout)
        self.assertEqual(legacy.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
