"""Offline staged-generation and frontend-only build regressions.

The small register fixture intentionally has no controller decomposition. Its
cached RTL tests transport/IR/lint, never target-controller behavioral correctness.
"""
import copy
import hashlib
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

from architecture import ROOT, emit_module_specs, fingerprint
from generate_specification import (UnsupportedSpecification, generation_context,
                                    build_prompt, propose_specification, validate_system)
from generate_microarch import parse_response
from specification import SpecificationError, read_document, validate_matrix
from specification_lowering import compile_package, create_approval, emit_review
from specification_request import build_specification_request
from verification_handoff import validate_handoff


def register_fixture():
    config = {'design_name': 'register_top', 'depth': 1,
              'requirements': ['Register each valid input byte for delivery at the next rising edge.']}
    request = build_specification_request(config)
    bit = {'kind': 'unsigned', 'width': 1}
    def port(direction, kind='bit'):
        return {'direction': direction, 'type': 'shared.types.' + kind}
    inputs = {'in_valid': port('input'), 'in_data': port('input', 'byte')}
    outputs = {'out_valid': port('output'), 'out_data': port('output', 'byte')}
    rules = {r['id']: {'origin': 'request.' + r['id'], 'statement': r['text'],
                      'verification': ['V_' + r['id']]} for r in request['requirements']}
    obligations = {'V_' + rid: {'rules': [rid], 'scenario': 'Present bytes on consecutive valid input edges, then reset.',
                   'expected': 'Output valid/data represent the preceding sampled input; reset cancels validity and zeros data.',
                   'status': 'planned_not_executed'} for rid in rules}
    system = {
        'status': 'supported',
        'package': {
            'specification_version': 1, 'package_id': 'byte_pipeline', 'design_name': 'register_top',
            'revision': 'generated_001', 'status': 'draft_for_review',
            'scope': {'plan': 'request', 'milestone': 'first_pipeline', 'covered': ['register'], 'pending': []},
            'verification_strategy': {'status': 'planned_not_executed',
                'configurations': {'parameters': {}, 'profiles': ['DDR4-3200']},
                'module_checks': 'Check boundary data and reset.', 'system_scenario': 'Consecutive bytes.',
                'system_evidence': 'Independent edge-based sampling.'},
            'decisions': {'D_RESET': {'origin': 'model_selected', 'status': 'recorded',
                         'value': 'synchronous', 'reason': 'Supported deterministic edge convention.'}},
            'assumptions': {}, 'open_questions': {},
        },
        'shared': {
            'parameters': {'DEPTH': {'value': 1, 'configurable': True, 'allowed': [1]}},
            'clock_reset': {'clock': 'clk', 'edge': 'rising', 'reset': 'rst_n', 'active_level': 0,
                'priority': 'reset_before_all_events',
                'assertion_style': 'synchronous_for_all_stateful_modules_in_this_example',
                'handshake_gating': 'Reset gates all valid events.'},
            'types': {'bit': bit, 'byte': {'kind': 'unsigned', 'width': 8}},
            'host_interface': {'clk': port('input'), 'rst_n': port('input'), **inputs, **outputs},
            'events': {'capture': {'condition': 'in_valid at a reset-free rising edge.',
                                   'effect': 'Register the input byte for delivery at the next edge.'}},
            'timing_profile': {'source': 'timing.yaml', 'selection': 'DDR4-3200',
                'supported_profiles': ['DDR4-3200'], 'unit': 'cycles', 'bindings': {},
                'note': 'No DDR timing rules apply to this register fixture.'}, 'timing_rules': {},
        },
        'system': {'slice_id': 'pipeline', 'rules': rules, 'verification': obligations},
        'execution': {'slice_id': 'none', 'rules': {}, 'verification': {}},
    }
    architecture = {
        'status': 'supported',
        'modules': {'contracts': {'stage': {'status': 'draft', 'role': 'host_protocol',
            'owned_state': {'valid': 'Previous input validity.', 'data': 'Previous sampled byte.'},
            'interfaces': {**inputs, **outputs}, 'handshakes': {'capture': 'Sample in_valid/data each edge.'},
            'reset': 'Synchronous clear all state and reset-gate out_valid.',
            'update_events': {'every_clock': 'Capture in_valid; capture data only when valid.'},
            'stalls': 'No input backpressure; retain data during invalid cycles.',
            'simultaneous_events': {'reset_and_input': 'Reset wins.'},
            'invariants': ['system.rules.' + rid for rid in rules],
            'verification': ['system.verification.' + vid for vid in obligations],
            'combinational_dependencies': {'out_valid': [], 'out_data': []}}}},
        'graph': {'graph_id': 'pipeline_wiring', 'clock_reset': {'definition': 'shared.clock_reset',
            'clock_source': 'top.clk', 'reset_source': 'top.rst_n', 'consumers': ['stage']},
            'connections': {'top.in_valid': ['stage.in_valid'], 'top.in_data': ['stage.in_data'],
                'stage.out_valid': ['top.out_valid'], 'stage.out_data': ['top.out_data']},
            'presence': {}, 'integration': 'Typed structural wiring only.'},
        'verification': {'binding_version': 1, 'sampling': {'phase': 'pre_edge', 'reset_gates_events': True},
            'events': {'capture': {'status': 'bound', 'when': {'endpoint': 'top.in_valid', 'equals': 1},
                                  'payload': {'data': {'endpoint': 'top.in_data'}}}},
            'observations': {'valid': {'endpoint': 'top.out_valid'}, 'data': {'endpoint': 'top.out_data'}},
            'checks': {'system.verification.' + vid: {'scope': 'both', 'events': ['capture'],
                'observations': ['valid', 'data'], 'note': 'Compare sampled edges independently.'} for vid in obligations}},
        'rule_owners': {'system.rules.' + rid: ['stage'] for rid in rules},
    }
    return config, request, {'system': system, 'architecture': architecture}


class GenerateSpecificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config, self.request, self.response = register_fixture()
        self.replay = self.root / 'responses.json'
        self.replay.write_text(json.dumps(self.response))

    def generate(self, **kwargs):
        return propose_specification(self.request, output_dir=self.root / 'source',
                                     response_path=self.replay, **kwargs)

    def cli(self, *args, **kwargs):
        return subprocess.run([sys.executable, str(ROOT / 'run_flow.py'), *map(str, args)],
                              capture_output=True, text=True, timeout=90, **kwargs)

    def test_two_stage_generation_preserves_request_and_exports_dynamic_intent(self):
        package = self.generate()
        manifest = compile_package(package)
        self.assertEqual(len(manifest['modules']), 1)
        self.assertNotIn('design_kind', manifest)
        proof = read_document(package)['generation']
        self.assertEqual(proof['request'], self.request)
        self.assertEqual(proof['input_fingerprint'], fingerprint(self.request))
        self.assertTrue(all(r['text'] == manifest['requirements'][i]['text']
                            for i, r in enumerate(self.request['requirements'])))
        self.assertEqual(len(validate_matrix(package)), 1)
        review = self.root / 'review/architecture.yaml'
        emit_review(manifest, review)
        handoff = validate_handoff(review.with_suffix('.handoff.json'))
        self.assertEqual(handoff['handoff_validation'], 'passed')
        self.assertFalse(list(self.root.rglob('*.sv')))
        self.assertFalse(list(self.root.rglob('*approval*')))

    def test_omitted_and_rewritten_request_constraints_fail_before_architecture_call(self):
        for mutation in ('omit', 'rewrite'):
            response = copy.deepcopy(self.response)
            rid = self.request['requirements'][-1]['id']
            if mutation == 'omit':
                del response['system']['system']['rules'][rid]
                del response['system']['system']['verification']['V_' + rid]
            else:
                response['system']['system']['rules'][rid]['statement'] = 'Weaker constraint.'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(SpecificationError, 'missing or rewritten'):
                validate_system(response['system'], self.request, generation_context()['timing_library'])

    def test_model_cannot_fabricate_review_or_edit_locked_system_during_decomposition(self):
        response = copy.deepcopy(self.response)
        response['system']['package']['review'] = {'outcome': 'approved'}
        with self.assertRaisesRegex(SpecificationError, 'no review may be fabricated'):
            validate_system(response['system'], self.request, generation_context()['timing_library'])
        response = copy.deepcopy(self.response)
        response['architecture']['system'] = {'rules': {}}
        self.replay.write_text(json.dumps(response))
        with self.assertRaisesRegex(SpecificationError, 'unknown fields.*system'):
            self.generate()
        self.assertFalse((self.root / 'source').exists())

    def test_bounded_repairs_keep_system_locked_and_record_evidence(self):
        broken = copy.deepcopy(self.response['architecture'])
        broken['graph']['connections']['top.in_valid'] = ['stage.typo']
        outputs = iter([self.response['system'], broken, self.response['architecture']])
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return json.dumps(next(outputs))
        package = propose_specification(self.request, output_dir=self.root / 'source', caller=caller)
        self.assertEqual(len(prompts), 3)
        self.assertIn('LOCKED SYSTEM', prompts[-1])
        self.assertIn('stage.typo', prompts[-1])
        proof = read_document(package)['generation']
        self.assertEqual(proof['validation_repairs'][0]['stage'], 'architecture')
        self.assertIn('Unknown graph endpoint', proof['validation_repairs'][0]['error'])
        journal = json.loads((package.parent / 'attempts.json').read_text())
        self.assertEqual([a['status'] for a in journal['attempts']], ['accepted', 'rejected', 'accepted'])
        self.assertEqual(journal['attempts'][1]['response'], json.dumps(broken))

    def test_unsupported_requests_report_required_extensions_without_partial_package(self):
        response = {'status': 'unsupported', 'unsupported': [{'requirement_ids': ['R1'],
                    'reason': 'A requested clock domain is unsupported.',
                    'required_extension': 'Add multi-clock specification and integration.'}]}
        with self.assertRaises(UnsupportedSpecification):
            propose_specification(self.request, output_dir=self.root / 'source',
                                  caller=lambda *args: json.dumps(response))
        report = json.loads((self.root / 'source.failure.json').read_text())
        self.assertEqual(report['status'], 'unsupported')
        self.assertEqual(report['rtl_generation'], 'not_run')
        self.assertEqual(report['accepted_stages'], [])
        journal = json.loads((self.root / report['attempts_file']).read_text())
        self.assertEqual(journal['attempts'][0]['status'], 'unsupported')
        self.assertEqual(json.loads(journal['attempts'][0]['response']), response)
        self.assertFalse((self.root / 'source').exists())

    def test_prompt_examples_match_each_stage_and_do_not_require_optional_reuse(self):
        context = generation_context()
        for stage in ('system', 'architecture'):
            prompt = build_prompt(stage, self.request, context)
            shown = json.loads(prompt.split('FORMAT AND CAPABILITIES:\n', 1)[1]
                               .split('\nLOCKED SYSTEM', 1)[0])['format_example']
            self.assertEqual(shown['status'], 'supported')
            if stage == 'system':
                self.assertEqual(set(shown), {'status', 'package', 'shared', 'system', 'execution'})
                self.assertFalse({'documents', 'design_kind', 'generation', 'review'} & shown['package'].keys())
                self.assertEqual(shown['shared']['clock_reset']['assertion_style'], 'synchronous')
                for section in ('system', 'execution'):
                    self.assertTrue(all('owners' not in rule for rule in shown[section]['rules'].values()))
            else:
                self.assertEqual(set(shown), {'status', 'modules', 'graph', 'verification', 'rule_owners'})
            self.assertIn('does not require mixing reset styles', prompt)
        # Building a stage example cannot edit the source format/reference data.
        self.assertIn('documents', context['format_example']['package'])

    def test_canonical_synchronous_reset_reaches_ir_and_legacy_alias_still_loads(self):
        self.response['system']['shared']['clock_reset']['assertion_style'] = 'synchronous'
        self.replay.write_text(json.dumps(self.response))
        package = self.generate()
        manifest = compile_package(package)
        self.assertTrue(manifest['modules'][0]['spec']['reset']['synchronous'])
        self.assertEqual(manifest['modules'][0]['spec']['behavior']['specification_context']
                         ['clock_reset']['assertion_style'], 'synchronous')
        # The supplied controller still uses the old spelling and remains valid.
        self.assertTrue(compile_package(ROOT / 'microarch/specification/ddr4_fifo/package.yaml'))

    def test_reset_schema_diagnostics_identify_received_and_expected_value(self):
        response = copy.deepcopy(self.response['system'])
        response['shared']['clock_reset']['assertion_style'] = 'mixed_per_block'
        with self.assertRaisesRegex(SpecificationError, 'expected "synchronous".*mixed_per_block'):
            validate_system(response, self.request, generation_context()['timing_library'])
        response = copy.deepcopy(self.response['system'])
        response['package']['documents'] = {'shared': 'shared.yaml'}
        with self.assertRaisesRegex(SpecificationError, "compiler-owned package fields.*documents"):
            validate_system(response, self.request, generation_context()['timing_library'])

    def test_replay_and_cache_require_current_request_and_context(self):
        package = self.generate()
        with patch('generate_specification.call_llm', side_effect=AssertionError('Unexpected API call')):
            self.assertEqual(propose_specification(self.request, output_dir=package.parent, cache=True), package)
            changed = copy.deepcopy(self.request)
            changed['constraints']['depth'] = 2
            with self.assertRaisesRegex(SpecificationError, 'cache is stale'):
                propose_specification(changed, output_dir=package.parent, cache=True)
        with self.assertRaisesRegex(SpecificationError, 'already exists'):
            self.generate()

    def test_saved_snapshot_rejects_edited_stage_one_and_propagates_request_into_ir_context(self):
        package = self.generate()
        manifest = compile_package(package)
        self.assertEqual(manifest['modules'][0]['spec']['behavior']['specification_context']['request'], self.request)
        doc = read_document(package.parent / 'shared.yaml')
        doc['events']['capture']['effect'] = 'Edited after elaboration.'
        (package.parent / 'shared.yaml').write_text(yaml.safe_dump(doc))
        with self.assertRaisesRegex(SpecificationError, 'changed after elaboration'):
            compile_package(package)

    def test_decomposition_can_add_internal_types_but_cannot_replace_system_types(self):
        self.response['architecture']['internal_types'] = {
            'internal_tag': {'kind': 'unsigned', 'width': 3}}
        self.replay.write_text(json.dumps(self.response))
        package = self.generate()
        manifest = compile_package(package)
        types = manifest['modules'][0]['spec']['behavior']['specification_context']['types']
        self.assertEqual(types['internal_tag']['width'], 3)
        self.response['architecture']['internal_types']['byte'] = {'kind': 'unsigned', 'width': 16}
        self.replay.write_text(json.dumps(self.response))
        with self.assertRaisesRegex(SpecificationError, 'cannot replace locked shared types'):
            propose_specification(self.request, output_dir=self.root / 'invalid-source', response_path=self.replay)

    def test_invalid_system_interface_is_repaired_before_decomposition(self):
        broken = copy.deepcopy(self.response['system'])
        broken['shared']['host_interface']['in_data']['type'] = 'shared.types.missing'
        broken_matrix = copy.deepcopy(self.response['system'])
        broken_matrix['package']['verification_strategy']['configurations']['parameters'] = {'UNKNOWN': [1]}
        outputs = iter([broken, broken_matrix, self.response['system'], self.response['architecture']])
        package = propose_specification(self.request, output_dir=self.root / 'source',
                    caller=lambda *args: json.dumps(next(outputs)))
        self.assertEqual(read_document(package)['generation']['validation_repairs'][0]['stage'], 'system')
        self.assertIn('Unknown parameter overrides', read_document(package)['generation']['validation_repairs'][1]['error'])

    def test_duplicate_json_keys_fail_instead_of_silently_replacing_constraints(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            parse_response('{"status":"unsupported","status":"supported"}')

    def test_target_request_has_explicit_first_configuration_and_baseline(self):
        request = build_specification_request(read_document(ROOT / 'microarch/specification/ddr4_fifo/request.yaml'))
        self.assertEqual(request['baseline_contract']['profile'], 'DDR4-3200')
        self.assertEqual(request['constraints']['features']['scheduler'], 'fifo')
        self.assertEqual(request['constraints']['topology']['banks'], 4)
        self.assertEqual(request['baseline_contract']['config']['microarchitecture']['waiting_capacity'], 4)
        self.assertEqual(request['design_kind'], 'memory_controller')

    def test_controller_request_preserves_baseline_through_stages_and_lowering(self):
        context = generation_context()
        example = copy.deepcopy(context['format_example'])
        request = build_specification_request(read_document(ROOT / 'microarch/specification/ddr4_fifo/request.yaml'))
        system = {'status': 'supported', **{key: example[key] for key in ('package', 'shared', 'system', 'execution')}}
        system['shared']['clock_reset']['assertion_style'] = 'synchronous'
        for key in ('documents', 'design_kind'):
            system['package'].pop(key)
        system['package']['scope']['pending'] = []
        for decision in system['package']['decisions'].values():
            decision['status'] = 'recorded'
        owners = {f'{section}.rules.{rid}': rule.pop('owners')
                  for section in ('system', 'execution') for rid, rule in system[section]['rules'].items()}
        architecture = {'status': 'supported', **{key: example[key] for key in ('modules', 'graph', 'verification')},
                        'rule_owners': owners}
        for requirement in request['requirements']:
            rid, vid = requirement['id'], 'V_' + requirement['id']
            rule_ref, check_ref = 'system.rules.' + rid, 'system.verification.' + vid
            system['system']['rules'][rid] = {'origin': 'request.' + rid,
                'statement': requirement['text'], 'verification': [vid]}
            system['system']['verification'][vid] = {'rules': [rid], 'scenario': 'Offline transport fixture.',
                'expected': 'Original requirement remains available for review.', 'status': 'planned_not_executed'}
            owners[rule_ref] = list(architecture['modules']['contracts'])
            for contract in architecture['modules']['contracts'].values():
                contract['invariants'].append(rule_ref)
                contract['verification'].append(check_ref)
            architecture['verification']['checks'][check_ref] = {'scope': 'both', 'events': ['admission'],
                'observations': [], 'note': 'Offline fixture tests requirement transport, not behavioral satisfaction.'}
        replay = self.root / 'controller-responses.json'
        replay.write_text(json.dumps({'system': system, 'architecture': architecture}))
        package = propose_specification(request, output_dir=self.root / 'controller-source', response_path=replay)
        manifest = compile_package(package)
        self.assertEqual(len(manifest['modules']), 8)
        self.assertEqual(manifest['baseline_contract'], request['baseline_contract'])
        system['shared']['host_interface']['txn_wdata']['type'] = 'shared.types.row'
        with self.assertRaisesRegex(ValueError, 'baseline host port missing or changed'):
            validate_system(system, request, context['timing_library'])

    def test_cli_request_export_then_unapproved_build_leaves_rtl_absent(self):
        config = self.root / 'request.yaml'
        config.write_text(yaml.safe_dump(self.config))
        output = self.root / 'review/architecture.yaml'
        result = self.cli(config, '--specification', '--specification-response', self.replay,
                          '--specification-output', self.root / 'source',
                          '--architecture-output', output, '--microarch-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Generated specification review export', result.stdout)
        result = self.cli(output, '--build-dir', self.root / 'blocked')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires revision-bound', result.stdout + result.stderr)
        self.assertFalse((self.root / 'blocked').exists())

    @unittest.skipUnless(shutil.which('verilator'), 'Verilator required')
    def test_frontend_only_cache_run_uses_real_ir_rtl_lint_and_handoff_routes(self):
        package = self.generate()
        manifest = compile_package(package)
        exported = self.root / 'review/architecture.yaml'
        emit_review(manifest, exported)
        approval = self.root / 'fixture-approval.json'
        approval.write_text(json.dumps(create_approval(manifest, 'offline-test-fixture')))
        build = self.root / 'build'
        specs = emit_module_specs(manifest, self.root / 'module_specs')
        digest = hashlib.sha256(specs[0].read_bytes()).hexdigest()
        cache = build / '.llm_cache' / digest
        cache.mkdir(parents=True)
        name = manifest['modules'][0]['spec']['design_name']
        (cache / (name + '.sv')).write_text(f'''module {name} (
          input logic clk, input logic rst_n, input logic in_valid,
          input logic [7:0] in_data, output logic out_valid, output logic [7:0] out_data
        );
          logic valid_q;
          assign out_valid = rst_n && valid_q;
          always_ff @(posedge clk) begin
            if (!rst_n) begin valid_q <= 1'b0; out_data <= '0; end
            else begin valid_q <= in_valid; if (in_valid) out_data <= in_data; end
          end
        endmodule
        ''')
        env = dict(os.environ)
        env.pop('TAMUS_AI_CHAT_API_KEY', None)
        for _ in range(2):
            result = self.cli(exported, '--spec-approval', approval, '--build-dir', build, '--cache', env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads((build / 'build_status.json').read_text())
            self.assertEqual(status['rtl_generation'], 'passed')
            self.assertEqual(status['integration_lint'], 'passed')
            self.assertEqual(status['behavioral_verification'], 'not_run')
            self.assertEqual(validate_handoff(build / 'verification_handoff.json')['handoff_validation'], 'passed')
            self.assertTrue((build / 'microarch/ir' / (name + '_ir.json')).exists())
            self.assertEqual(len(list((build / 'rtl_output').glob('*.sv'))), 2)
            self.assertNotIn('Running expand_spec', result.stdout)
        (cache / (name + '.sv')).write_text('module broken invalid cache;')
        result = self.cli(exported, '--spec-approval', approval, '--build-dir', build, '--cache', env=env)
        self.assertNotEqual(result.returncode, 0)
        status = json.loads((build / 'build_status.json').read_text())
        self.assertEqual(status['rtl_generation'], 'failed')
        self.assertEqual(status['verification_handoff'], 'not_run')
        self.assertFalse((build / 'verification_handoff.json').exists())


if __name__ == '__main__':
    unittest.main()
