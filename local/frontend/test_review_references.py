"""Offline reference transport, adversarial correction and admission regressions."""
import contextlib
import copy
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from architecture import ArchitectureError, fingerprint
from generate_specification import propose_specification
from prompt_context import expand_context, serialize_context
from repair_review import build_repair_context
from repair_specification import plan_repair
from review_references import (reference_index, reference_view, expand_reference_view,
    compact_change_locations, reference_slots, canonicalize_references, apply_reference_corrections)
from review_specification import (ASPECTS, ReviewReferenceErrors, ReviewChangesRequired,
    review_snapshot, review_specification, validate_verdict, validate_reference_protocol,
    _repair_prompt_context, _resolve)
from specification import SpecificationError, read_document
from specification_lowering import _compile_snapshot, compile_package, require_approval
from specification_request import system_projection
from test_generate_specification import register_fixture


class ReferenceTransportTests(unittest.TestCase):
    def test_stable_ids_bind_paths_while_fingerprint_binds_values(self):
        a = {'package': {'decisions': {'D': {'value': 'old'}}}}
        b = {'package': {'decisions': {'D': {'value': 'new'}, 'OTHER': {}}}}
        first, second = reference_index(a), reference_index(b)
        for ref, path in first['targets'].items():
            self.assertEqual(second['targets'][ref], path)
            _resolve(a, path)
        self.assertNotEqual(first['snapshot_fingerprint'], second['snapshot_fingerprint'])
        reordered = {'package': {'decisions': dict(reversed(list(b['package']['decisions'].items())))}}
        self.assertEqual(reference_index(reordered), second)

    def test_seeded_varied_roundtrips_preserve_unicode_escaping_lists_and_null(self):
        rng = random.Random(91)
        for i in range(40):
            snapshot = {'package': {'decisions': {
                'D/~λ' + str(i): {'value': rng.choice([None, False, 0, 'λ', ['R123', {'$ref': 'literal'}]])}},
                'assumptions': {}}, 'documents': {'shared': {'events': {'E/~': [True, None, i]}}}}
            before = copy.deepcopy(snapshot)
            index = reference_index(snapshot)
            view = reference_view(snapshot, index)
            wire = expand_context(json.loads(serialize_context(view)))
            self.assertEqual(expand_reference_view(wire, index), snapshot)
            self.assertEqual(snapshot, before)
            for path in index['targets'].values():
                _resolve(snapshot, path)

    def test_reject_wrong_index_missing_labels_and_changed_data(self):
        snapshot = {'package': {'decisions': {'D': {'value': None}}}}
        index = reference_index(snapshot)
        with self.assertRaises(SpecificationError):
            reference_view({'package': {}}, index)
        view = reference_view(snapshot, index)
        for bad in (snapshot, {**view, 'extra': 1}):
            with self.assertRaises(SpecificationError):
                expand_reference_view(bad, index)

    def test_compact_changes_reconstruct_exact_original_including_remove_and_fallback(self):
        snapshot = {'package': {}, 'documents': {'modules': {'contracts': {
            'unusually_long_module_name': {'state': {'x': [None, False, 'λ']}}}}}}
        path = '/documents/modules/contracts/unusually_long_module_name/state/x'
        changes = [{'op': 'replace', 'path': path, 'before': [1], 'after': [None, False, 'λ']},
            {'op': 'add', 'path': path + '/0', 'after': None},
            {'op': 'remove', 'path': path + '/old', 'before': 'prior'},
            {'op': 'add', 'path': '/missing', 'after': 'preserved fallback'}]
        context = {'changes': changes}
        before = copy.deepcopy(context)
        index = reference_index(snapshot)
        compact = compact_change_locations(_repair_prompt_context(context, snapshot), index)
        decoded = []
        self.assertIsInstance(compact['changes'][0][1], list)
        for row in compact['changes']:
            if isinstance(row, dict):
                decoded.append(row)
                continue
            op, location, *old = row
            path = index['targets'][location[0]] + location[1] if isinstance(location, list) else location
            change = {'op': op, 'path': path}
            if old:
                change['before'] = old[0]
            if op != 'remove':
                change['after'] = _resolve(snapshot, path)
            decoded.append(change)
        self.assertEqual(decoded, changes)
        self.assertEqual(context, before)

    def test_saved_refresh_failure_reports_all_three_slots_and_preserves_verdict(self):
        fixture = read_document(Path(__file__).parent / 'testdata/review_reference_failure.json')
        manifest = {'requirements': fixture['requirements'],
                    'provenance': {'specification': {'snapshot': fixture['snapshot']}}}
        context = {'version': 1, 'parent_manifest_fingerprint': 'saved-parent',
            'parent_report_fingerprint': 'saved-report', 'target_manifest_fingerprint': fingerprint(manifest),
            'findings': fixture['original_findings'], 'changes': []}
        verdict = fixture['verdict']
        before = copy.deepcopy(verdict)
        with self.assertRaises(ReviewReferenceErrors) as caught:
            validate_verdict(verdict, manifest, context)
        issues = caught.exception.issues
        self.assertEqual([v['slot'] for v in issues], ['/findings/0/references/6',
            '/preservation_assessment/references/1', '/preservation_assessment/references/2'])
        index = reference_index(fixture['snapshot'])
        by_path = {path: ref for ref, path in index['targets'].items()}
        correction = {'corrections': {v['slot']: by_path[v['reference'].replace(
            '/documents/shared/system/', '/documents/system/').replace(
            '/documents/shared/execution/', '/documents/execution/')] for v in issues}}
        corrected = apply_reference_corrections(verdict, issues, correction, index)
        validate_verdict(corrected, manifest, context)
        self.assertEqual(corrected['status'], 'changes_required')
        self.assertEqual(corrected['findings'][0]['message'], verdict['findings'][0]['message'])
        self.assertEqual(verdict, before)


class ReferenceAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        _, request, responses = register_fixture()
        raw = root / 'response.json'
        raw.write_text(json.dumps(responses))
        with contextlib.redirect_stdout(io.StringIO()):
            cls.manifest = compile_package(propose_specification(request, output_dir=root / 'source', response_path=raw))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'review'
        self.index = reference_index(review_snapshot(self.manifest))
        self.ids = {p: r for r, p in self.index['targets'].items()}
        self.event = '/documents/shared/events/capture'
        self.verdict = {'status': 'changes_required', 'reviewed_aspects': list(ASPECTS), 'findings': [
            {'id': 'F_KEEP', 'scope': 'architecture', 'resolution': 'design_repair',
             'message': 'Keep this exact blocking claim.', 'references': ['/wrong', self.event], 'requirement_ids': []}]}
        self.correction = {'corrections': {'/findings/0/references/0': self.ids['/documents/modules/contracts/stage']}}
        # Fail closed if any test accidentally escapes mocked/replayed calls.
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def run_review(self, **kwargs):
        return review_specification(self.manifest, output_dir=self.output, **kwargs)

    def read(self, name):
        return json.loads((self.output / name).read_text())

    def test_clean_id_review_resolves_to_canonical_evidence_and_approves_once(self):
        verdict = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []}
        caller = Mock(return_value=verdict)
        approval = self.run_review(caller=caller)
        require_approval(self.manifest, approval)
        self.assertEqual(caller.call_count, 1)
        self.assertEqual(self.read('reference_index.json'), self.index)
        self.assertFalse((self.output / 'reference_correction.response.json').exists())

    def test_ids_in_findings_and_all_assessments_resolve_without_retry(self):
        context = build_repair_context(self.manifest, self.manifest,
            {'report_fingerprint': 'fixture', 'findings': self.verdict['findings']})
        verdict = copy.deepcopy(self.verdict)
        verdict['findings'][0]['references'] = [self.ids[self.event]]
        verdict['repair_assessments'] = [{'finding_id': 'F_KEEP', 'status': 'unresolved',
            'explanation': 'Still open.', 'references': [self.ids[self.event]], 'remaining_finding_ids': ['F_KEEP']}]
        verdict['preservation_assessment'] = {'status': 'preserved', 'explanation': 'Preserved.',
            'references': [self.ids[self.event]], 'regression_finding_ids': []}
        caller = Mock(return_value=verdict)
        with self.assertRaises(ReviewChangesRequired):
            self.run_review(caller=caller, repair_context=context)
        report = self.read('report.json')
        self.assertEqual(report['verdict'], canonicalize_references(verdict, self.index))
        self.assertEqual(caller.call_count, 1)
        plan_repair(self.manifest, report)

    def test_correction_preserves_substantive_rejection_and_enters_repair_admission(self):
        before = copy.deepcopy(self.verdict)
        caller = Mock(side_effect=[json.dumps(self.verdict), json.dumps(self.correction)])
        with self.assertRaises(ReviewChangesRequired):
            self.run_review(caller=caller)
        report = self.read('report.json')
        self.assertEqual(caller.call_count, 2)
        self.assertEqual(report['status'], 'changes_required')
        self.assertEqual(plan_repair(self.manifest, report)['findings'], report['verdict']['findings'])
        expected = copy.deepcopy(before)
        expected['findings'][0]['references'][0] = '/documents/modules/contracts/stage'
        self.assertEqual(report['verdict'], expected)
        self.assertEqual(report['reference_protocol']['raw_verdict'], before)
        self.assertEqual(self.read('reference_correction.result.json')['status'], 'applied')
        self.assertFalse((self.output / 'approval.json').exists())
        self.assertEqual(self.verdict, before)

    def test_three_section_bad_refs_collected_then_corrected_with_one_call(self):
        context = build_repair_context(self.manifest, self.manifest,
            {'report_fingerprint': 'fixture', 'findings': self.verdict['findings']})
        verdict = copy.deepcopy(self.verdict)
        verdict['repair_assessments'] = [{'finding_id': 'F_KEEP', 'status': 'unresolved',
            'explanation': 'Still open.', 'references': ['/wrong-assessment'], 'remaining_finding_ids': ['F_KEEP']}]
        verdict['preservation_assessment'] = {'status': 'preserved', 'explanation': 'Preserved.',
            'references': ['/wrong-preservation'], 'regression_finding_ids': []}
        correction = copy.deepcopy(self.correction)
        correction['corrections'].update({'/repair_assessments/0/references/0': self.ids[self.event],
            '/preservation_assessment/references/0': self.ids[self.event]})
        caller = Mock(side_effect=[verdict, correction])
        with self.assertRaises(ReviewChangesRequired):
            self.run_review(caller=caller, repair_context=context)
        self.assertEqual(len(self.read('reference_errors.json')), 3)
        self.assertEqual(caller.call_count, 2)
        validate_reference_protocol(self.read('report.json'), self.manifest)

    def test_failed_corrections_are_bounded_and_cannot_edit_or_drop_claims(self):
        bad = [{}, {'corrections': {}}, {'corrections': {'/findings/0/references/0': 'R_unknown'}},
            {'corrections': {'/findings/0/references/0': self.event}},
            {'corrections': {'/findings/0/references/0': self.ids[self.event]}},  # duplicate evidence
            {**self.correction, 'status': 'passed'},
            {'corrections': {**self.correction['corrections'], '/findings/0/message': self.ids[self.event]}},
            'invalid JSON', SpecificationError('Correction transport failed')]
        for i, response in enumerate(bad):
            with self.subTest(response=response):
                self.output = self.root / str(i)
                caller = Mock(side_effect=[self.verdict, response])
                with self.assertRaises(ReviewReferenceErrors):
                    self.run_review(caller=caller)
                self.assertEqual(caller.call_count, 2)
                self.assertEqual(self.read('report.json')['verdict'], self.verdict)
                self.assertEqual(self.read('reference_correction.result.json')['status'], 'failed')
                self.assertFalse((self.output / 'approval.json').exists())

    def test_nonreference_errors_never_trigger_correction_even_with_bad_refs(self):
        values = []
        for key, value in [('scope', 'bad'), ('requirement_ids', ['unknown']), ('references', []),
                           ('references', [None]), ('id', ''), ('resolution', 'bad')]:
            verdict = copy.deepcopy(self.verdict)
            verdict['findings'][0][key] = value
            values.append(verdict)
        values += [{'status': 'passed'}, 'not JSON', SpecificationError('HTTP 524')]
        for i, verdict in enumerate(values):
            with self.subTest(verdict=verdict):
                self.output = self.root / str(i)
                caller = Mock(side_effect=[verdict, self.correction])
                with self.assertRaises((SpecificationError, ArchitectureError)):
                    self.run_review(caller=caller)
                self.assertEqual(caller.call_count, 1)

    def test_bad_assessment_links_prevent_reference_recovery(self):
        context = build_repair_context(self.manifest, self.manifest,
            {'report_fingerprint': 'fixture', 'findings': self.verdict['findings']})
        verdict = copy.deepcopy(self.verdict)
        verdict['repair_assessments'] = [{'finding_id': 'F_KEEP', 'status': 'unresolved',
            'explanation': 'Still open.', 'references': ['/bad'], 'remaining_finding_ids': ['NOT_REPORTED']}]
        verdict['preservation_assessment'] = {'status': 'preserved', 'explanation': 'Preserved.',
            'references': ['/bad'], 'regression_finding_ids': []}
        caller = Mock(return_value=verdict)
        with self.assertRaises(SpecificationError):
            self.run_review(caller=caller, repair_context=context)
        self.assertEqual(caller.call_count, 1)

    def test_offline_replay_never_falls_back_to_network_and_explicit_recovery_works(self):
        raw = self.root / 'verdict.json'
        raw.write_text(json.dumps(self.verdict))
        with patch('review_specification.call_llm') as llm:
            with self.assertRaises(ReviewReferenceErrors):
                self.run_review(response_path=raw)
            llm.assert_not_called()
        self.output = self.root / 'corrected'
        correction = self.root / 'correction.json'
        correction.write_text(json.dumps(self.correction))
        with patch('review_specification.call_llm') as llm:
            with self.assertRaises(ReviewChangesRequired):
                self.run_review(response_path=raw, reference_response_path=correction)
            llm.assert_not_called()
        self.assertEqual(self.read('report.json')['mode'], 'replay')

    def test_default_calls_have_distinct_labels_and_bounded_token_caps(self):
        with patch('review_specification.call_llm', side_effect=[self.verdict, self.correction]) as llm:
            with self.assertRaises(ReviewChangesRequired):
                self.run_review()
        self.assertEqual([(c.kwargs['label'], c.kwargs['max_tokens']) for c in llm.call_args_list],
            [('Specification-Review', 6000), ('Specification-Reference-Correction', 4000)])

    def test_clean_repair_approval_replays_corrections_and_rejects_audit_tampering(self):
        context = build_repair_context(self.manifest, self.manifest,
            {'report_fingerprint': 'fixture', 'findings': self.verdict['findings']})
        verdict = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': [],
            'repair_assessments': [{'finding_id': 'F_KEEP', 'status': 'resolved',
                'explanation': 'Resolved.', 'references': ['/wrong'], 'remaining_finding_ids': []}],
            'preservation_assessment': {'status': 'preserved', 'explanation': 'Preserved.',
                'references': [self.ids[self.event]], 'regression_finding_ids': []}}
        correction = {'corrections': {'/repair_assessments/0/references/0': self.ids[self.event]}}
        approval = self.run_review(caller=Mock(side_effect=[verdict, correction]), repair_context=context)
        require_approval(self.manifest, approval)
        record = read_document(approval)
        for key, value in [('index_fingerprint', 'changed'), ('corrections', None)]:
            changed = copy.deepcopy(record)
            changed['automated_review']['reference_protocol'][key] = value
            approval.write_text(json.dumps(changed))
            with self.subTest(key=key), self.assertRaises(SpecificationError):
                require_approval(self.manifest, approval)
        changed = copy.deepcopy(record)
        changed['automated_review']['reference_protocol']['raw_verdict']['repair_assessments'][0]['explanation'] = 'Changed'
        approval.write_text(json.dumps(changed))
        with self.assertRaises(SpecificationError):
            require_approval(self.manifest, approval)

    def test_valid_references_cannot_have_spurious_corrections_attached(self):
        verdict = copy.deepcopy(self.verdict)
        verdict['findings'][0]['references'] = [self.ids[self.event]]
        with self.assertRaises(ReviewChangesRequired):
            self.run_review(caller=Mock(return_value=verdict))
        report = self.read('report.json')
        report['reference_protocol']['corrections'] = self.correction['corrections']
        with self.assertRaisesRegex(SpecificationError, 'without invalid references'):
            plan_repair(self.manifest, report)

    def test_external_blocker_and_readiness_gate_survive_reference_recovery(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['assumptions']['EXTERNAL'] = {
            'status': 'requires_review', 'statement': 'External timing evidence needs agreement.'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(
            system_projection(snapshot['package'], snapshot['documents']))
        manifest = _compile_snapshot(snapshot)[0]
        index = reference_index(review_snapshot(manifest))
        ids = {p: r for r, p in index['targets'].items()}
        verdict = copy.deepcopy(self.verdict)
        verdict['findings'][0].update(scope='system', resolution='external_agreement', references=['/bad'])
        for i, path in enumerate([self.event, '/package/assumptions/EXTERNAL']):
            output = self.root / ('external' + str(i))
            correction = {'corrections': {'/findings/0/references/0': ids[path]}}
            caller = Mock(side_effect=[verdict, correction])
            with self.subTest(path=path), self.assertRaises(
                    ReviewReferenceErrors if i == 0 else ReviewChangesRequired):
                review_specification(manifest, output_dir=output, caller=caller)
            self.assertEqual(caller.call_count, 2)
            self.assertFalse((output / 'approval.json').exists())
        output = self.root / 'readiness'
        with self.assertRaises(ReviewChangesRequired):
            review_specification(manifest, output_dir=output, caller=Mock(return_value={
                'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []}))
        self.assertFalse((output / 'approval.json').exists())

    def test_offline_correction_requires_initial_replay_before_creating_output(self):
        caller = Mock()
        with self.assertRaisesRegex(SpecificationError, 'requires --response'):
            self.run_review(reference_response_path=self.root / 'unused.json', caller=caller)
        caller.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_cli_replay_uses_explicit_correction_file_and_reports_remaining_finding(self):
        from review_specification import main
        from token_usage import UsageRun
        architecture, raw, correction = [self.root / name for name in (
            'architecture.json', 'verdict.json', 'correction.json')]
        for path, value in ((architecture, self.manifest), (raw, self.verdict), (correction, self.correction)):
            path.write_text(json.dumps(value))
        args = ['review_specification.py', str(architecture), '--response', str(raw),
                '--reference-response', str(correction), '--output-dir', str(self.output)]
        with patch.object(sys, 'argv', args), patch('review_specification.call_llm') as llm, patch(
                'token_usage.UsageRun', side_effect=lambda *a, **kw: UsageRun(self.root / 'usage', **kw)):
            self.assertEqual(main(), 1)  # Correct references do not close a design finding.
        llm.assert_not_called()
        self.assertEqual(self.read('report.json')['status'], 'changes_required')
        self.assertEqual(self.read('reference_correction.result.json')['status'], 'applied')

    def test_reference_protocol_rejects_tampering_in_raw_content_index_and_corrections(self):
        with self.assertRaises(ReviewChangesRequired):
            self.run_review(caller=Mock(side_effect=[self.verdict, self.correction]))
        report = self.read('report.json')
        for key, value in [('index_fingerprint', 'stale'), ('version', True),
            ('corrections', None), ('corrections', {}),
            ('raw_verdict', {**self.verdict, 'findings': []})]:
            changed = copy.deepcopy(report)
            changed['reference_protocol'][key] = value
            with self.subTest(key=key), self.assertRaises(SpecificationError):
                plan_repair(self.manifest, changed)
        changed = copy.deepcopy(report)
        changed['verdict']['findings'][0]['message'] = 'Altered claim.'
        with self.assertRaises(SpecificationError):
            plan_repair(self.manifest, changed)

    def test_null_evidence_is_valid_and_hundred_entry_limit_is_enforced(self):
        manifest = copy.deepcopy(self.manifest)
        # Validator-only snapshot projection: null is a valid JSON pointer target.
        manifest['provenance']['specification']['snapshot']['documents']['shared']['events']['null'] = None
        verdict = copy.deepcopy(self.verdict)
        verdict['findings'][0]['references'] = ['/documents/shared/events/null']
        validate_verdict(verdict, manifest)
        verdict = copy.deepcopy(self.verdict)
        verdict['findings'][0]['references'] = ['/bad' + str(i) for i in range(101)]
        caller = Mock(return_value=verdict)
        with self.assertRaises(ReviewReferenceErrors):
            self.run_review(caller=caller)
        self.assertEqual(caller.call_count, 1)
        self.assertEqual(len(self.read('reference_errors.json')), 101)


if __name__ == '__main__':
    unittest.main()
