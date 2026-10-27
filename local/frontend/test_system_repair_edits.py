"""Bounded system semantic edits, untouched behavior and real compiler checks."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from architecture import fingerprint
from generate_specification import propose_specification
from repair_specification import repair_and_review
from review_specification import ASPECTS, REVIEW_REVISION
from specification import SpecificationError, read_document
from specification_change_audit import audit_changes
from specification_edits import apply_system_edits
from specification_lowering import compile_package
from specification_request import system_projection
from test_generate_specification import register_fixture


class SystemRepairEditsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        _, request, cls.responses = register_fixture()
        replay = cls.root / 'responses.json'
        replay.write_text(json.dumps(cls.responses))
        with contextlib.redirect_stdout(io.StringIO()):
            package = propose_specification(request, output_dir=cls.root / 'source', response_path=replay)
        cls.manifest = compile_package(package)
        s = cls.manifest['provenance']['specification']['snapshot']
        cls.base = {'status': 'supported', **system_projection(s['package'], s['documents'])}
        for key in ('documents', 'design_kind', 'generation', 'review'):
            cls.base['package'].pop(key, None)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        self.destination = Path(self.output.name) / 'repair'
        self.prompts = []

    def edit(self, *edits, base=None):
        return {'base_fingerprint': fingerprint(self.base if base is None else base), 'edits': list(edits)}

    def execute(self, replies):
        manifest = self.manifest
        report = {'review_version': 1, 'review_revision': REVIEW_REVISION, 'status': 'changes_required',
                  'model': 'offline', 'mode': 'replay', 'reviewed_at': 'offline',
                  'manifest_fingerprint': fingerprint(manifest),
                  'package_fingerprint': manifest['provenance']['specification']['package_fingerprint'],
                  'readiness_error': None,
                  'verdict': {'status': 'changes_required', 'reviewed_aspects': list(ASPECTS), 'findings': [
                      {'id': 'F_CAPTURE', 'scope': 'system', 'resolution': 'design_repair',
                       'message': 'Clarify capture effect while preserving other edge behavior.',
                       'references': ['/documents/shared/events/capture'], 'requirement_ids': []}]}}
        iterator = iter(replies)
        def caller(prompt, model):
            self.prompts.append(prompt)
            return json.dumps(next(iterator))
        with contextlib.redirect_stdout(io.StringIO()):
            return repair_and_review(manifest, report, output_dir=self.destination,
                generation_caller=caller,
                review_caller=lambda *args: json.dumps({'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': [],
                    'repair_assessments': [{'finding_id': 'F_CAPTURE', 'status': 'resolved',
                        'explanation': 'Offline fixture assessment.', 'references': ['/documents/shared/events/capture'],
                        'remaining_finding_ids': []}],
                    'preservation_assessment': {'status': 'preserved', 'explanation': 'Offline fixture assessment.',
                        'references': ['/documents/shared/events/capture'], 'regression_finding_ids': []}}))

    def test_saved_narrow_edit_preserves_active_cleanup_and_type_descriptions(self):
        f = read_document(Path(__file__).parent / 'testdata/repair_preservation_audit.json')
        base = f['before']
        before = copy.deepcopy(base)
        condition = f['after']['shared']['events']['dispatch']['condition']
        result = apply_system_edits(base, self.edit(
            {'op': 'set', 'path': '/shared/events/dispatch/condition', 'value': condition}, base=base))
        self.assertEqual([c['path'] for c in audit_changes(base, result)], ['/shared/events/dispatch/condition'])
        self.assertEqual(result['shared']['types'], base['shared']['types'])
        for key in ('candidate_create', 'cleanup_candidate_create'):
            self.assertEqual(result['shared']['events'][key], base['shared']['events'][key])
        self.assertEqual(base, before)

    def test_system_edits_reject_wholesale_replacement_wrong_stage_and_wrong_base(self):
        for change in ({'op': 'set', 'path': '/shared', 'value': {}},
                       {'op': 'set', 'path': '/shared/events/capture', 'value': {}},
                       {'op': 'set', 'path': '/modules', 'value': {}},
                       {'op': 'remove', 'path': '/status'}):
            with self.subTest(change=change), self.assertRaises(SpecificationError):
                apply_system_edits(self.base, self.edit(change))
        with self.assertRaisesRegex(SpecificationError, 'base_fingerprint'):
            apply_system_edits(self.base, {'base_fingerprint': 'wrong', 'edits': [{}]})

    def test_saved_drain_guard_edits_keep_repaired_dispatch_condition(self):
        f = read_document(Path(__file__).parent / 'testdata/repair_preservation_audit.json')
        base = f['after']
        edits = [{'op': 'set', 'path': f'/shared/events/{event}/condition',
                  'value': f['before']['shared']['events'][event]['condition']}
                 for event in ('candidate_create', 'cleanup_candidate_create')]
        result = apply_system_edits(base, self.edit(*edits, base=base))
        self.assertEqual(result['shared']['events']['dispatch'], base['shared']['events']['dispatch'])
        self.assertEqual(len(audit_changes(base, result)), 2)
        for event in ('candidate_create', 'cleanup_candidate_create'):
            self.assertNotIn('refresh_pending is low', result['shared']['events'][event]['condition'])
        # Scope preservation on recorded text, not an executed progress proof.

    def test_valid_edit_publishes_and_journals_exact_scope(self):
        before = copy.deepcopy(self.base)
        value = self.base['shared']['events']['capture']['effect'] + ' Preserve next-edge delivery.'
        reply = self.edit({'op': 'set', 'path': '/shared/events/capture/effect', 'value': value})
        self.execute([reply, self.responses['architecture']])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn('SYSTEM EDIT MODE:', self.prompts[0])
        self.assertIn('FRESH DECOMPOSITION:', self.prompts[1])
        journal = read_document(self.destination / 'source/attempts.json')['attempts'][0]
        self.assertEqual(json.loads(journal['response']), reply)
        self.assertEqual(journal['repair_base_fingerprint'], fingerprint(self.base))
        self.assertEqual([c['path'] for c in journal['applied_changes']], ['/shared/events/capture/effect'])
        self.assertEqual(self.base, before)

    def test_invalid_edit_keeps_base_and_does_not_consume_extra_round(self):
        good = self.edit({'op': 'set', 'path': '/package/revision', 'value': 'repaired'})
        self.execute([{**good, 'base_fingerprint': 'stale'}, good, self.responses['architecture']])
        journal = read_document(self.destination / 'source/attempts.json')['attempts']
        self.assertEqual([e['status'] for e in journal], ['rejected', 'accepted', 'accepted'])
        self.assertEqual(journal[0]['repair_base_fingerprint'], journal[1]['repair_base_fingerprint'])

    def test_complete_system_rewrites_are_rejected_with_existing_limit(self):
        with self.assertRaisesRegex(SpecificationError, 'base_fingerprint and edits only'):
            self.execute([self.responses['system']] * 3)
        self.assertEqual(len(self.prompts), 3)
        self.assertFalse((self.destination / 'source').exists())
        self.assertFalse((self.destination / 'review').exists())

    def test_requested_text_cannot_be_edited_even_though_fresh_generation_normalizes_it(self):
        rid = next(iter(self.base['system']['rules']))
        reply = self.edit({'op': 'set', 'path': f'/system/rules/{rid}/statement', 'value': 'Weakened request.'})
        with self.assertRaisesRegex(SpecificationError, 'authoritative request text'):
            self.execute([reply] * 3)
        journal = read_document(self.destination / 'source.attempts.json')['attempts']
        self.assertTrue(all(e['repair_base_fingerprint'] == fingerprint(self.base) for e in journal))
        self.assertFalse((self.destination / 'source').exists())

    def test_compiler_invalid_edited_candidate_is_checked_and_then_repaired(self):
        bad = self.edit({'op': 'set', 'path': '/shared/host_interface/in_data/type', 'value': 'shared.types.missing'})
        bad_base = apply_system_edits(self.base, bad)
        good = self.edit({'op': 'set', 'path': '/shared/host_interface/in_data/type', 'value': 'shared.types.byte'}, base=bad_base)
        self.execute([bad, good, self.responses['architecture']])
        journal = read_document(self.destination / 'source/attempts.json')['attempts']
        self.assertEqual(journal[0]['status'], 'rejected')
        self.assertIn('invalid host port', journal[0]['error'])
        self.assertEqual(journal[1]['repair_base_fingerprint'], fingerprint(bad_base))


if __name__ == '__main__':
    unittest.main()
