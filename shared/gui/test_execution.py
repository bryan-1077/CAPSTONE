import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from .execution import validate


class ExecutionTests(unittest.TestCase):
    def test_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'spec.yaml'
            path.write_text('design: example\n')
            base = dict(input_yaml=str(path), target_mhz='200', skip_verification=True, no_backend=True)
            self.assertEqual(validate(base)['target_mhz'], 200)
            for value in ('nan', '-1', 'inf'):
                with self.assertRaises(ValueError):
                    validate(dict(base, target_mhz=value))
            with self.assertRaises(ValueError):
                validate(dict(base, skip_lec=True, no_backend=True))

    def test_worker_uses_existing_orchestrator_and_credentials(self):
        from .worker import execute
        from shared.top_orchestrator import orchestrator, auth
        original = orchestrator.prepare_password
        def fake_run(config, directory):
            with auth.password_session():
                orchestrator.prepare_password(config)
                self.assertEqual(auth.cached_password('test-host', 'test-user'), 'secret')
            return 'finished'
        with patch.object(orchestrator, 'run', side_effect=fake_run), patch(
            'shared.top_orchestrator.remote.RemoteConfig.load') as remote:
            remote.return_value.host = 'test-host'
            remote.return_value.username = 'test-user'
            result = execute(dict(input_yaml='/tmp/spec.yaml', target_mhz=200), 'secret', Path('/tmp/run'))
            self.assertEqual(result, 'finished')
        self.assertIs(orchestrator.prepare_password, original)
        self.assertIsNone(auth.cached_password('test-host', 'test-user'))
