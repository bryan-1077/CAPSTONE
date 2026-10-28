"""Resume CLI preserves the old run and routes only through backend and GDSII validation."""

import io
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from shared.top_orchestrator.contracts import StageResult
from shared.top_orchestrator.nodes import backend_command
from shared.top_orchestrator import resume
from shared.top_orchestrator import test_lec as lec_fixtures


class ResumeTests(unittest.TestCase):
    def setUp(self):
        lec_fixtures.LECTests.setUp(self)
        self.original = self.root / 'original_run'
        self.original.mkdir()
        self.saved_backend = self.original / 'backend_result.json'
        self.state.update(lec_enabled=True, timing_target_clock_period_ns=5,
                          stage_status={**self.state['stage_status'], 'lec': 'failed', 'gdsii': 'pending'})
        self.saved_backend.write_text(json.dumps(self.state))
        self.ledger = self.original / 'state.json'
        self.ledger.write_text(json.dumps(dict(config={}, results=[dict(stage='backend', status='failed',
            artifacts={'result': str(self.saved_backend)})])))
        stub = patch('shared.top_orchestrator.resume.verify_backend_handoff', return_value=self.validation)
        stub.start()
        self.addCleanup(stub.stop)

    def test_plan_checks_saved_inputs_without_starting_remote_work(self):
        with patch.object(resume, 'run') as execute, patch('sys.stdout', io.StringIO()) as output:
            self.assertEqual(resume.main(['--run', str(self.original), '--plan']), 0)
        execute.assert_not_called()
        self.assertIn('200 MHz', output.getvalue())
        config = resume.resume_config(self.original, self.remote_path)
        self.assertEqual(config.backend_resume_state, self.saved_backend)
        command = backend_command(config, config.mailbox)
        self.assertEqual(command[command.index('--resume-state') + 1], str(self.saved_backend))

    def test_resume_runs_backend_then_gdsii_validator_and_preserves_old_run(self):
        seen = []
        before = self.ledger.read_bytes(), self.saved_backend.read_bytes()

        def backend(config, directory):
            seen.append('backend')
            self.assertEqual(config.backend_resume_state, self.saved_backend)
            self.assertEqual(config.validation_result, Path(self.state['lec_validation_result']))
            result = directory / 'backend_result.json'
            result.write_text('{}')
            return StageResult('backend', 'passed', 'LEC and implementation passed', {'result': str(result)})

        def validator(config, directory):
            seen.append('gdsii-validation')
            self.assertNotEqual(config.backend_result, self.saved_backend)
            self.assertTrue(config.backend_result.is_file())
            return StageResult('gdsii-validation', 'passed', 'passed')

        with patch('shared.top_orchestrator.orchestrator.prepare_password'), patch.dict(
                'shared.top_orchestrator.orchestrator.NODES', {'backend': backend, 'gdsii-validation': validator}), \
                patch('sys.stdout', io.StringIO()):
            self.assertEqual(resume.main(['--run', str(self.original), '--remote-config', str(self.remote_path)]), 0)
        self.assertEqual(seen, ['backend', 'gdsii-validation'])
        self.assertEqual((self.ledger.read_bytes(), self.saved_backend.read_bytes()), before)
        destinations = list(self.root.glob('*_resume_*/state.json'))
        self.assertEqual(len(destinations), 1)
        self.assertEqual(json.loads(destinations[0].read_text())['status'], 'complete')

    def test_incomplete_mapping_cannot_resume(self):
        self.state['stage_status']['mapped_netlist'] = 'failed'
        self.saved_backend.write_text(json.dumps(self.state))
        with self.assertRaisesRegex(ValueError, 'required'):
            resume.resume_config(self.original)


if __name__ == '__main__':
    unittest.main()
