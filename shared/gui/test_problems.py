from pathlib import Path
import unittest
from unittest.mock import patch
from .problems import is_connection_error
from .run_reader import SavedRun, summary
from .history import run_status
from .progress import progress_state


class ConnectionProblemTests(unittest.TestCase):
    def test_allocation_socket_timeout_is_presented_as_connection_error(self):
        result = dict(stage='validation', status='failed', message=
            'RuntimeError: Shared Slurm allocation failed: TimeoutError: timed out')
        run = SavedRun(Path('/tmp/connection-test'), dict(status='failed', results=[result]))
        self.assertTrue(is_connection_error(result))
        with patch('shared.gui.history.read_record', return_value={'lifecycle': 'failed'}):
            self.assertEqual(run_status(run)[0], 'connection_error')
        with patch('shared.gui.registry.read_record', return_value={}):
            self.assertIn('VPN', summary(run))
            self.assertIn(result['message'], summary(run))
        statuses, title, _, _, _ = progress_state(run.state)
        self.assertEqual(statuses['validation'], 'connection_error')
        self.assertIn('Could not connect', title)
        self.assertEqual(result['status'], 'failed')

    def test_command_queue_model_and_design_failures_are_not_connection_errors(self):
        for stage, message in (
            ('validation', 'TimeoutError: Remote command exceeded 14400 seconds'),
            ('validation', 'Shared Slurm allocation failed: allocation queue wait exceeded'),
            ('generate', 'Connection timed out calling model provider'),
            ('validation', 'Assertion failed: timing mismatch'),
            ('validation', 'TimeoutError: timed out'),
        ):
            with self.subTest(stage=stage, message=message):
                self.assertFalse(is_connection_error(dict(stage=stage, status='failed', message=message)))

    def test_terminal_success_does_not_reuse_an_old_connection_error(self):
        run = SavedRun(Path('/tmp/connection-test'), dict(status='complete', results=[
            dict(stage='validation', status='failed', message='Connection refused'),
            dict(stage='validation', status='passed', message='Done')]))
        with patch('shared.gui.history.read_record', return_value={'lifecycle': 'complete'}):
            self.assertEqual(run_status(run)[0], 'complete')
