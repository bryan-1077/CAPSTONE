import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import test_module_probe as fixtures
from .specification_repair import prepare_workspace, check_workspace, recheck_workspace, publish_repair
from .mailbox import file_hashes


class RepairWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleProbeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.mailbox = self.fixture.mailbox
        (self.fixture.server / 'mode').write_text('incomplete')
        result, _ = self.fixture.execute()
        self.assertNotEqual(result.status, 'passed')
        self.run = self.root / 'run'
        self.work = self.root / 'repair'

    def prepare(self):
        return prepare_workspace(self.mailbox, self.run, self.work)

    def test_owned_candidate_has_no_stale_passes_and_leaves_source_unchanged(self):
        before = (self.mailbox / 'snapshot.json').read_bytes()
        context = self.prepare()
        self.assertEqual(context['module_name'], 'unit')
        self.assertEqual(context['editable_rtl'], 'rtl_output/unit.sv')
        self.assertEqual(check_workspace(self.work)['status'], 'no_patch')
        for name in ('build_status.json', 'verification_handoff.json', 'integration_lint.json'):
            self.assertFalse((self.work / 'candidate' / name).exists())
        path = self.work / 'candidate/rtl_output/unit.sv'
        path.write_text(path.read_text() + '\n// repair\n')
        result = check_workspace(self.work)
        self.assertEqual(result['changed_files'], ['rtl_output/unit.sv'])
        self.assertFalse(result['backend_ready'])
        self.assertEqual(before, (self.mailbox / 'snapshot.json').read_bytes())
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_contract_and_wrapper_edits_are_outside_scope(self):
        self.prepare()
        for name in ('architecture.yaml', 'rtl_output/top.sv', 'microarch/gen_exp/unit.yaml'):
            path = self.work / 'candidate' / name
            original = path.read_bytes()
            path.write_bytes(original + b'\nchanged')
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'new approved frontend build'):
                check_workspace(self.work)
            path.write_bytes(original)

    def test_receipt_and_baseline_tampering_rejected(self):
        self.prepare()
        path = self.work / 'evidence/reports.zip'
        with path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'reports differ'):
            check_workspace(self.work)

    def test_stale_parent_and_nested_destination_rejected(self):
        with self.assertRaises(ValueError):
            prepare_workspace(self.mailbox, self.run, self.mailbox / 'repair')
        path = self.run / 'validation_result.json'
        data = json.loads(path.read_text())
        data['parent_rtl_revision'] = 'stale'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'different parent'):
            self.prepare()
        self.assertFalse(self.work.exists())

    def test_no_patch_cannot_create_fresh_build(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'No RTL patch'):
            recheck_workspace(self.work, self.root / 'fresh_rechecked_build')
        self.assertFalse((self.root / 'fresh_rechecked_build').exists())

    def test_symlinked_candidate_rejected(self):
        self.prepare()
        path = self.work / 'candidate/rtl_output/unit.sv'
        path.unlink()
        path.symlink_to(self.mailbox / 'rtl/unit.sv')
        with self.assertRaises(ValueError):
            check_workspace(self.work)

    def test_failed_native_recheck_never_emits_acceptance_receipt(self):
        self.prepare()
        path = self.work / 'candidate/rtl_output/unit.sv'
        path.write_text(path.read_text() + '\n// patch')
        with patch('shared.top_orchestrator.specification_repair.subprocess.run',
                   return_value=SimpleNamespace(returncode=1, stdout='', stderr='compiler rejected patch')):
            with self.assertRaisesRegex(ValueError, 'recheck failed'):
                recheck_workspace(self.work, self.root / 'fresh_build')
        self.assertFalse((self.work / 'recheck_receipt.json').exists())

    def test_edit_after_recheck_cannot_be_published(self):
        self.prepare()
        path = self.work / 'candidate/rtl_output/unit.sv'
        path.write_text(path.read_text() + '\n// first patch')
        context = check_workspace(self.work)
        receipt = dict(context, status='local_recheck_passed', build_files=file_hashes(self.root / 'build'))
        (self.work / 'recheck_receipt.json').write_text(json.dumps(receipt))
        path.write_text(path.read_text() + '\n// second patch')
        with patch('shared.top_orchestrator.specification_repair.publish_specification') as publish:
            with self.assertRaisesRegex(ValueError, 'differs from its local recheck receipt'):
                publish_repair(self.work, self.root / 'build', self.root / 'next_revision')
            publish.assert_not_called()
