"""Offline evidence for repair preservation; no model calls or behavioral simulation."""
import copy
import json
from pathlib import Path
import unittest

from architecture import fingerprint
from specification_change_audit import audit_changes


class SpecificationChangeAuditTests(unittest.TestCase):
    def test_equal_data_and_mapping_order_produce_no_changes(self):
        self.assertEqual(audit_changes({'b': [1, 2], 'a': {'x': 3}},
                                       {'a': {'x': 3}, 'b': [1, 2]}), [])

    def test_add_remove_and_replace_are_distinct_including_null(self):
        self.assertEqual(audit_changes({'gone': None, 'value': 1}, {'added': None, 'value': 2}), [
            {'op': 'add', 'path': '/added', 'after': None},
            {'op': 'remove', 'path': '/gone', 'before': None},
            {'op': 'replace', 'path': '/value', 'before': 1, 'after': 2}])

    def test_lists_are_atomic_and_json_types_are_distinct(self):
        before = {'items': [1, {'x': True}], 'flag': True}
        after = {'items': [1, {'x': 1}], 'flag': 1}
        changes = audit_changes(before, after)
        self.assertEqual([c['path'] for c in changes], ['/flag', '/items'])
        self.assertTrue(all(c['op'] == 'replace' for c in changes))

    def test_paths_escape_object_keys(self):
        self.assertEqual(audit_changes({'a/b~c': {'': 1}}, {'a/b~c': {'': 2}})[0]['path'], '/a~1b~0c/')

    def test_results_and_inputs_have_independent_nested_values(self):
        before = {'items': [{'x': 1}]}
        after = {'items': [{'x': 2}]}
        expected_before, expected_after = copy.deepcopy(before), copy.deepcopy(after)
        changes = audit_changes(before, after)
        changes[0]['before'][0]['x'] = 3
        changes[0]['after'][0]['x'] = 4
        self.assertEqual(before, expected_before)
        self.assertEqual(after, expected_after)

    def load_fixture(self):
        return json.loads((Path(__file__).parent / 'testdata/repair_preservation_audit.json').read_text())

    def test_saved_failure_has_stable_exact_change_evidence(self):
        fixture = self.load_fixture()
        before = copy.deepcopy(fixture)
        self.assertEqual(fingerprint(fixture['before']), fixture['before_fingerprint'])
        self.assertEqual(fingerprint(fixture['after']), fixture['after_fingerprint'])
        changes = audit_changes(fixture['before'], fixture['after'])
        self.assertEqual([c['path'] for c in changes], [
            '/execution/rules/EXEC_CONTROL_CREATION/statement',
            '/execution/rules/EXEC_REFRESH/statement',
            '/shared/events/candidate_create/condition',
            '/shared/events/cleanup_candidate_create/condition',
            '/shared/events/cleanup_candidate_create/effect',
            '/shared/events/dispatch/condition',
            '/shared/events/dispatch/effect',
            '/shared/events/refresh_candidate_create/condition',
            '/shared/types/command/semantics',
            '/shared/types/request/legal_values',
            '/shared/types/request/mapping',
            '/system/rules/SYS_CONTROL_PRIORITY/statement'])
        self.assertEqual([c['path'] for c in changes if c['op'] == 'remove'], [
            '/shared/types/command/semantics', '/shared/types/request/legal_values', '/shared/types/request/mapping'])
        self.assertEqual(audit_changes(fixture['before'], fixture['after']), changes)
        self.assertEqual(fixture, before)

    def test_saved_regression_records_broadened_guards_and_drain_requirement(self):
        fixture = self.load_fixture()
        changes = {c['path']: c for c in audit_changes(fixture['before'], fixture['after'])}
        for event in ('dispatch', 'candidate_create', 'cleanup_candidate_create'):
            change = changes[f'/shared/events/{event}/condition']
            self.assertNotIn('refresh_pending is low', change['before'])
            self.assertIn('refresh_pending is low', change['after'])
        self.assertIn('waits for already owned active work and cleanup completion',
                      fixture['after']['execution']['rules']['EXEC_REFRESH']['statement'])
        self.assertTrue(any(f['id'] == 'F_REFRESH_PENDING_DISPATCH_RACE' for f in fixture['initial_findings']))
        self.assertTrue(any(f['id'] == 'F_REFRESH_DRAIN_DEADLOCK' for f in fixture['post_repair_findings']))
        # These assertions reproduce evidence of this known defect; they do not
        # infer arbitrary event semantics or execute a controller reference model.

    def test_audit_distinguishes_illustrative_narrow_change_from_saved_rewrite(self):
        fixture = self.load_fixture()
        narrow = copy.deepcopy(fixture['before'])
        narrow['shared']['events']['dispatch']['condition'] = fixture['after']['shared']['events']['dispatch']['condition']
        changes = audit_changes(fixture['before'], narrow)
        self.assertEqual([c['path'] for c in changes], ['/shared/events/dispatch/condition'])
        for event in ('candidate_create', 'cleanup_candidate_create'):
            self.assertEqual(narrow['shared']['events'][event], fixture['before']['shared']['events'][event])
        # Illustrative change scope only: this is not a generated/approved repair.


if __name__ == '__main__':
    unittest.main()
