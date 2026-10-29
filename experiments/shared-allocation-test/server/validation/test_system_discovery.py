"""Phase 1 input/transport regressions. No LLM, HDL compiler or agent runs.

Run: python3 -B -m unittest discover -s server/validation -p test_system_discovery.py -v
"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_pipeline
from controller_manifest import (build_manifest, discover_system, file_digest,
                                 module_declarations, publish_snapshot)
import yaml


REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / 'local' / 'frontend'


class SystemDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='controller-handoff-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'frontend run with spaces'
        for name in ('rtl_output', 'expanded', 'inputs/generated', 'ir', 'jedec', 'tb'):
            shutil.copytree(str(FRONTEND / name), str(self.root / name),
                            ignore=shutil.ignore_patterns('verification_reports', '__pycache__'))
        self.manifest_path = self.root / 'rtl_output' / 'manifest.json'
        self.manifest = json.loads(self.manifest_path.read_text())

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def rebuild(self, configuration=None):
        directory = self.manifest_path.parent
        files = sorted(directory.glob('*.sv'))
        filelist = directory / 'filelist.f'
        filelist.write_text(''.join(p.name + '\n' for p in files))
        self.manifest = build_manifest(
            self.root, directory, [p.relative_to(directory) for p in files], filelist,
            'ddr4_controller_top', self.root / 'inputs/generated/ddr4_bank.yaml',
            configuration or self.manifest['configuration'])
        self.write_manifest()

    def test_actual_inventory_and_clock_reset(self):
        result = discover_system(self.manifest_path)
        self.assertEqual(result['discovery_result'], 'PASS')
        self.assertEqual(result['verification_scope'], 'input_discovery_only')
        self.assertEqual(result['rtl_file_count'], len(self.manifest['rtl_files']))
        self.assertEqual(result['module_definition_count'], len(self.manifest['module_definitions']))
        self.assertEqual(result['module_spec_count'], len(self.manifest['module_specs']))
        self.assertEqual(self.manifest['reset'],
                         {'name': 'rst_n', 'active_low': True, 'synchronous': True})
        self.assertEqual(self.manifest['hierarchy_analysis'], 'deferred_to_agent1')
        self.assertEqual(self.manifest['modules_without_specs'],
                         ['ddr4_bank_top', 'ddr4_controller_top'])

    def test_declarations_ignore_comments_and_strings_and_allow_other_filenames(self):
        self.assertEqual(module_declarations(
            '// module fake;\n/* module fake2; */\n'
            'module automatic real_one; string s="module fake3;"; endmodule\n'
            'module real_two; endmodule'), ['real_one', 'real_two'])
        directory = self.manifest_path.parent
        (directory / 'ddr4_request_queue.sv').rename(directory / 'unrelated_filename.sv')
        self.rebuild()
        result = discover_system(self.manifest_path)
        self.assertIn({'name': 'ddr4_request_queue', 'file': 'unrelated_filename.sv'},
                      result['manifest']['module_definitions'])
        self.assertNotIn('unrelated_filename', result['manifest']['generated_modules'])

    def test_missing_inputs_are_rejected(self):
        for relative in (self.manifest['controller_config'], self.manifest['system_testbench'],
                         self.manifest['module_specs'][0]['path'], self.manifest['ir_files'][0]):
            path = self.manifest_path.parent / relative
            contents = path.read_bytes()
            with self.subTest(path=relative):
                path.unlink()
                with self.assertRaisesRegex(ValueError, 'Missing system input'):
                    discover_system(self.manifest_path)
                path.write_bytes(contents)

    def test_absolute_and_escaping_paths_are_rejected(self):
        for value in ('/tmp/controller.sv', '../../../outside.sv'):
            with self.subTest(path=value):
                self.manifest['system_testbench'] = value
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, 'relative POSIX|escapes'):
                    discover_system(self.manifest_path)

    def test_stale_inventory_config_and_master_paths_are_rejected(self):
        self.manifest['module_definitions'][0]['name'] = 'stale_name'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            discover_system(self.manifest_path)
        self.rebuild()
        self.manifest['configuration']['bank_count'] = 2
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'configuration differs'):
            discover_system(self.manifest_path)
        self.rebuild(configuration=dict(self.manifest['configuration'], bank_count=4))
        master = self.root / 'expanded/ddr4_bank/master.yaml'
        data = yaml.safe_load(master.read_text())
        data['submodules'][0]['spec'] = '/other/machine/activate.yaml'
        master.write_text(yaml.safe_dump(data))
        with self.assertRaisesRegex(ValueError, 'Nonportable master'):
            discover_system(self.manifest_path)

    def test_configuration_and_optional_modules_are_not_fixed_to_current_run(self):
        removed = {'ddr4_refresh_refresh_controller', 'ddr4_tRRD_simple_tRRD', 'ddr4_tFAW_tFAW_tracker'}
        expected_files = len(self.manifest['rtl_files']) - len(removed)
        expected_specs = sum(s['module_name'] not in removed for s in self.manifest['module_specs'])
        for name in removed:
            (self.manifest_path.parent / (name + '.sv')).unlink()
        for relative in self.manifest['configuration_sources']:
            config_path = self.manifest_path.parent / relative
            config = yaml.safe_load(config_path.read_text())
            config['controller_config']['topology']['banks'] = 2
            config['controller_config']['page_policy'] = 'close_page'
            config_path.write_text(yaml.safe_dump(config))
        configuration = dict(self.manifest['configuration'], bank_count=2, page_policy='close_page',
                             refresh_enabled=False, trrd_enabled=False, tfaw_enabled=False)
        self.rebuild(configuration)
        result = discover_system(self.manifest_path)
        self.assertEqual(result['manifest']['configuration'], configuration)
        self.assertEqual(result['rtl_file_count'], expected_files)
        self.assertEqual(result['module_spec_count'], expected_specs)

    def test_growing_rtl_sets_and_multiple_modules_per_file_survive_transport(self):
        directory = self.manifest_path.parent
        baseline_files = len(self.manifest['rtl_files'])
        baseline_definitions = len(self.manifest['module_definitions'])
        extra_files, extra_modules = [], set()
        top = directory / next(row['file'] for row in self.manifest['module_definitions']
                               if row['name'] == self.manifest['top_module'])
        for target_count in (1, 16, 64):
            with self.subTest(extra_files=target_count):
                instances = []
                for index in range(len(extra_files), target_count):
                    leaf, parent = 'extra_leaf_{}'.format(index), 'extra_parent_{}'.format(index)
                    path = directory / 'generated_bundle_{}.sv'.format(index)
                    path.write_text('module {}; endmodule\nmodule {}; {} u_leaf(); endmodule\n'.format(
                        leaf, parent, leaf))
                    extra_files.append(path.name)
                    extra_modules.update((leaf, parent))
                    instances.append('{} u_extra_{}();'.format(parent, index))
                top.write_text(top.read_text().replace('endmodule', '\n'.join(instances) + '\nendmodule', 1))
                self.rebuild()
                result = discover_system(self.manifest_path)
                self.assertEqual(result['rtl_file_count'], baseline_files + len(extra_files))
                self.assertEqual(result['module_definition_count'], baseline_definitions + len(extra_modules))
                self.assertTrue(set(extra_files) <= set(result['manifest']['rtl_files']))
                self.assertTrue(extra_modules <= set(result['manifest']['generated_modules']))
        destination = Path(self.temp.name) / 'grown system snapshot'
        snapshot_path = publish_snapshot(self.manifest_path, destination)
        snapshot = json.loads(snapshot_path.read_text())
        shutil.rmtree(str(self.root))
        result = discover_system(destination / snapshot['system_manifest'])
        self.assertEqual(result['rtl_file_count'], baseline_files + len(extra_files))
        self.assertEqual(result['module_definition_count'], baseline_definitions + len(extra_modules))

    def test_snapshot_relocates_without_original_and_preserves_hashes(self):
        destination = Path(self.temp.name) / 'exported input'
        original_manifest_hash = file_digest(self.manifest_path)
        snapshot_path = publish_snapshot(self.manifest_path, destination)
        snapshot = json.loads(snapshot_path.read_text())
        self.assertEqual(file_digest(self.manifest_path), original_manifest_hash)
        for relative, digest in snapshot['files'].items():
            self.assertEqual(file_digest(destination / relative), digest)
        for relative in ('inputs/generated/ddr4_bank.yaml', 'tb/tb_ddr4_controller_top.sv',
                         'expanded/ddr4_bank/master.yaml', 'ir/ddr4_request_queue_ir.json'):
            self.assertIn(relative, snapshot['files'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            publish_snapshot(self.manifest_path, destination)
        shutil.rmtree(str(self.root))
        result = discover_system(destination / snapshot['system_manifest'])
        self.assertEqual(result['rtl_file_count'], len(self.manifest['rtl_files']))
        self.assertEqual(len(snapshot['rtl_revision']), 64)

    def test_expansion_writes_portable_master_paths(self):
        directory = Path(self.temp.name) / 'new expanded bank'
        completed = subprocess.run(
            [sys.executable, '-B', str(FRONTEND / 'expand_spec.py'),
             str(self.root / 'inputs/generated/ddr4_bank.yaml'), '--output-dir', str(directory)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        master = yaml.safe_load((directory / 'master.yaml').read_text())
        for child in master['submodules']:
            self.assertFalse(Path(child['spec']).is_absolute())
            self.assertTrue((directory / child['spec']).is_file())

    def test_cli_auto_and_explicit_system_never_run_agents_or_create_reports(self):
        for args in ([str(self.root)], [str(self.manifest_path.parent), '--system'],
                     [str(self.manifest_path), '--json']):
            with self.subTest(args=args), patch.object(sys, 'argv', ['run_pipeline.py'] + args + ['--discovery-only']):
                with patch.object(run_pipeline, 'execute_reported_process',
                                  side_effect=AssertionError('An agent must not run')):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        run_pipeline.main()
                    self.assertIn('PASS', output.getvalue())
        self.assertEqual(list(self.root.rglob('verification_reports')), [])

    def test_module_option_selects_one_diagnostic(self):
        with patch.object(sys, 'argv', ['run_pipeline.py', str(self.root), '--module', 'ddr4_request_queue']):
            with patch.object(run_pipeline, 'create_report_run', return_value='/tmp/unused-report'):
                with patch.object(run_pipeline, 'run_pipeline_stages') as stages:
                    with contextlib.redirect_stdout(io.StringIO()):
                        run_pipeline.main()
        self.assertEqual(stages.call_count, 1)
        self.assertEqual(stages.call_args[1]['design']['module_name'], 'ddr4_request_queue')
        self.assertEqual(len(stages.call_args[0][1]), 1)


if __name__ == '__main__':
    unittest.main()
