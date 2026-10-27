"""Independent wiring diagnostics; invalid partial graphs never reach later checks."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from specification import SpecificationError, SpecificationErrors, validate_graph
from test_generate_specification import register_fixture


class GraphDiagnosticTests(unittest.TestCase):
    def setUp(self):
        _, _, responses = register_fixture()
        self.docs = {'shared': responses['system']['shared'],
                     'modules': responses['architecture']['modules'],
                     'graph': responses['architecture']['graph']}
        self.parameters = {'DEPTH': 1}
        self.widths = {'bit': 1, 'byte': 8}

    def validate(self):
        return validate_graph(self.docs, self.parameters, self.widths)

    def test_independent_edge_faults_report_together_without_mutation_or_cascades(self):
        connections = self.docs['graph']['connections']
        connections['top.missing'] = ['stage.missing']
        connections['top.in_data'].append('stage.in_valid')  # Type mismatch.
        connections['stage.in_valid'] = ['top.out_data']  # Wrong source direction.
        connections['stage.out_valid'].append('stage.in_valid')  # Duplicate driver.
        connections['stage.out_data'] += ['top.out_valid', 'stage.in_data']
        before = copy.deepcopy(self.docs)
        snapshots = []
        with patch('specification.validate_dependencies') as dependencies, patch('specification.dependency_cycle') as cycle:
            for _ in range(2):
                with self.assertRaises(SpecificationErrors) as caught:
                    self.validate()
                errors = caught.exception.errors
                self.assertEqual(len(errors), 7)
                for expected in ('Unknown graph endpoint top.missing', 'Unknown graph endpoint stage.missing',
                                 'Type/width mismatch: top.in_data -> stage.in_valid',
                                 'Wrong graph direction: stage.in_valid -> top.out_data',
                                 'Multiple drivers for stage.in_valid',
                                 'Type/width mismatch: stage.out_data -> top.out_valid',
                                 'Multiple drivers for stage.in_data'):
                    self.assertIn(expected, errors)
                self.assertNotIn('Undriven', str(caught.exception))
                self.assertNotIn('unused sources', str(caught.exception))
                snapshots.append(errors)
            dependencies.assert_not_called()
            cycle.assert_not_called()
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(self.docs, before)

    def test_malformed_and_empty_sink_lists_do_not_hide_other_sources(self):
        self.docs['graph']['connections'].update({
            'stage.out_valid': None, 'stage.out_data': [],
            'top.missing': ['stage.missing'], 'top.in_valid': ['stage.in_valid', 'stage.in_valid']})
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        errors = caught.exception.errors
        self.assertEqual(len(errors), 5)
        self.assertIn('stage.out_valid: expected a list of unique names', errors)
        self.assertIn('top.in_valid: expected a list of unique names', errors)
        self.assertIn('stage.out_data: connection needs at least one sink', errors)

    def test_repeated_unknown_endpoint_is_deduplicated(self):
        self.docs['graph']['connections']['top.in_valid'].append('stage.missing')
        self.docs['graph']['connections']['top.in_data'].append('stage.missing')
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        self.assertEqual(caught.exception.errors, ['Unknown graph endpoint stage.missing'])

    def add_conditional_ports(self):
        ports = self.docs['modules']['contracts']['stage']['interfaces']
        for name, direction in [('optional_out', 'output'), ('optional_in', 'input')]:
            ports[name] = {'direction': direction, 'type': 'shared.types.bit', 'when': 'DEPTH > 1'}
            self.docs['graph']['presence']['stage.' + name] = 'DEPTH > 1'
        self.docs['modules']['contracts']['stage']['combinational_dependencies']['optional_out'] = []

    def test_partial_conditional_edges_report_together(self):
        self.add_conditional_ports()
        connections = self.docs['graph']['connections']
        connections['stage.optional_out'] = ['stage.in_valid']
        connections['top.in_valid'].append('stage.optional_in')
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        self.assertEqual(len(caught.exception.errors), 2)
        self.assertTrue(all('Conditional connection must omit both ends' in e for e in caught.exception.errors))

    def test_fully_absent_conditional_connection_still_valid(self):
        self.add_conditional_ports()
        self.docs['graph']['connections']['stage.optional_out'] = ['stage.optional_in']
        self.assertEqual(self.validate(), 4)

    def test_missing_and_unused_coverage_reported_after_valid_edges(self):
        del self.docs['graph']['connections']['top.in_valid']
        del self.docs['graph']['connections']['stage.out_data']
        with self.assertRaises(SpecificationError) as caught:
            self.validate()
        message = str(caught.exception)
        for endpoint in ('stage.in_valid', 'top.out_data', 'top.in_valid', 'stage.out_data'):
            self.assertIn(endpoint, message)
        self.assertIn('Undriven graph sinks', message)
        self.assertIn('unused sources', message)

    def test_saved_duplicate_driver_precedes_declaration_checks(self):
        fixture = json.loads((Path(__file__).parent / 'testdata/verification_output_conflicts.json').read_text())
        self.docs = fixture['documents']
        self.parameters, self.widths = fixture['parameters'], fixture['widths']
        # Restore the exact extra edge from architecture attempt 1 of the saved
        # 232854Z run onto its attempt-2 projection. Other declarations stay bad.
        self.docs['graph']['connections']['host_adapter.enq_request'].append('command_engine.dispatch_request')
        before = copy.deepcopy(self.docs)
        with self.assertRaises(SpecificationErrors) as caught:
            validate_graph(self.docs, self.parameters, self.widths, fixture['bindings'])
        self.assertEqual(caught.exception.errors, ['Multiple drivers for command_engine.dispatch_request'])
        self.assertEqual(self.docs, before)
        self.docs['graph']['connections']['host_adapter.enq_request'].remove('command_engine.dispatch_request')
        with self.assertRaises(SpecificationErrors) as caught:
            validate_graph(self.docs, self.parameters, self.widths, fixture['bindings'])
        self.assertEqual(len(caught.exception.errors), 13)


if __name__ == '__main__':
    unittest.main()
