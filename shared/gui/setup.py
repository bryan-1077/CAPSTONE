"""Approved-YAML setup and local process monitoring."""
from datetime import datetime
import json
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QWidget, QFormLayout, QLineEdit, QPushButton,
                              QFileDialog, QCheckBox, QLabel, QPlainTextEdit, QMessageBox,
                              QVBoxLayout, QHBoxLayout, QGroupBox, QScrollArea)
from .execution import launch
from .run_reader import load_run, summary, preview


class SetupPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.directory = None
        layout = QVBoxLayout(self)
        from .progress import FlowStatus
        self.flow_status = FlowStatus()
        layout.addWidget(self.flow_status)
        actions = QHBoxLayout()
        self.start = QPushButton('Start design')
        self.start.setObjectName('primaryAction')
        self.start.clicked.connect(self.start_run)
        actions.addWidget(self.start)
        self.stop = QPushButton('Stop…')
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_run)
        actions.addWidget(self.stop)
        actions.addStretch()
        layout.addLayout(actions)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText('Describe the controller you want to build. You will review it before generation starts.')
        self.description.setMaximumHeight(100)
        form.addRow('Describe your design', self.description)
        self.yaml = QLineEdit()
        self.yaml.setPlaceholderText('Optional: use a saved specification instead')
        file_row = QHBoxLayout()
        file_row.addWidget(self.yaml)
        browse = QPushButton('Browse…')
        browse.clicked.connect(lambda: self.pick(self.yaml, 'YAML (*.yaml *.yml)'))
        file_row.addWidget(browse)
        form.addRow('Or load a specification', file_row)
        self.frequency = QLineEdit('200')
        self.frequency.setToolTip('Requested controller clock speed; actual performance depends on the completed design.')
        form.addRow('Target speed (MHz)', self.frequency)
        self.advanced_toggle = QPushButton('Show advanced settings')
        self.advanced_toggle.setCheckable(True)
        form.addRow(self.advanced_toggle)
        self.advanced = QGroupBox('Advanced settings')
        advanced = QFormLayout(self.advanced)
        self.review_frequency = QCheckBox('Review target speed and optionally ask for advice')
        advanced.addRow(self.review_frequency)
        self.remote = QLineEdit()
        self.remote.setPlaceholderText('Uses environment settings when blank')
        advanced.addRow('Server configuration', self.remote)
        remote_browse = QPushButton('Choose server configuration…')
        remote_browse.clicked.connect(lambda: self.pick(self.remote, 'JSON (*.json)'))
        advanced.addRow(remote_browse)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText('You can also enter this when requested')
        advanced.addRow('Server password', self.password)
        self.flags = {}
        for key, label in [('cache', 'Reuse generated design files'), ('no_bist', 'Skip built-in simulation'),
                           ('skip_verification', 'Skip design verification'),
                           ('no_backend', 'Stop before physical design'), ('skip_lec', 'Skip equivalence checking')]:
            self.flags[key] = QCheckBox(label)
            advanced.addRow(self.flags[key])
        form.addRow(self.advanced)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda checked: self.advanced_toggle.setText(
            'Hide advanced settings' if checked else 'Show advanced settings'))
        from .input_panel import InputPanel
        self.input_panel = InputPanel()
        form.addRow(self.input_panel)
        self.details_toggle = QPushButton('Show technical details')
        self.details_toggle.setCheckable(True)
        form.addRow(self.details_toggle)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        details_layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMinimumHeight(180)
        details_layout.addWidget(self.output)
        form.addRow(self.details)
        self.details.hide()
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.details_toggle.toggled.connect(lambda checked: self.details_toggle.setText(
            'Hide technical details' if checked else 'Show technical details'))
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        from .registry import discover_worker, read_record, WorkerHandle
        active = discover_worker()
        if active is None:
            previous = read_record()
            if previous.get('identity') and previous.get('run_dir'):
                active = WorkerHandle(previous)
        if active is not None:
            self.process = active
            self.directory = Path(active.record['run_dir'])
            self.started = datetime.fromtimestamp(active.record['started'])
            self.start.setEnabled(False)
            self.stop.setEnabled(True)
            self.timer.start()
            self.poll()

    def pick(self, field, pattern):
        path, _ = QFileDialog.getOpenFileName(self, 'Select file', '', pattern)
        if path:
            field.setText(path)

    def start_run(self):
        config = {key: field.isChecked() for key, field in self.flags.items()}
        config.update(input_yaml=self.yaml.text(), target_mhz=self.frequency.text(),
                      remote_config=self.remote.text() or None,
                      description=self.description.toPlainText().strip(),
                      review_frequency=self.review_frequency.isChecked())
        try:
            self.process, self.directory = launch(config, self.password.text())
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Cannot start run', str(exc))
            return
        self.password.clear()
        self.started = datetime.now()
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.timer.start()
        self.poll()

    def poll(self):
        code = self.process.poll()
        connected = False
        remote_state = 'running'
        pending = None
        if code is None and hasattr(self.process, 'gui_socket'):
            from .interaction import rpc
            try:
                snapshot = rpc(self.process.gui_socket, 'poll')
                if snapshot.get('worker_id') != self.process.worker_id:
                    raise ValueError('Worker identity mismatch')
                connected = True
                remote_state = snapshot.get('lifecycle', 'running')
                pending = snapshot.get('pending')
                if pending:
                    self.input_panel.display(self.process.gui_socket, pending)
            except (OSError, ValueError):
                pass
        elapsed = int((datetime.now() - self.started).total_seconds())
        label = (remote_state if connected else 'Disconnected; worker process still alive') if code is None else 'Worker exited: ' + str(code)
        self.status.setText(f'{label} · {elapsed}s · {self.directory}')
        self.stop.setEnabled(code is None and connected and remote_state != 'cancelling')
        state = load_run(self.directory)
        log = self.directory.parent / (self.directory.name + '.worker.log')
        from .registry import read_record, lifecycle_path, atomic_write
        receipt = read_record(lifecycle_path(self.directory))
        if code is not None and receipt.get('lifecycle') == 'running':
            receipt.update(lifecycle='interrupted', returncode=code,
                           remote_cleanup={'status': 'cleanup_unconfirmed',
                                           'reason': 'Worker exited without a cleanup receipt.'})
            atomic_write(lifecycle_path(self.directory), receipt)
        self.flow_status.update_run(state.state, alive=code is None, connected=connected,
                                    pending=pending, lifecycle=remote_state if code is None else receipt.get('lifecycle', 'interrupted'),
                                    elapsed=elapsed)
        lifecycle = json.dumps(receipt, indent=2) if receipt else 'Lifecycle record not available yet.'
        self.output.setPlainText(summary(state) + '\n\nWorker lifecycle:\n' + lifecycle + '\n\n' + preview(log))
        if code is not None:
            self.input_panel.value.clear()
            self.input_panel.hide()
            self.timer.stop()
            self.start.setEnabled(True)

    def stop_run(self):
        if self.process is None or self.process.poll() is not None:
            return False
        result = QMessageBox.question(self, 'Stop design flow?',
            'Stop new stages, interrupt the worker, terminate its local tools, and release its owned Slurm allocation? '
            'Remote termination may remain unconfirmed if SSH is unavailable. Existing reports will be preserved.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if result != QMessageBox.StandardButton.Yes:
            return False
        from .interaction import rpc
        try:
            response = rpc(self.process.gui_socket, 'cancel', worker_id=self.process.worker_id)
            if not response.get('accepted'):
                raise ValueError(response.get('error', 'Cancellation was not accepted'))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, 'Stop not confirmed',
                f'{exc}. Reconnect before retrying; do not assume local or remote work stopped.')
            return False
        self.input_panel.value.clear()
        self.input_panel.hide()
        self.stop.setEnabled(False)
        self.status.setText('Cancellation requested; waiting for cleanup evidence.')
        return True
