"""Compiler/export/approval regressions; generated stubs never verify DDR behavior."""
import copy
from dataclasses import asdict
import itertools
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from architecture import (ArchitectureError, ROOT, lower_architecture,
                          validate_architecture)
from design import generation_route, ROUTE_DUAL_LLM
from generate_microarch import build_request
from ir_to_llm_context import build_llm_context
from specification import SpecificationError, read_document
from specification_lowering import (compile_package, create_approval, emit_review,
                                     parse_overrides, require_approval)
from validator import validate_spec


SOURCE = ROOT / 'microarch/specification/ddr4_fifo/package.yaml'


class SpecificationLoweringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = compile_package(SOURCE)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def approve_fixture(self, manifest=None, **kwargs):
        manifest = self.manifest if manifest is None else manifest
        path = self.root / 'fixture-approval.json'
        path.write_text(json.dumps(create_approval(manifest, 'offline-test-fixture', **kwargs)))
        return path

    def review_question_fixture(self):
        from specification_lowering import _compile_snapshot
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['open_questions']['Q_REVIEW'] = {
            'blocks': 'design_review', 'question': 'Review this exact exported contract before RTL generation.'}
        return _compile_snapshot(snapshot)[0]

    def test_compilation_preserves_ownership_rules_and_packed_abi(self):
        manifest = self.manifest
        self.assertEqual(len(manifest['modules']), 8)
        self.assertEqual(len(manifest['requirements']), 19)
        self.assertEqual(manifest['design_kind'], 'memory_controller')
        specs = {m['instance']: m['spec'] for m in manifest['modules']}
        context = specs['command_engine']['behavior']['specification_context']
        request = {f['name']: (f['msb'], f['lsb']) for f in context['types']['request']['fields']}
        self.assertEqual(request, {'bank': (50, 49), 'row': (48, 39), 'column': (38, 33),
                                   'is_write': (32, 32), 'wdata': (31, 0)})
        command = {f['name']: (f['msb'], f['lsb']) for f in context['types']['command']['fields']}
        self.assertEqual(command['opcode'], (52, 50))
        self.assertEqual(command['bank'], (49, 48))
        self.assertEqual(context['types']['bank_rows']['element_width'], 10)
        self.assertEqual(context['types']['bank_rows']['packed_order'], 'ascending_index_from_lsb')
        self.assertEqual(context['timing_rules']['tRFC']['minimum'], 560)
        self.assertEqual(context['timing_rules']['tFAW']['limit'], 4)
        for spec in specs.values():
            self.assertEqual(spec['design_type'], 'datapath')
            self.assertEqual(spec['reset'], {'name': 'rst_n', 'active_low': True, 'synchronous': True})
            self.assertIn('simultaneous_events', spec['behavior']['contract'])
            self.assertIn('owned_state', spec['behavior']['contract'])
        self.assertEqual(specs['waiting_queue']['combinational_dependencies']['head_valid'], [])
        self.assertNotIn('candidate_ok', specs['command_engine']['combinational_dependencies']['dispatch_ready'])

    def test_configuration_matrix_lowers_through_existing_architecture_validator(self):
        for banks, capacity, profile, policy in itertools.product((1, 2, 4), (1, 3, 4),
                ('DDR4-2400', 'DDR4-3200'), ('open_page', 'close_page')):
            with self.subTest(banks=banks, capacity=capacity, profile=profile, policy=policy):
                manifest = compile_package(SOURCE, {'BANK_COUNT': banks, 'WAITING_CAPACITY': capacity,
                                                     'PAGE_POLICY': policy}, profile)
                ports = {p['name']: p['width'] for p in manifest['top_ports']}
                self.assertEqual(ports.get('txn_bank'), {1: None, 2: 1, 4: 2}[banks])
                adapter = next(m['spec'] for m in manifest['modules'] if m['instance'] == 'host_adapter')
                self.assertEqual('txn_bank' in adapter['behavior']['contract']['interfaces'], banks > 1)
                self.assertEqual('txn_bank' in adapter['combinational_dependencies']['enq_request'], banks > 1)
                context = adapter['behavior']['specification_context']
                self.assertEqual(context['parameters']['WAITING_CAPACITY'], capacity)
                self.assertEqual(context['parameters']['PAGE_POLICY'], policy)
                self.assertEqual(context['timing_profile']['values']['tRFC'], 420 if profile == 'DDR4-2400' else 560)

    def test_exported_yamls_reach_real_ir_and_llm_context_without_rtl(self):
        output = self.root / 'review/architecture.yaml'
        specs = emit_review(self.manifest, output)
        self.assertEqual(len(specs), 8)
        exported = read_document(output)
        self.assertEqual(exported, self.manifest)
        seen_obligations = set()
        for path in specs:
            ir = validate_spec(str(path))
            self.assertEqual(generation_route(ir, ir.design_name), ROUTE_DUAL_LLM)
            context = build_llm_context(ir)
            behavior = context['metadata']['behavior']
            self.assertIn('types', behavior['specification_context'])
            self.assertIn('update_events', behavior['contract'])
            seen_obligations.update(behavior['verification_obligations'])
            self.assertTrue(ir.metadata['verification_requirements'])
        self.assertEqual(len(seen_obligations), 21)
        self.assertFalse(list(self.root.rglob('*.sv')))
        self.assertFalse(list(self.root.rglob('*_ir.json')))
        self.assertFalse(list(self.root.rglob('*approval*')))
        evidence = json.loads(output.with_suffix('.verification.json').read_text())
        self.assertEqual(evidence['behavioral_verification'], 'not_run')
        self.assertTrue(all(r['result'] == 'unverified' for r in evidence['requirements']))
        self.assertEqual(len(evidence['obligations']), 21)
        self.assertTrue(all(o['result'] == 'unverified' for o in evidence['obligations'].values()))

    def test_missing_approval_blocks_lowering_before_output_creation(self):
        output = self.root / 'build'
        with self.assertRaisesRegex(SpecificationError, 'requires revision-bound'):
            lower_architecture(self.manifest, output)
        self.assertFalse(output.exists())

    def test_approval_allows_structural_lowering_and_preserves_source_snapshot(self):
        approval = self.approve_fixture()
        output = self.root / 'build'
        specs = lower_architecture(self.manifest, output, specification_approval=approval)
        self.assertEqual(len(specs), 8)
        wrapper = (output / 'rtl_output/ddr4_controller_top.sv').read_text()
        self.assertNotIn('always_', wrapper)
        self.assertNotIn('bank_mem', wrapper)
        self.assertIn('.enq_request(', wrapper)
        self.assertEqual(read_document(output / 'architecture.yaml'), self.manifest)
        self.assertEqual(len(list(output.rglob('*.sv'))), 1)
        self.assertEqual(json.loads((output / 'specification_approval.json').read_text())['status'], 'approved')
        verification = json.loads((output / 'verification_plan.json').read_text())
        self.assertEqual(len(verification['obligations']), 21)
        self.assertTrue(all(r['result'] == 'unverified' for r in verification['requirement_results']))

    def test_stale_approval_rejects_configuration_and_content_changes(self):
        approval = self.approve_fixture()
        changed = compile_package(SOURCE, {'WAITING_CAPACITY': 3})
        with self.assertRaisesRegex(SpecificationError, 'stale'):
            require_approval(changed, approval)
        package = self.root / 'package/package.yaml'
        shutil.copytree(SOURCE.parent, package.parent)
        shared = read_document(package.parent / 'shared.yaml')
        # Use a portable local timing reference for this modified fixture.
        shared['timing_profile']['source'] = 'timing.yaml'
        (package.parent / 'shared.yaml').write_text(yaml.safe_dump(shared))
        shutil.copy(ROOT / 'microarch/jedec/jedec_dictionary.yaml', package.parent / 'timing.yaml')
        lifecycle = read_document(package.parent / 'request_lifecycle.yaml')
        lifecycle['rules']['REQ_FIFO']['statement'] += ' Additional reviewed constraint.'
        (package.parent / 'request_lifecycle.yaml').write_text(yaml.safe_dump(lifecycle))
        with self.assertRaisesRegex(SpecificationError, 'stale'):
            require_approval(compile_package(package), approval)

    def test_edited_derived_specs_and_dropped_provenance_cannot_bypass_review(self):
        for mutation in ('behavior', 'provenance', 'graph'):
            manifest = copy.deepcopy(self.manifest)
            if mutation == 'behavior':
                manifest['modules'][0]['spec']['behavior']['description'] = 'Changed behavior.'
            elif mutation == 'provenance':
                del manifest['provenance']
            else:
                manifest['requirements'][0]['text'] = 'Weakened requirement.'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ArchitectureError, 'snapshot|regenerate'):
                validate_architecture(manifest)

    def test_unresolved_design_questions_cannot_be_approved(self):
        from specification_lowering import _compile_snapshot
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['open_questions']['Q_POLICY'] = {'blocks': 'design_review', 'question': 'Which ordering policy?'}
        manifest, _ = _compile_snapshot(snapshot)
        with self.assertRaisesRegex(SpecificationError, 'Q_POLICY'):
            create_approval(manifest, 'offline-test-fixture')

    def test_explicit_review_confirmation_is_bound_to_unchanged_export(self):
        from architecture import fingerprint
        from specification_lowering import _compile_snapshot
        manifest = self.review_question_fixture()
        digest = fingerprint(manifest)
        with self.assertRaisesRegex(SpecificationError, 'Q_REVIEW'):
            create_approval(manifest, 'offline-test-fixture')
        approval = self.approve_fixture(manifest, review_confirmations=['Q_REVIEW'])
        recorded = require_approval(manifest, approval)
        self.assertEqual(recorded['approval_version'], 2)
        self.assertEqual(recorded['review_confirmations'], ['Q_REVIEW'])
        self.assertEqual(fingerprint(manifest), digest)
        self.assertEqual(manifest['provenance']['specification']['snapshot']['package']
                         ['open_questions']['Q_REVIEW']['blocks'], 'design_review')
        snapshot = copy.deepcopy(manifest['provenance']['specification']['snapshot'])
        snapshot['overrides']['WAITING_CAPACITY'] = 3
        with self.assertRaisesRegex(SpecificationError, 'stale'):
            require_approval(_compile_snapshot(snapshot)[0], approval)
        recorded.pop('review_confirmations')
        recorded['approval_version'] = 1
        approval.write_text(json.dumps(recorded))
        with self.assertRaisesRegex(SpecificationError, 'Q_REVIEW'):
            require_approval(manifest, approval)

    def test_review_confirmation_does_not_bypass_other_blockers(self):
        from specification_lowering import _compile_snapshot
        manifest = self.review_question_fixture()
        for confirmations in (['MISSING'], ['Q_REVIEW', 'Q_REVIEW'], ['Q_RTL_APPROVAL']):
            with self.subTest(confirmations=confirmations), self.assertRaises(SpecificationError):
                create_approval(manifest, 'offline-test-fixture', review_confirmations=confirmations)
        snapshot = copy.deepcopy(manifest['provenance']['specification']['snapshot'])
        snapshot['package']['decisions']['D_FIFO']['status'] = 'proposed'
        with self.assertRaisesRegex(SpecificationError, 'D_FIFO'):
            create_approval(_compile_snapshot(snapshot)[0], 'offline-test-fixture', review_confirmations=['Q_REVIEW'])
        snapshot = copy.deepcopy(manifest['provenance']['specification']['snapshot'])
        snapshot['package']['assumptions']['A_HOST']['status'] = 'requires_review'
        with self.assertRaisesRegex(SpecificationError, 'A_HOST'):
            create_approval(_compile_snapshot(snapshot)[0], 'offline-test-fixture', review_confirmations=['Q_REVIEW'])

    def test_cli_records_review_confirmation_for_fixture_without_generating_rtl(self):
        manifest = self.review_question_fixture()
        exported = self.root / 'review/architecture.yaml'
        emit_review(manifest, exported)
        approval = self.root / 'fixture-approval.json'
        result = subprocess.run([sys.executable, str(ROOT / 'specification_lowering.py'), str(exported),
            '--record-approval', str(approval), '--reviewer', 'offline-test-fixture',
            '--confirm-review-question', 'Q_REVIEW'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(require_approval(manifest, approval)['review_confirmations'], ['Q_REVIEW'])
        self.assertFalse(list(self.root.rglob('*.sv')))
        self.assertFalse(list(self.root.rglob('*_ir.json')))

    def test_controller_kind_prevents_losing_a_major_role(self):
        from specification_lowering import _compile_snapshot
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['documents']['modules']['contracts']['timing']['role'] = 'host_protocol'
        manifest, _ = _compile_snapshot(snapshot)
        with self.assertRaisesRegex(ArchitectureError, 'separate modules for roles.*timing'):
            validate_architecture(manifest)

    def test_portable_manifest_needs_no_original_package_files(self):
        exported = self.root / 'portable/architecture.yaml'
        emit_review(self.manifest, exported)
        loaded = read_document(exported)
        # Validation and approval use the embedded snapshot, never load_package.
        with patch('specification_lowering.load_package', side_effect=AssertionError('Unexpected source read')):
            validate_architecture(loaded)
            require_approval(loaded, self.approve_fixture(loaded))

    def test_cli_export_and_build_gate_do_not_call_legacy_expansion_or_llm(self):
        output = self.root / 'review/architecture.yaml'
        command = [sys.executable, str(ROOT / 'run_flow.py'), str(SOURCE), '--microarch-only',
                   '--spec-set', 'BANK_COUNT=1', '--spec-set', 'WAITING_CAPACITY=3',
                   '--spec-profile', 'DDR4-2400', '--architecture-output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('8 definitions', result.stdout)
        self.assertNotIn('Running expand_spec', result.stdout)
        self.assertFalse(list(self.root.rglob('*.sv')))
        result = subprocess.run([sys.executable, str(ROOT / 'run_flow.py'), str(output),
                                 '--build-dir', str(self.root / 'blocked')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires revision-bound', result.stdout + result.stderr)
        self.assertFalse((self.root / 'blocked').exists())

    def test_approved_run_flow_invokes_existing_generation_route_in_isolation(self):
        from run_flow import parse_args, run_architecture_flow
        exported = self.root / 'review/architecture.yaml'
        manifest = self.review_question_fixture()
        emit_review(manifest, exported)
        approval = self.approve_fixture(manifest, review_confirmations=['Q_REVIEW'])
        build = self.root / 'build'
        with patch.object(sys, 'argv', ['run_flow.py', str(exported), '--spec-approval', str(approval),
                                       '--build-dir', str(build), '--no-lint']):
            args = parse_args()
        commands = []
        # Exercise orchestration with interface-only fixtures instead of LLM RTL.
        # Controller behavior is explicitly outside this test's assertions.
        def generate_fixture(command):
            commands.append(command)
            self.assertEqual(command[1], 'design.py')
            self.assertEqual(command[-2:], ['--output-root', str(build)])
            spec = validate_spec(command[2])
            (build / 'rtl_output' / (spec.design_name + '.sv')).write_text(
                f'module {spec.design_name}; endmodule // Test fixture, no behavior.\n')
            ir_directory = build / 'microarch/ir'
            ir_directory.mkdir(parents=True, exist_ok=True)
            (ir_directory / (spec.design_name + '_ir.json')).write_text(json.dumps(asdict(spec)))
        with patch('run_flow.run_command', side_effect=generate_fixture):
            self.assertEqual(run_architecture_flow(exported, args), 0)
        self.assertEqual(len(commands), 8)
        status = json.loads((build / 'build_status.json').read_text())
        self.assertEqual(status['behavioral_verification'], 'not_run')
        self.assertEqual(status['specification_approval'], 'approved')
        self.assertEqual(status['integration_lint'], 'skipped')
        self.assertEqual(status['verification_consumer'], 'adapter_required')
        self.assertTrue((build / 'verification_handoff.json').is_file())
        self.assertEqual(json.loads((build / 'specification_approval.json').read_text())['approval_version'], 2)

    def test_invalid_overrides_and_wrong_entry_point_fail_explicitly(self):
        for values in (['WAITING_CAPACITY'], ['=3'], ['WAITING_CAPACITY=3', 'WAITING_CAPACITY=4']):
            with self.subTest(values=values), self.assertRaises(SpecificationError):
                parse_overrides(values)
        with self.assertRaisesRegex(ArchitectureError, 'System-specification packages'):
            build_request(read_document(SOURCE))
        with self.assertRaisesRegex(SpecificationError, 'not configurable'):
            compile_package(SOURCE, {'ACTIVE_CAPACITY': 2})


if __name__ == '__main__':
    unittest.main()
