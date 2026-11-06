"""Approved-YAML setup and local process monitoring."""
from datetime import datetime
import json
from pathlib import Path
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QWidget, QFormLayout, QLineEdit, QPushButton,
                              QFileDialog, QCheckBox, QLabel, QPlainTextEdit, QMessageBox,
                              QVBoxLayout, QHBoxLayout, QGroupBox, QScrollArea, QSplitter)
from .execution import launch
from .run_reader import load_run, summary, preview


class SetupPanel(QWidget):
    run_started = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.directory = None
        self.problem = None
        self.problem_dialog = None
        self.notified_problem = None
        self.selected_stage = None
        self._last_stage = None
        self._stage_run = None
        self._stage_alive = False
        self._pending_decision = False
        self._workspace_started = False
        self._log_preference = None
        self.spec_dialog = None
        layout = QVBoxLayout(self)
        from .progress import FlowStatus
        self.flow_status = FlowStatus()
        layout.addWidget(self.flow_status)
        self.flow_status.graph.stage_clicked.connect(self.open_stage)
        self.set_stage_navigation(False)
        actions = self.flow_status.actions
        self.start = QPushButton('Start Design')
        self.start.setObjectName('primaryAction')
        self.start.setProperty('runControl', True)
        self.start.clicked.connect(self.start_run)
        actions.addWidget(self.start)
        self.stop = QPushButton('Stop Run…')
        self.stop.setObjectName('dangerAction')
        self.stop.setProperty('runControl', True)
        self.stop.hide()
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.stop_run)
        actions.addWidget(self.stop)
        self.problem_button = QPushButton('View Problem')
        self.problem_button.hide()
        self.problem_button.clicked.connect(self.show_problem)
        actions.addWidget(self.problem_button)
        scroll = self.scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName('designBody')
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(12)
        from .input_panel import InputPanel
        self.input_panel = InputPanel()
        body_layout.addWidget(self.input_panel)
        self.input_panel.decision_changed.connect(self.focus_decision)
        from .cards import SectionCard
        from .stage_details import StageDetails
        self.rtl_modules = self.stage_details = StageDetails()
        self.module_tree = self.stage_details.modules
        self.setup_fields = SectionCard('Design Setup', 'Describe a design or load a specification, then choose the target speed.')
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(16)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setup_fields.content.addLayout(form)
        body_layout.addWidget(self.setup_fields)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.run_workspace = QWidget()
        workspace_layout = QVBoxLayout(self.run_workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_controls = QHBoxLayout()
        self.stage_caption = QLabel('Stage Details')
        self.stage_caption.setObjectName('fieldHeading')
        workspace_controls.addWidget(self.stage_caption, 1)
        self.active_stage_button = QPushButton('Back to Active Stage')
        self.active_stage_button.clicked.connect(self.return_to_active_stage)
        self.active_stage_button.hide()
        workspace_controls.addWidget(self.active_stage_button)
        self.spec_button = QPushButton('Approved Specification')
        self.spec_button.setToolTip('Read the approved specification saved with this run.')
        self.spec_button.setEnabled(False)
        self.spec_button.clicked.connect(self.show_approved_specification)
        workspace_controls.addWidget(self.spec_button)
        self.details_toggle = QPushButton('Show Run Log')
        self.details_toggle.setCheckable(True)
        workspace_controls.addWidget(self.details_toggle)
        workspace_layout.addLayout(workspace_controls)
        self.module_split = QSplitter(Qt.Orientation.Horizontal)
        self.module_split.setHandleWidth(8)
        self.module_split.setChildrenCollapsible(False)
        self.module_split.addWidget(self.rtl_modules)
        self.module_split.setStretchFactor(0, 1)
        self.module_split.setStretchFactor(1, 1)
        workspace_layout.addWidget(self.module_split, 1)
        layout.addWidget(self.run_workspace, 1)
        self.run_workspace.hide()
        self.description = QPlainTextEdit()
        self.description.setPlaceholderText('Describe the controller you want to build. You will review it before generation starts.')
        self.description.setMaximumHeight(100)
        form.addRow('Design Description', self.description)
        self.yaml = QLineEdit()
        self.yaml.setPlaceholderText('Optional: use a saved specification instead')
        file_row = QHBoxLayout()
        file_row.addWidget(self.yaml)
        browse = QPushButton('Browse…')
        browse.clicked.connect(lambda: self.pick(self.yaml, 'YAML (*.yaml *.yml)'))
        file_row.addWidget(browse)
        form.addRow('Saved Specification', file_row)
        self.frequency = QLineEdit('200')
        self.frequency.setToolTip('Requested controller clock speed; actual performance depends on the completed design.')
        form.addRow('Target Speed (MHz)', self.frequency)
        self.advanced_toggle = QPushButton('Show Advanced Settings')
        self.advanced_toggle.setCheckable(True)
        form.addRow(self.advanced_toggle)
        self.advanced = QGroupBox('Advanced Settings')
        advanced = QFormLayout(self.advanced)
        self.review_frequency = QCheckBox('Review target speed and optionally ask for advice')
        advanced.addRow(self.review_frequency)
        self.remote = QLineEdit()
        self.remote.setPlaceholderText('Uses environment settings when blank')
        advanced.addRow('Server Configuration', self.remote)
        remote_browse = QPushButton('Choose Server Configuration…')
        remote_browse.clicked.connect(lambda: self.pick(self.remote, 'JSON (*.json)'))
        advanced.addRow(remote_browse)
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
            'Hide Advanced Settings' if checked else 'Show Advanced Settings'))
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        details_layout.addWidget(self.status)
        from .live_output import LiveOutput
        self.output = LiveOutput()
        self.output.setProperty('technicalText', True)
        self.output.setMinimumSize(120, 80)
        output_controls = QHBoxLayout()
        output_controls.addStretch()
        self.latest_output = QPushButton('Jump to Latest')
        self.latest_output.setObjectName('primaryAction')
        self.latest_output.setToolTip('Scroll to the newest line and resume following output.')
        self.latest_output.clicked.connect(self.output.jump_to_latest)
        self.latest_output.hide()
        self.output.following_changed.connect(lambda following: self.latest_output.setVisible(not following))
        output_controls.addWidget(self.latest_output)
        details_layout.addLayout(output_controls)
        details_layout.addWidget(self.output, 1)
        self.module_split.addWidget(self.details)
        self.module_split.setSizes([480, 520])
        body_layout.addStretch()
        self.details.hide()
        self.details_toggle.toggled.connect(self.details.setVisible)
        self.details_toggle.toggled.connect(self.show_log_workspace)
        self.details_toggle.toggled.connect(lambda checked: self.details_toggle.setText(
            'Hide Run Log' if checked else 'Show Run Log'))
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        from .platform_support import host_capabilities
        host = host_capabilities()
        if not host.local_execution:
            self.start.setEnabled(False)
            self.start.setToolTip(host.execution_message)
            self.platform_notice = QLabel(host.execution_message)
            self.platform_notice.setWordWrap(True)
            body_layout.insertWidget(0, self.platform_notice)
            return
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
            self.poll(notify=False)

    def pick(self, field, pattern):
        path, _ = QFileDialog.getOpenFileName(self, 'Select file', '', pattern)
        if path:
            field.setText(path)

    def show_log_workspace(self, checked):
        if checked and not self._pending_decision:
            self.run_workspace.show()
            self.scroll.hide()

    def show_approved_specification(self):
        if self.directory is None:
            return
        from .approved_spec import ApprovedSpecification
        if self.spec_dialog is not None:
            self.spec_dialog.close()
        self.spec_dialog = ApprovedSpecification(self.directory, self)
        self.spec_dialog.show()
        self.spec_dialog.raise_()
        self.spec_dialog.activateWindow()

    def open_stage(self, stage):
        if self._stage_run is None or self._pending_decision:
            return
        self.selected_stage = stage
        self.run_workspace.show()
        self.scroll.hide()
        self.render_stage()

    def set_stage_navigation(self, available):
        for node in self.flow_status.graph.nodes.values():
            node.setProperty('stageNavigation', available)
            node.setCursor(Qt.CursorShape.PointingHandCursor if available else Qt.CursorShape.ArrowCursor)
            node.setFocusPolicy(Qt.FocusPolicy.StrongFocus if available else Qt.FocusPolicy.NoFocus)
            purpose = node.toolTip().split('\n', 1)[0]
            guidance = ('Click to view stage details.' if available else
                        'Respond to the pending request to return to stage details.' if self._pending_decision else
                        'Stage details become available after you start a design. Your draft stays in Design Setup.')
            node.setToolTip(purpose + '\n' + guidance)
            node.style().unpolish(node)
            node.style().polish(node)

    def return_to_active_stage(self):
        self.open_stage(self._last_stage or 'generate')

    def render_stage(self):
        from .run_reader import SavedRun
        stage = self.selected_stage or 'generate'
        run = self._stage_run or SavedRun(self.directory or Path('.'), {})
        self.stage_details.show()
        self.stage_details.display(run, stage, alive=self._stage_alive)
        from .node_graph import MAIN_NODES
        self.stage_caption.setText(dict(MAIN_NODES)[stage])
        self.active_stage_button.setVisible(bool(self._last_stage and stage != self._last_stage))
        for key, node in self.flow_status.graph.nodes.items():
            node.setProperty('inspecting', key == stage)
            node.style().unpolish(node)
            node.style().polish(node)

    def update_stage_workspace(self, run, *, alive, pending):
        self._stage_run = run
        self._stage_alive = alive
        self._pending_decision = bool(pending)
        self.set_stage_navigation(not self._pending_decision)
        if alive and not pending and not self._workspace_started:
            self._workspace_started = True
            self.details_toggle.setChecked(True if self._log_preference is None else self._log_preference)
        current = run.state.get('current_stage')
        if current == 'debug':
            current = 'generate'
        from .node_graph import MAIN_NODES
        keys = dict(MAIN_NODES)
        if current not in keys:
            current = next((r['stage'] for r in reversed(run.results) if r.get('stage') in keys), 'generate')
        # Switch once on a transition; polling must not override a manual selection.
        if current != self._last_stage:
            self.selected_stage = current
            self._last_stage = current
        self.render_stage()
        self.scroll.setVisible(bool(pending))
        self.run_workspace.setVisible(not pending)

    def reset_design(self):
        """Clear the finished run and draft; retain machine/server configuration."""
        if self.process is not None and self.process.poll() is None:
            return False
        self.timer.stop()
        self.process = None
        self.directory = None
        self.started = None
        self.problem = None
        self.notified_problem = None
        if self.problem_dialog is not None:
            self.problem_dialog.close()
            self.problem_dialog = None
        self.problem_button.hide()
        self.input_panel.clear_request()
        self.input_panel.setEnabled(True)
        self.description.clear()
        self.yaml.clear()
        self.frequency.setText('200')
        self.review_frequency.setChecked(False)
        for field in self.flags.values():
            field.setChecked(False)
        self.advanced_toggle.setChecked(False)
        self.details_toggle.setChecked(False)
        self.status.clear()
        self.output.reset_output()
        self.setup_fields.show()
        self.start.show()
        from .platform_support import host_capabilities
        self.start.setEnabled(host_capabilities().local_execution)
        self.stop.hide()
        self.stop.setEnabled(False)
        self.flow_status.update_run({})
        self.rtl_modules.hide()
        from .rtl_progress import populate_module_tree
        populate_module_tree(self.module_tree, [])
        self._stage_run = None
        self._stage_alive = False
        self._pending_decision = False
        self.set_stage_navigation(False)
        self._workspace_started = False
        self.spec_button.setEnabled(False)
        if self.spec_dialog is not None:
            self.spec_dialog.close()
            self.spec_dialog = None
        self.selected_stage = self._last_stage = None
        self.run_workspace.hide()
        self.scroll.show()
        for node in self.flow_status.graph.nodes.values():
            node.setProperty('inspecting', False)
            node.style().unpolish(node)
            node.style().polish(node)
        return True

    def focus_decision(self):
        # Pending decisions are first in the scroll area, with actions above the editor.
        self.scroll.verticalScrollBar().setValue(0)

    def start_run(self):
        config = {key: field.isChecked() for key, field in self.flags.items()}
        config.update(input_yaml=self.yaml.text(), target_mhz=self.frequency.text(),
                      remote_config=self.remote.text() or None,
                      description=self.description.toPlainText().strip(),
                      review_frequency=self.review_frequency.isChecked())
        try:
            self.process, self.directory = launch(config, '')
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, 'Cannot start run', str(exc))
            return
        self.problem = None
        self.problem_button.hide()
        if self.problem_dialog is not None:
            self.problem_dialog.close()
        self.output.reset_output()
        self.started = datetime.now()
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.timer.start()
        self.poll()
        self.run_started.emit(self.directory)

    def poll(self, *, notify=True):
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
                else:
                    self.input_panel.clear_request()
            except (OSError, ValueError):
                pass
        self.setup_fields.setVisible(code is not None)
        self.start.setVisible(code is not None)
        self.stop.setVisible(code is None)
        self.input_panel.setEnabled(connected and remote_state != 'cancelling')
        self.stop.setEnabled(code is None and connected and remote_state != 'cancelling')
        state = load_run(self.directory)
        from .approved_spec import approved_spec_path
        self.spec_button.setEnabled(approved_spec_path(self.directory) is not None)
        self.stage_details.show()
        self.update_stage_workspace(state, alive=code is None, pending=pending)
        from .run_storage import worker_log_path, consolidate_completed_run
        if code is not None:
            consolidate_completed_run(self.directory)
        log = worker_log_path(self.directory)
        from .registry import read_record, lifecycle_path, atomic_write
        receipt = read_record(lifecycle_path(self.directory))
        if code is not None and receipt.get('lifecycle') == 'running':
            receipt.update(lifecycle='interrupted', returncode=code,
                           remote_cleanup={'status': 'cleanup_unconfirmed',
                                           'reason': 'Worker exited without a cleanup receipt.'})
            atomic_write(lifecycle_path(self.directory), receipt)
        if code is None:
            end = datetime.now().timestamp()
        else:
            end = receipt.get('finished')
            if not isinstance(end, (int, float)):
                # Older/interrupted runs may lack a finish time. Use their last
                # saved evidence rather than counting time since the GUI reopened.
                evidence = [path.stat().st_mtime for path in
                            (self.directory / 'state.json', log) if path.is_file()]
                end = max(evidence) if evidence else self.started.timestamp()
        elapsed = max(0, int(end - self.started.timestamp()))
        label = (remote_state if connected else 'Disconnected; worker process still alive') if code is None else 'Worker exited: ' + str(code)
        self.status.setText(f'Run Log · {label}')
        self.status.setToolTip(str(self.directory))
        self.flow_status.update_run(state.state, alive=code is None, connected=connected,
                                    pending=pending, lifecycle=remote_state if code is None else receipt.get('lifecycle', 'interrupted'),
                                    elapsed=elapsed)
        self.output.update_snapshot(preview(log))
        if code is not None:
            self.input_panel.clear_request()
            self.timer.stop()
            self.start.setEnabled(True)
            self.report_problem(state, receipt, code, log,
                                notify=notify and (self.parentWidget() is None or self.isVisible()))

    def report_problem(self, run, receipt, code, log, *, notify=True):
        outcome = receipt.get('lifecycle') or run.state.get('status') or 'interrupted'
        if outcome not in ('needs_attention', 'failed', 'interrupted'):
            return
        from .progress import STAGES
        failed = next((result for result in reversed(run.results)
                       if result.get('status') in ('needs_attention', 'failed')), {})
        stage = dict(STAGES).get(failed.get('stage'), failed.get('stage', ''))
        reason = failed.get('message', '').strip()
        if not reason:
            reason = ('The worker stopped unexpectedly without recording a reason. '
                      f'Worker exit code: {code}.')
        guidance = ('Review the details below and the technical output for this run before trying again.')
        if 'No module named' in reason:
            guidance = ('The Python environment running the GUI is missing an execution dependency. '
                        'Install shared/gui/requirements-execution.txt in that same environment, '
                        'then restart the GUI and try again.')
        elif 'Shared Slurm allocation failed' in reason and 'timed out' in reason.lower():
            guidance = ('The remote allocation request timed out before verification could run. '
                        'Check server connectivity and the shared_allocation.json receipt. '
                        'This does not indicate an RTL generation failure.')
        elif outcome == 'interrupted':
            guidance = ('Review the technical output before starting a new run. '
                        'This run cannot be resumed automatically.')
        cleanup = receipt.get('remote_cleanup', {})
        if cleanup.get('status') == 'cleanup_unconfirmed':
            guidance += ' Remote cleanup is unconfirmed; check the allocation receipt before retrying remote work.'
        title = f'{stage} stopped' if stage else 'Design run stopped'
        from .problems import is_connection_error, CONNECTION_TITLE, CONNECTION_REASON, CONNECTION_GUIDANCE
        if is_connection_error(failed):
            title = CONNECTION_TITLE
            reason = CONNECTION_REASON
            guidance = CONNECTION_GUIDANCE
            if cleanup.get('status') == 'cleanup_unconfirmed':
                guidance += ' Remote cleanup is unconfirmed; check the allocation receipt before retrying.'
            self.flow_status.activity.setProperty('activityState', 'waiting')
            self.flow_status.activity.style().unpolish(self.flow_status.activity)
            self.flow_status.activity.style().polish(self.flow_status.activity)
        self.problem = (title, reason, guidance,
                        summary(run) + f'\n\nWorker exit code: {code}\nWorker log: {log}'
                        + '\n\n' + json.dumps(receipt, indent=2)
                        + '\n\nWorker output:\n' + preview(log))
        self.flow_status.activity.setText(
            reason + ' Check VPN and network access.' if is_connection_error(failed) else f'{title}: {reason}')
        self.problem_button.show()
        key = (str(self.directory), outcome, reason)
        if self.notified_problem != key:
            self.notified_problem = key
            if notify:
                self.show_problem()

    def show_problem(self):
        if self.problem is None:
            return
        if self.problem_dialog is not None:
            self.problem_dialog.close()
        title, reason, guidance, details = self.problem
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle(title)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(reason)
        dialog.setInformativeText(guidance)
        dialog.setDetailedText(details)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        self.problem_dialog = dialog
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def stop_run(self, *, confirm=True):
        if self.process is None or self.process.poll() is not None:
            return False
        if confirm:
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
