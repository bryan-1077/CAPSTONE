import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import debug_agent as agent
import debug_investigation as investigation
from verif_debug_intake import capture_verif_run

class VerifRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.run = self.root / 'arbitrary_location'
        self.failure = self.root / 'debug/failures/test'
        self.intake = self.failure / 'intake'
        self.intake.mkdir(parents=True)
        for iteration in (3, 4):
            candidate = self.run / f'agent2/candidates/iter_{iteration}/dut_tb_top.sv'
            candidate.parent.mkdir(parents=True)
            candidate.write_text('assert(count == 1);')
        self.write('summary.json', {'overall_status': 'FAIL', 'stages': {'agent1': {'status': 'PASS'}}})
        self.write('agent2/report.json', {'status': 'FAIL', 'reason': 'Simulation failed', 'requirements': [{'id': 'REQ-1', 'status': 'FAIL', 'requirement': 'count increments', 'evidence': 'expected 1 observed 0'}]})
        (self.run / 'agent2/sim_iter_4.log').write_text('FAIL count expected 1 observed 0')
        (self.run / 'agent2/counter.sv').write_text('historical RTL')

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def test_capture_and_catalog(self):
        intake = agent.capture_intake(argparse.Namespace(source='verif', log=str(self.run), command=None), self.failure)
        raw = (self.intake / 'raw.log').read_text()
        self.assertFalse(agent.parse_verif_json_report(raw)['is_valid'])
        self.assertIn('REQ-1', raw)
        self.assertEqual(intake.mode, 'verif_run')
        catalog = investigation.source_catalog(self.root, self.failure)
        self.assertTrue(any(name.endswith('sim_iter_4.log') and kind == 'observation' for name, kind in catalog.items()))
        self.assertTrue(any('iter_4' in name and kind == 'checker' for name, kind in catalog.items()))
        self.assertFalse(any('iter_3' in name or name.endswith('/counter.sv') for name in catalog))
        self.assertFalse((self.intake / 'evidence/verif_run/agent2/counter.sv').exists())
        manifest = json.loads((self.intake / 'evidence_files.json').read_text())
        self.assertTrue(all(len(entry['sha256']) == 64 for entry in manifest))

    def test_cli_all_intake_modes(self):
        for prefix in (['verif'], ['auto', 'verif'], ['graph', 'verif']):
            with patch('sys.argv', ['debug_agent.py', *prefix, '--verif-run', str(self.run)]):
                args = agent.parse_args()
                self.assertEqual(args.log, str(self.run))
                self.assertIsNone(args.command)

    def test_wrong_source_rejected(self):
        with self.assertRaisesRegex(ValueError, "source 'verif'"):
            agent.capture_intake(argparse.Namespace(source='lint', log=str(self.run), command=None), self.failure)

    def test_invalid_summary_rejected(self):
        self.write('summary.json', {})
        with self.assertRaisesRegex(ValueError, 'overall_status'):
            capture_verif_run(self.run, self.intake)

    def test_symlink_not_imported(self):
        outside = self.root / 'outside.log'
        outside.write_text('not evidence')
        (self.run / 'agent2/linked.log').symlink_to(outside)
        capture_verif_run(self.run, self.intake)
        self.assertFalse((self.intake / 'evidence/verif_run/agent2/linked.log').exists())

    def test_initial_context_contains_failure_and_deep_checker(self):
        checker = self.run / 'agent2/candidates/iter_4/dut_pkg.sv'
        checker.write_text("// header\n" * 70 + "task check_count();\n  if (count != expected) $error(\"mismatch\");\nendtask\n")
        log = self.run / 'agent2/sim_iter_4.log'
        log.write_text("startup\n" * 80 + "UVM_ERROR expected 1 observed 0\n")
        agent.capture_intake(argparse.Namespace(source='verif', log=str(self.run), command=None), self.failure)
        record = investigation.new_investigation(self.root, self.failure, [], ["count"], 8, 64000)
        investigation.inspect(record, self.root)
        evidence = record['evidence']
        self.assertTrue(any('REQ-1' in item['text'] for item in evidence))
        self.assertTrue(any('expected 1 observed 0' in item['text'] and item['path'].endswith('.log') for item in evidence))
        self.assertTrue(any('if (count != expected)' in item['text'] and item['kind'] == 'checker' for item in evidence))
