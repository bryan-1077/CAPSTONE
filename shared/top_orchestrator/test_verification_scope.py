"""Scope protocol tests use synthetic evidence, never verification agents."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

import yaml

from . import test_specification_mailbox as fixtures
from .verification_scope import draft_routing, build_plan, validate_plan, accept_results, export_bundle, RESULT_SCHEMA


class VerificationScopeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SpecificationMailboxTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        spec_path = fixture.build / 'microarch/gen_exp/unit.yaml'
        spec = yaml.safe_load(spec_path.read_text())
        spec['verification_requirements'][0]['owners'] = ['unit', 'other']
        spec['verification_requirements'].append({'id': 'META', 'text': 'Package metadata', 'owners': ['unit']})
        spec['implementation_constraints'] = ['Exact interfaces']
        spec['invariants'] = ['R1 contributes to complete system behavior']
        spec_path.write_text(yaml.safe_dump(spec))
        self.mailbox = fixture.publish()
        self.routing = draft_routing(self.mailbox, 'unit')
        self.routing['review'] = {'status': 'approved', 'reviewer': 'synthetic-test-reviewer',
                                  'rationale': 'Fixture explicitly distinguishes metadata, structure and runtime.'}
        for key in self.routing['sources']:
            self.routing['sources'][key] = {'scope': 'system', 'checks': [], 'reason': 'Full composed behavior is deferred.'}
        self.routing['sources']['behavior.contract'] = {'scope': 'module', 'checks': ['local_reset'],
                                                        'reason': 'Complete local fixture contract is reset-only.'}
        self.routing['sources']['META'] = {'scope': 'metadata', 'checks': ['metadata'], 'reason': 'Inspect package metadata.'}
        self.routing['sources']['yaml.implementation_constraints.0'] = {
            'scope': 'structural', 'checks': ['structure'], 'reason': 'Compare elaborated interfaces.'}
        self.routing['checks'] = [
            {'id': 'local_reset', 'scope': 'module', 'method': 'simulation', 'description': 'Reset local fixture state.',
             'source_ids': ['behavior.contract', 'R1'], 'contract_facets': ['/behavior/contract/reset'], 'assumptions': []},
            {'id': 'metadata', 'scope': 'metadata', 'method': 'metadata', 'description': 'Inspect package identity.',
             'source_ids': ['META'], 'contract_facets': [], 'assumptions': []},
            {'id': 'structure', 'scope': 'structural', 'method': 'structural', 'description': 'Compare interfaces.',
             'source_ids': ['yaml.implementation_constraints.0'], 'contract_facets': [], 'assumptions': []}]
        self.plan = build_plan(self.mailbox, self.routing)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        (self.evidence / 'fixture.log').write_text('Synthetic executed-check evidence fixture, not a real simulation.\n')
        artifact = {'path': 'fixture.log', 'sha256': hashlib.sha256((self.evidence / 'fixture.log').read_bytes()).hexdigest()}
        self.result = {'schema': RESULT_SCHEMA, 'schema_version': 1, 'identity': deepcopy(self.plan['identity']),
                       'plan_sha256': self.plan['plan_sha256'], 'consumer_capability': self.plan['consumer_capability'],
                       'scope': 'module_checks_only', 'backend_ready': False,
                       'executed_tests': [{'id': 'fixture-reset', 'method': 'simulation', 'status': 'passed',
                                           'execution_completed': True, 'execution_succeeded': True,
                                           'evidence': [deepcopy(artifact)]}],
                       'checks': [{'id': row['id'], 'method': row['method'], 'status': 'passed',
                                   'test_ids': ['fixture-reset'] if row['method'] == 'simulation' else [],
                                   'evidence': [deepcopy(artifact)]} for row in self.plan['checks']],
                       'source_results': {key: {'status': 'unverified' if row['scope'] == 'system' else 'passed',
                                                'check_ids': row['checks']}
                                          for key, row in self.plan['assignments'].items()}}

    def accept(self, result=None):
        return accept_results(self.mailbox, self.plan, result or self.result, self.evidence)

    def test_metadata_structure_and_local_behavior_pass_with_system_pending(self):
        receipt = self.accept()
        self.assertEqual(receipt['status'], 'module_checks_passed')
        self.assertIn('R1', receipt['deferred_system_sources'])
        self.assertFalse(receipt['backend_ready'])
        self.assertEqual(receipt['system_verification'], 'not_accepted')
        self.assertEqual(validate_plan(self.mailbox, self.plan), self.plan)

    def test_draft_and_unresolved_routes_cannot_establish_acceptance(self):
        draft = draft_routing(self.mailbox, 'unit')
        with self.assertRaisesRegex(ValueError, 'explicit scope review'):
            build_plan(self.mailbox, draft)
        draft['review'] = self.routing['review']
        draft['checks'] = self.routing['checks']
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            build_plan(self.mailbox, draft)

    def test_source_loss_and_unknown_checks_rejected(self):
        for mode in ('missing', 'invented', 'bad_check'):
            routing = deepcopy(self.routing)
            if mode == 'missing':
                routing['sources'].pop('R1')
            elif mode == 'invented':
                routing['sources']['invented'] = routing['sources']['R1']
            else:
                routing['sources']['META']['checks'] = ['unknown']
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                build_plan(self.mailbox, routing)

    def test_system_source_cannot_be_certified_by_local_contribution(self):
        routing = deepcopy(self.routing)
        routing['sources']['R1']['checks'] = ['local_reset']
        with self.assertRaisesRegex(ValueError, 'cannot certify'):
            build_plan(self.mailbox, routing)
        result = deepcopy(self.result)
        result['source_results']['R1']['status'] = 'passed'
        with self.assertRaisesRegex(ValueError, 'claims deferred coverage'):
            self.accept(result)

    def test_whole_contract_cannot_be_replaced_by_metadata_or_narrow_check(self):
        routing = deepcopy(self.routing)
        routing['checks'][0].update(scope='structural', method='structural')
        # Retain another simulation check to exercise facet method validation.
        routing['checks'].append(dict(self.routing['checks'][0], id='unrelated_sim', contract_facets=[]))
        with self.assertRaisesRegex(ValueError, 'facets require simulation'):
            build_plan(self.mailbox, routing)
        routing = deepcopy(self.routing)
        routing['checks'][0]['contract_facets'] = []
        with self.assertRaisesRegex(ValueError, 'facets lack'):
            build_plan(self.mailbox, routing)

    def test_wrong_identity_capability_or_backend_claim_rejected(self):
        for key, value in [('plan_sha256', 'stale'), ('consumer_capability', 'old-reader'),
                           ('backend_ready', True), ('scope', 'system')]:
            result = deepcopy(self.result)
            result[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.accept(result)

    def test_results_require_all_checks_exactly_once_and_simulation_method(self):
        for mode in ('missing', 'duplicate', 'method', 'unexecuted', 'no_test'):
            result = deepcopy(self.result)
            if mode == 'missing': result['checks'].pop()
            elif mode == 'duplicate': result['checks'].append(result['checks'][0])
            elif mode == 'method': result['checks'][0]['method'] = 'static'
            elif mode == 'unexecuted': result['checks'][0]['status'] = 'unverified'
            else: result['checks'][0]['test_ids'] = []
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.accept(result)

    def test_evidence_traversal_symlinks_and_hash_changes_rejected(self):
        for path in ('../escape', '/absolute', 'missing.log'):
            result = deepcopy(self.result)
            result['checks'][0]['evidence'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.accept(result)
        link = self.evidence / 'link.log'
        link.symlink_to(self.evidence / 'fixture.log')
        result = deepcopy(self.result)
        result['checks'][0]['evidence'][0]['path'] = 'link.log'
        with self.assertRaises(ValueError): self.accept(result)
        (self.evidence / 'fixture.log').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed evidence'):
            self.accept()

    def test_check_ids_must_resolve_to_completed_simulation_records(self):
        for mode in ('missing', 'incomplete', 'unbound'):
            result = deepcopy(self.result)
            if mode == 'missing': result['executed_tests'] = []
            elif mode == 'incomplete': result['executed_tests'][0]['execution_completed'] = False
            else: result['executed_tests'][0]['evidence'] = []
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.accept(result)

    def test_plan_source_objects_and_assignments_are_authenticated(self):
        for field in ('sources', 'assignments'):
            plan = deepcopy(self.plan)
            plan[field].pop('R1')
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_plan(self.mailbox, plan)

    def test_multi_owner_full_source_cannot_be_relabelled_module_local(self):
        self.assertIn('R1', self.plan['composition_sources'])
        routing = deepcopy(self.routing)
        routing['sources']['R1'].update(scope='module', checks=['local_reset'])
        with self.assertRaisesRegex(ValueError, 'Multi-owner'):
            build_plan(self.mailbox, routing)

    def test_bundle_preserves_input_pair_and_binds_plan_separately(self):
        from .specification_mailbox import verify_module_probe
        output = self.root / 'bundle'
        request = export_bundle(self.mailbox, self.plan, output)
        self.assertFalse(request['backend_ready'])
        self.assertEqual(request['plan_sha256'], self.plan['plan_sha256'])
        pair = verify_module_probe(output / 'input', self.mailbox)
        self.assertEqual(pair['module_name'], 'unit')
        self.assertEqual((output / 'input/specs/unit.yaml').read_bytes(), (self.mailbox / 'specs/unit.yaml').read_bytes())
        with self.assertRaises(FileExistsError):
            export_bundle(self.mailbox, self.plan, output)
