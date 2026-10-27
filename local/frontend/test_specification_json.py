"""Bounded specification JSON recovery; no model calls or permissive parsing."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from architecture import ArchitectureError, fingerprint
from generate_microarch import parse_response
from generate_specification import parse_specification_response, propose_specification
from specification import SpecificationError, read_document
from test_generate_specification import register_fixture


class SpecificationJSONTests(unittest.TestCase):
    def test_valid_json_and_braces_in_strings_are_unchanged(self):
        value = {'text': 'An escaped quote " and braces } {', 'nested': {'items': [1, 2]}}
        for raw in (json.dumps(value), ' \n' + json.dumps(value) + '\n',
                    '```json\n' + json.dumps(value) + '\n```'):
            self.assertEqual(parse_specification_response(raw), (value, None))

    def test_only_one_redundant_closing_brace_is_removed(self):
        value = {'nested': {'text': 'literal }'}}
        for raw in (json.dumps(value) + '}', ' \n' + json.dumps(value) + '\n } \n'):
            decoded, recovery = parse_specification_response(raw)
            self.assertEqual(decoded, value)
            self.assertEqual(recovery, {'kind': 'extra_trailing_closing_brace', 'removed': '}'})

    def test_other_invalid_responses_and_duplicate_keys_remain_rejected(self):
        for raw in ('{} }}', '{} {}', '{} explanation', '{} } explanation',
                    '{"a":', '{"a":1', '{"a":1,}}', '{"a":1,"a":2}}',
                    '{"nested":{"a":1,"a":2}}}', '[]}', '[1]',
                    '```json\n{}}\n```', '{}]'):
            with self.subTest(raw=raw), self.assertRaises(ArchitectureError):
                parse_specification_response(raw)

    def test_shared_parser_remains_strict(self):
        with self.assertRaises(ArchitectureError):
            parse_response('{}}')

    def test_both_stages_recover_without_extra_calls_and_preserve_raw_journals(self):
        _, request, responses = register_fixture()
        raw = [json.dumps(responses[stage]) + '}' for stage in ('system', 'architecture')]
        replies, prompts = iter(raw), []
        def caller(prompt, model):
            prompts.append(prompt)
            return next(replies)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            root = Path(directory)
            package = propose_specification(request, output_dir=root / 'source', caller=caller)
            journal = json.loads((package.parent / 'attempts.json').read_text())
            self.assertEqual(len(prompts), 2)
            self.assertEqual([a['response'] for a in journal['attempts']], raw)
            self.assertTrue(all(a['status'] == 'accepted' and a['parse_recovery'] for a in journal['attempts']))
            self.assertEqual(read_document(package)['generation']['validation_repairs'], [])
            self.assertIn('full validation still required', output.getvalue())
            self.assertFalse(list(root.rglob('*.sv')))
            self.assertFalse(list(root.rglob('*approval*')))

    def test_architecture_nesting_repairs_remain_bounded_and_journal_raw_responses(self):
        _, request, responses = register_fixture()
        nested = copy.deepcopy(responses['architecture'])
        nested['modules']['internal_types'] = {}
        malformed = copy.deepcopy(nested)
        for key in ('graph', 'verification', 'rule_owners'):
            malformed['modules'][key] = malformed.pop(key)
        # Once valid JSON supplies a complete candidate, compiler repair uses
        # the existing edit protocol rather than another complete response.
        edits = {'base_fingerprint': fingerprint(nested),
                 'edits': [{'op': 'remove', 'path': '/modules/internal_types'}]}
        raw = [json.dumps(responses['system']), json.dumps(malformed)[:-1],
               json.dumps(nested), json.dumps(edits)]
        replies, prompts = iter(raw), []
        def caller(prompt, model):
            prompts.append(prompt)
            return next(replies)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            package = propose_specification(request, output_dir=root / 'source', caller=caller)
            journal = json.loads((package.parent / 'attempts.json').read_text())
            self.assertEqual([a['response'] for a in journal['attempts']], raw)
            self.assertEqual([a['status'] for a in journal['attempts']],
                             ['accepted', 'rejected', 'rejected', 'accepted'])
            self.assertIn('Check object closures', prompts[2])
            self.assertIn('modules.internal_types belongs at the response root', prompts[3])
            self.assertIn('ARCHITECTURE EDIT MODE', prompts[3])
            self.assertEqual(journal['attempts'][-1]['repair_base_fingerprint'], fingerprint(nested))
            self.assertEqual(journal['attempts'][-1]['applied_response'], responses['architecture'])
            self.assertTrue(all('parse_recovery' not in a for a in journal['attempts']))
            self.assertFalse(list(root.rglob('*.sv')))
            self.assertFalse(list(root.rglob('*approval*')))

    def test_recovered_json_still_fails_semantic_validation_and_bounded_attempts(self):
        _, request, responses = register_fixture()
        broken = copy.deepcopy(responses['system'])
        broken['package']['review'] = {'outcome': 'approved'}
        raw = json.dumps(broken) + '}'
        calls = []
        def caller(prompt, model):
            calls.append(prompt)
            return raw
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            with self.assertRaisesRegex(SpecificationError, 'no review may be fabricated'):
                propose_specification(request, output_dir=root / 'source', caller=caller)
            journal = json.loads((root / 'source.attempts.json').read_text())
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(a['status'] == 'rejected' and a['parse_recovery'] for a in journal['attempts']))
            self.assertEqual([a['response'] for a in journal['attempts']], [raw] * 3)
            self.assertFalse((root / 'source').exists())


if __name__ == '__main__':
    unittest.main()
