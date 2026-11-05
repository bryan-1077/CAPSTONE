"""Interaction and approval tests without model or SSH calls."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from .interaction import Interaction, rpc
from .specification import select_specification


class InteractionTests(unittest.TestCase):
    def test_secret_response_and_stale_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input.sock'
            channel = Interaction(path)
            result = []
            task = threading.Thread(target=lambda: result.append(channel.request('credential', {'host': 'test'})))
            try:
                task.start()
                with channel.condition:
                    channel.condition.wait_for(lambda: channel.pending is not None, timeout=1)
                pending = rpc(path, 'poll')['pending']
                self.assertEqual(rpc(path, 'respond', request_id='stale', response={})['error'], 'Stale or duplicate response')
                self.assertTrue(rpc(path, 'respond', request_id=pending['request_id'],
                                    response={'action': 'submit', 'value': 'secret'})['accepted'])
                task.join(timeout=2)
                self.assertFalse(task.is_alive())
                self.assertEqual(result[0]['value'], 'secret')
                self.assertIsNone(rpc(path, 'poll')['pending'])
                self.assertEqual(list(Path(tmp).iterdir()), [path])
            finally:
                channel.close()
                task.join(timeout=2)


class SpecificationTests(unittest.TestCase):
    def api(self):
        return SimpleNamespace(MAX_CLARIFICATION_WARNINGS=3, MAX_CONFIRMATION_REJECTIONS=2,
            DEFAULT_LLM_MODEL='test', FatalConfigTextError=type('Fatal', (Exception,), {}),
            ConfigTextError=type('Recoverable', (Exception,), {}),
            extract_config_with_llm=lambda text, model: text,
            normalize_result=lambda value: ({'design': value}, ['Assumed default'], 'none', None))

    def test_revision_then_approval(self):
        class Client:
            count = 0
            def request(self, kind, context):
                self.count += 1
                if self.count == 1:
                    return {'action': 'revise', 'value': 'revised design'}
                return {'action': 'approve', 'digest': context['digest']}
        with tempfile.TemporaryDirectory() as tmp:
            path = select_specification('original', Path(tmp), Client(), self.api())
            self.assertIn('revised design', path.read_text())
            record = json.loads((Path(tmp) / 'gui_spec_approval.json').read_text())
            self.assertEqual(record['revision'], 2)
            self.assertTrue(record['approved'])

    def test_mismatched_approval_writes_nothing(self):
        client = SimpleNamespace(request=lambda kind, context: {'action': 'approve', 'digest': 'old'})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                select_specification('design', Path(tmp), client, self.api())
            self.assertEqual(list(Path(tmp).iterdir()), [])

class PanelTests(unittest.TestCase):
    def test_approval_and_password_display(self):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication, QLineEdit
        from .input_panel import InputPanel
        app = QApplication.instance() or QApplication([])
        panel = InputPanel()
        panel.display('/unused', dict(request_id='one', kind='spec_approval', context=dict(
            candidate='design: test', assumptions=['Default timing'], preset='none',
            revision=1, digest='digest', description='design')))
        self.assertIn('Default timing', panel.context.toPlainText())
        self.assertEqual(panel.continue_button.text(), 'Approve specification')
        panel.display('/unused', dict(request_id='two', kind='credential', context=dict(host='host', username='user')))
        self.assertEqual(panel.value.echoMode(), QLineEdit.EchoMode.Password)
        self.assertEqual(panel.value.text(), '')
        panel.close()


class RetryTests(unittest.TestCase):
    def test_bounded_auth_path_uses_gui_response(self):
        from unittest.mock import patch
        from shared.top_orchestrator import auth, orchestrator
        from shared.gui.worker import execute
        calls = []
        client = SimpleNamespace(request=lambda kind, context: calls.append(kind) or dict(action='submit', value='replacement'))
        ssh = SimpleNamespace(host='host', username='user', password=None)
        def fake_run(config, directory):
            with auth.password_session():
                auth.prompt_password(ssh)
                self.assertEqual(auth.cached_password('host', 'user'), 'replacement')
            return 'done'
        with patch.object(orchestrator, 'run', side_effect=fake_run):
            execute(dict(input_yaml='/tmp/spec.yaml', target_mhz=200), '', Path('/tmp/run'), client)
        self.assertEqual(calls, ['credential'])
