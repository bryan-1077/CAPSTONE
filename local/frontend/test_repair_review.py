"""Repair obligations cannot disappear; preservation evidence never implies simulation."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from architecture import fingerprint
from generate_specification import propose_specification
from repair_review import build_repair_context
from repair_specification import plan_repair
from review_specification import (ASPECTS, ReviewChangesRequired, build_review_prompt,
                                  review_specification, validate_verdict, _repair_prompt_context, _resolve)
from prompt_context import expand_context, serialize_context
from specification import SpecificationError, read_document
from specification_lowering import _compile_snapshot, compile_package, require_approval
from test_generate_specification import register_fixture


class RepairReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        _, request, responses = register_fixture()
        path = cls.root / 'responses.json'
        path.write_text(json.dumps(responses))
        with contextlib.redirect_stdout(io.StringIO()):
            package = propose_specification(request, output_dir=cls.root / 'source', response_path=path)
        cls.parent = compile_package(package)
        snapshot = copy.deepcopy(cls.parent['provenance']['specification']['snapshot'])
        snapshot['documents']['modules']['contracts']['stage']['handshakes']['capture'] += ' Preserve next-edge visibility.'
        cls.target = _compile_snapshot(snapshot)[0]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.temp_output = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_output.cleanup)
        self.output = Path(self.temp_output.name) / 'review'
        self.original = self.finding('F_ORIGINAL')
        self.context = build_repair_context(self.parent, self.target,
            {'report_fingerprint': 'offline-parent-report', 'findings': [self.original]})
        self.verdict = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': [],
            'repair_assessments': [{'finding_id': 'F_ORIGINAL', 'status': 'resolved',
                'explanation': 'Offline fixture: current capture contract clarifies the edge.',
                'references': ['/documents/modules/contracts/stage/handshakes/capture'], 'remaining_finding_ids': []}],
            'preservation_assessment': {'status': 'preserved', 'explanation': 'Offline fixture: registered-edge behavior is retained.',
                'references': ['/documents/shared/events/capture'], 'regression_finding_ids': []}}
        self.calls = []

    def finding(self, finding_id):
        return {'id': finding_id, 'scope': 'architecture', 'resolution': 'design_repair',
                'message': 'Capture behavior needs correction.', 'references': ['/documents/shared/events/capture'],
                'requirement_ids': []}

    def review(self, context=None):
        def caller(prompt, model):
            self.calls.append(prompt)
            return json.dumps(self.verdict)
        with contextlib.redirect_stdout(io.StringIO()):
            return review_specification(self.target, output_dir=self.output, caller=caller,
                repair_context=self.context if context is None else context)

    def test_context_has_exact_change_scope_and_independent_parent_findings(self):
        self.assertEqual(self.context['target_manifest_fingerprint'], fingerprint(self.target))
        self.assertEqual(self.context['parent_manifest_fingerprint'], fingerprint(self.parent))
        self.assertEqual([c['path'] for c in self.context['changes']],
                         ['/documents/modules/contracts/stage/handshakes/capture'])
        self.context['findings'][0]['message'] = 'Changed private copy.'
        self.assertNotEqual(self.original['message'], self.context['findings'][0]['message'])
        prompt = build_review_prompt(self.target, self.context)
        self.assertIn('refer to the PARENT', prompt)
        self.assertIn('current snapshot is authoritative', prompt)
        self.assertIn('progress, ordering, priority, reset', prompt)
        self.assertNotIn('validation_repairs', json.dumps(self.context['changes']))

    def test_every_original_must_be_assessed_exactly_once_with_current_references(self):
        candidates = []
        missing = copy.deepcopy(self.verdict); del missing['repair_assessments']; candidates.append(missing)
        missing = copy.deepcopy(self.verdict); missing['repair_assessments'] = []; candidates.append(missing)
        duplicate = copy.deepcopy(self.verdict); duplicate['repair_assessments'] *= 2; candidates.append(duplicate)
        unknown = copy.deepcopy(self.verdict); unknown['repair_assessments'][0]['finding_id'] = 'OTHER'; candidates.append(unknown)
        bad = copy.deepcopy(self.verdict); bad['repair_assessments'][0]['references'] = ['/documents/missing']; candidates.append(bad)
        missing = copy.deepcopy(self.verdict); del missing['preservation_assessment']; candidates.append(missing)
        for verdict in candidates:
            with self.subTest(verdict=verdict), self.assertRaises(SpecificationError):
                validate_verdict(verdict, self.target, self.context)

    def test_change_transport_recovers_exact_audit_without_mutating_evidence(self):
        snapshot = {'new/key': [False, 0, None, 'λ'], 'changed~key': {'value': 'current'}}
        context = copy.deepcopy(self.context)
        context['changes'] = [
            {'op': 'add', 'path': '/new~1key', 'after': snapshot['new/key']},
            {'op': 'replace', 'path': '/changed~0key/value', 'before': 'parent', 'after': 'current'},
            {'op': 'remove', 'path': '/removed', 'before': [True, 'parent']}]
        original = copy.deepcopy(context)
        transport = expand_context(json.loads(serialize_context(_repair_prompt_context(context, snapshot))))
        self.assertEqual(transport['change_format'], 'snapshot_after_v1')
        self.assertEqual(transport['findings'], original['findings'])
        recovered = []
        for op, path, *before in transport['changes']:
            change = {'op': op, 'path': path}
            if before:
                change['before'] = before[0]
            if op != 'remove':
                change['after'] = _resolve(snapshot, path)
            recovered.append(change)
        self.assertEqual(recovered, original['changes'])
        self.assertEqual(context, original)
        self.assertEqual(snapshot, {'new/key': [False, 0, None, 'λ'], 'changed~key': {'value': 'current'}})

    def test_change_transport_retains_unreconstructable_entries_in_full(self):
        context = copy.deepcopy(self.context)
        context['changes'] = [
            {'op': 'add', 'path': '/missing', 'after': 'keep evidence'},
            {'op': 'replace', 'path': '/value', 'before': 2, 'after': True},
            {'op': 'remove', 'path': '/old', 'before': 0, 'extra': 'preserve'},
            {'op': 'unknown', 'path': '/value', 'after': 1}]
        self.assertEqual(_repair_prompt_context(context, {'value': 1})['changes'], context['changes'])

    def test_large_change_transport_reduces_reservation_without_dropping_changes(self):
        from tpm_pacing import estimate_input
        context = copy.deepcopy(self.context)
        snapshot = {f'field_{i}': f'Current behavior {i}: ' + 'Preserve reset and progress. ' * 16
                    for i in range(200)}
        context['changes'] = [{'op': 'replace', 'path': '/' + key,
            'before': f'Parent behavior {i}: keep exact evidence.', 'after': value}
            for i, (key, value) in enumerate(snapshot.items())]
        encoded = _repair_prompt_context(context, snapshot)
        def reservation(evidence):
            return estimate_input([{'role': 'user', 'content': serialize_context(evidence)
                                    + '\n' + serialize_context(snapshot)}]) + 6000
        self.assertGreater(reservation(context) - reservation(encoded), 10000)
        self.assertEqual(len(encoded['changes']), 200)
        prompt = build_review_prompt(self.target, self.context)
        self.assertIn('the exact after value is at path', prompt)
        self.assertIn('CURRENT expanded snapshot', prompt)

    def test_unresolved_original_cannot_hide_behind_clean_verdict_or_bad_links(self):
        self.verdict['repair_assessments'][0]['status'] = 'unresolved'
        with self.assertRaises(SpecificationError):
            validate_verdict(self.verdict, self.target, self.context)
        self.verdict['repair_assessments'][0]['remaining_finding_ids'] = ['NOT_REPORTED']
        with self.assertRaises(SpecificationError):
            validate_verdict(self.verdict, self.target, self.context)

    def test_unresolved_finding_is_persisted_and_remains_next_repair_input(self):
        self.verdict.update(status='changes_required', findings=[self.finding('F_STILL_OPEN')])
        self.verdict['repair_assessments'][0].update(status='unresolved', remaining_finding_ids=['F_STILL_OPEN'])
        with self.assertRaises(ReviewChangesRequired):
            self.review()
        report = read_document(self.output / 'report.json')
        self.assertEqual(report['verdict']['repair_assessments'], self.verdict['repair_assessments'])
        self.assertEqual([f['id'] for f in plan_repair(self.target, report)['findings']], ['F_STILL_OPEN'])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.output / 'approval.json').exists())

    def test_regression_must_be_a_current_design_finding_and_blocks_approval(self):
        self.verdict['preservation_assessment'].update(status='regressions_found')
        with self.assertRaises(SpecificationError):
            validate_verdict(self.verdict, self.target, self.context)
        self.verdict.update(status='changes_required', findings=[self.finding('F_REGRESSION')])
        self.verdict['preservation_assessment']['regression_finding_ids'] = ['F_REGRESSION']
        with self.assertRaises(ReviewChangesRequired):
            self.review()
        self.assertFalse((self.output / 'approval.json').exists())
        self.assertEqual(len(self.calls), 1)

    def test_complete_pass_binds_assessments_to_approval_and_detects_tampering(self):
        approval = self.review()
        require_approval(self.target, approval)
        record = read_document(approval)
        self.assertEqual(record['automated_review']['repair_context'], self.context)
        record['automated_review']['verdict']['repair_assessments'] = []
        approval.write_text(json.dumps(record))
        with self.assertRaises(SpecificationError):
            require_approval(self.target, approval)

    def test_stale_context_stops_before_call_or_output(self):
        stale = {**self.context, 'target_manifest_fingerprint': 'stale'}
        with self.assertRaisesRegex(SpecificationError, 'another target revision'):
            self.review(stale)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.output.exists())

    def test_standalone_replay_can_retain_saved_repair_obligations(self):
        import sys
        from review_specification import main
        root = Path(self.temp_output.name)
        manifest_path, context_path, verdict_path = [root / name for name in ('architecture.yaml', 'context.json', 'verdict.json')]
        for path, value in ((manifest_path, self.target), (context_path, self.context), (verdict_path, self.verdict)):
            path.write_text(json.dumps(value))
        args = ['review_specification.py', str(manifest_path), '--repair-context', str(context_path),
                '--response', str(verdict_path), '--output-dir', str(self.output)]
        with patch.object(sys, 'argv', args), patch('token_usage.UsageRun'), \
             patch('review_specification.call_llm') as model, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
            model.assert_not_called()
        self.assertEqual(read_document(self.output / 'report.json')['repair_context'], self.context)

    def test_omitted_assessment_fails_review_and_retains_context_without_approval(self):
        self.verdict['repair_assessments'] = []
        with self.assertRaises(SpecificationError):
            self.review()
        report = read_document(self.output / 'report.json')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['repair_context'], self.context)
        self.assertFalse((self.output / 'approval.json').exists())

    def test_saved_dispatch_fix_can_be_resolved_while_drain_regression_blocks(self):
        # A review-context projection only; never admitted as a complete design.
        fixture = read_document(Path(__file__).parent / 'testdata/repair_preservation_audit.json')
        def projection(document):
            return {'provenance': {'specification': {'snapshot': {
                'package': {'generation': {'validation_repairs': [{'error': 'HISTORICAL_ERROR'}]}},
                'documents': document}}}}
        parent, target = projection(fixture['before']), projection(fixture['after'])
        original = next(f for f in fixture['initial_findings'] if f['id'] == 'F_REFRESH_PENDING_DISPATCH_RACE')
        context = build_repair_context(parent, target, {'report_fingerprint': 'offline-saved-projection', 'findings': [original]})
        regression = copy.deepcopy(next(f for f in fixture['post_repair_findings'] if f['id'] == 'F_REFRESH_DRAIN_DEADLOCK'))
        regression['references'] = ['/documents/shared/events/candidate_create/condition',
                                    '/documents/shared/events/cleanup_candidate_create/condition']
        target['requirements'] = [{'id': rid} for rid in regression['requirement_ids']]
        # Context binds the whole target, including its requirement-ID projection.
        context['target_manifest_fingerprint'] = fingerprint(target)
        verdict = {'status': 'changes_required', 'reviewed_aspects': list(ASPECTS), 'findings': [regression],
            'repair_assessments': [{'finding_id': original['id'], 'status': 'resolved',
                'explanation': 'Dispatch now excludes pending refresh.',
                'references': ['/documents/shared/events/dispatch/condition'], 'remaining_finding_ids': []}],
            'preservation_assessment': {'status': 'regressions_found',
                'explanation': 'Active and cleanup creation now require pending low, preventing required drain.',
                'references': regression['references'], 'regression_finding_ids': [regression['id']]}}
        validate_verdict(verdict, target, context)
        self.assertNotIn('HISTORICAL_ERROR', json.dumps(context))
        paths = {c['path'] for c in context['changes']}
        self.assertIn('/documents/shared/events/candidate_create/condition', paths)
        self.assertIn('/documents/shared/events/cleanup_candidate_create/condition', paths)


if __name__ == '__main__':
    unittest.main()
