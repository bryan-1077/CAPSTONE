import hashlib
import json
import unittest

from local.backend.services.mailbox_snapshot import freeze_mailbox
from . import test_specification_mailbox as fixtures
from .specification_backend import handoff_contract, prepare_offline_handoff, verify_offline_handoff


class OfflineBackendTests(unittest.TestCase):
    def setUp(self):
        fixtures.SpecificationMailboxTests.setUp(self)
        self.doc['shared'] = {'clock_reset': {'clock': 'clk', 'reset': 'rst_n',
                            'edge': 'rising', 'active_level': 0, 'assertion_style': 'synchronous'}}
        self.doc['design']['top_ports'] = [dict(name=name, direction='input', width=1)
                                           for name in ('clk', 'rst_n')]
        (self.build / 'verification_handoff.json').write_text(json.dumps(self.doc))
        (self.build / 'rtl_output/unit.sv').write_text('module unit(input clk); endmodule')
        (self.build / 'rtl_output/top.sv').write_text('module top(input clk, rst_n); unit u(clk); endmodule')

    def publish(self):
        return fixtures.SpecificationMailboxTests.publish(self)

    def test_frozen_tree_matches_backend_transport_digest(self):
        self.publish()
        destination = self.root / 'offline'
        report = prepare_offline_handoff(self.mailbox, destination)
        transported = freeze_mailbox(self.mailbox, self.root / 'transport')
        self.assertEqual(report['mailbox_revision'], transported['mailbox_revision'])
        self.assertEqual(report['mailbox_file_hashes'], transported['mailbox_file_hashes'])
        self.assertEqual(report['staged_compile_order'], ['unit.sv', 'top.sv'])
        self.assertEqual(report['clock_period_ns'], 5)
        self.assertFalse(report['backend_ready'])
        self.assertEqual(report['physical_design'], 'not_run')
        self.assertEqual(report['rtl_revision'], 'revision')
        for name, digest in report['mailbox_file_hashes'].items():
            self.assertEqual(hashlib.sha256((destination / 'rtl' / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(verify_offline_handoff(self.mailbox, destination), report)
        with self.assertRaises(FileExistsError):
            prepare_offline_handoff(self.mailbox, destination)

    def test_clock_conflict_and_nested_output_fail_without_writing(self):
        self.publish()
        with self.assertRaises(ValueError):
            prepare_offline_handoff(self.mailbox, self.root / 'bad', 1600)
        self.assertFalse((self.root / 'bad').exists())
        with self.assertRaises(ValueError):
            prepare_offline_handoff(self.mailbox, self.mailbox / 'backend')

    def test_tampered_published_file_rejected(self):
        self.publish()
        (self.mailbox / 'rtl/unit.sv').write_text('changed')
        with self.assertRaises(ValueError):
            handoff_contract(self.mailbox)

    def test_missing_clock_port_rejected(self):
        self.doc['design']['top_ports'] = []
        (self.build / 'verification_handoff.json').write_text(json.dumps(self.doc))
        self.publish()
        with self.assertRaisesRegex(ValueError, 'scalar top-level clock'):
            handoff_contract(self.mailbox)

    def test_duplicate_module_declaration_rejected(self):
        (self.build / 'rtl_output/unit.sv').write_text('module top; endmodule')
        self.publish()
        with self.assertRaisesRegex(ValueError, 'Duplicate RTL'):
            handoff_contract(self.mailbox)

    def test_snapshot_transport_rejects_symlinks_before_copy(self):
        self.publish()
        (self.mailbox / 'rtl/escape.sv').symlink_to(self.root / 'missing')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            freeze_mailbox(self.mailbox, self.root / 'transport')
        self.assertFalse((self.root / 'transport').exists())

    def test_recheck_rejects_modified_metadata_or_transport(self):
        self.publish()
        destination = self.root / 'offline'
        report = prepare_offline_handoff(self.mailbox, destination)
        path = destination / 'backend_handoff.json'
        changed = dict(report, backend_ready=True)
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'source contract'):
            verify_offline_handoff(self.mailbox, destination)
        path.write_text(json.dumps(report))
        (destination / 'rtl/top.sv').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            verify_offline_handoff(self.mailbox, destination)
