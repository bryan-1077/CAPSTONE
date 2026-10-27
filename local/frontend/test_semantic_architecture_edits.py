"""Narrow semantic architecture edits: scope, retries and real compiler admission."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from architecture import fingerprint
from generate_specification import propose_specification, build_prompt, generation_context, repair_feedback
from repair_specification import repair_and_review
from review_specification import ASPECTS, REVIEW_REVISION, ReviewChangesRequired
from specification import SpecificationError, read_document
from specification_change_audit import audit_changes
from specification_edits import apply_architecture_edits
from specification_lowering import compile_package, require_approval
from specification_request import system_projection
from test_generate_specification import register_fixture
from test_specification_prompts import repair_context, CANDIDATE_MARKER
from tpm_pacing import estimate_input


def architecture_response(manifest):
    s = manifest['provenance']['specification']['snapshot']
    docs = s['documents']
    return {'status': 'supported', **{k: copy.deepcopy(docs[k]) for k in ('modules', 'graph', 'verification')},
            'internal_types': copy.deepcopy(s['package']['generation']['internal_types']),
            'rule_owners': {f'{section}.rules.{rid}': copy.deepcopy(rule['owners'])
                for section in ('system', 'execution') for rid, rule in docs[section]['rules'].items()}}


class NarrowArchitectureProtocolTests(unittest.TestCase):
    def setUp(self):
        _, self.request, self.responses = register_fixture()
        self.base = copy.deepcopy(self.responses['architecture'])
        self.base.setdefault('internal_types', {})

    def envelope(self, *edits, base=None):
        return {'base_fingerprint': fingerprint(self.base if base is None else base), 'edits': list(edits)}

    def test_nested_scalar_list_add_remove_and_escaped_keys_preserve_everything_else(self):
        self.base['internal_types']['odd/~λ'] = {'description': 'before', 'other': 'preserved'}
        self.base['graph']['temporary_note'] = 'remove me'
        original = copy.deepcopy(self.base)
        edits = self.envelope(
            {'op': 'set', 'path': '/internal_types/odd~1~0λ/description', 'value': 'after'},
            {'op': 'set', 'path': '/graph/connections/top.in_valid', 'value': ['stage.in_valid']},
            {'op': 'set', 'path': '/modules/contracts/stage/dependency_note', 'value': 'Clarified.'},
            {'op': 'remove', 'path': '/graph/temporary_note'})
        result = apply_architecture_edits(self.base, edits, narrow=True)
        self.assertEqual(self.base, original)
        changes = audit_changes(original, result)
        self.assertEqual({c['path'] for c in changes}, {'/internal_types/odd~1~0λ/description',
            '/modules/contracts/stage/dependency_note', '/graph/temporary_note'})
        self.assertEqual(result['internal_types']['odd/~λ']['other'], 'preserved')
        self.assertEqual(result['verification'], original['verification'])
        self.assertEqual(result['rule_owners'], original['rule_owners'])

    def test_wholesale_object_edits_and_remove_then_replace_are_rejected_atomically(self):
        original = copy.deepcopy(self.base)
        for path in ('/modules', '/graph', '/verification', '/internal_types', '/rule_owners',
                     '/modules/contracts', '/modules/contracts/stage', '/graph/connections',
                     '/modules/contracts/stage/interfaces'):
            for op in ('set', 'remove'):
                edit = {'op': op, 'path': path}
                if op == 'set':
                    edit['value'] = {}
                with self.subTest(path=path, op=op), self.assertRaises(SpecificationError):
                    apply_architecture_edits(self.base, self.envelope(
                        {'op': 'set', 'path': '/graph/integration', 'value': 'temporary'}, edit), narrow=True)
                self.assertEqual(self.base, original)
        with self.assertRaises(SpecificationError):
            apply_architecture_edits(self.base, self.envelope(
                {'op': 'remove', 'path': '/modules/contracts/stage'},
                {'op': 'set', 'path': '/modules/contracts/stage', 'value': {'role': 'rewritten'}}), narrow=True)
        self.assertEqual(self.base, original)

    def test_locked_roots_status_list_indices_and_malformed_edits_stay_rejected(self):
        bad = [{'op': 'set', 'path': '/system/rules/R1/statement', 'value': 'weaken'},
               {'op': 'set', 'path': '/package/revision', 'value': 'new'},
               {'op': 'set', 'path': '/shared/events/capture/effect', 'value': 'weaken'},
               {'op': 'set', 'path': '/status', 'value': 'unsupported'},
               {'op': 'set', 'path': '/graph/connections/top.in_valid/0', 'value': 'other'},
               {'op': 'remove', 'path': '/graph/missing'},
               {'op': 'set', 'path': '/graph/~bad', 'value': 'other'}]
        for edit in bad:
            with self.subTest(edit=edit), self.assertRaises(SpecificationError):
                apply_architecture_edits(self.base, self.envelope(edit), narrow=True)
        for response in (self.base, self.envelope(), self.envelope(*([{}] * 101)),
                         {'base_fingerprint': 'stale', 'edits': [{}]}):
            with self.subTest(response=response), self.assertRaises(SpecificationError):
                apply_architecture_edits(self.base, response, narrow=True)

    def test_new_structured_fields_can_be_added_without_replacing_existing_objects(self):
        response = self.envelope({'op': 'set', 'path': '/graph/verification_outputs',
                                  'value': {'stage.out_valid': 'Needed observation.'}})
        result = apply_architecture_edits(self.base, response, narrow=True)
        self.assertEqual([c['path'] for c in audit_changes(self.base, result)], ['/graph/verification_outputs'])

    def test_ordinary_compiler_repair_can_still_replace_object_shapes(self):
        response = self.envelope({'op': 'set', 'path': '/graph/clock_reset',
                                  'value': copy.deepcopy(self.base['graph']['clock_reset'])})
        self.assertEqual(apply_architecture_edits(self.base, response), self.base)
        with self.assertRaises(SpecificationError):
            apply_architecture_edits(self.base, response, narrow=True)

    def test_saved_observation_repair_changes_only_three_binding_fields(self):
        fixture = read_document(Path(__file__).parent / 'testdata/architecture_observation_repair.json')
        base = fixture['architecture_projection']
        before = copy.deepcopy(base)
        path = '/verification/checks/' + fixture['check_id']
        edits = [{'op': 'set', 'path': path + '/' + key, 'value': fixture['after_check'][key]}
                 for key in ('events', 'note', 'observations')]
        result = apply_architecture_edits(base, self.envelope(*edits, base=base), narrow=True)
        self.assertEqual([c['path'] for c in audit_changes(base, result)],
                         [path + '/' + key for key in ('events', 'note', 'observations')])
        self.assertEqual(result['modules'], base['modules'])
        self.assertEqual(result['graph'], base['graph'])
        self.assertEqual(base, before)


class SemanticArchitectureIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        _, request, cls.responses = register_fixture()
        raw = root / 'raw.json'
        raw.write_text(json.dumps(cls.responses))
        with contextlib.redirect_stdout(io.StringIO()):
            cls.package = propose_specification(request, output_dir=root / 'source', response_path=raw)
        cls.manifest = compile_package(cls.package)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.output = Path(temp.name) / 'repair'
        self.base = architecture_response(self.manifest)
        self.prompts = []
        self.review_calls = []
        self.finding = {'id': 'F_OBSERVATION', 'scope': 'architecture', 'resolution': 'design_repair',
            'message': 'Clarify the declared observation note.',
            'references': ['/documents/modules/contracts/stage'], 'requirement_ids': []}
        self.report = {'review_version': 1, 'review_revision': REVIEW_REVISION, 'status': 'changes_required',
            'model': 'offline-fixture', 'mode': 'replay', 'reviewed_at': 'offline-fixture',
            'manifest_fingerprint': fingerprint(self.manifest),
            'package_fingerprint': self.manifest['provenance']['specification']['package_fingerprint'],
            'readiness_error': None, 'verdict': {'status': 'changes_required', 'reviewed_aspects': list(ASPECTS),
                                               'findings': [self.finding]}}
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def edit(self, *edits, base=None):
        return {'base_fingerprint': fingerprint(self.base if base is None else base), 'edits': list(edits)}

    def note(self):
        return self.edit({'op': 'set', 'path': '/modules/contracts/stage/dependency_note', 'value': 'Precise observation note.'})

    def execute(self, replies, reject=False):
        replies = iter(replies)
        def caller(prompt, model):
            self.prompts.append(prompt)
            value = next(replies)
            if isinstance(value, Exception):
                raise value
            return value if isinstance(value, str) else json.dumps(value)
        def reviewer(prompt, model):
            self.review_calls.append(prompt)
            return {'status': 'changes_required' if reject else 'passed', 'reviewed_aspects': list(ASPECTS),
                'findings': [self.finding] if reject else [],
                'repair_assessments': [{'finding_id': self.finding['id'], 'status': 'unresolved' if reject else 'resolved',
                    'explanation': 'Mocked offline assessment.', 'references': ['/documents/modules/contracts/stage'],
                    'remaining_finding_ids': [self.finding['id']] if reject else []}],
                'preservation_assessment': {'status': 'preserved', 'explanation': 'Mocked offline assessment.',
                    'references': ['/documents/shared/events/capture'], 'regression_finding_ids': []}}
        return repair_and_review(self.manifest, self.report, output_dir=self.output,
                                  generation_caller=caller, review_caller=reviewer)

    def journal(self, published=True):
        return read_document(self.output / ('source/attempts.json' if published else 'source.attempts.json'))['attempts']

    def test_first_call_edits_existing_base_and_persists_exact_scope(self):
        before = copy.deepcopy(self.manifest)
        package_files = {p: p.read_bytes() for p in self.package.parent.iterdir() if p.is_file()}
        reply = self.note()
        repaired, approval, export = self.execute([reply])
        self.assertEqual(len(self.prompts), 1)
        prompt = self.prompts[0]
        self.assertIn('SEMANTIC ARCHITECTURE REPAIR:', prompt)
        self.assertIn('ARCHITECTURE EDIT MODE:', prompt)
        self.assertNotIn('FRESH DECOMPOSITION:', prompt)
        self.assertEqual(json.loads(prompt.split(CANDIDATE_MARKER)[1]), self.base)
        shown = repair_context(prompt)
        self.assertNotIn('format_example', shown['capabilities'])
        old, new = [m['provenance']['specification']['snapshot'] for m in (self.manifest, repaired)]
        self.assertEqual(shown['locked_system'], system_projection(old['package'], old['documents']))
        self.assertEqual(system_projection(old['package'], old['documents']), system_projection(new['package'], new['documents']))
        for key in ('timing_library', 'overrides', 'profile'):
            self.assertEqual(old[key], new[key])
        self.assertEqual(old['package']['generation']['request'], new['package']['generation']['request'])
        expected_docs = copy.deepcopy(old['documents'])
        expected_docs['modules']['contracts']['stage']['dependency_note'] = 'Precise observation note.'
        self.assertEqual(new['documents'], expected_docs)
        attempt = self.journal()[0]
        self.assertEqual(json.loads(attempt['response']), reply)
        self.assertEqual(attempt['repair_base_fingerprint'], fingerprint(self.base))
        self.assertEqual([c['path'] for c in attempt['applied_changes']], ['/modules/contracts/stage/dependency_note'])
        self.assertEqual(attempt['applied_response'], apply_architecture_edits(self.base, reply, narrow=True))
        context = read_document(self.output / 'repair_review_context.json')
        self.assertEqual([c['path'] for c in context['changes']], ['/documents/modules/contracts/stage/dependency_note'])
        self.assertEqual(self.manifest, before)
        self.assertEqual(package_files, {p: p.read_bytes() for p in package_files})
        self.assertEqual(read_document(export), repaired)
        require_approval(repaired, approval)
        self.assertFalse(list(self.output.rglob('*.sv')))

    def test_full_responses_fail_all_three_attempts_without_regeneration_or_review(self):
        with self.assertRaisesRegex(SpecificationError, 'base_fingerprint and edits only'):
            self.execute([self.base] * 3)
        self.assertEqual(len(self.prompts), 3)
        self.assertTrue(all('SEMANTIC ARCHITECTURE REPAIR:' in p and 'F_OBSERVATION' in p for p in self.prompts))
        self.assertEqual(self.review_calls, [])
        attempts = self.journal(False)
        self.assertTrue(all(a['repair_base_fingerprint'] == fingerprint(self.base) for a in attempts))
        self.assertFalse((self.output / 'source').exists())
        self.assertFalse((self.output / 'architecture.yaml').exists())
        self.assertEqual(read_document(self.output / 'repair_status.json')['status'], 'stopped')

    def test_stale_fingerprint_and_malformed_json_leave_original_base_for_next_attempt(self):
        reply = self.note()
        self.execute([{**reply, 'base_fingerprint': 'stale'}, '{broken', reply])
        attempts = self.journal()
        self.assertEqual([a['status'] for a in attempts], ['rejected', 'rejected', 'accepted'])
        self.assertTrue(all(a['repair_base_fingerprint'] == fingerprint(self.base) for a in attempts))
        self.assertTrue(all('applied_changes' not in a for a in attempts[:-1]))

    def test_compiler_invalid_edit_is_next_base_and_keeps_prior_changes(self):
        broken = self.edit(
            {'op': 'set', 'path': '/modules/contracts/stage/dependency_note', 'value': 'Keep this earlier repair.'},
            {'op': 'set', 'path': '/graph/connections/top.in_valid', 'value': ['stage.typo']})
        updated = apply_architecture_edits(self.base, broken, narrow=True)
        good = self.edit({'op': 'set', 'path': '/graph/connections/top.in_valid', 'value': ['stage.in_valid']}, base=updated)
        repaired, _, _ = self.execute([broken, good])
        attempts = self.journal()
        self.assertEqual([a['status'] for a in attempts], ['rejected', 'accepted'])
        self.assertIn('stage.typo', attempts[0]['error'])
        self.assertEqual(attempts[1]['repair_base_fingerprint'], fingerprint(updated))
        self.assertEqual(json.loads(self.prompts[1].split(CANDIDATE_MARKER)[1]), updated)
        self.assertEqual(repaired['provenance']['specification']['snapshot']['documents']['modules']['contracts']['stage']['dependency_note'],
                         'Keep this earlier repair.')
        self.assertEqual(len(attempts[0]['applied_changes']), 2)
        self.assertEqual(len(attempts[1]['applied_changes']), 1)

    def test_wholesale_edit_and_locked_system_mutation_are_rejected_before_compilation(self):
        bad = [self.edit({'op': 'set', 'path': '/modules/contracts/stage', 'value': self.base['modules']['contracts']['stage']}),
               self.edit({'op': 'set', 'path': '/shared/events/capture/effect', 'value': 'weaken'})]
        self.execute([*bad, self.note()])
        attempts = self.journal()
        self.assertTrue(all('applied_response' not in a for a in attempts[:2]))
        self.assertEqual([a['status'] for a in attempts], ['rejected', 'rejected', 'accepted'])

    def test_rejection_after_valid_edit_stops_without_another_semantic_cycle(self):
        with self.assertRaises(ReviewChangesRequired):
            self.execute([self.note()], reject=True)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.review_calls), 1)
        self.assertFalse((self.output / 'review/approval.json').exists())
        self.assertEqual(read_document(self.output / 'repair_status.json')['status'], 'stopped')

    def test_transport_failure_does_not_fall_back_to_full_generation(self):
        with self.assertRaisesRegex(SpecificationError, 'mock transport'):
            self.execute([SpecificationError('mock transport failure')])
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.review_calls, [])
        self.assertFalse((self.output / 'source').exists())

    def test_large_prompt_preserves_full_base_and_fits_default_token_reservation(self):
        # Prompt-only fixture: independent prose prevents accidental deduplication
        # from hiding growth. Full compilation is exercised separately above.
        base = copy.deepcopy(self.base)
        base['modules']['contracts']['stage']['dependency_note'] = ''.join(
            f'Independent clause {i}: retain observation, progress and reset. ' for i in range(600))
        snapshot = self.manifest['provenance']['specification']['snapshot']
        system = system_projection(snapshot['package'], snapshot['documents'])
        request = snapshot['package']['generation']['request']
        prompt = build_prompt('architecture', request, generation_context(), system,
            repair_feedback('architecture', base, 'Clarify observation note only.'),
            repair_base=base, narrow_architecture=True)
        self.assertEqual(json.loads(prompt.split(CANDIDATE_MARKER)[1]), base)
        self.assertEqual(repair_context(prompt)['locked_system'], system)
        self.assertLessEqual(estimate_input([{'role': 'user', 'content': prompt}]) + 24000, 80000)


if __name__ == '__main__':
    unittest.main()
