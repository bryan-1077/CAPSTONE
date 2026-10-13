"""Phase 2 regressions. Compiler mocks test orchestration, not HDL correctness.

Run: python3 -B -m unittest discover -s server/validation -p 'test_system*.py' -v
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent1_rtl_validator as agent1
import run_pipeline
from controller_manifest import discover_system, publish_snapshot
from system_rtl_validation import (declaration_width, integer_value,
                                   validate_system)
import test_system_discovery as discovery_tests


class SystemAgent1Tests(unittest.TestCase):
    setUp = discovery_tests.SystemDiscoveryTests.setUp
    write_manifest = discovery_tests.SystemDiscoveryTests.write_manifest
    rebuild = discovery_tests.SystemDiscoveryTests.rebuild

    def validate(self, compiler_result=('vcs', True, 'mocked compile/elaboration')):
        output = Path(self.temp.name) / 'agent1' / 'validation_result.json'
        with patch.object(agent1, 'compile_rtl', return_value=compiler_result) as compiler:
            result = validate_system(agent1, self.manifest_path, output)
        self.assertEqual(json.loads(output.read_text()), result)
        self.compiler = compiler
        return result

    def top_path(self):
        row = next(r for r in self.manifest['module_definitions'] if r['name'] == self.manifest['top_module'])
        return self.manifest_path.parent / row['file']

    def append_top(self, text):
        top = self.top_path()
        top.write_text(top.read_text().replace('endmodule', text + '\nendmodule'))

    def add_rtl(self, filename, text):
        (self.manifest_path.parent / filename).write_text(text)
        self.rebuild()

    def remove_instance(self, feature):
        group = next(Path(self.manifest_path.parent / p) for p in self.manifest['spec_groups']
                     if agent1.load_yaml(str(self.manifest_path.parent / p)).get('source_feature') == feature)
        master = agent1.load_yaml(str(group))
        name = agent1.load_yaml(str(group.parent / master['submodules'][0]['spec']))['design_name']
        top = self.top_path()
        source = top.read_text()
        source, count = re.subn(r'\b' + re.escape(name) + r'\s+\w+\s*\([\s\S]*?\);', '', source)
        self.assertEqual(count, 1)
        top.write_text(source)
        self.rebuild()
        return name

    def test_valid_complete_hierarchy_and_wrappers_without_yaml(self):
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertEqual(result['reachable_modules'], result['generated_modules'])
        self.assertEqual(result['verification_readiness'], 'READY_FOR_SYSTEM_AGENT_2_AND_AGENT_3')
        self.assertFalse(result['functional_verification_passed'])
        self.assertFalse(result['ready_for_backend'])
        self.assertTrue(result['hierarchy_passed'])
        self.assertEqual(self.compiler.call_count, 1)
        files, top = self.compiler.call_args[0]
        self.assertEqual(len(files), len(self.manifest['rtl_files']))
        self.assertEqual(top, self.manifest['top_module'])
        self.assertEqual(set(result['module_spec_results']), {s['module_name'] for s in self.manifest['module_specs']})
        for name in self.manifest['modules_without_specs']:
            self.assertEqual(result['module_spec_status'][name], 'NO_MODULE_SPEC')
        self.assertEqual(result['configuration'], self.manifest['configuration'])

    def test_dynamic_growth_and_multiple_modules_per_unrelated_filename(self):
        initial_files = len(self.manifest['rtl_files'])
        initial_modules = len(self.manifest['module_definitions'])
        for count in (1, 17, 32):
            with self.subTest(additional_files=count):
                for index in range(count):
                    path = self.manifest_path.parent / ('generated_chunk_{}.sv'.format(index))
                    path.write_text('module grown_parent_{i}(input logic clk, input logic rst_n); '
                                    'grown_leaf_{i} leaf(.clk(clk),.rst_n(rst_n)); endmodule\n'
                                    'module grown_leaf_{i}(input logic clk, input logic rst_n); endmodule\n'.format(i=index))
                # Replace the appended growth instances on each iteration.
                top = self.top_path()
                source = re.sub(r'grown_parent_\d+ growth_\d+\([^;]*\);', '', top.read_text())
                top.write_text(source)
                self.append_top('\n'.join('grown_parent_{i} growth_{i}(.clk(clk),.rst_n(rst_n));'.format(i=i) for i in range(count)))
                self.rebuild()
                result = self.validate()
                self.assertTrue(result['is_valid'], result['errors'])
                self.assertEqual(len(result['rtl_files']), initial_files + count)
                self.assertEqual(result['generated_module_count'], initial_modules + 2 * count)
                self.assertEqual(len(result['reachable_modules']), initial_modules + 2 * count)
                self.assertEqual(self.compiler.call_count, 1)

    def test_missing_child_includes_context(self):
        self.append_top('absent_child u_missing(.clk(clk));')
        self.rebuild()
        result = self.validate()
        self.assertFalse(result['is_valid'])
        self.assertIn('SYSTEM_MISSING_DEPENDENCY', result['failure_categories'])
        self.assertEqual(result['missing_modules'], [{
            'parent_module': self.manifest['top_module'], 'child_module': 'absent_child',
            'source_file': str(self.top_path()), 'instance_name': 'u_missing'}])

    def test_duplicate_declaration_blocks_even_with_different_filenames(self):
        module = self.manifest['module_definitions'][0]
        self.add_rtl('another_definition.sv', (self.manifest_path.parent / module['file']).read_text())
        result = self.validate()
        self.assertFalse(result['is_valid'])
        self.assertIn('SYSTEM_DUPLICATE_MODULE', result['failure_categories'])
        self.assertEqual(result['duplicate_modules'][0]['name'], module['name'])
        self.assertEqual(len(result['duplicate_modules'][0]['files']), 2)

    def test_unused_optional_module_is_warning_and_still_compiled(self):
        self.add_rtl('optional.sv', 'module unused_optional(input logic clk); endmodule')
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertEqual(result['unused_generated_modules'], ['unused_optional'])
        self.assertTrue(any('Unused' in w for w in result['warnings']))
        self.assertIn(str(self.manifest_path.parent / 'optional.sv'), self.compiler.call_args[0][0])

    def test_enabled_feature_unreachable_is_integration_failure(self):
        for feature in ('refresh_controller', 'tFAW_tracker'):
            with self.subTest(feature=feature):
                name = self.remove_instance(feature)
                result = self.validate()
                self.assertFalse(result['is_valid'])
                self.assertIn(name, result['unused_generated_modules'])
                self.assertTrue(any(d['category'] == 'SYSTEM_CONFIG_INTEGRATION_FAILED' and d.get('module') == name
                                    for d in result['failure_details']))

    def test_top_missing_is_specific_failure(self):
        path = self.top_path()
        path.write_text(path.read_text().replace('module ddr4_controller_top', 'module alternate_top'))
        result = self.validate()
        self.assertIn('SYSTEM_TOP_NOT_FOUND', result['failure_categories'])
        self.assertFalse(result['is_valid'])

    def test_relocated_snapshot_does_not_use_original_paths(self):
        destination = Path(self.temp.name) / 'snapshot with spaces'
        snapshot = json.loads(publish_snapshot(self.manifest_path, destination).read_text())
        shutil.rmtree(str(self.root))
        self.manifest_path = destination / snapshot['system_manifest']
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertTrue(all(str(destination) in p for p in result['rtl_files']))

    def test_compile_and_elaboration_failures_propagate_with_saved_log(self):
        result = self.validate(('vcs', False, 'Error: unresolved module during elaboration'))
        self.assertFalse(result['compile_passed'])
        self.assertFalse(result['is_valid'])
        self.assertIn('SYSTEM_RTL_COMPILE_FAILED', result['failure_categories'])
        self.assertIn('SYSTEM_ELABORATION_FAILED', result['failure_categories'])
        self.assertEqual(Path(result['compile_log']).read_text(), result['compile_output'])
        self.assertEqual(result['verification_readiness'], 'BLOCKED_SYSTEM_AGENT_1')

    def test_compiler_unavailable_blocks_readiness(self):
        result = self.validate((None, False, 'No VCS or iverilog executable was found.'))
        self.assertFalse(result['is_valid'])
        self.assertIsNone(result['compile_tool'])
        self.assertIn('SYSTEM_RTL_COMPILE_FAILED', result['failure_categories'])

    def test_bad_named_ports_and_duplicate_parameter_overrides(self):
        self.append_top('test_leaf #(.WIDTH(32), .WIDTH(32)) u_test(.bad_name(txn_wdata));')
        self.add_rtl('test_leaf.sv', 'module test_leaf #(parameter int WIDTH=32)(input logic [WIDTH-1:0] data); endmodule')
        result = self.validate()
        self.assertIn('SYSTEM_INTERFACE_FAILED', result['failure_categories'])
        self.assertTrue(any('Unknown binding name: bad_name' in e for e in result['errors']))
        self.assertTrue(any('Duplicate binding: WIDTH' in e for e in result['errors']))

    def test_widths_use_instance_overrides_and_reject_clear_mismatch(self):
        self.append_top('test_leaf #(.WIDTH(DATA_WIDTH)) u_test(.data(txn_wdata));')
        self.add_rtl('test_leaf.sv', 'module test_leaf #(parameter int WIDTH=7)(input logic [WIDTH-1:0] data); endmodule')
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.top_path().write_text(self.top_path().read_text().replace('.WIDTH(DATA_WIDTH)', '.WIDTH(7)'))
        result = self.validate()
        self.assertIn('SYSTEM_INTERFACE_FAILED', result['failure_categories'])
        self.assertTrue(any('width 7 connected to width 32' in e for e in result['errors']))

    def test_unknown_typedef_width_deferred_instead_of_scalar(self):
        self.append_top('test_leaf u_test(.data(selected_req));')
        self.add_rtl('test_leaf.sv', 'module test_leaf(input logic [50:0] data); endmodule')
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertTrue(any('u_test: width/type of port data' in d for d in result['deferred_checks']))
        self.assertIsNone(declaration_width({'name': 'data', 'custom_type': 'request_t'}, {}))

    def test_non_ansi_leaf_width_is_deferred_without_false_scalar_mismatch(self):
        self.append_top('test_leaf u_test(.data(txn_wdata));')
        self.add_rtl('test_leaf.sv', 'module test_leaf(data); input logic [31:0] data; endmodule')
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertTrue(any('u_test: width/type of port data' in d for d in result['deferred_checks']))

    def test_direction_and_constant_clock_failures(self):
        self.append_top('test_leaf u_test(.clk(1\'b0),.rst_n(rst_n),.data(txn_valid));')
        self.add_rtl('test_leaf.sv', 'module test_leaf(input logic clk, input logic rst_n, output logic data); endmodule')
        result = self.validate()
        self.assertIn('SYSTEM_CLOCK_RESET_FAILED', result['failure_categories'])
        self.assertIn('SYSTEM_INTERFACE_FAILED', result['failure_categories'])

    def test_primitives_and_procedural_calls_are_not_missing_modules(self):
        self.append_top('wire primitive_net; and gate_0(primitive_net, txn_valid, rst_n);\n'
                        'function automatic int calculate(input int x); return x; endfunction\n')
        self.rebuild()
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        self.assertEqual(result['missing_modules'], [])

    def test_recursive_hierarchy_blocks_readiness(self):
        self.append_top('recursive_leaf u_recurse(.clk(clk));')
        self.add_rtl('recurse.sv', 'module recursive_leaf(input logic clk); recursive_leaf again(.clk(clk)); endmodule')
        result = self.validate()
        self.assertIn('SYSTEM_HIERARCHY_FAILED', result['failure_categories'])
        self.assertFalse(result['is_valid'])

    def test_multi_instance_and_positional_bindings(self):
        self.append_top('test_leaf a(clk,rst_n), b(clk,rst_n);')
        self.add_rtl('test_leaf.sv', 'module test_leaf(input logic clk, input logic rst_n); endmodule')
        result = self.validate()
        self.assertTrue(result['is_valid'], result['errors'])
        top = next(r for r in result['module_definitions'] if r['name'] == result['top_module'])
        self.assertEqual([i['name'] for i in top['instantiated_modules'] if i['module'] == 'test_leaf'], ['a', 'b'])

    def test_static_only_yaml_comparison_preserves_module_behavior_gate(self):
        spec_row = next(s for s in self.manifest['module_specs'] if 'tFAW' in s['module_name'])
        name = spec_row['module_name']
        module = next(m for m in self.manifest['module_definitions'] if m['name'] == name)
        spec = agent1.load_yaml(str(self.manifest_path.parent / spec_row['path']))
        source = agent1.read_text(str(self.manifest_path.parent / module['file']))
        full = agent1.compare_yaml_rtl(name, source, spec)
        structural = agent1.compare_yaml_rtl(name, source, spec, structural_only=True)
        self.assertFalse(full[0])
        self.assertTrue(structural[0], structural[1])
        self.assertTrue(any('combinational_behavior' in e for e in full[1]))

    def test_module_diagnostic_still_requires_matching_yaml(self):
        source = self.manifest_path.parent / 'diagnostic_only.sv'
        source.write_text('module diagnostic_only(input logic clk); endmodule')
        output = Path(self.temp.name) / 'module_result.json'
        result = agent1.validate_rtl(str(source), str(output), top_module='diagnostic_only')
        self.assertFalse(result['is_valid'])
        self.assertEqual(result['error_code'], 'YAML_SPEC_NOT_FOUND')
        self.assertTrue(any('yaml' in e.lower() for e in result['issues']))

    def test_matching_yaml_module_diagnostic_still_passes(self):
        row = next(s for s in self.manifest['module_specs'] if s['module_name'] == 'ddr4_request_queue')
        definition = next(m for m in self.manifest['module_definitions'] if m['name'] == row['module_name'])
        source = str(self.manifest_path.parent / definition['file'])
        with patch.object(agent1, 'compile_rtl', return_value=('vcs', True, 'mocked module compile')) as compiler:
            result = agent1.validate_rtl(source, str(Path(self.temp.name) / 'module_result.json'),
                                         top_module=row['module_name'],
                                         yaml_path=str(self.manifest_path.parent / row['path']),
                                         rtl_files_override=[source])
        self.assertTrue(result['is_valid'], result['issues'])
        self.assertEqual(compiler.call_count, 1)

    def test_declared_library_cell_is_deferred_to_compiler(self):
        self.append_top('library_cell u_cell(.clk(clk));')
        self.rebuild()
        self.manifest['external_modules'] = ['library_cell']
        self.write_manifest()
        result = self.validate(('vcs', False, 'Unknown module library_cell during elaboration'))
        self.assertEqual(result['missing_modules'], [])
        self.assertIn('SYSTEM_ELABORATION_FAILED', result['failure_categories'])
        self.assertFalse(result['is_valid'])

    def test_direct_system_cli_never_claims_functional_verification(self):
        from system_rtl_validation import run_system_agent
        with patch.object(agent1, 'compile_rtl', return_value=('vcs', True, 'mocked compile')):
            with patch.dict(os.environ, {run_pipeline.REPORT_RUN_ENV: ''}):
                with contextlib.redirect_stdout(io.StringIO()):
                    run_system_agent(agent1, str(self.manifest_path), str(Path(self.temp.name) / 'direct.json'))
        summary = json.loads((self.manifest_path.parent / 'verification_reports' / 'latest.json').read_text())
        self.assertEqual(summary['overall_status'], 'PASS')
        self.assertFalse(summary['verification_complete'])
        self.assertFalse(summary['ready_for_backend'])

    def test_system_pipeline_requests_only_agent1_and_stops_on_pass_and_fail(self):
        discovery = discover_system(self.manifest_path)
        for passed in (True, False):
            with self.subTest(passed=passed):
                def execute(stage, command, run_dir, environment):
                    self.assertEqual(stage, 'agent1')
                    self.assertIn('--system', command)
                    self.assertEqual(command[command.index('--out') + 1], environment['RTL_VALIDATION_RESULT_FILE'])
                    result = {'mode': 'system', 'is_valid': passed,
                              'verification_readiness': 'READY_FOR_SYSTEM_AGENT_2_AND_AGENT_3' if passed else 'BLOCKED_SYSTEM_AGENT_1'}
                    run_pipeline.write_report_json(environment['RTL_VALIDATION_RESULT_FILE'], result)
                    reporter = run_pipeline.AgentReport('agent1', run_dir=run_dir)
                    reporter.update(verification_status='PASS' if passed else 'FAIL')
                    reporter.finish(0 if passed else 1)
                    return 0 if passed else 1
                with patch.object(run_pipeline, 'execute_reported_process', side_effect=execute) as process:
                    with patch.object(run_pipeline, 'run_pipeline_stages', side_effect=AssertionError('Module pipeline must not run')):
                        with contextlib.redirect_stdout(io.StringIO()):
                            if passed:
                                run_pipeline.run_system_pipeline(self.manifest_path, discovery, stop_after_agent1=True)
                            else:
                                with self.assertRaises(SystemExit) as exit_code:
                                    run_pipeline.run_system_pipeline(self.manifest_path, discovery, stop_after_agent1=True)
                                self.assertEqual(exit_code.exception.code, 1)
                self.assertEqual(process.call_count, 1)
                summary = json.loads((self.manifest_path.parent / 'verification_reports' / 'latest.json').read_text())
                self.assertEqual(summary['requested_stages'], ['agent1'])
                self.assertFalse(summary['verification_complete'])
                self.assertFalse(summary['ready_for_backend'])
                self.assertEqual(summary['stages']['agent2']['status'], 'SKIPPED')
                self.assertEqual(summary['stages']['agent3']['status'], 'SKIPPED')

    def test_pipeline_auto_and_explicit_modes_enter_system_agent1(self):
        for arguments in ([str(self.root)], [str(self.manifest_path.parent), '--system']):
            with self.subTest(arguments=arguments), patch.object(sys, 'argv', ['run_pipeline.py'] + arguments):
                with patch.object(run_pipeline, 'run_system_pipeline') as system:
                    with contextlib.redirect_stdout(io.StringIO()):
                        run_pipeline.main()
                self.assertEqual(system.call_count, 1)

    def test_discovery_failure_prevents_agent1(self):
        self.manifest['rtl_files'][0] = 'missing_input.sv'
        self.write_manifest()
        with patch.object(sys, 'argv', ['run_pipeline.py', str(self.root), '--system']):
            with patch.object(run_pipeline, 'run_system_pipeline') as system:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    run_pipeline.main()
                system.assert_not_called()

    def test_real_cli_unavailable_compiler_saves_failure_report(self):
        # Uses the actual CLI/report subprocess path, without any installed HDL
        # compiler. This proves absence cannot become structural PASS.
        environment = os.environ.copy()
        environment['PATH'] = str(Path(self.temp.name) / 'empty-tool-path')
        environment.pop(run_pipeline.REPORT_RUN_ENV, None)
        cli = Path(agent1.__file__).resolve()
        output = Path(self.temp.name) / 'cli_result.json'
        process = subprocess.run([sys.executable, '-B', str(cli), str(self.manifest_path), '--system', '--out', str(output)],
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(process.returncode, 1, process.stdout + process.stderr)
        result = json.loads(output.read_text())
        self.assertIn('SYSTEM_RTL_COMPILE_FAILED', result['failure_categories'])
        self.assertIn('AGENT 1 SYSTEM RESULT : FAIL', process.stdout)
        reports = list(self.manifest_path.parent.glob('verification_reports/*/agent1/report.json'))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text())
        self.assertEqual(report['verification_status'], 'FAIL')
        self.assertIn('compile.log', report['artifacts'])


class ConstantExpressionTests(unittest.TestCase):
    def test_safe_known_and_unknown_expressions(self):
        self.assertEqual(integer_value('$clog2(DEPTH+1)', {'DEPTH': 7}), 3)
        self.assertEqual(integer_value("3'd3", {}), 3)
        for expression in ('UNKNOWN+1', "WIDTH'(3)", "4'bx001", '$bits(request_t)', 'obj.attr'):
            self.assertIsNone(integer_value(expression, {}))
        self.assertIsNone(declaration_width({'name': 'data', 'width_text': '[UNKNOWN-1:0]'}, {}))
        self.assertIsNone(declaration_width({'name': 'data', 'direction': None}, {}))

    def test_existing_compiler_uses_full_file_list_top_and_isolated_directory(self):
        from types import SimpleNamespace
        files = ['/tmp/source with spaces/one.sv', '/tmp/source with spaces/two.sv']
        with tempfile.TemporaryDirectory() as work:
            for available, expected_tool in (({'vcs', 'iverilog'}, 'vcs'), ({'iverilog'}, 'iverilog')):
                with self.subTest(tool=expected_tool):
                    with patch.object(agent1.shutil, 'which', side_effect=lambda n: '/tool/' + n if n in available else None):
                        with patch.object(agent1.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='compiled', stderr='')) as compiler:
                            tool, passed, _ = agent1.compile_rtl(files, 'ddr4_controller_top', work_dir=work, working_directory=work)
                    self.assertEqual(tool, expected_tool)
                    self.assertTrue(passed)
                    self.assertEqual(compiler.call_count, 1)
                    command = compiler.call_args[0][0]
                    self.assertEqual(command[-len(files):], files)
                    top_flag = '-top' if expected_tool == 'vcs' else '-s'
                    self.assertEqual(command[command.index(top_flag) + 1], 'ddr4_controller_top')
                    self.assertEqual(compiler.call_args[1]['cwd'], work)


if __name__ == '__main__':
    unittest.main()
