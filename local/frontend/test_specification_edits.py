"""Offline edit protocol and bounded real-compiler repair regressions."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from architecture import fingerprint
from generate_specification import propose_specification
from specification import SpecificationError
from specification_edits import apply_architecture_edits
from test_generate_specification import register_fixture


class ArchitectureEditsTests(unittest.TestCase):
    def setUp(self):
        _, self.request, self.fixture = register_fixture()
        self.base = copy.deepcopy(self.fixture['architecture'])

    def envelope(self, *edits):
        return {'base_fingerprint': fingerprint(self.base), 'edits': list(edits)}

    def test_precise_set_preserves_unrelated_fields_and_source(self):
        before = copy.deepcopy(self.base)
        edit = self.envelope({'op': 'set', 'path': '/graph/verification_outputs',
                              'value': {'stage.monitor': 'Used by checks.'}})
        result = apply_architecture_edits(self.base, edit)
        self.assertEqual(result['graph'].pop('verification_outputs'), edit['edits'][0]['value'])
        self.assertEqual(result, before)
        self.assertEqual(self.base, before)

    def test_escaped_keys_and_list_replacement(self):
        self.base['internal_types'] = {'a/b~c': {}}
        result = apply_architecture_edits(self.base, self.envelope(
            {'op': 'remove', 'path': '/internal_types/a~1b~0c'},
            {'op': 'set', 'path': '/graph/connections/top.in_valid', 'value': []}))
        self.assertEqual(result['internal_types'], {})
        self.assertEqual(result['graph']['connections']['top.in_valid'], [])

    def test_rejects_invalid_edits_atomically(self):
        invalid = [
            {'op': 'set', 'path': '/system', 'value': {}},
            {'op': 'set', 'path': '/status', 'value': 'unsupported'},
            {'op': 'set', 'path': '', 'value': {}},
            {'op': 'set', 'path': '/graph/~2bad', 'value': {}},
            {'op': 'set', 'path': '/missing/field', 'value': {}},
            {'op': 'set', 'path': '/graph/connections/top.in_valid/0', 'value': 'bad'},
            {'op': 'remove', 'path': '/graph/absent'},
            {'op': 'remove', 'path': '/graph', 'value': {}},
            {'op': 'move', 'path': '/graph'},
        ]
        before = copy.deepcopy(self.base)
        for edit in invalid:
            with self.subTest(edit=edit), self.assertRaises(SpecificationError):
                apply_architecture_edits(self.base, self.envelope(
                    {'op': 'set', 'path': '/graph/integration', 'value': 'temporary'}, edit))
            self.assertEqual(self.base, before)

    def test_rejects_wrong_base_empty_edits_and_full_response(self):
        for response in (self.base, {'base_fingerprint': 'stale', 'edits': []},
                         self.envelope(), self.envelope(*([{}] * 101))):
            with self.subTest(response=response), self.assertRaises(SpecificationError):
                apply_architecture_edits(self.base, response)

    def run_outputs(self, outputs):
        prompts = []
        iterator = iter(outputs)
        def caller(prompt, model):
            prompts.append(prompt)
            return json.dumps(next(iterator))
        with tempfile.TemporaryDirectory() as directory:
            package = propose_specification(self.request, output_dir=Path(directory) / 'source', caller=caller)
            journal = json.loads((package.parent / 'attempts.json').read_text())
            accepted = json.loads((package.parent / 'responses.json').read_text())
        return prompts, journal, accepted

    def test_invalid_edit_keeps_base_for_next_bounded_attempt(self):
        self.base['graph']['connections']['top.in_valid'] = ['stage.typo']
        good = self.envelope({'op': 'set', 'path': '/graph/connections/top.in_valid',
                              'value': ['stage.in_valid']})
        bad = {**good, 'base_fingerprint': 'stale'}
        prompts, journal, accepted = self.run_outputs([self.fixture['system'], self.base, bad, good])
        self.assertEqual([a['status'] for a in journal['attempts']],
                         ['accepted', 'rejected', 'rejected', 'accepted'])
        self.assertIn(fingerprint(self.base), prompts[-1])
        self.assertEqual(accepted['architecture'], self.fixture['architecture'])
        self.assertEqual(journal['attempts'][-1]['response'], json.dumps(good))

    def test_applied_invalid_candidate_is_next_base_and_still_validated(self):
        self.base['graph']['connections']['top.in_valid'] = ['stage.typo']
        first = self.envelope({'op': 'set', 'path': '/graph/connections/top.in_valid',
                               'value': ['stage.other_typo']})
        updated = apply_architecture_edits(self.base, first)
        second = {'base_fingerprint': fingerprint(updated), 'edits': [
            {'op': 'set', 'path': '/graph/connections/top.in_valid', 'value': ['stage.in_valid']}]}
        prompts, journal, accepted = self.run_outputs([self.fixture['system'], self.base, first, second])
        self.assertIn('stage.other_typo', journal['attempts'][2]['error'])
        self.assertIn(fingerprint(updated), prompts[-1])
        self.assertEqual(accepted['architecture'], self.fixture['architecture'])

    def test_malformed_json_keeps_full_regeneration_path(self):
        outputs = iter([json.dumps(self.fixture['system']), '{broken', json.dumps(self.base)])
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return next(outputs)
        with tempfile.TemporaryDirectory() as directory:
            propose_specification(self.request, output_dir=Path(directory) / 'source', caller=caller)
        self.assertIn('Return a COMPLETE corrected stage response', prompts[-1])
        self.assertNotIn('ARCHITECTURE EDIT MODE', prompts[-1])

    def test_invalid_edits_exhaust_existing_limit_without_publishing(self):
        self.base['graph']['connections']['top.in_valid'] = ['stage.typo']
        outputs = iter([self.fixture['system'], self.base, self.base, self.base])
        prompts = []
        def caller(prompt, model):
            prompts.append(prompt)
            return json.dumps(next(outputs))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'source'
            with self.assertRaisesRegex(SpecificationError, 'architecture specification failed'):
                propose_specification(self.request, output_dir=output, caller=caller)
            self.assertFalse(output.exists())
            journal = json.loads(output.with_name('source.attempts.json').read_text())
        self.assertEqual(len(prompts), 4)
        self.assertEqual([a['status'] for a in journal['attempts']],
                         ['accepted', 'rejected', 'rejected', 'rejected'])
        self.assertEqual(journal['attempts'][-1]['repair_base_fingerprint'], fingerprint(self.base))


if __name__ == '__main__':
    unittest.main()
