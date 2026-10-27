"""Review criteria evidence and gate regressions; no semantic prose solver or LLM."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from architecture import fingerprint
from finding_ledger import obligations, source_from_report
from generate_specification import propose_specification
from repair_specification import plan_repair
from review_specification import (ASPECTS, ReviewChangesRequired, blocking_review_policy,
                                  build_review_prompt, review_specification, validate_prior_report)
from specification import SpecificationError, read_document
from specification_lowering import compile_package, require_approval
from test_generate_specification import register_fixture
from test_review_specification import prompt_snapshot


def deadlines_absolute(interval, resets, end):
    """Manual example implementation using an absolute clock/deadline."""
    deadline = None
    due = []
    for edge in range(end):
        if edge in resets:
            deadline = edge + interval
        elif edge == deadline:
            due.append(edge)
            deadline += interval
    return due


def deadlines_elapsed(interval, resets, end):
    """Independent example using elapsed cycles, with no absolute deadline register."""
    elapsed = None
    due = []
    for edge in range(end):
        if edge in resets:
            elapsed = 0
        elif elapsed is not None:
            elapsed += 1
            if elapsed % interval == 0:
                due.append(edge)
    return due


class ReviewPolicyEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = read_document(Path(__file__).parent / 'testdata/review_refresh_policy.json')

    def test_saved_evidence_includes_timing_and_ownership_without_exhaustive_update_restriction(self):
        snapshot = self.fixture['snapshot']
        docs = snapshot['documents']
        contract = docs['modules']['contracts']['refresh']
        self.assertNotIn('every_clock', contract['update_events'])
        self.assertIn('E0+tREFI', docs['system']['rules']['SYS_REFRESH']['statement'])
        self.assertIn('independent of REF commit', docs['system']['rules']['SYS_REFRESH']['statement'])
        self.assertIn('At E+tRFC, the exclusion ends', docs['execution']['rules']['EXEC_REFRESH_EXCLUSION']['statement'])
        self.assertIn('Deadlines continue', contract['stalls'])
        self.assertIn('Periodic tREFI deadline', contract['owned_state']['deadline'])
        self.assertIn('Committed REF age', contract['owned_state']['service_state'])
        # These fixture facts support the manual analysis; no generic classifier
        # infers behavior or approval from absence/presence of a phrase.
        self.assertNotIn('only', json.dumps(contract['update_events']).lower())

    def test_two_internal_representations_obey_same_required_deadlines_across_resets(self):
        for interval in (1, 2, 7, 13):
            for resets in ({0}, {0, 1, 2}, {0, 8, 19}, {0, 7, 8, 9}):
                with self.subTest(interval=interval, resets=resets):
                    absolute = deadlines_absolute(interval, resets, 70)
                    elapsed = deadlines_elapsed(interval, resets, 70)
                    self.assertEqual(absolute, elapsed)
                    self.assertTrue(absolute)
                    self.assertEqual(next(e for e in absolute if e > max(resets)), max(resets) + interval)

    def test_service_age_and_absolute_expiry_agree_at_inclusive_release_boundary(self):
        # Selected rule interpretation only; these are not target RTL traces.
        for committed_edge in (0, 3, 12):
            for trfc in (1, 4, 9):
                absolute = [edge for edge in range(committed_edge + 1, committed_edge + trfc + 3)
                            if edge < committed_edge + trfc]
                age = 0
                counted = []
                for edge in range(committed_edge + 1, committed_edge + trfc + 3):
                    age += 1
                    if age < trfc:
                        counted.append(edge)
                self.assertEqual(counted, absolute)
                self.assertNotIn(committed_edge + trfc, counted)

    def test_never_advancing_state_violates_existing_rule_rather_than_demonstrating_ambiguity(self):
        required = deadlines_absolute(7, {0, 1}, 24)
        stuck_implementation = []
        self.assertEqual(required[0], 8)
        self.assertNotEqual(stuck_implementation, required)
        # The allegation's failing implementation is not a second behavior
        # satisfying ALL the saved clauses: it misses an already required due edge.

    def test_explicit_exhaustive_update_restriction_can_make_progress_impossible(self):
        contract = copy.deepcopy(self.fixture['snapshot']['documents']['modules']['contracts']['refresh'])
        contract['stalls'] += ' All timebase state may change only on refresh_due; reset clears it to zero.'
        # Manual abstraction of this deliberately added, contradictory restriction:
        # due depends on nonzero elapsed state; absent due, every transition stutters.
        elapsed = 0
        for _ in range(20):
            due = elapsed >= 7
            if due:
                elapsed += 7
        self.assertEqual(elapsed, 0)
        self.assertFalse(due)
        self.assertIn('only on refresh_due', contract['stalls'])
        self.assertTrue(deadlines_absolute(7, {0}, 20))
        # This differs from the saved contract, which does not contain that restriction.

    def test_genuinely_missing_first_deadline_choice_allows_materially_different_schedules(self):
        # A weakened illustrative contract says only "periodic, interval 7";
        # both schedules satisfy it, but disagree on observable initial behavior.
        a, b = list(range(7, 36, 7)), list(range(14, 36, 7))
        self.assertTrue(all(y - x == 7 for schedule in (a, b) for x, y in zip(schedule, schedule[1:])))
        self.assertNotEqual(a[0], b[0])
        # The real saved first-deadline clause removes that choice.
        self.assertEqual(deadlines_absolute(7, {0}, 36), a)

    def test_review_prompt_keeps_saved_evidence_losslessly_without_selecting_a_verdict(self):
        manifest = {'provenance': {'specification': {'snapshot': self.fixture['snapshot']}}}
        before = copy.deepcopy(manifest)
        prompt = build_review_prompt(manifest)
        self.assertEqual(prompt_snapshot(prompt, manifest), self.fixture['snapshot'])
        self.assertEqual(manifest, before)
        self.assertEqual(prompt.count(blocking_review_policy()), 1)
        self.assertIn('permitted by ALL clauses', prompt)
        self.assertIn('allowed-update restriction preventing required progress still blocks', prompt)
        self.assertNotIn('SYS_REFRESH', blocking_review_policy())
        self.assertNotIn('E0+tREFI', blocking_review_policy())
        self.assertNotIn('Use system scope for package/shared/system/', prompt)


class ReviewPolicyGateTests(unittest.TestCase):
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
        self.event = '/documents/shared/events/capture'
        self.clean = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []}
        self.finding = {'id': 'F_CONFLICT', 'scope': 'architecture', 'resolution': 'design_repair',
            'message': 'The contract must obey the existing locked capture rule; correct its conflicting update.',
            'references': ['/documents/system/rules/' + self.manifest['requirements'][0]['id'],
                           '/documents/modules/contracts/stage'], 'requirement_ids': []}
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def rejection(self, finding, directory='first'):
        with self.assertRaises(ReviewChangesRequired):
            review_specification(self.manifest, output_dir=self.root / directory,
                caller=Mock(return_value={**self.clean, 'status': 'changes_required', 'findings': [finding]}))
        return read_document(self.root / directory / 'report.json')

    def test_architecture_finding_citing_system_rule_stays_blocking_and_routes_narrowly(self):
        report = self.rejection(self.finding)
        self.assertEqual(plan_repair(self.manifest, report)['stage'], 'architecture')
        self.assertEqual(report['verdict']['findings'][0], self.finding)
        self.assertFalse((self.root / 'first/approval.json').exists())
        self.assertEqual(report['finding_ledger']['entries'][0]['status'], 'open')

    def test_system_finding_still_routes_to_system_without_automatic_reclassification(self):
        finding = {**self.finding, 'scope': 'system',
            'message': 'The locked expected result contradicts the required response edge; correct the locked expected result.'}
        report = self.rejection(finding)
        self.assertEqual(plan_repair(self.manifest, report)['stage'], 'system')
        self.assertEqual(report['verdict']['findings'][0], finding)

    def test_policy_change_does_not_silently_close_a_carried_finding(self):
        prior = self.rejection(self.finding)
        caller = Mock(return_value=self.clean)
        with self.assertRaises(SpecificationError):
            review_specification(self.manifest, output_dir=self.root / 'missing', prior_review_reports=[prior], caller=caller)
        self.assertEqual(caller.call_count, 1)
        self.assertFalse((self.root / 'missing/approval.json').exists())
        key = obligations([source_from_report(prior)])[0]['key']
        verdict = {**self.clean, 'finding_assessments': [{'ledger_key': key, 'status': 'resolved',
            'explanation': 'Mocked explicit adjudication: current contract already obeys the locked rule; previous allegation unsupported.',
            'references': [self.event], 'remaining_finding_ids': []}]}
        approval = review_specification(self.manifest, output_dir=self.root / 'explicit',
            prior_review_reports=[prior], caller=Mock(return_value=verdict))
        require_approval(self.manifest, approval)
        report = read_document(self.root / 'explicit/report.json')
        self.assertEqual(report['finding_ledger']['entries'][0]['status'], 'resolved')
        validate_prior_report(report, self.manifest)


if __name__ == '__main__':
    unittest.main()
