"""Multi-review finding conservation, explicit closure and portable admission."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from architecture import fingerprint
from finding_ledger import (finding_key, new_entries, source_from_report, obligations,
                            validate_entries, merge_entries, validate_ledger)
from generate_specification import propose_specification
from repair_specification import plan_repair, repair_and_review
from review_specification import (ASPECTS, ReviewChangesRequired, ReviewReferenceErrors,
    review_specification, validate_prior_report, validate_pass_report, build_review_prompt,
    review_snapshot, main)
from review_references import reference_index
from specification import SpecificationError, read_document
from specification_lowering import _compile_snapshot, compile_package, require_approval
from specification_request import system_projection
from token_usage import UsageRun
from test_generate_specification import register_fixture


class FindingLedgerTests(unittest.TestCase):
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
        self.root = Path(temp.name)
        self.counter = 0
        self.event = '/documents/shared/events/capture'
        self.ids = {p: r for r, p in reference_index(review_snapshot(self.manifest))['targets'].items()}
        self.clean = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []}
        self.finding = {'id': 'F_CAPTURE', 'scope': 'architecture', 'resolution': 'design_repair',
                        'message': 'Mocked observation concern.', 'references': [self.event], 'requirement_ids': []}
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def verdict(self, *findings):
        return {**copy.deepcopy(self.clean), 'status': 'changes_required' if findings else 'passed',
                'findings': copy.deepcopy(list(findings))}

    def review(self, verdict, prior=(), context=None, correction=None, manifest=None):
        self.counter += 1
        output = self.root / str(self.counter)
        caller = Mock(side_effect=[verdict] + ([correction] if correction else []))
        error = None
        try:
            approval = review_specification(manifest or self.manifest, output_dir=output, caller=caller,
                                           prior_review_reports=prior, repair_context=context)
        except SpecificationError as exc:
            approval, error = None, exc
        return read_document(output / 'report.json'), approval, error, caller

    def assessment(self, key, *, open_ids=(), reference=None):
        return {'ledger_key': key, 'status': 'unresolved' if open_ids else 'resolved',
                'explanation': 'Explicit mock adjudication of the previous claim.',
                'references': [reference or self.ids[self.event]], 'remaining_finding_ids': list(open_ids)}

    def test_first_review_records_all_findings_with_stable_distinct_keys(self):
        second = {**self.finding, 'id': 'F_SECOND', 'message': 'Separate concern.'}
        report, approval, error, _ = self.review(self.verdict(self.finding, second))
        self.assertIsInstance(error, ReviewChangesRequired)
        self.assertIsNone(approval)
        ledger = report['finding_ledger']
        self.assertEqual(len(ledger['entries']), 2)
        self.assertTrue(all(e['status'] == 'open' for e in ledger['entries']))
        self.assertEqual({e['finding']['id'] for e in ledger['entries']}, {'F_CAPTURE', 'F_SECOND'})
        validate_prior_report(report, self.manifest)
        self.assertEqual(ledger, read_document(self.root / '1/finding_ledger.json'))

    def test_silent_drop_in_clean_rereview_is_rejected_without_correction(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        report, approval, error, caller = self.review(self.clean, [first])
        self.assertIsInstance(error, SpecificationError)
        self.assertIn('finding_assessments', str(error))
        self.assertIsNone(approval)
        self.assertEqual(caller.call_count, 1)
        self.assertEqual(report['finding_ledger']['sources'][0]['entries'], first['finding_ledger']['entries'])

    def test_explicit_false_positive_disposition_can_close_unchanged_artifact(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        verdict = {**self.clean, 'finding_assessments': [self.assessment(key)]}
        verdict['finding_assessments'][0]['explanation'] = 'Prior allegation is unsupported: the existing serialized schedule prevents the claimed collision.'
        report, approval, error, _ = self.review(verdict, [first])
        self.assertIsNone(error)
        require_approval(self.manifest, approval)
        entry = report['finding_ledger']['entries'][0]
        self.assertEqual(entry['finding'], self.finding)
        self.assertEqual(entry['status'], 'resolved')
        self.assertEqual(len(entry['history']), 2)
        self.assertIn('unsupported', entry['history'][-1]['explanation'])
        # Closed findings remain in the next report without requiring redundant answers.
        third, approval, error, _ = self.review(self.clean, [report])
        self.assertIsNone(error)
        self.assertEqual(third['finding_ledger']['entries'], report['finding_ledger']['entries'])
        require_approval(self.manifest, approval)

    def test_alias_and_new_finding_are_both_carried_until_explicitly_assessed(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        alias = {**self.finding, 'id': 'F_RENAMED', 'message': 'Rephrased remaining concern.'}
        new = {**self.finding, 'id': 'F_NEW', 'message': 'New unrelated obligation.'}
        second, _, error, _ = self.review({**self.verdict(alias, new),
            'finding_assessments': [self.assessment(key, open_ids=['F_RENAMED'])]}, [first])
        self.assertIsInstance(error, ReviewChangesRequired)
        self.assertEqual(len(second['finding_ledger']['entries']), 3)
        self.assertTrue(all(e['status'] == 'open' for e in second['finding_ledger']['entries']))
        # Linking an alias is not closure of the old allegation.
        third, approval, error, _ = self.review({**self.clean, 'finding_assessments': [
            self.assessment(e['key']) for e in second['finding_ledger']['entries'][:-1]]}, [second])
        self.assertIsNotNone(error)
        self.assertIsNone(approval)
        fourth, approval, error, _ = self.review({**self.clean, 'finding_assessments': [
            self.assessment(e['key']) for e in second['finding_ledger']['entries']]}, [second])
        self.assertIsNone(error)
        self.assertTrue(all(e['status'] == 'resolved' for e in fourth['finding_ledger']['entries']))
        require_approval(self.manifest, approval)

    def test_missing_duplicate_unknown_empty_evidence_and_bad_links_cannot_close(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        good = self.assessment(key)
        cases = [[], [good, good], [self.assessment('unknown')], [{**good, 'references': []}],
                 [{**good, 'explanation': ''}], [{**good, 'status': 'unresolved', 'remaining_finding_ids': []}],
                 [{**good, 'status': 'unresolved', 'remaining_finding_ids': ['missing']}]]
        for values in cases:
            with self.subTest(values=values):
                _, approval, error, caller = self.review({**self.clean, 'finding_assessments': values}, [first])
                self.assertIsNotNone(error)
                self.assertIsNone(approval)
                self.assertEqual(caller.call_count, 1)

    def test_reference_only_correction_covers_ledger_assessments_without_changing_disposition(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        verdict = {**self.clean, 'finding_assessments': [self.assessment(key, reference='/wrong')]}
        correction = {'corrections': {'/finding_assessments/0/references/0': self.ids[self.event]}}
        report, approval, error, caller = self.review(verdict, [first], correction=correction)
        self.assertIsNone(error)
        self.assertEqual(caller.call_count, 2)
        self.assertEqual(report['finding_ledger']['entries'][0]['status'], 'resolved')
        require_approval(self.manifest, approval)

    def test_failed_reference_review_retains_new_finding_and_cannot_close_old_one(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        new = {**self.finding, 'id': 'F_REFRESH', 'message': 'New maintenance concern.', 'references': ['/bad']}
        second, _, error, _ = self.review({**self.verdict(new), 'finding_assessments': [self.assessment(key)]},
                                         [first], correction={'corrections': {}})
        self.assertIsInstance(error, ReviewReferenceErrors)
        self.assertEqual(second['status'], 'failed')
        self.assertTrue(all(e['status'] == 'open' for e in second['finding_ledger']['entries']))
        validate_prior_report(second, self.manifest)
        third, approval, error, _ = self.review(self.clean, [second])
        self.assertIsNotNone(error)
        self.assertIsNone(approval)
        self.assertEqual(len(obligations(third['finding_ledger']['sources'])), 2)

    def test_historical_misspelled_locations_do_not_need_current_reference_resolution(self):
        # Legacy reference-only failed report has substantive findings worth retaining.
        first, _, _, _ = self.review(self.verdict(self.finding))
        first.pop('finding_ledger')
        first.pop('reference_protocol')
        first['status'] = 'failed'
        first['verdict']['findings'][0]['references'] = ['/documents/shared/system/rules/WRONG_ROOT']
        validate_prior_report(first, self.manifest)
        key = source_from_report(first)['entries'][0]['key']
        report, approval, error, _ = self.review({**self.clean, 'finding_assessments': [self.assessment(key)]}, [first])
        self.assertIsNone(error)
        self.assertEqual(report['finding_ledger']['entries'][0]['finding']['references'],
                         ['/documents/shared/system/rules/WRONG_ROOT'])
        require_approval(self.manifest, approval)

    def test_wrong_artifact_configuration_and_nonreference_failures_stop_before_call(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        cases = []
        stale = copy.deepcopy(first); stale['manifest_fingerprint'] = 'other'; cases.append(stale)
        bad = copy.deepcopy(first); bad['verdict']['findings'][0]['scope'] = 'invalid'; cases.append(bad)
        malformed = copy.deepcopy(first); malformed['status'] = 'failed'; malformed['verdict'] = None; cases.append(malformed)
        for i, prior in enumerate(cases):
            caller = Mock(return_value=self.clean)
            output = self.root / ('invalid' + str(i))
            with self.subTest(i=i), self.assertRaises(SpecificationError):
                review_specification(self.manifest, output_dir=output, caller=caller, prior_review_reports=[prior])
            caller.assert_not_called()
            self.assertFalse(output.exists())
        configured = compile_package(self.package, {'DEPTH': 1})
        with self.assertRaises(SpecificationError):
            review_specification(configured, output_dir=self.root / 'configured', prior_review_reports=[first])

    def test_same_finding_id_in_different_reports_does_not_overwrite_claims(self):
        a, _, _, _ = self.review(self.verdict(self.finding))
        other = {**self.finding, 'message': 'Different allegation sharing the same display ID.'}
        b, _, _, _ = self.review(self.verdict(other))
        sources = [source_from_report(a), source_from_report(b)]
        self.assertEqual(len(obligations(sources)), 2)
        report, approval, error, _ = self.review({**self.clean, 'finding_assessments': [
            self.assessment(e['key']) for e in obligations(sources)]}, [a, b])
        self.assertIsNone(error)
        self.assertEqual(len(report['finding_ledger']['entries']), 2)
        require_approval(self.manifest, approval)

    def test_merging_older_open_and_later_closed_history_keeps_explicit_closure(self):
        a, _, _, _ = self.review(self.verdict(self.finding))
        key = a['finding_ledger']['entries'][0]['key']
        b, _, _, _ = self.review({**self.clean, 'finding_assessments': [self.assessment(key)]}, [a])
        c, approval, error, _ = self.review(self.clean, [a, b, a])
        self.assertIsNone(error)
        self.assertEqual(len(c['finding_ledger']['entries']), 1)
        self.assertEqual(c['finding_ledger']['entries'][0]['status'], 'resolved')
        require_approval(self.manifest, approval)
        branch = copy.deepcopy(b)
        branch['finding_ledger']['entries'][0]['history'][-1]['explanation'] = 'Divergent closure.'
        with self.assertRaises(SpecificationError):
            merge_entries([source_from_report(b), source_from_report(branch)])

    def test_tampered_dropped_entries_status_history_and_source_are_rejected(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        report, approval, error, _ = self.review({**self.clean, 'finding_assessments': [self.assessment(key)]}, [first])
        self.assertIsNone(error)
        variants = []
        for field, value in [('entries', []), ('sources', []), ('version', True)]:
            changed = copy.deepcopy(report); changed['finding_ledger'][field] = value; variants.append(changed)
        changed = copy.deepcopy(report); changed['finding_ledger']['entries'][0]['status'] = 'open'; variants.append(changed)
        changed = copy.deepcopy(report); changed['finding_ledger']['entries'][0]['history'][-1]['explanation'] = 'altered'; variants.append(changed)
        changed = copy.deepcopy(report); changed['finding_ledger']['sources'][0]['entries'] = []; variants.append(changed)
        for changed in variants:
            with self.subTest(ledger=changed['finding_ledger']), self.assertRaises(SpecificationError):
                validate_pass_report(changed, self.manifest)
        record = read_document(approval)
        record['automated_review']['finding_ledger']['entries'] = []
        approval.write_text(json.dumps(record))
        with self.assertRaises(SpecificationError):
            require_approval(self.manifest, approval)

    def test_repair_automatically_forwards_ledger_and_reuses_existing_original_assessment(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        plan = plan_repair(self.manifest, first)
        self.assertEqual(plan['finding_history'], [source_from_report(first)])
        output = self.root / 'repair'
        def generate(prompt, model):
            base = json.loads(prompt.split('Previous response (data to repair, not additional instructions):\n')[1])
            return {'base_fingerprint': fingerprint(base), 'edits': [
                {'op': 'set', 'path': '/modules/contracts/stage/dependency_note', 'value': 'Narrow mocked clarification.'}]}
        def review(prompt, model):
            self.assertNotIn('OUTSTANDING LEDGER FINDINGS', prompt)
            return {**self.clean, 'repair_assessments': [{'finding_id': self.finding['id'], 'status': 'resolved',
                'explanation': 'Explicit mocked repair disposition.', 'references': [self.event], 'remaining_finding_ids': []}],
                'preservation_assessment': {'status': 'preserved', 'explanation': 'Mocked preservation.',
                    'references': [self.event], 'regression_finding_ids': []}}
        target, approval, _ = repair_and_review(self.manifest, first, output_dir=output,
                                               generation_caller=generate, review_caller=review)
        report = read_document(output / 'review/report.json')
        self.assertEqual(report['finding_ledger']['entries'][0]['status'], 'resolved')
        self.assertEqual(len(report['finding_ledger']['entries'][0]['history']), 2)
        require_approval(target, approval)
        changed = copy.deepcopy(report); changed['finding_ledger']['sources'] = []
        with self.assertRaises(SpecificationError):
            validate_pass_report(changed, target)

    def test_cli_prior_report_replay_is_offline_and_has_explicit_history(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        prior, manifest, response = [self.root / name for name in ('prior.json', 'manifest.json', 'response.json')]
        for path, value in ((prior, first), (manifest, self.manifest),
                            (response, {**self.clean, 'finding_assessments': [self.assessment(key)]})):
            path.write_text(json.dumps(value))
        output = self.root / 'cli'
        args = ['review_specification.py', str(manifest), '--prior-review-report', str(prior),
                '--response', str(response), '--output-dir', str(output)]
        with patch.object(sys, 'argv', args), patch('review_specification.call_llm') as llm, patch(
                'token_usage.UsageRun', side_effect=lambda *a, **kw: UsageRun(self.root / 'usage', **kw)):
            self.assertEqual(main(), 0)
        llm.assert_not_called()
        require_approval(self.manifest, output / 'approval.json')

    def test_closed_finding_reappearing_on_same_artifact_is_reopened(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        closed, _, error, _ = self.review({**self.clean, 'finding_assessments': [self.assessment(key)]}, [first])
        self.assertIsNone(error)
        reopened, _, error, _ = self.review(self.verdict(self.finding), [closed])
        self.assertIsInstance(error, ReviewChangesRequired)
        entry = reopened['finding_ledger']['entries'][0]
        self.assertEqual(entry['key'], key)
        self.assertEqual(entry['status'], 'open')
        self.assertEqual([e['status'] for e in entry['history']], ['open', 'resolved', 'open'])

    def test_transport_failure_retains_input_history_without_claiming_new_dispositions(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        output = self.root / 'transport'
        caller = Mock(side_effect=SpecificationError('Mock provider failure'))
        with self.assertRaises(SpecificationError):
            review_specification(self.manifest, output_dir=output, prior_review_reports=[first], caller=caller)
        failed = read_document(output / 'report.json')
        self.assertEqual(failed['finding_ledger']['entries'], first['finding_ledger']['entries'])
        self.assertEqual(failed['status'], 'failed')
        validate_prior_report(failed, self.manifest)
        retry, approval, error, _ = self.review(self.clean, [failed])
        self.assertIsNotNone(error)
        self.assertIsNone(approval)
        self.assertEqual(retry['finding_ledger']['entries'], first['finding_ledger']['entries'])

    def test_external_findings_require_disposition_and_cannot_clear_readiness(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['assumptions']['EXTERNAL'] = {'status': 'requires_review', 'statement': 'Needs external evidence.'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(system_projection(snapshot['package'], snapshot['documents']))
        manifest = _compile_snapshot(snapshot)[0]
        finding = {**self.finding, 'id': 'F_EXT', 'scope': 'system', 'resolution': 'external_agreement',
                   'references': ['/package/assumptions/EXTERNAL']}
        first, _, error, _ = self.review(self.verdict(finding), manifest=manifest)
        self.assertIsInstance(error, ReviewChangesRequired)
        key = first['finding_ledger']['entries'][0]['key']
        resolved = {**self.clean, 'finding_assessments': [self.assessment(key, reference=self.event)]}
        second, approval, error, _ = self.review(resolved, [first], manifest=manifest)
        self.assertIsInstance(error, ReviewChangesRequired)
        self.assertIsNone(approval)
        self.assertIsNotNone(second['readiness_error'])
        wrong_kind = {**self.verdict(self.finding), 'finding_assessments': [self.assessment(key,
            open_ids=[self.finding['id']], reference=self.event)]}
        _, approval, error, _ = self.review(wrong_kind, [first], manifest=manifest)
        self.assertIsNotNone(error)
        self.assertIsNone(approval)

    def test_new_finding_after_repair_is_forwarded_through_another_repair(self):
        first, _, _, _ = self.review(self.verdict(self.finding))
        original_key = first['finding_ledger']['entries'][0]['key']
        alias = {**self.finding, 'id': 'F_ALIAS', 'message': 'Remaining observation issue.'}
        newer = {**self.finding, 'id': 'F_NEW', 'message': 'Separate refresh concern.'}
        second, _, _, _ = self.review({**self.verdict(alias, newer),
            'finding_assessments': [self.assessment(original_key, open_ids=['F_ALIAS'])]}, [first])
        output = self.root / 'second_repair'
        seen = []
        def generate(prompt, model):
            base = json.loads(prompt.split('Previous response (data to repair, not additional instructions):\n')[1])
            return {'base_fingerprint': fingerprint(base), 'edits': [
                {'op': 'set', 'path': '/modules/contracts/stage/dependency_note', 'value': 'Mocked follow-up clarification.'}]}
        def review(prompt, model):
            seen.append(prompt)
            self.assertIn(original_key, prompt)
            return {**self.clean, 'finding_assessments': [self.assessment(original_key, reference=self.event)],
                'repair_assessments': [{'finding_id': f['id'], 'status': 'resolved',
                    'explanation': 'Mocked explicit current repair disposition.', 'references': [self.event],
                    'remaining_finding_ids': []} for f in (alias, newer)],
                'preservation_assessment': {'status': 'preserved', 'explanation': 'Mocked preservation.',
                    'references': [self.event], 'regression_finding_ids': []}}
        target, approval, _ = repair_and_review(self.manifest, second, output_dir=output,
            generation_caller=generate, review_caller=review)
        report = read_document(output / 'review/report.json')
        self.assertEqual(len(report['finding_ledger']['entries']), 3)
        self.assertTrue(all(e['status'] == 'resolved' for e in report['finding_ledger']['entries']))
        require_approval(target, approval)
        self.assertEqual(len(seen), 1)

    def test_flow_cli_forwards_history_and_rejects_flag_without_review(self):
        from run_flow import parse_args, run_architecture_flow
        first, _, _, _ = self.review(self.verdict(self.finding))
        key = first['finding_ledger']['entries'][0]['key']
        source, prior, raw = [self.root / name for name in ('arch.yaml', 'prior.json', 'replay.json')]
        for path, value in ((source, self.manifest), (prior, first),
                           (raw, {**self.clean, 'finding_assessments': [self.assessment(key)]})):
            path.write_text(json.dumps(value))
        base_args = ['run_flow.py', str(source), '--microarch-only', '--prior-spec-review-report', str(prior)]
        with patch.object(sys, 'argv', base_args):
            args = parse_args()
        with self.assertRaisesRegex(ValueError, 'requires --automated'):
            run_architecture_flow(source, args)
        with patch.object(sys, 'argv', base_args + ['--automated-spec-review', '--spec-review-response', str(raw),
                '--spec-review-output', str(self.root / 'flow-review'), '--architecture-output', str(self.root / 'export/architecture.yaml')]):
            args = parse_args()
        with patch('run_flow.run_command') as rtl, patch('review_specification.call_llm') as llm:
            self.assertEqual(run_architecture_flow(source, args), 0)
        rtl.assert_not_called()
        llm.assert_not_called()
        require_approval(self.manifest, self.root / 'flow-review/approval.json')


if __name__ == '__main__':
    unittest.main()
