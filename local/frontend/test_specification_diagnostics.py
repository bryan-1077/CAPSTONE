"""Aggregate independent errors without accepting invalid candidates or cascading schema failures."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from architecture import ROOT, fingerprint
from generate_specification import propose_specification
from specification import (SpecificationError, SpecificationErrors, load_package, read_document, validate_contents)
from test_generate_specification import register_fixture

SOURCE = ROOT / 'microarch/specification/ddr4_fifo/package.yaml'


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.package, self.documents, self.library = load_package(SOURCE)

    def validate(self):
        return validate_contents(self.package, self.documents, self.library)

    def break_ownership_and_dependencies(self):
        rule_id, rule = next(iter(self.documents['system']['rules'].items()))
        owner = rule['owners'][0]
        ref = 'system.rules.' + rule_id
        self.documents['modules']['contracts'][owner]['invariants'].remove(ref)
        dependency_paths = []
        for name, contract in list(self.documents['modules']['contracts'].items())[:2]:
            out = next(iter(contract['combinational_dependencies']))
            contract['combinational_dependencies'][out] = [out]
            dependency_paths.append(name + '.' + out)
        return ref, owner, dependency_paths

    def test_reports_ownership_and_every_bad_dependency_together_without_duplicates(self):
        ref, owner, paths = self.break_ownership_and_dependencies()
        snapshots = []
        for _ in range(2):
            with self.assertRaises(SpecificationErrors) as caught:
                self.validate()
            errors = caught.exception.errors
            snapshots.append(errors)
            self.assertTrue(any(ref in e and f'owner {owner} lacks' in e for e in errors))
            for path in paths:
                self.assertTrue(any(path in e and 'dependency must name an input' in e for e in errors))
            self.assertEqual(len(errors), len(set(errors)))
        self.assertEqual(*snapshots)

    def test_independent_graph_and_metadata_errors_are_not_hidden(self):
        self.break_ownership_and_dependencies()
        target = next(iter(self.documents['graph']['connections'].values()))
        self.documents['graph']['connections']['top.missing_source'] = copy.deepcopy(target)
        decision = next(iter(self.package['decisions'].values()))
        decision['value'] = 123
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        text = str(caught.exception)
        self.assertIn('owner ', text)
        self.assertIn('dependency must name an input', text)
        self.assertIn('Unknown graph endpoint top.missing_source', text)
        self.assertIn('expected nonempty text', text)

    def test_unknown_references_produce_diagnostics_without_key_errors(self):
        rule_id, rule = next(iter(self.documents['system']['rules'].items()))
        rule['owners'].append('missing_owner')
        rule['verification'].append('missing_obligation')
        obligation = next(iter(self.documents['system']['verification'].values()))
        obligation['rules'].append('missing_rule')
        contract = next(iter(self.documents['modules']['contracts'].values()))
        contract['verification'].append('system.verification.unknown')
        with self.assertRaises(SpecificationErrors) as caught:
            self.validate()
        self.assertIn('unknown rule owners', str(caught.exception))
        self.assertIn('missing_obligation', str(caught.exception))
        self.assertIn('missing_rule', str(caught.exception))
        self.assertIn('unknown verification reference', str(caught.exception))

    def test_malformed_contract_stops_before_relational_checks(self):
        contract = next(iter(self.documents['modules']['contracts'].values()))
        contract['interfaces'] = []
        with patch('specification.validate_ownership') as ownership, patch('specification.validate_graph') as graph:
            with self.assertRaisesRegex(SpecificationError, 'interfaces: expected a mapping'):
                self.validate()
        ownership.assert_not_called()
        graph.assert_not_called()

    def test_all_contract_mapping_and_clock_wiring_shapes_are_reported_together(self):
        original = copy.deepcopy(self.documents)
        selected = list(self.documents['modules']['contracts'])[:2]
        for name in selected:
            contract = self.documents['modules']['contracts'][name]
            contract['owned_state'] = 'State description in the wrong field shape.'
            contract['simultaneous_events'] = 'Reset wins.'
        clock = self.documents['graph']['clock_reset']
        clock['source'] = {'clock': clock.pop('clock_source'), 'reset': clock.pop('reset_source')}
        broken = copy.deepcopy(self.documents)
        with patch('specification.validate_ownership') as ownership, patch('specification.validate_graph') as graph:
            for _ in range(2):
                with self.assertRaises(SpecificationErrors) as caught:
                    self.validate()
                errors = caught.exception.errors
                self.assertEqual(len(errors), 5)
                for name in selected:
                    self.assertTrue(any(name + '.owned_state:' in e for e in errors))
                    self.assertTrue(any(name + '.simultaneous_events:' in e for e in errors))
                self.assertTrue(any('graph.clock_reset:' in e for e in errors))
            ownership.assert_not_called()
            graph.assert_not_called()
        self.assertEqual(self.documents, broken)
        self.assertNotEqual(self.documents, original)

    def test_missing_and_malformed_contracts_do_not_cascade(self):
        contracts = self.documents['modules']['contracts']
        selected = list(contracts)[:2]
        contracts[selected[0]] = None
        contracts[selected[1]].pop('owned_state')
        with patch('specification.validate_ownership') as ownership:
            with self.assertRaises(SpecificationErrors) as caught:
                self.validate()
        self.assertEqual(len(caught.exception.errors), 2)
        ownership.assert_not_called()

    def test_valid_package_still_produces_evidence_without_behavioral_claim(self):
        evidence = self.validate()
        self.assertEqual(evidence['specification_validation'], 'passed')
        self.assertEqual(evidence['behavioral_verification'], 'not_run')


class RepairDiagnosticTests(unittest.TestCase):
    def test_one_repair_sees_both_errors_and_journal_keeps_structured_diagnostics(self):
        _, request, responses = register_fixture()
        broken = copy.deepcopy(responses['architecture'])
        contract = next(iter(broken['modules']['contracts'].values()))
        missing_invariant = contract['invariants'].pop()
        output = next(iter(contract['combinational_dependencies']))
        contract['combinational_dependencies'][output] = [output]
        fixed = responses['architecture']['modules']['contracts']['stage']
        edits = {'base_fingerprint': fingerprint(broken), 'edits': [
            {'op': 'set', 'path': '/modules/contracts/stage/invariants', 'value': fixed['invariants']},
            {'op': 'set', 'path': '/modules/contracts/stage/combinational_dependencies/' + output,
             'value': fixed['combinational_dependencies'][output]}]}
        replies = iter([responses['system'], broken, edits])
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return json.dumps(next(replies))
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            package = propose_specification(request, output_dir=root / 'source', caller=caller)
            self.assertEqual(len(prompts), 3)
            self.assertIn(missing_invariant, prompts[-1])
            self.assertIn('lacks invariant/verification obligation', prompts[-1])
            self.assertIn('dependency must name an input', prompts[-1])
            journal = json.loads((package.parent / 'attempts.json').read_text())
            rejected = journal['attempts'][1]
            self.assertGreaterEqual(len(rejected['diagnostics']), 2)
            self.assertEqual(json.loads(rejected['response']), broken)
            proof = read_document(package)['generation']
            self.assertEqual(proof['validation_repairs'][0]['diagnostics'], rejected['diagnostics'])
            self.assertFalse(list(root.rglob('*.sv')))
            self.assertFalse(list(root.rglob('*approval*')))

    def test_three_failed_repairs_still_stop_without_publishing_package(self):
        _, request, responses = register_fixture()
        broken = copy.deepcopy(responses['architecture'])
        contract = next(iter(broken['modules']['contracts'].values()))
        contract['invariants'].pop()
        output = next(iter(contract['combinational_dependencies']))
        contract['combinational_dependencies'][output] = [output]
        # Well-formed edits that leave both compiler faults unresolved must
        # exhaust the limit without publishing, rather than fail on protocol.
        unchanged = {'base_fingerprint': fingerprint(broken), 'edits': [
            {'op': 'set', 'path': '/modules/contracts/stage/combinational_dependencies/' + output,
             'value': [output]}]}
        replies = iter([responses['system'], broken, unchanged, unchanged])
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            with self.assertRaisesRegex(SpecificationError, 'architecture specification failed validation'):
                propose_specification(request, output_dir=root / 'source', caller=lambda *args: json.dumps(next(replies)))
            report = json.loads((root / 'source.failure.json').read_text())
            self.assertEqual(len(report['validation_repairs']), 3)
            self.assertTrue(all(len(r['diagnostics']) >= 2 for r in report['validation_repairs']))
            self.assertEqual(report['accepted_stages'], ['system'])
            self.assertFalse((root / 'source').exists())


if __name__ == '__main__':
    unittest.main()
