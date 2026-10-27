"""Offline semantic-review admission checks on an unrelated register design."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from architecture import ROOT, fingerprint, lower_architecture
from generate_specification import propose_specification
from review_specification import (ASPECTS, REVIEW_REVISION, build_review_prompt,
                                  review_snapshot, review_specification, validate_verdict)
from specification import SpecificationError, SpecificationErrors, read_document
from specification_lowering import _compile_snapshot, compile_package, require_approval
from test_generate_specification import register_fixture
from review_references import expand_reference_view, reference_index


def prompt_snapshot(prompt, manifest):
    from prompt_context import expand_context
    wire = json.loads(prompt.split("SNAPSHOT AND SELECTED CONFIGURATION:\n")[1])
    return expand_reference_view(expand_context(wire), reference_index(review_snapshot(manifest)))


class ReviewSpecificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture_dir.name)
        _, request, responses = register_fixture()
        cls.responses = responses
        raw = cls.root / 'generation.json'
        raw.write_text(json.dumps(responses))
        with contextlib.redirect_stdout(io.StringIO()):
            package = propose_specification(request, output_dir=cls.root / 'source', response_path=raw)
        cls.manifest = compile_package(package)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.verdict = {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []}

    def review(self, verdict=None, manifest=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return review_specification(manifest or self.manifest, output_dir=self.directory / 'review',
                                        caller=lambda prompt, model: json.dumps(verdict or self.verdict), **kwargs)

    def changed_manifest(self, change):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        change(snapshot)
        # This helper deliberately creates a fresh synthetic elaboration; production
        # mutation without renewed provenance remains rejected.
        from specification_request import system_projection
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(
            system_projection(snapshot['package'], snapshot['documents']))
        return _compile_snapshot(snapshot)[0]

    def finding(self):
        return {'id': 'F1', 'scope': 'system', 'resolution': 'design_repair', 'message': 'The event and expected result conflict.',
                'references': ['/documents/shared/events/capture'],
                'requirement_ids': [self.manifest['requirements'][0]['id']]}

    def test_saved_response_case_keeps_contract_evidence_and_requests_reachable_failures(self):
        fixture = read_document(ROOT / 'testdata/review_reachability_context.json')
        original = copy.deepcopy(fixture)
        parent, target = [fixture[key]['documents'] for key in ('parent', 'target')]
        self.assertEqual(parent['system']['rules']['SYS_MEMORY_AND_RESPONSE'],
                         target['system']['rules']['SYS_MEMORY_AND_RESPONSE'])
        self.assertEqual(parent['modules']['contracts']['command_engine']['interfaces'],
                         target['modules']['contracts']['command_engine']['interfaces'])
        for docs in (parent, target):
            engine = docs['modules']['contracts']['command_engine']
            self.assertIn('only on a later edge', engine['update_events']['shared.events.dispatch'])
            self.assertEqual(engine['simultaneous_events']['retirement_and_dispatch'],
                             'Retirement does not permit same-edge replacement dispatch.')
            self.assertIn('E+1', docs['execution']['rules']['EXEC_RESPONSE_EDGE']['statement'])
        prompt = build_review_prompt({'provenance': {'specification': {'snapshot': fixture['target']}}})
        self.assertIn('reachable edge sequences', prompt)
        self.assertIn('Scheduling can enforce safety without feedback wiring', prompt)
        self.assertIn('only defects introduced by the repair', prompt)
        self.assertIn('No read may retire while a prior response is valid', prompt)
        self.assertEqual(fixture, original)

    def test_manual_serialized_response_abstraction_excludes_claimed_collision(self):
        # Manually interpret the saved clauses: one active request, no dispatch
        # on retirement, later registered candidate formation, one-cycle response.
        # This is NOT execution of prose/RTL or automatic approval of the package.
        states = {(False, False, True)}  # immediately after a READ: active,candidate,response
        saw_next_read = False
        for _ in range(8):
            following = set()
            for active, candidate, response in states:
                for dispatch in (False, True):
                    for form in (False, True):
                        for commit in (False, True):
                            if dispatch and active or form and (not active or candidate) or commit and not candidate:
                                continue
                            if commit:
                                self.assertFalse(response, 'a second READ overlaps the prior response')
                                saw_next_read = True
                            following.add(((active or dispatch) and not commit,
                                           (candidate or form) and not commit, commit))
            states = following
        self.assertTrue(saw_next_read)  # the abstraction permits progress, not just indefinite stalls

    def test_pass_approval_is_distinct_revision_bound_and_allows_lowering(self):
        original = copy.deepcopy(self.manifest)
        approval = self.review()
        value = require_approval(self.manifest, approval)
        self.assertEqual(value['approval_version'], 3)
        self.assertTrue(value['reviewer'].startswith('automated:llm:'))
        self.assertEqual(value['automated_review']['manifest_fingerprint'], fingerprint(self.manifest))
        self.assertEqual(len(lower_architecture(self.manifest, self.directory / 'build', specification_approval=approval)), 1)
        self.assertEqual(self.manifest, original)
        self.assertFalse(list((self.directory / 'review').rglob('*.sv')))

    def test_findings_block_and_persist_without_approval(self):
        self.verdict.update(status='changes_required', findings=[self.finding()])
        with self.assertRaisesRegex(SpecificationError, 'requires changes'):
            self.review()
        report = read_document(self.directory / 'review/report.json')
        self.assertEqual(report['status'], 'changes_required')
        self.assertEqual(report['verdict']['findings'], self.verdict['findings'])
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_malformed_incomplete_and_inconsistent_verdicts_are_rejected(self):
        candidates = [[], {'status': 'passed'},
                      {**self.verdict, 'reviewed_aspects': list(ASPECTS[:-1])},
                      {**self.verdict, 'status': 'changes_required'},
                      {**self.verdict, 'findings': [self.finding()]},
                      {**self.verdict, 'extra': 'ignored'}]
        for value in candidates:
            with self.subTest(value=value), self.assertRaises(SpecificationError):
                validate_verdict(value, self.manifest)

    def test_findings_require_real_locations_known_requirements_and_unique_ids(self):
        for field, value in [('references', ['/documents/missing']), ('references', []),
                             ('references', ['/documents/~oops']),
                             ('requirement_ids', ['NOT_A_REQUIREMENT']), ('scope', 'anything')]:
            finding = self.finding()
            finding[field] = value
            verdict = {**self.verdict, 'status': 'changes_required', 'findings': [finding]}
            with self.subTest(field=field, value=value), self.assertRaises(SpecificationError):
                validate_verdict(verdict, self.manifest)
        duplicate = {**self.verdict, 'status': 'changes_required', 'findings': [self.finding(), self.finding()]}
        with self.assertRaises(SpecificationError):
            validate_verdict(duplicate, self.manifest)

    def test_external_assumption_cannot_be_approved_by_a_clean_model_verdict(self):
        changed = self.changed_manifest(lambda s: s['package']['assumptions'].update(
            EXTERNAL={'status': 'requires_review', 'statement': 'A board property needs external confirmation.'}))
        with self.assertRaisesRegex(SpecificationError, 'EXTERNAL'):
            self.review(manifest=changed)
        report = read_document(self.directory / 'review/report.json')
        self.assertEqual(report['verdict']['status'], 'passed')
        self.assertEqual(report['status'], 'changes_required')
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_external_finding_requires_classification_and_a_recorded_blocker(self):
        finding = self.finding()
        for value in (None, 'skip', []):
            finding['resolution'] = value
            with self.subTest(value=value), self.assertRaises(SpecificationError):
                validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, self.manifest)
        finding['resolution'] = 'external_agreement'
        with self.assertRaisesRegex(SpecificationError, 'recorded blocking entry'):
            validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, self.manifest)
        del finding['resolution']
        with self.assertRaisesRegex(SpecificationError, 'missing fields'):
            validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, self.manifest)

    def test_external_finding_accepts_only_blocking_entry_references(self):
        def change(snapshot):
            package = snapshot['package']
            package['assumptions']['EXT'] = {'statement': 'External timing provenance.', 'status': 'requires_review'}
            package['assumptions']['DOCUMENTED'] = {'statement': 'Recorded fixture fact.', 'status': 'documented'}
            package['open_questions']['EXT_Q'] = {'blocks': 'design_review', 'question': 'External agreement needed.'}
            package['open_questions']['APPROVE'] = {'blocks': 'rtl_generation', 'question': 'Approve exact export.'}
            package['decisions']['EXT_D'] = {'origin': 'model_selected', 'status': 'requires_review',
                                            'value': 'unresolved', 'reason': 'External selection needed.'}
        changed = self.changed_manifest(change)
        finding = {**self.finding(), 'resolution': 'external_agreement'}
        verdict = {**self.verdict, 'status': 'changes_required', 'findings': [finding]}
        for pointer in ('/package/assumptions/EXT', '/package/assumptions/EXT/statement',
                        '/package/open_questions/EXT_Q', '/package/decisions/EXT_D'):
            finding['references'] = [pointer]
            with self.subTest(pointer=pointer):
                validate_verdict(verdict, changed)
        for pointer in ('/package/assumptions', '/package/assumptions/DOCUMENTED',
                        '/package/open_questions/APPROVE', '/package/decisions/D_RESET',
                        '/package/assumptions/EXT_NOT_REAL', '/package/assumptions/EXT/unknown'):
            finding['references'] = [pointer]
            with self.subTest(pointer=pointer), self.assertRaises(SpecificationError):
                validate_verdict(verdict, changed)
        finding.update(scope='architecture', references=['/package/assumptions/EXT'])
        with self.assertRaisesRegex(SpecificationError, 'system scope'):
            validate_verdict(verdict, changed)

    def test_external_only_review_still_blocks_and_persists_finding(self):
        changed = self.changed_manifest(lambda s: s['package']['assumptions'].update(
            EXT={'status': 'requires_review', 'statement': 'External provenance needs confirmation.'}))
        finding = {**self.finding(), 'resolution': 'external_agreement',
                   'references': ['/package/assumptions/EXT']}
        verdict = {**self.verdict, 'status': 'changes_required', 'findings': [finding]}
        with self.assertRaisesRegex(SpecificationError, 'external agreement'):
            self.review(verdict, manifest=changed)
        report = read_document(self.directory / 'review/report.json')
        self.assertEqual(report['verdict']['findings'], [finding])
        self.assertEqual(report['status'], 'changes_required')
        self.assertIn('EXT', report['readiness_error'])
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_stale_manifest_report_protocol_and_findings_cannot_reuse_approval(self):
        approval = self.review()
        original = read_document(approval)
        changed = self.changed_manifest(lambda s: s['package'].update(revision='changed'))
        with self.assertRaises(SpecificationError):
            require_approval(changed, approval)
        for key, value in [('review_revision', REVIEW_REVISION + 1), ('status', 'failed'),
                           ('manifest_fingerprint', 'wrong'), ('mode', 'invented')]:
            broken = copy.deepcopy(original)
            broken['automated_review'][key] = value
            approval.write_text(json.dumps(broken))
            with self.subTest(key=key), self.assertRaises(SpecificationError):
                require_approval(self.manifest, approval)
        broken = copy.deepcopy(original)
        broken['automated_review']['verdict'].update(status='changes_required', findings=[self.finding()])
        approval.write_text(json.dumps(broken))
        with self.assertRaises(SpecificationError):
            require_approval(self.manifest, approval)

    def test_changed_selected_configuration_invalidates_a_passing_review(self):
        approval = self.review()
        configured = self.changed_manifest(lambda s: s['overrides'].update(DEPTH=1))
        with self.assertRaisesRegex(SpecificationError, 'stale'):
            require_approval(configured, approval)

    def test_valid_finding_can_point_into_original_requirement_list(self):
        finding = self.finding()
        finding['references'] = ['/package/generation/request/requirements/0/text']
        validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, self.manifest)

    def test_invalid_verdict_is_saved_as_failed_without_approval(self):
        with self.assertRaises(SpecificationError):
            self.review({'status': 'passed'})
        self.assertEqual(read_document(self.directory / 'review/report.json')['status'], 'failed')
        self.assertTrue((self.directory / 'review/response.json').exists())
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_compressed_prompt_explains_expanded_reference_coordinates(self):
        from prompt_context import expand_context
        manifest = copy.deepcopy(self.manifest)
        snapshot = manifest['provenance']['specification']['snapshot']
        # Prompt-only metadata forces sharing without relying on fixture prose size.
        repeated = 'A long independent descriptive fact. ' * 30
        snapshot['package']['assumptions']['PROMPT_A'] = {'statement': repeated, 'status': 'documented'}
        snapshot['package']['assumptions']['PROMPT_B'] = {'statement': repeated, 'status': 'documented'}
        prompt = build_review_prompt(manifest)
        wire = json.loads(prompt.split('SNAPSHOT AND SELECTED CONFIGURATION:\n')[1])
        self.assertEqual(wire['context_format'], 'shared_definitions_v1')
        expected = copy.deepcopy(snapshot)
        expected['package']['generation'].pop('validation_repairs')
        self.assertEqual(prompt_snapshot(prompt, manifest), expected)
        self.assertIn('validation_repairs', snapshot['package']['generation'])
        for phrase in ('EXPANDED SNAPSHOT', 'Never prefix returned pointers with /context',
                       '/timing_library are sibling snapshot roots', 'REPAIR SCOPE'):
            self.assertIn(phrase, prompt)

    def test_only_repair_history_is_excluded_without_mutating_artifact(self):
        from prompt_context import expand_context
        changed = self.changed_manifest(lambda s: s['package']['generation']['validation_repairs'].append(
            {'stage': 'architecture', 'attempt': 2, 'error': 'STALE_UNDRIVEN_RESPONSE_SENTINEL'}))
        before = copy.deepcopy(changed)
        prompt = build_review_prompt(changed)
        view = prompt_snapshot(prompt, changed)
        expected = copy.deepcopy(changed['provenance']['specification']['snapshot'])
        del expected['package']['generation']['validation_repairs']
        self.assertEqual(view, expected)
        self.assertNotIn('STALE_UNDRIVEN_RESPONSE_SENTINEL', prompt)
        self.assertIn('CURRENT accepted artifact', prompt)
        self.assertEqual(changed, before)
        # The view also owns its nested data, so consumers cannot mutate provenance.
        view['documents']['graph']['connections'].clear()
        self.assertEqual(changed, before)

    def test_historical_reference_cannot_support_a_current_finding(self):
        changed = self.changed_manifest(lambda s: s['package']['generation']['validation_repairs'].append(
            {'stage': 'architecture', 'attempt': 2, 'error': 'Already repaired.'}))
        for pointer in ('/package/generation/validation_repairs', '/package/generation/validation_repairs/0'):
            finding = self.finding()
            finding['references'] = [pointer]
            with self.subTest(pointer=pointer), self.assertRaisesRegex(SpecificationError, 'Unknown review reference'):
                validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, changed)

    def test_review_fingerprints_still_bind_full_artifact_including_history(self):
        changed = self.changed_manifest(lambda s: s['package']['generation']['validation_repairs'].append(
            {'stage': 'architecture', 'attempt': 2, 'error': 'Already repaired.'}))
        before = copy.deepcopy(changed)
        approval = self.review(manifest=changed)
        value = require_approval(changed, approval)
        self.assertEqual(value['manifest_fingerprint'], fingerprint(changed))
        self.assertEqual(changed, before)
        changed_again = self.changed_manifest(lambda s: s['package']['generation']['validation_repairs'].append(
            {'stage': 'architecture', 'attempt': 2, 'error': 'Different audit history.'}))
        with self.assertRaises(SpecificationError):
            require_approval(changed_again, approval)

    def test_readiness_and_current_findings_remain_blocking_with_history_excluded(self):
        def change(snapshot):
            snapshot['package']['generation']['validation_repairs'].append({'error': 'Already repaired.'})
            snapshot['package']['assumptions']['PROFILE'] = {
                'status': 'requires_review', 'statement': 'External timing provenance needs agreement.'}
        changed = self.changed_manifest(change)
        self.assertIn('PROFILE', review_snapshot(changed)['package']['assumptions'])
        with self.assertRaisesRegex(SpecificationError, 'PROFILE'):
            self.review(manifest=changed)
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_saved_response_wiring_false_positive_is_not_in_current_review_context(self):
        from prompt_context import expand_context
        fixture = read_document(Path(__file__).parent / 'testdata/review_history_context.json')
        manifest = {'provenance': {'specification': {'snapshot': fixture['snapshot']}}, 'requirements': []}
        before = copy.deepcopy(manifest)
        prompt = build_review_prompt(manifest)
        view = prompt_snapshot(prompt, manifest)
        connections = view['documents']['graph']['connections']
        self.assertEqual(connections['host_adapter.rsp_valid'], ['top.rsp_valid'])
        self.assertEqual(connections['host_adapter.rsp_rdata'], ['top.rsp_rdata'])
        self.assertIn('A_PROFILE', view['package']['assumptions'])
        self.assertIn('Q_PROFILE_PROVENANCE', view['package']['open_questions'])
        self.assertNotIn('Undriven graph sinks', prompt)
        finding = copy.deepcopy(fixture['historical_finding'])
        finding['resolution'] = 'design_repair'
        finding['requirement_ids'] = []
        finding['references'] = ['/package/generation/validation_repairs/2']
        with self.assertRaisesRegex(SpecificationError, 'Unknown review reference'):
            validate_verdict({**self.verdict, 'status': 'changes_required', 'findings': [finding]}, manifest)
        self.assertEqual(manifest, before)

    def test_all_bad_reference_paths_are_reported_and_findings_retained(self):
        first = self.finding()
        first['references'] = ['/context/documents/shared/events/capture', '/package/timing_library']
        second = self.finding()
        second.update(id='F2', references=['/definitions/C001'])
        verdict = {**self.verdict, 'status': 'changes_required', 'findings': [first, second]}
        original = copy.deepcopy(verdict)
        with self.assertRaises(SpecificationErrors) as caught:
            self.review(verdict)
        self.assertEqual(len(caught.exception.errors), 3)
        report = read_document(self.directory / 'review/report.json')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['verdict'], original)
        failure = read_document(self.directory / 'review/failure.json')
        self.assertEqual(failure['diagnostics'], caught.exception.errors)
        self.assertEqual(verdict, original)
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_transport_failure_persists_failure_and_never_retries(self):
        calls = []
        def caller(prompt, model):
            calls.append(prompt)
            raise SpecificationError('Provider unavailable')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SpecificationError):
            review_specification(self.manifest, output_dir=self.directory / 'review', caller=caller)
        self.assertEqual(len(calls), 1)
        self.assertEqual(read_document(self.directory / 'review/report.json')['status'], 'failed')
        self.assertTrue((self.directory / 'review/failure.json').exists())
        self.assertFalse((self.directory / 'review/approval.json').exists())

    def test_review_prompt_includes_independent_cross_document_checks(self):
        prompt = build_review_prompt(self.manifest)
        for term in ('authoritative request', 'every required service mode', 'valid-low payload',
                     'declared observations', 'unresolved external assumptions', 'fresh downstream decomposition'):
            self.assertIn(term, prompt)
        self.assertNotIn('cleanup PRE', prompt)  # No target-specific behavioral template.

    def test_saved_approval_misclassification_has_explicit_prompt_policy_and_still_fails(self):
        from prompt_context import expand_context
        fixture = read_document(Path(__file__).parent / 'testdata/review_approval_classification.json')
        manifest = {'provenance': {'specification': {'snapshot': fixture['snapshot']}},
                    'requirements': fixture['requirements']}
        before = copy.deepcopy(manifest)
        prompt = build_review_prompt(manifest)
        self.assertIn('do not require separate external approval before this review can pass', prompt)
        self.assertIn('absence of that approval as a design defect or external_agreement finding', prompt)
        allowed = json.loads(prompt.split('Eligible external blocker reference IDs are: ')[1].split('. Separate external agreement', 1)[0])
        self.assertEqual({reference_index(review_snapshot(manifest))['targets'][ref] for ref in allowed},
                         {'/package/assumptions/A_PROFILE', '/package/open_questions/Q_PROFILE_PROVENANCE'})
        view = prompt_snapshot(prompt, manifest)
        self.assertEqual(view, fixture['snapshot'])
        self.assertEqual(view['package']['open_questions']['Q_RTL_APPROVAL']['blocks'], 'rtl_generation')
        profile, approval = fixture['findings']
        verdict = {**self.verdict, 'status': 'changes_required', 'findings': [profile]}
        validate_verdict(verdict, manifest)
        verdict['findings'] = [approval]
        with self.assertRaisesRegex(SpecificationError, 'recorded blocking entry'):
            validate_verdict(verdict, manifest)
        self.assertEqual(manifest, before)

    def test_clean_review_can_approve_draft_with_only_normal_rtl_gate(self):
        changed = self.changed_manifest(lambda s: s['package']['open_questions'].update(
            NORMAL_GATE={'blocks': 'rtl_generation', 'question': 'Approve this exact export before RTL.'}))
        before = copy.deepcopy(changed)
        approval = self.review(manifest=changed)
        require_approval(changed, approval)
        self.assertEqual(changed, before)
        self.assertEqual(changed['provenance']['specification']['snapshot']['package']['status'], 'draft_for_review')

    def test_prompt_eligible_paths_do_not_include_recorded_facts_or_normal_gate(self):
        def change(snapshot):
            snapshot['package']['assumptions']['DOC'] = {'status': 'documented', 'statement': 'Recorded assumption.'}
            snapshot['package']['open_questions']['NORMAL'] = {'blocks': 'rtl_generation', 'question': 'Approve export.'}
        changed = self.changed_manifest(change)
        prompt = build_review_prompt(changed)
        self.assertIn('Eligible external blocker reference IDs are: [].', prompt)

    def test_legacy_explicit_approvals_still_work(self):
        from specification_lowering import create_approval
        for confirmations in ((),):
            value = create_approval(self.manifest, 'fixture', review_confirmations=confirmations)
            path = self.directory / 'manual.json'
            path.write_text(json.dumps(value))
            self.assertEqual(require_approval(self.manifest, path)['approval_version'], 1)

    def test_existing_output_is_not_overwritten_or_called_again(self):
        self.review()
        with patch('review_specification.call_llm') as caller, self.assertRaises(SpecificationError):
            review_specification(self.manifest, output_dir=self.directory / 'review')
        caller.assert_not_called()

    def test_flow_proposal_only_runs_review_without_rtl_and_preserves_request(self):
        from run_flow import parse_args, run_architecture_flow
        config, _, _ = register_fixture()
        request_path = self.directory / 'request.yaml'
        request_path.write_text(json.dumps(config))
        original = request_path.read_bytes()
        generation = self.directory / 'generation.json'
        generation.write_text(json.dumps(self.responses))
        response = self.directory / 'verdict.json'
        response.write_text(json.dumps(self.verdict))
        argv = ['run_flow.py', str(request_path), '--specification', '--microarch-only',
                '--specification-response', str(generation), '--specification-output', str(self.directory / 'source'),
                '--architecture-output', str(self.directory / 'architecture.yaml'), '--automated-spec-review',
                '--spec-review-response', str(response), '--spec-review-output', str(self.directory / 'review')]
        with patch.object(sys, 'argv', argv):
            args = parse_args()
        with patch('run_flow.run_command') as command, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_architecture_flow(request_path, args), 0)
        command.assert_not_called()
        self.assertEqual(request_path.read_bytes(), original)
        exported = read_document(self.directory / 'architecture.yaml')
        approval = require_approval(exported, self.directory / 'review/approval.json')
        self.assertEqual(approval['automated_review']['mode'], 'replay')

    def test_clean_review_continues_existing_frontend_route_without_manual_approval(self):
        from dataclasses import asdict
        from run_flow import parse_args, run_architecture_flow
        from validator import validate_spec
        manifest_path = self.directory / 'architecture.yaml'
        manifest_path.write_text(json.dumps(self.manifest))
        response = self.directory / 'verdict.json'
        response.write_text(json.dumps(self.verdict))
        build = self.directory / 'build'
        argv = ['run_flow.py', str(manifest_path), '--automated-spec-review',
                '--spec-review-response', str(response), '--spec-review-output', str(self.directory / 'review'),
                '--build-dir', str(build), '--no-lint']
        with patch.object(sys, 'argv', argv):
            args = parse_args()
        commands = []
        def generate_fixture(command):
            commands.append(command)
            spec = validate_spec(command[2])
            (build / 'rtl_output' / (spec.design_name + '.sv')).write_text(
                f'module {spec.design_name}; endmodule // Interface-only test fixture.\n')
            ir = build / 'microarch/ir'
            ir.mkdir(parents=True, exist_ok=True)
            (ir / (spec.design_name + '_ir.json')).write_text(json.dumps(asdict(spec)))
        with patch('run_flow.run_command', side_effect=generate_fixture), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_architecture_flow(manifest_path, args), 0)
        self.assertEqual(len(commands), 1)
        status = read_document(build / 'build_status.json')
        self.assertEqual(status['result'], 'generated')
        self.assertEqual(status['behavioral_verification'], 'not_run')
        self.assertEqual(read_document(build / 'specification_approval.json')['approval_version'], 3)

    def test_rejected_review_never_invokes_rtl_or_creates_build(self):
        from run_flow import parse_args, run_architecture_flow
        manifest = self.directory / 'architecture.yaml'
        manifest.write_text(json.dumps(self.manifest))
        response = self.directory / 'verdict.json'
        self.verdict.update(status='changes_required', findings=[self.finding()])
        response.write_text(json.dumps(self.verdict))
        argv = ['run_flow.py', str(manifest), '--automated-spec-review',
                '--spec-review-response', str(response), '--spec-review-output', str(self.directory / 'review'),
                '--build-dir', str(self.directory / 'build')]
        with patch.object(sys, 'argv', argv):
            args = parse_args()
        with patch('run_flow.run_command') as command, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SpecificationError):
            run_architecture_flow(manifest, args)
        command.assert_not_called()
        self.assertFalse((self.directory / 'build').exists())

    def test_review_flags_do_not_start_network_for_invalid_inputs(self):
        from run_flow import parse_args, run_architecture_flow
        path = self.directory / 'generic.yaml'
        path.write_text('{}')
        for flags in (['--spec-review-output', str(self.directory / 'out')], ['--automated-spec-review']):
            with patch.object(sys, 'argv', ['run_flow.py', str(path), *flags]):
                args = parse_args()
            with patch('review_specification.call_llm') as caller, self.assertRaises(ValueError):
                run_architecture_flow(path, args)
            caller.assert_not_called()


if __name__ == '__main__':
    unittest.main()
