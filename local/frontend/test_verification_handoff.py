"""Frontend handoff contracts/inventory tests; no downstream agents are run."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from architecture import ROOT, lower_architecture
from specification import SpecificationError, read_document, validate_contents
from specification_lowering import _compile_snapshot, compile_package, create_approval, emit_review
from validator import validate_spec
from verification_bindings import elaborate_bindings
from verification_handoff import build_handoff, emit_handoff, validate_handoff


SOURCE = ROOT / 'microarch/specification/ddr4_fifo/package.yaml'


class VerificationHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.manifest = compile_package(SOURCE)
        cls.baseline = Path(cls.temporary.name) / 'baseline'
        emit_review(cls.manifest, cls.baseline / 'architecture.yaml')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.review = self.root / 'review'
        shutil.copytree(self.baseline, self.review)
        self.handoff = self.review / 'architecture.handoff.json'

    def bindings(self, documents=None):
        snapshot = self.manifest['provenance']['specification']['snapshot']
        evidence = validate_contents(snapshot['package'], snapshot['documents'], snapshot['timing_library'],
                                     snapshot['overrides'], snapshot['profile'])
        return elaborate_bindings(documents or snapshot['documents'], evidence['parameters'], evidence['type_widths'])

    def build_fixture(self):
        build = self.root / 'build'
        approval = self.root / 'fixture-approval.json'
        approval.write_text(json.dumps(create_approval(self.manifest, 'offline-handoff-test')))
        specs = lower_architecture(self.manifest, build, specification_approval=approval)
        ir_directory = build / 'microarch/ir'
        ir_directory.mkdir()
        for path in specs:
            spec = validate_spec(str(path))
            (ir_directory / (spec.design_name + '_ir.json')).write_text(json.dumps(asdict(spec)))
            (build / 'rtl_output' / (spec.design_name + '.sv')).write_text(
                f'module {spec.design_name}; endmodule // Inventory fixture, no controller behavior.\n')
        return build, specs

    def test_review_exports_typed_bindings_and_independent_unverified_intent(self):
        result = validate_handoff(self.handoff)
        self.assertEqual(result['handoff_validation'], 'passed')
        self.assertEqual(result['phase'], 'review')
        self.assertEqual(result['behavioral_verification'], 'not_run')
        document = read_document(self.handoff)
        intent = document['verification_intent']
        self.assertEqual(len(intent['requirements']), 19)
        self.assertEqual(len(intent['obligations']), 21)
        self.assertEqual(len(intent['bindings']['checks']), 21)
        self.assertEqual(intent['bindings']['events']['retirement']['when']['all'][1]['any'][0]['lsb'], 50)
        self.assertEqual(intent['bindings']['events']['retirement']['when']['all'][1]['any'][0]['equals'], 3)
        self.assertEqual(intent['bindings']['observations']['candidate_command']['dut_path_suffix'], 'command_engine.candidate_command')
        self.assertEqual(document['identity']['rtl_revision'], None)
        self.assertEqual(document['rtl_files'], [])
        self.assertTrue(all(o['result'] == 'unverified' for o in intent['obligations'].values()))
        self.assertFalse(list(self.review.rglob('*.sv')))

    def test_consumer_gap_and_unobservable_events_remain_explicit(self):
        document = read_document(self.handoff)
        self.assertEqual(document['consumer_compatibility']['status'], 'adapter_required')
        codes = {r['code'] for r in document['consumer_compatibility']['blocking_reasons']}
        self.assertIn('PRIVATE_WRAPPER_OBSERVATIONS', codes)
        self.assertIn('FIXED_BEHAVIOR_AND_REQUIREMENTS', codes)
        self.assertIn('ISOLATED_BUILD_TRANSPORT', codes)
        bindings = document['verification_intent']['bindings']
        self.assertEqual(bindings['events']['refresh_due']['status'], 'unbound')
        self.assertEqual(bindings['events']['refresh_end']['status'], 'unbound')
        self.assertEqual(len(bindings['limitations']), 2)
        self.assertEqual(bindings['sampling']['phase'], 'pre_edge')
        self.assertTrue(bindings['sampling']['reset_gates_events'])

    def test_bad_endpoints_fields_enums_and_comparison_widths_fail(self):
        for change in ('endpoint', 'field', 'enum', 'width'):
            documents = copy.deepcopy(self.manifest['provenance']['specification']['snapshot']['documents'])
            events = documents['verification']['events']
            if change == 'endpoint':
                events['dispatch']['payload']['request']['endpoint'] = 'command_engine.private_active_register'
            elif change == 'field':
                events['refresh_start']['when']['all'][1]['field'] = 'missing'
            elif change == 'enum':
                events['refresh_start']['when']['all'][1]['equals'] = 'MISSING'
            else:
                events['admission']['when']['all'][0]['equals'] = 2
            with self.subTest(change=change), self.assertRaises(SpecificationError):
                self.bindings(documents)

    def test_missing_event_check_or_unknown_observation_is_rejected(self):
        for change in ('event', 'check', 'observation'):
            documents = copy.deepcopy(self.manifest['provenance']['specification']['snapshot']['documents'])
            bindings = documents['verification']
            if change == 'event':
                del bindings['events']['dispatch']
            elif change == 'check':
                del bindings['checks']['system.verification.V_FIFO']
            else:
                bindings['checks']['system.verification.V_FIFO']['observations'] = ['unavailable']
            with self.subTest(change=change), self.assertRaises(SpecificationError):
                self.bindings(documents)

    def test_new_event_requires_a_declared_binding_instead_of_a_name_heuristic(self):
        documents = copy.deepcopy(self.manifest['provenance']['specification']['snapshot']['documents'])
        documents['shared']['events']['new_service_event'] = {'condition': 'Reviewed new event.', 'effect': 'Reviewed update.'}
        with self.assertRaisesRegex(SpecificationError, 'Every shared event'):
            self.bindings(documents)
        documents['verification']['events']['new_service_event'] = {'status': 'bound',
            'when': {'endpoint': 'command_engine.commit_valid', 'equals': 1}, 'payload': {}}
        self.assertIn('new_service_event', self.bindings(documents)['events'])

    def test_renamed_architecture_ports_bind_from_data(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        def rename(value):
            if isinstance(value, dict):
                return {key.replace('command_engine', 'issue_unit'): rename(item) for key, item in value.items()}
            if isinstance(value, list):
                return [rename(item) for item in value]
            if isinstance(value, str):
                return value.replace('command_engine', 'issue_unit')
            return value
        snapshot['documents'] = rename(snapshot['documents'])
        manifest, _ = _compile_snapshot(snapshot)
        # A role name is vocabulary, not an instance name; restore its canonical value.
        module = next(m for m in manifest['modules'] if m['instance'] == 'issue_unit')
        self.assertEqual(module['roles'], ['command_engine'])
        output = self.root / 'renamed/architecture.yaml'
        emit_review(manifest, output)
        data = read_document(output.with_suffix('.handoff.json'))
        observed = data['verification_intent']['bindings']['observations']['candidate_command']
        self.assertEqual(observed['dut_path_suffix'], 'issue_unit.candidate_command')

    def test_single_bank_configuration_and_profile_use_resolved_values(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['overrides'] = {'BANK_COUNT': 1, 'WAITING_CAPACITY': 3}
        snapshot['profile'] = 'DDR4-2400'
        manifest, _ = _compile_snapshot(snapshot)
        output = self.root / 'single-bank/architecture.yaml'
        emit_review(manifest, output)
        document = read_document(output.with_suffix('.handoff.json'))
        self.assertNotIn('txn_bank', {p['name'] for p in document['design']['top_ports']})
        self.assertEqual(document['shared']['parameters']['WAITING_CAPACITY'], 3)
        self.assertEqual(document['shared']['timing_profile']['values']['tRFC'], 420)
        self.assertEqual(validate_handoff(output.with_suffix('.handoff.json'))['handoff_validation'], 'passed')

    def test_handoff_relocates_and_detects_changed_spec_files(self):
        moved = self.root / 'moved review'
        shutil.move(self.review, moved)
        path = moved / self.handoff.name
        self.assertEqual(validate_handoff(path)['handoff_validation'], 'passed')
        spec = next((moved / 'module_specs').rglob('*.yaml'))
        spec.write_text(spec.read_text() + '\n# modified after export\n')
        with self.assertRaisesRegex(SpecificationError, 'hash changed'):
            validate_handoff(path)

    def test_forged_intent_rejected_even_after_rehashing_document(self):
        from architecture import fingerprint
        document = read_document(self.handoff)
        document['verification_intent']['requirements']['system.rules.REQ_FIFO']['statement'] = 'Allow arbitrary order.'
        del document['handoff_fingerprint']
        document['handoff_fingerprint'] = fingerprint(document)
        self.handoff.write_text(json.dumps(document))
        with self.assertRaisesRegex(SpecificationError, 'differs from the reviewed'):
            validate_handoff(self.handoff)

    def test_rtl_handoff_authenticates_inventory_approval_ir_and_relocation(self):
        build, specs = self.build_fixture()
        output = emit_handoff(self.manifest, build / 'architecture.yaml', specs, phase='rtl', output=build / 'verification_handoff.json')
        document = read_document(output)
        self.assertEqual(len(document['rtl_files']), 9)
        self.assertTrue(document['identity']['rtl_revision'])
        self.assertEqual(document['execution']['uvm'], 'not_run')
        self.assertEqual(document['execution']['vectors'], 'not_run')
        self.assertEqual(document['result_contract']['requirement_keys'], list(document['verification_intent']['requirements']))
        self.assertEqual(validate_handoff(output)['handoff_validation'], 'passed')
        moved = self.root / 'relocated build'
        shutil.move(build, moved)
        self.assertEqual(validate_handoff(moved / output.name)['handoff_validation'], 'passed')
        rtl = moved / document['rtl_files'][0]
        rtl.write_text(rtl.read_text() + '\n// changed RTL revision\n')
        with self.assertRaisesRegex(SpecificationError, 'hash changed'):
            validate_handoff(moved / output.name)

    def test_missing_rtl_wrong_filelist_stale_ir_and_duplicate_definitions_fail(self):
        build, specs = self.build_fixture()
        filelist = build / 'rtl_output/filelist.f'
        original = filelist.read_text()
        filelist.write_text(original + 'unrelated.sv\n')
        with self.assertRaisesRegex(SpecificationError, 'filelist differs'):
            build_handoff(self.manifest, build / 'architecture.yaml', specs, phase='rtl')
        filelist.write_text(original)
        name = self.manifest['modules'][0]['spec']['design_name']
        ir = build / 'microarch/ir' / (name + '_ir.json')
        previous = ir.read_text()
        ir.write_text(json.dumps({'design_name': name}))
        with self.assertRaisesRegex(SpecificationError, 'IR differs'):
            build_handoff(self.manifest, build / 'architecture.yaml', specs, phase='rtl')
        ir.write_text(previous)
        rtl = build / 'rtl_output' / (name + '.sv')
        original_rtl = rtl.read_text()
        rtl.unlink()
        with self.assertRaisesRegex(SpecificationError, 'Missing'):
            build_handoff(self.manifest, build / 'architecture.yaml', specs, phase='rtl')
        rtl.write_text(original_rtl)
        rtl.write_text(f'module {name}; endmodule\nmodule {name}; endmodule\n')
        with self.assertRaisesRegex(SpecificationError, 'primary RTL module|Duplicate RTL'):
            build_handoff(self.manifest, build / 'architecture.yaml', specs, phase='rtl')

    def test_missing_and_symlinked_inputs_fail(self):
        spec = next((self.review / 'module_specs').rglob('*.yaml'))
        content = spec.read_text()
        spec.unlink()
        with self.assertRaisesRegex(SpecificationError, 'Missing'):
            validate_handoff(self.handoff)
        outside = self.root / 'outside.yaml'
        outside.write_text(content)
        spec.symlink_to(outside)
        with self.assertRaisesRegex(SpecificationError, 'symlinks'):
            validate_handoff(self.handoff)


if __name__ == '__main__':
    unittest.main()
