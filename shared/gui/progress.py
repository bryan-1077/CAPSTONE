"""Stage progress from saved evidence and verified worker liveness."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel
from .activity import ActivityBar
from .node_graph import NodeGraph, MAIN_NODES
from .problems import is_connection_error, run_connection_error, CONNECTION_REASON

STAGES = MAIN_NODES


def progress_state(state, *, alive=False, connected=False, pending=None, lifecycle='', elapsed=0):
    config = state.get('config', {})
    statuses = {name: 'pending' for name, _ in STAGES}
    if config.get('skip_verification'):
        statuses['validation'] = 'skipped'
    if config.get('no_backend'):
        statuses['backend'] = statuses['gdsii-validation'] = 'skipped'
    for result in state.get('results', []):
        stage = result.get('stage')
        if stage in statuses:
            statuses[stage] = 'connection_error' if is_connection_error(result) else result.get('status', 'unknown')
    current = state.get('current_stage')
    stage = 'generate' if current == 'debug' else current
    active = alive and connected
    if active and stage in statuses:
        statuses[stage] = 'waiting' if pending else 'running'
    if active and lifecycle == 'cancelling':
        title, busy = 'Stopping and checking cleanup', True
    elif alive and not connected:
        title, busy = 'Disconnected — worker process is alive; progress unconfirmed', False
    elif active and pending:
        title = {'spec_approval': 'Waiting for you — approve your design', 'description': 'Waiting for you — revise your description',
                 'frequency': 'Waiting for you — confirm the target speed', 'credential': 'Waiting for you — enter SSH password'}.get(pending.get('kind'), 'Waiting for you — input required')
        busy = False
    elif active:
        title = 'Repairing RTL' if current == 'debug' else dict(STAGES).get(current, 'Preparing run')
        busy = True
    else:
        outcome = lifecycle or state.get('status', 'idle')
        title = {'idle': 'Ready to start', 'complete': 'Your design is ready',
                 'cancelled': 'Design run stopped', 'failed': 'Something went wrong',
                 'needs_attention': 'Your design needs attention',
                 'interrupted': 'Design run was interrupted'}.get(outcome, str(outcome).replace('_', ' ').capitalize())
        busy = False
        if stage in statuses and statuses[stage] == 'pending':
            statuses[stage] = 'waiting'
        if outcome == 'failed' and run_connection_error(state):
            title = CONNECTION_REASON + ' Check VPN and network access.'
    complete = not alive and (lifecycle or state.get('status')) == 'complete'
    return statuses, title, busy, complete, max(0, int(elapsed))


class FlowStatus(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('flowStatus')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        self.graph = NodeGraph()
        self.stages = self.graph.nodes
        self.heading = QLabel('Design Flow')
        self.heading.setObjectName('flowHeading')
        heading_row = QHBoxLayout()
        heading_row.addWidget(self.heading, 1)
        from .decoration import TraceBand
        traces = TraceBand(compact=True)
        traces.setFixedWidth(150)
        heading_row.addWidget(traces)
        layout.addLayout(heading_row)
        layout.addWidget(self.graph)
        self.activity = QLabel()
        self.activity.setTextFormat(Qt.TextFormat.PlainText)
        self.activity.setWordWrap(True)
        self.activity.setObjectName('flowActivity')
        self.activity.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.elapsed_label = QLabel()
        self.elapsed_label.setObjectName('elapsed')
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        description = QVBoxLayout()
        description.setSpacing(6)
        description.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        description.addWidget(self.activity)
        description.addWidget(self.elapsed_label)
        status_row = QHBoxLayout()
        status_row.setSpacing(20)
        self.actions = QVBoxLayout()
        self.actions.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        status_row.addLayout(self.actions)
        import os
        from .chip import ChipIndicator
        from .forge import ForgeIndicator
        indicator = ForgeIndicator if os.environ.get('CAPSTONE_GUI_INDICATOR') == 'anvil' else ChipIndicator
        self.forge = indicator()
        status_row.addWidget(self.forge, alignment=Qt.AlignmentFlag.AlignVCenter)
        status_row.addLayout(description, 1)
        layout.addLayout(status_row)
        self.bar = ActivityBar()
        self.bar.setFixedHeight(10)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        self.update_run({})

    def update_run(self, state, **runtime):
        statuses, title, _busy, complete, elapsed = progress_state(state, **runtime)
        for stage, caption in STAGES:
            status = statuses[stage]
            label = self.stages[stage]
            label.set_status(status)
        self.graph.update()
        self.activity.setText(title)
        emphasis = ('waiting' if runtime.get('alive') and runtime.get('connected') and runtime.get('pending')
                    else 'running' if runtime.get('alive') else
                    'waiting' if run_connection_error(state) else
                    'passed' if complete else runtime.get('lifecycle') or state.get('status', 'idle'))
        self.activity.setProperty('activityState', emphasis)
        self.activity.style().unpolish(self.activity)
        self.activity.style().polish(self.activity)
        self.elapsed_label.setText(f'Elapsed {elapsed // 60}m {elapsed % 60:02d}s')
        self.elapsed_label.setVisible(bool(state) or 'elapsed' in runtime)
        # The indicator represents a living flow, including input waits and reconnects.
        self.forge.set_running(bool(runtime.get('alive', False)))
        waiting = bool(runtime.get('pending')) and runtime.get('lifecycle') != 'cancelling'
        self.bar.set_running(bool(runtime.get('alive', False)), complete=complete, paused=waiting)
        self.bar.setAccessibleName(title)
