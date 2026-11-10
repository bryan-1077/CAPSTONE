"""Offline lifecycle tests; subprocess harness never runs the design flow."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from .interaction import rpc
from .registry import process_identity, alive, WorkerHandle, atomic_write, lifecycle_path


class IdentityTests(unittest.TestCase):
    def test_pid_reuse_and_remote_unknown(self):
        from .lifecycle import remote_cleanup
        identity = process_identity(os.getpid())
        self.assertTrue(alive({'identity': identity}))
        self.assertFalse(alive({'identity': dict(identity, start_ticks='wrong')}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.assertEqual(remote_cleanup(path, False)['status'], 'not_required')
            (path / 'shared_allocation.json').write_text(json.dumps(dict(
                status='release_failed', lease={'job_id': '123'})))
            self.assertEqual(remote_cleanup(path, True)['status'], 'cleanup_unconfirmed')

    def test_dead_worker_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'
            run.mkdir()
            atomic_write(lifecycle_path(run), {'returncode': 130, 'lifecycle': 'cancelled'})
            handle = WorkerHandle(dict(identity={'pid': 99999999}, socket_path='/absent',
                                       worker_id='id', run_dir=str(run)))
            self.assertEqual(handle.poll(), 130)


class WorkerTests(unittest.TestCase):
    def test_detach_reconnect_and_cancel_pending_input(self):
        from .registry import REPO_ROOT
        script = '''
import sys
from shared.gui import worker, registry, run_storage
registry.ACTIVE = __import__('pathlib').Path(sys.argv[5])
run_storage.RUNTIME_WORKERS = registry.ACTIVE.parent / 'private-workers'
def fake_execute(config, password, directory, interaction):
    import subprocess, json, time
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    directory.with_suffix('.child.json').write_text(json.dumps(registry.process_identity(child.pid)))
    interaction.request('credential', {'host': 'offline', 'username': 'test'})
    raise RuntimeError('Unexpected continuation')
worker.execute = fake_execute
sys.exit(worker.main())
'''
        with tempfile.TemporaryDirectory(prefix='capstone-test-') as tmp:
            root = Path(tmp)
            run, endpoint, active = root / 'run', root / 'input.sock', root / 'active.json'
            read_fd, write_fd = os.pipe()
            lock = (root / 'lock').open('w')
            process = subprocess.Popen([sys.executable, '-c', script, str(run), str(lock.fileno()),
                                        str(read_fd), str(endpoint), str(active)],
                                       pass_fds=(lock.fileno(), read_fd), cwd=REPO_ROOT,
                                       start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            os.close(read_fd)
            record = dict(worker_id='offline', identity=process_identity(process.pid),
                          run_dir=str(run), socket_path=str(endpoint), started=time.time())
            atomic_write(active, record)
            with os.fdopen(write_fd, 'w') as stream:
                json.dump({'config': {'skip_verification': True, 'no_backend': True}, 'password': ''}, stream)
            try:
                deadline = time.monotonic() + 5
                pending = None
                while time.monotonic() < deadline:
                    try:
                        pending = rpc(endpoint, 'poll').get('pending')
                        if pending:
                            break
                    except OSError:
                        pass
                    time.sleep(.05)
                self.assertIsNotNone(pending)
                # New client handle after the original client has disconnected.
                handle = WorkerHandle(record)
                self.assertIsNone(handle.poll())
                self.assertEqual(rpc(handle.gui_socket, 'poll')['pending']['request_id'], pending['request_id'])
                self.assertIn('error', rpc(endpoint, 'cancel', worker_id='another-worker'))
                self.assertTrue(rpc(endpoint, 'cancel', worker_id='offline')['accepted'])
                self.assertEqual(process.wait(timeout=8), 130)
                receipt = json.loads(lifecycle_path(run).read_text())
                self.assertEqual(receipt['lifecycle'], 'cancelled')
                self.assertEqual(receipt['remote_cleanup']['status'], 'not_required')
                self.assertNotIn('password', receipt)
                child = json.loads(run.with_suffix('.child.json').read_text())
                self.assertFalse(alive({'identity': child}))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                lock.close()

class CloseDialogTests(unittest.TestCase):
    def test_leave_and_keep_choices(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QCloseEvent
        from .window import RunWindow
        from types import SimpleNamespace
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as tmp:
            with patch('shared.gui.registry.discover_worker', return_value=None), patch(
                    'shared.gui.registry.read_record', return_value={}):
                window = RunWindow(tmp, preferences_path=__import__('pathlib').Path(tmp) / 'ui.json')
            window.setup.process = SimpleNamespace(poll=lambda: None)
            for choice, expected, stop_accepted in [('keep', False, False), ('leave', True, False),
                                                   ('stop', False, True), ('stop', False, False)]:
                labels = []
                class Dialog:
                    ButtonRole = SimpleNamespace(RejectRole=0, AcceptRole=1, DestructiveRole=2)
                    def __init__(self, parent):
                        self.default = None
                    def setWindowTitle(self, text):
                        self_test.assertEqual(text, 'Close?')
                    def setText(self, text): pass
                    def addButton(self, text, role):
                        labels.append(text)
                        return ['keep', 'leave', 'stop'][role]
                    def setDefaultButton(self, button):
                        self.default = button
                        self_test.assertEqual(button, 'keep')
                    def setEscapeButton(self, button): pass
                    def exec(self): pass
                    def clickedButton(self): return choice
                self_test = self
                with patch('shared.gui.window.QMessageBox', Dialog), patch.object(
                        window.setup, 'stop_run', return_value=stop_accepted) as stop_run:
                    event = QCloseEvent()
                    window.closeEvent(event)
                    self.assertEqual(event.isAccepted(), expected)
                    self.assertEqual(labels, ['Stop Run and Close', 'Maintain Run and Close', 'Cancel'])
                    if choice == 'stop':
                        stop_run.assert_called_once_with(confirm=False)
                    else:
                        stop_run.assert_not_called()
                    self.assertEqual(window.close_when_stopped, stop_accepted)
                    if stop_accepted:
                        self.assertTrue(window.close_timer.isActive())
                        with patch.object(window, 'close') as close:
                            window.finish_close()
                            close.assert_not_called()
                            window.setup.process = SimpleNamespace(poll=lambda: 130)
                            window.finish_close()
                            close.assert_called_once()
                        window.setup.process = SimpleNamespace(poll=lambda: None)
                        window.close_when_stopped = False
                    else:
                        self.assertFalse(window.close_timer.isActive())
            window.setup.process = None
            window.close()
