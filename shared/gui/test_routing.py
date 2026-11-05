"""Run the actual router with offline stage adapters; no EDA/model/SSH execution."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from shared.top_orchestrator.contracts import StageResult
from shared.top_orchestrator.remote import RemoteConfig
from shared.top_orchestrator import orchestrator, allocation
from .worker import execute


class RoutingTests(unittest.TestCase):
    def scenario(self, validation_status='passed', **controls):
        visited = []
        remote = RemoteConfig(host='offline', username='test', project_dir='/offline',
                              partition='test', qos='test')
        def adapter(stage):
            def run(config, directory):
                visited.append(stage)
                return StageResult(stage, validation_status if stage == 'validation' else 'passed',
                                   'Offline stage adapter')
            return run
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            source = root / 'spec.yaml'
            source.write_text('design_name: offline\n')
            stack.enter_context(patch.dict(orchestrator.NODES, {stage: adapter(stage) for stage in orchestrator.NODES}))
            stack.enter_context(patch.object(orchestrator, 'publish', return_value=root / 'mailbox'))
            stack.enter_context(patch.object(orchestrator, 'final_summary', return_value='Offline summary'))
            stack.enter_context(patch.object(allocation.AllocationSession, 'acquire', return_value=None))
            stack.enter_context(patch.object(RemoteConfig, 'load', return_value=remote))
            stack.enter_context(redirect_stdout(StringIO()))
            result = execute(dict(input_yaml=str(source), target_mhz=200, **controls),
                             'offline-test-secret', root / 'run')
            self.assertNotIn('offline-test-secret', (root / 'run/state.json').read_text())
            return visited, result.status

    def test_full_route(self):
        self.assertEqual(self.scenario(),
                         (['generate', 'validation', 'backend', 'gdsii-validation'], 'complete'))

    def test_failed_verification_blocks_backend(self):
        self.assertEqual(self.scenario(validation_status='needs_attention'),
                         (['generate', 'validation'], 'needs_attention'))

    def test_explicit_skips(self):
        self.assertEqual(self.scenario(skip_verification=True, no_backend=True), (['generate'], 'complete'))
        self.assertEqual(self.scenario(skip_verification=True),
                         (['generate', 'backend', 'gdsii-validation'], 'complete'))
