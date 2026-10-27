"""Bounded semantic repair on an unrelated register pipeline; no API calls."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from architecture import fingerprint
from generate_specification import propose_specification
from repair_specification import plan_repair, repair_and_review
from finding_ledger import obligations
from review_specification import ASPECTS, REVIEW_REVISION, ReviewChangesRequired
from specification import SpecificationError, read_document
from specification_lowering import _compile_snapshot, compile_package, require_approval
from specification_request import system_projection
from specification_change_audit import audit_changes
from test_generate_specification import register_fixture
from test_specification_prompts import repair_context


class RepairSpecificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.TemporaryDirectory()
        cls.base_root = Path(cls.base.name)
        _, request, cls.responses = register_fixture()
        replay = cls.base_root / 'replay.json'
        replay.write_text(json.dumps(cls.responses))
        with contextlib.redirect_stdout(io.StringIO()):
            cls.package = propose_specification(request, output_dir=cls.base_root / 'source', response_path=replay)
        cls.manifest = compile_package(cls.package)

    @classmethod
    def tearDownClass(cls):
        cls.base.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.generation_prompts, self.review_prompts = [], []

    def report(self, scopes=('architecture',), manifest=None):
        manifest = manifest or self.manifest
        return {'review_version': 1, 'review_revision': REVIEW_REVISION, 'status': 'changes_required',
                'model': 'offline-fixture', 'mode': 'replay', 'reviewed_at': 'offline-fixture',
                'manifest_fingerprint': fingerprint(manifest),
                'package_fingerprint': manifest['provenance']['specification']['package_fingerprint'],
                'readiness_error': None, 'verdict': {
                    'status': 'changes_required', 'reviewed_aspects': list(ASPECTS),
                    'findings': [{'id': f'F{i}', 'scope': scope, 'resolution': 'design_repair',
                                  'message': 'Clarify the declared capture behavior and its observation plan.',
                                  'references': ['/documents/shared/events/capture'],
                                  'requirement_ids': [manifest['requirements'][0]['id']]}
                                 for i, scope in enumerate(scopes)]}}

    def run_repair(self, report=None, replies=None, final=None, manifest=None):
        responses = copy.deepcopy(self.responses)
        responses['architecture']['modules']['contracts']['stage']['dependency_note'] = 'Explicit registered-byte dependency statement.'
        if replies is None:
            replies = [responses['architecture']]
        replies = iter(replies)
        def generate(prompt, model):
            self.generation_prompts.append(prompt)
            reply = next(replies)
            if ('SYSTEM EDIT MODE:' in prompt or 'ARCHITECTURE EDIT MODE:' in prompt) and reply.get('status') == 'supported':
                # Legacy fixture convenience only: production rejects complete
                # responses in edit mode. Translate intended fixture differences.
                base = json.loads(prompt.split('Previous response (data to repair, not additional instructions):\n')[1])
                if 'ARCHITECTURE EDIT MODE:' in prompt:
                    reply = copy.deepcopy(reply)
                    reply.setdefault('internal_types', copy.deepcopy(base['internal_types']))
                changes = audit_changes(base, reply)
                reply = {'base_fingerprint': fingerprint(base), 'edits': [
                    {'op': 'remove', 'path': c['path']} if c['op'] == 'remove' else
                    {'op': 'set', 'path': c['path'], 'value': c['after']} for c in changes] or [
                    ({'op': 'set', 'path': '/package/revision', 'value': base['package']['revision']}
                     if 'SYSTEM EDIT MODE:' in prompt else
                     {'op': 'set', 'path': '/graph/integration', 'value': base['graph']['integration']})]}
            return json.dumps(reply)
        def review(prompt, model):
            self.review_prompts.append(prompt)
            value = copy.deepcopy(final or {'status': 'passed', 'reviewed_aspects': list(ASPECTS), 'findings': []})
            original = (report or self.report())['verdict']['findings']
            open_ids = [f['id'] for f in value['findings'] if f['resolution'] == 'design_repair']
            value['repair_assessments'] = [
                {'finding_id': f['id'], 'status': 'unresolved' if open_ids else 'resolved',
                 'explanation': 'Offline fixture assessment, not model evidence.',
                 'references': ['/documents/shared/events/capture'], 'remaining_finding_ids': open_ids}
                for f in original if f['resolution'] == 'design_repair']
            value['preservation_assessment'] = {'status': 'preserved',
                'explanation': 'Offline fixture preservation assessment.',
                'references': ['/documents/shared/events/capture'], 'regression_finding_ids': []}
            context = read_document(self.root / 'repair/repair_review_context.json')
            pending = obligations(context.get('finding_history', []), context)
            if pending:
                value['finding_assessments'] = [{'ledger_key': e['key'], 'status': 'resolved',
                    'explanation': 'Mocked disposition; independent readiness still applies.',
                    'references': ['/documents/shared/events/capture'], 'remaining_finding_ids': []} for e in pending]
            return json.dumps(value)
        with contextlib.redirect_stdout(io.StringIO()):
            return repair_and_review(manifest or self.manifest, report or self.report(), output_dir=self.root / 'repair',
                                     generation_caller=generate, review_caller=review)

    def test_architecture_route_preserves_locked_system_library_and_request(self):
        original = copy.deepcopy(self.manifest)
        repaired, approval, export = self.run_repair()
        old = original['provenance']['specification']['snapshot']
        new = repaired['provenance']['specification']['snapshot']
        self.assertEqual(system_projection(old['package'], old['documents']), system_projection(new['package'], new['documents']))
        self.assertEqual(old['timing_library'], new['timing_library'])
        self.assertEqual(old['package']['generation']['request'], new['package']['generation']['request'])
        self.assertEqual(len(self.generation_prompts), 1)
        self.assertIsNotNone(repair_context(self.generation_prompts[0])['locked_system'])
        self.assertEqual(len(self.review_prompts), 1)
        self.assertEqual(require_approval(repaired, approval)['approval_version'], 3)
        self.assertEqual(read_document(export), repaired)
        self.assertEqual(self.manifest, original)
        self.assertNotEqual(fingerprint(repaired), fingerprint(original))
        self.assertFalse(list(self.root.rglob('*.sv')))

    def test_system_findings_dominate_and_regenerate_architecture_against_new_system(self):
        responses = copy.deepcopy(self.responses)
        responses['system']['shared']['events']['capture']['effect'] += ' Preserve the declared next-edge visibility.'
        repaired, _, _ = self.run_repair(report=self.report(('architecture', 'system')),
                                         replies=[responses['system'], responses['architecture']])
        self.assertEqual(len(self.generation_prompts), 2)
        first, second = map(repair_context, self.generation_prompts)
        self.assertIsNone(first['locked_system'])
        self.assertIn('format_example', second['capabilities'])
        self.assertIn('FRESH DECOMPOSITION:', self.generation_prompts[1])
        self.assertEqual(second['locked_system']['shared']['events']['capture'], responses['system']['shared']['events']['capture'])
        self.assertIn('old architecture is invalidated', self.generation_prompts[1])
        self.assertEqual(read_document(self.root / 'repair/repair_plan.json')['stage'], 'system')
        snapshot = repaired['provenance']['specification']['snapshot']
        self.assertEqual(snapshot['package']['generation']['request'], self.manifest['provenance']['specification']['snapshot']['package']['generation']['request'])
        self.assertEqual(snapshot['documents']['shared']['events']['capture'], responses['system']['shared']['events']['capture'])

    def test_fresh_architecture_then_candidate_repair_keep_all_shape_diagnostics(self):
        broken = copy.deepcopy(self.responses['architecture'])
        contract = broken['modules']['contracts']['stage']
        contract['owned_state'] = 'Registered data and valid.'
        contract['simultaneous_events'] = 'Reset wins.'
        wiring = broken['graph']['clock_reset']
        wiring['source'] = {'clock': wiring.pop('clock_source'), 'reset': wiring.pop('reset_source')}
        edits = {'base_fingerprint': fingerprint(broken), 'edits': [
            {'op': 'set', 'path': '/modules/contracts/stage/' + key,
             'value': self.responses['architecture']['modules']['contracts']['stage'][key]}
            for key in ('owned_state', 'simultaneous_events')] + [
            {'op': 'set', 'path': '/graph/clock_reset',
             'value': self.responses['architecture']['graph']['clock_reset']}]}
        self.run_repair(report=self.report(('system',)),
                        replies=[self.responses['system'], broken, edits])
        self.assertEqual(len(self.generation_prompts), 3)
        first_arch = repair_context(self.generation_prompts[1])
        retry_arch = repair_context(self.generation_prompts[2])
        self.assertIn('format_example', first_arch['capabilities'])
        self.assertNotIn('format_example', retry_arch['capabilities'])
        self.assertIn('ARCHITECTURE EDIT MODE:', self.generation_prompts[2])
        for message in ('stage.owned_state:', 'stage.simultaneous_events:', 'graph.clock_reset:'):
            self.assertIn(message, self.generation_prompts[2])
        journal = read_document(self.root / 'repair/source/attempts.json')
        rejected = [a for a in journal['attempts'] if a['status'] == 'rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(rejected[0]['diagnostics']), 3)
        self.assertEqual(json.loads(rejected[0]['response']), broken)

    def test_failed_second_review_stops_after_one_cycle_and_retains_report(self):
        final = self.report()['verdict']
        with self.assertRaises(ReviewChangesRequired):
            self.run_repair(final=final)
        self.assertEqual(len(self.generation_prompts), 1)
        self.assertEqual(len(self.review_prompts), 1)
        self.assertEqual(read_document(self.root / 'repair/repair_status.json')['status'], 'stopped')
        self.assertTrue((self.root / 'repair/review/report.json').exists())
        self.assertFalse((self.root / 'repair/review/approval.json').exists())

    def test_malformed_or_stale_report_stops_before_generation_and_output(self):
        for key, value in [('status', 'failed'), ('review_revision', REVIEW_REVISION + 1),
                           ('manifest_fingerprint', 'stale')]:
            report = self.report()
            report[key] = value
            with self.subTest(key=key), self.assertRaises(SpecificationError):
                self.run_repair(report=report)
            self.assertFalse((self.root / 'repair').exists())
        self.assertEqual(self.generation_prompts, [])

    def test_readiness_only_blocker_is_not_sent_for_a_model_repair(self):
        report = self.report()
        report['verdict'].update(status='passed', findings=[])
        report['readiness_error'] = 'External review needed'
        with self.assertRaisesRegex(SpecificationError, 'explicit disposition'):
            self.run_repair(report=report)
        self.assertEqual(self.generation_prompts, [])

    def external_parent(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['assumptions']['EXTERNAL'] = {'status': 'requires_review', 'statement': 'External timing provenance.'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(system_projection(snapshot['package'], snapshot['documents']))
        return _compile_snapshot(snapshot)[0]

    def external_finding(self):
        return {'id': 'EXT', 'scope': 'system', 'resolution': 'external_agreement',
                'message': 'External timing provenance requires agreement.',
                'references': ['/package/assumptions/EXTERNAL'], 'requirement_ids': []}

    def test_external_only_finding_stops_before_calls_or_output(self):
        parent = self.external_parent()
        report = self.report(manifest=parent)
        report['verdict']['findings'] = [self.external_finding()]
        report['readiness_error'] = 'EXTERNAL requires agreement'
        with self.assertRaisesRegex(SpecificationError, 'external agreement requires explicit disposition'):
            self.run_repair(report=report, manifest=parent)
        self.assertEqual(self.generation_prompts, [])
        self.assertEqual(self.review_prompts, [])
        self.assertFalse((self.root / 'repair').exists())

    def test_mixed_findings_route_architecture_preserve_blocker_and_recheck(self):
        parent = self.external_parent()
        report = self.report(manifest=parent)
        report['verdict']['findings'].append(self.external_finding())
        report['readiness_error'] = 'EXTERNAL requires agreement'
        before = copy.deepcopy(parent)
        plan = plan_repair(parent, report)
        self.assertEqual(plan['stage'], 'architecture')
        self.assertEqual(plan['scopes'], ['architecture'])
        self.assertEqual([f['id'] for f in plan['findings']], ['F0'])
        self.assertEqual([f['id'] for f in plan['external_findings']], ['EXT'])
        # A clean model re-review cannot clear the independent readiness gate.
        with self.assertRaisesRegex(ReviewChangesRequired, 'EXTERNAL'):
            self.run_repair(report=report, manifest=parent)
        self.assertEqual(len(self.generation_prompts), 1)
        self.assertEqual(len(self.review_prompts), 1)
        self.assertIn('not external_findings', self.generation_prompts[0])
        saved = read_document(self.root / 'repair/repair_plan.json')
        self.assertEqual(saved, plan)
        export = read_document(self.root / 'repair/architecture.yaml')
        self.assertEqual(export['provenance']['specification']['snapshot']['package']['assumptions']['EXTERNAL'],
                         parent['provenance']['specification']['snapshot']['package']['assumptions']['EXTERNAL'])
        self.assertFalse((self.root / 'repair/review/approval.json').exists())
        self.assertEqual(parent, before)

    def test_repairable_system_issue_still_dominates_external_and_architecture(self):
        parent = self.external_parent()
        report = self.report(('architecture', 'system'), parent)
        report['verdict']['findings'].append(self.external_finding())
        plan = plan_repair(parent, report)
        self.assertEqual(plan['stage'], 'system')
        self.assertEqual(len(plan['findings']), 2)
        self.assertEqual(len(plan['external_findings']), 1)

    def test_system_repair_cannot_resolve_blocking_decision(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['decisions']['EXT_DECISION'] = {
            'origin': 'model_selected', 'status': 'requires_review', 'value': 'pending', 'reason': 'Needs external selection.'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(system_projection(snapshot['package'], snapshot['documents']))
        parent = _compile_snapshot(snapshot)[0]
        with self.assertRaisesRegex(SpecificationError, 'decision requiring external review'):
            self.run_repair(report=self.report(('system',), parent), manifest=parent,
                            replies=[copy.deepcopy(self.responses['system'])] * 3)
        self.assertEqual(len(self.generation_prompts), 3)
        self.assertEqual(self.review_prompts, [])

    def test_compiler_repairs_remain_bounded_and_keep_semantic_findings(self):
        broken = copy.deepcopy(self.responses['architecture'])
        broken['system'] = {'attempted': 'locked-system mutation'}
        with self.assertRaises(SpecificationError):
            self.run_repair(replies=[broken] * 3)
        self.assertEqual(len(self.generation_prompts), 3)
        self.assertTrue(all('F0' in prompt for prompt in self.generation_prompts))
        self.assertEqual(self.review_prompts, [])
        self.assertFalse((self.root / 'repair/source').exists())
        self.assertTrue((self.root / 'repair/source.failure.json').exists())

    def test_automatic_repair_cannot_erase_external_review_assumptions(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['assumptions']['EXTERNAL'] = {'status': 'requires_review', 'statement': 'External board fact.'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(system_projection(snapshot['package'], snapshot['documents']))
        parent = _compile_snapshot(snapshot)[0]
        replies = [copy.deepcopy(self.responses['system'])] * 3  # Omits EXTERNAL.
        with self.assertRaisesRegex(SpecificationError, 'cannot remove or resolve'):
            self.run_repair(report=self.report(('system',), parent), manifest=parent, replies=replies)
        self.assertEqual(len(self.generation_prompts), 3)
        self.assertEqual(self.review_prompts, [])
        self.assertFalse((self.root / 'repair/source').exists())

    def test_automatic_repair_cannot_erase_a_question_requiring_agreement(self):
        snapshot = copy.deepcopy(self.manifest['provenance']['specification']['snapshot'])
        snapshot['package']['open_questions']['EXTERNAL_Q'] = {
            'blocks': 'design_review', 'question': 'Which externally required interface convention is intended?'}
        snapshot['package']['generation']['system_fingerprint'] = fingerprint(system_projection(snapshot['package'], snapshot['documents']))
        parent = _compile_snapshot(snapshot)[0]
        with self.assertRaisesRegex(SpecificationError, 'question requiring agreement'):
            self.run_repair(report=self.report(('system',), parent), manifest=parent,
                            replies=[copy.deepcopy(self.responses['system'])] * 3)
        self.assertEqual(len(self.generation_prompts), 3)
        self.assertEqual(self.review_prompts, [])

    def test_selected_configuration_is_preserved(self):
        configured = compile_package(self.package, {'DEPTH': 1})
        repaired, _, _ = self.run_repair(report=self.report(manifest=configured), manifest=configured)
        self.assertEqual(repaired['provenance']['specification']['snapshot']['overrides'], {'DEPTH': 1})

    def test_existing_output_is_not_overwritten(self):
        self.run_repair()
        self.generation_prompts.clear()
        with self.assertRaisesRegex(SpecificationError, 'already exists'):
            self.run_repair()
        self.assertEqual(self.generation_prompts, [])

    def test_flow_routes_rejection_and_proposal_only_keeps_original_export(self):
        from run_flow import parse_args, run_architecture_flow
        repaired, approval, export = self.run_repair()
        parent = self.root / 'parent.yaml'
        parent.write_text(json.dumps(self.manifest))
        before = parent.read_bytes()
        report_path = self.root / 'initial_report.json'
        report_path.write_text(json.dumps(self.report()))
        argv = ['run_flow.py', str(parent), '--automated-spec-review', '--auto-spec-repair', '--microarch-only']
        with patch.object(sys, 'argv', argv):
            args = parse_args()
        with patch('review_specification.review_specification', side_effect=ReviewChangesRequired('fixture rejection', report_path)), \
             patch('repair_specification.repair_and_review', return_value=(repaired, approval, export)) as repair, \
             patch('run_flow.run_command') as rtl, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_architecture_flow(parent, args), 0)
        repair.assert_called_once()
        rtl.assert_not_called()
        self.assertEqual(parent.read_bytes(), before)
        self.assertEqual(require_approval(read_document(export), approval)['approval_version'], 3)

    def test_flags_require_automated_review_and_do_not_call_models(self):
        from run_flow import parse_args, run_architecture_flow
        parent = self.root / 'parent.yaml'
        parent.write_text(json.dumps(self.manifest))
        for flags in (['--auto-spec-repair'], ['--spec-repair-output', str(self.root / 'out')],
                      ['--automated-spec-review', '--auto-spec-repair', '--resume']):
            with patch.object(sys, 'argv', ['run_flow.py', str(parent), *flags]):
                args = parse_args()
            with patch('review_specification.call_llm') as model, self.assertRaises(ValueError):
                run_architecture_flow(parent, args)
            model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
