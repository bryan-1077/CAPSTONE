"""Offline structural regressions; none assert controller behavior is verified."""
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from specification import (SpecificationError, elaborate_parameters, expression,
                           read_document, validate_matrix, validate_package)


SOURCE = Path(__file__).parent / 'microarch/specification/ddr4_fifo'


class SpecificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / 'specification/ddr4_fifo/package.yaml'
        shutil.copytree(SOURCE, self.package.parent)
        timing = self.root / 'jedec'
        timing.mkdir()
        shutil.copy(SOURCE.parent.parent / 'jedec/jedec_dictionary.yaml', timing)

    def change(self, document, mutate):
        path = self.package.parent / document
        value = read_document(path)
        mutate(value)
        path.write_text(yaml.safe_dump(value, sort_keys=False))

    def test_full_matrix_preserves_types_timing_and_unverified_status(self):
        results = validate_matrix(self.package)
        self.assertEqual(len(results), 36)
        for result in results:
            self.assertEqual(result['modules'], 8)
            self.assertEqual(result['rules'], 19)
            self.assertEqual(result['verification_obligations'], 21)
            self.assertEqual(result['type_widths']['request'], 51)
            self.assertEqual(result['type_widths']['command'], 53)
            self.assertEqual(result['parameters']['ACTIVE_CAPACITY'], 1)
            self.assertEqual(result['specification_validation'], 'passed')
            self.assertEqual(result['behavioral_verification'], 'not_run')
            expected = 420 if result['profile'] == 'DDR4-2400' else 560
            self.assertEqual(result['timing_cycles']['tRFC'], expected)

    def test_unknown_fields_and_duplicate_yaml_keys_fail(self):
        self.change('shared.yaml', lambda d: d.update(typo={}))
        with self.assertRaisesRegex(SpecificationError, 'unknown fields.*typo'):
            validate_package(self.package)
        self.package.write_text('revision: one\nrevision: two\n')
        with self.assertRaisesRegex(SpecificationError, 'Duplicate'):
            read_document(self.package)

    def test_parameters_reject_invalid_fixed_and_derived_overrides(self):
        for override in ({'BANK_COUNT': 3}, {'WAITING_CAPACITY': 0}, {'WAITING_CAPACITY': True},
                         {'ACTIVE_CAPACITY': 2}, {'HOST_BANK_WIDTH': 8}, {'UNKNOWN': 1}):
            with self.subTest(override=override), self.assertRaises(SpecificationError):
                validate_package(self.package, override)
        with self.assertRaisesRegex(SpecificationError, 'cyclic'):
            elaborate_parameters({'A': {'derive': 'ceil_log2(B)'}, 'B': {'derive': 'ceil_log2(A)'}}, {})

    def test_expression_vocabulary_cannot_execute_python(self):
        for source in ("__import__('os').system('false')", '2**10000000', 'BANK_COUNT + 1'):
            with self.subTest(source=source), self.assertRaises(SpecificationError):
                expression(source, {'BANK_COUNT': 4})

    def test_unknown_and_cyclic_type_references_fail(self):
        self.change('shared.yaml', lambda d: d['types']['request']['fields'][0].update(type='shared.types.missing'))
        with self.assertRaisesRegex(SpecificationError, 'Unknown type reference'):
            validate_package(self.package)
        self.change('shared.yaml', lambda d: d['types']['request']['fields'][0].update(type='shared.types.request'))
        with self.assertRaisesRegex(SpecificationError, 'Cyclic type'):
            validate_package(self.package)

    def test_enum_encodings_and_semantic_type_mismatches_fail(self):
        self.change('shared.yaml', lambda d: d['types']['opcode']['encodings'].update(WRITE=3))
        with self.assertRaisesRegex(SpecificationError, 'enum encoding'):
            validate_package(self.package)
        self.change('shared.yaml', lambda d: d['types']['opcode']['encodings'].update(WRITE=4))
        # Same packed width still does not make unrelated types interchangeable.
        self.change('shared.yaml', lambda d: d['types'].update(other_bit={'kind': 'unsigned', 'width': 1}))
        self.change('module_contracts.yaml', lambda d: d['contracts']['host_adapter']['interfaces']['txn_valid'].update(type='shared.types.other_bit'))
        with self.assertRaisesRegex(SpecificationError, 'Type/width mismatch'):
            validate_package(self.package)

    def test_rule_owners_and_reciprocal_obligations_fail_when_missing(self):
        self.change('module_contracts.yaml', lambda d: d['contracts']['waiting_queue']['verification'].remove('system.verification.V_ACTIVE'))
        with self.assertRaisesRegex(SpecificationError, 'owner waiting_queue lacks'):
            validate_package(self.package)
        self.change('request_lifecycle.yaml', lambda d: d['rules']['REQ_FIFO']['owners'].append('missing'))
        with self.assertRaisesRegex(SpecificationError, 'unknown rule owners'):
            validate_package(self.package)

    def test_undriven_and_multiple_driver_graphs_fail(self):
        self.change('connections.yaml', lambda d: d['connections'].pop('top.txn_valid'))
        with self.assertRaisesRegex(SpecificationError, 'Undriven graph sinks'):
            validate_package(self.package)
        self.change('connections.yaml', lambda d: d['connections'].update({'top.txn_valid': ['host_adapter.txn_valid'], 'top.txn_is_write': ['host_adapter.txn_is_write', 'host_adapter.txn_valid']}))
        with self.assertRaisesRegex(SpecificationError, 'Multiple drivers'):
            validate_package(self.package)

    def test_unknown_graph_endpoints_and_partial_conditional_wiring_fail(self):
        self.change('connections.yaml', lambda d: d['connections'].update({'top.typo': []}))
        with self.assertRaisesRegex(SpecificationError, 'Unknown graph endpoint'):
            validate_package(self.package)
        self.change('connections.yaml', lambda d: d['connections'].pop('top.typo'))
        self.change('connections.yaml', lambda d: d['connections'].update({'top.txn_bank': ['host_adapter.txn_valid']}))
        with self.assertRaisesRegex(SpecificationError, 'omit both ends'):
            validate_package(self.package, {'BANK_COUNT': 1})

    def test_clock_reset_fanout_and_declared_combinational_cycles_fail(self):
        self.change('connections.yaml', lambda d: d['clock_reset']['consumers'].remove('timing'))
        with self.assertRaisesRegex(SpecificationError, 'every contract'):
            validate_package(self.package)
        self.change('connections.yaml', lambda d: d['clock_reset']['consumers'].append('timing'))
        # Permission depending on its accepted commit creates the forbidden loop.
        self.change('module_contracts.yaml', lambda d: d['contracts']['timing']['combinational_dependencies']['candidate_ok'].append('commit_valid'))
        with self.assertRaisesRegex(SpecificationError, 'combinational feedback cycle'):
            validate_package(self.package)

    def test_invalid_timing_bindings_and_profiles_fail(self):
        with self.assertRaisesRegex(SpecificationError, 'Unsupported timing profile'):
            validate_package(self.package, profile='missing')
        self.change('shared.yaml', lambda d: d['timing_profile']['bindings'].update(tRFC='timing_cycles.missing'))
        with self.assertRaisesRegex(SpecificationError, 'unresolved timing binding'):
            validate_package(self.package)

    def test_package_document_escape_is_rejected(self):
        self.change('package.yaml', lambda d: d['documents'].update(shared='../outside.yaml'))
        with self.assertRaisesRegex(SpecificationError, 'stay inside'):
            validate_package(self.package)


if __name__ == '__main__':
    unittest.main()
