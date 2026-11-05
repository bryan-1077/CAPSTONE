"""Stage progress from saved evidence and verified worker liveness."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QProgressBar

STAGES = [('generate', 'Create design'), ('validation', 'Check design'),
          ('backend', 'Build layout'), ('gdsii-validation', 'Check layout')]
SYMBOLS = {'passed': '✓', 'running': '▶', 'waiting': '…', 'failed': '✕',
           'needs_attention': '!', 'skipped': '—', 'pending': '○', 'not_run': '—'}


def progress_state(state, *, alive=False, connected=False, pending=None, lifecycle='', elapsed=0):
    config = state.get('config', {})
    statuses = {name: 'pending' for name, _ in STAGES}
    if config.get('skip_verification'):
        statuses['validation'] = 'skipped'
    if config.get('no_backend'):
        statuses['backend'] = statuses['gdsii-validation'] = 'skipped'
    for result in state.get('results', []):
        stage = 'generate' if result.get('stage') == 'debug' else result.get('stage')
        if stage in statuses:
            statuses[stage] = result.get('status', 'unknown')
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
        title = {'spec_approval': 'Waiting for specification approval', 'description': 'Waiting for revised description',
                 'frequency': 'Waiting for clock selection', 'credential': 'Waiting for SSH password'}.get(pending.get('kind'), 'Waiting for input')
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
    complete = not alive and (lifecycle or state.get('status')) == 'complete'
    return statuses, title, busy, complete, max(0, int(elapsed))


class FlowStatus(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('flowStatus')
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.stages = {}
        for stage, caption in STAGES:
            label = QLabel()
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            row.addWidget(label)
            self.stages[stage] = label
        self.heading = QLabel('Your design progress')
        self.heading.setStyleSheet('font-size: 18px; font-weight: 600;')
        layout.addWidget(self.heading)
        layout.addLayout(row)
        self.activity = QLabel()
        self.activity.setTextFormat(Qt.TextFormat.PlainText)
        self.activity.setWordWrap(True)
        self.activity.setStyleSheet('font-size: 22px; font-weight: 600;')
        self.elapsed_label = QLabel()
        self.elapsed_label.setStyleSheet('font-size: 13px;')
        description = QVBoxLayout()
        description.addWidget(self.activity)
        description.addWidget(self.elapsed_label)
        status_row = QHBoxLayout()
        status_row.addLayout(description, 1)
        from .forge import ForgeIndicator
        self.forge = ForgeIndicator()
        status_row.addWidget(self.forge, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(status_row)
        self.bar = QProgressBar()
        self.bar.setFixedHeight(24)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        self.update_run({})

    def update_run(self, state, **runtime):
        statuses, title, busy, complete, elapsed = progress_state(state, **runtime)
        for stage, caption in STAGES:
            status = statuses[stage]
            label = self.stages[stage]
            label.setText(f'{SYMBOLS.get(status, "?")} {caption} · {status.replace("_", " ")}')
            label.setProperty('flowState', status)
            label.style().unpolish(label)
            label.style().polish(label)
        self.activity.setText(title)
        self.elapsed_label.setText(f'Elapsed {elapsed // 60}m {elapsed % 60:02d}s')
        # The anvil represents a living flow, including input waits and reconnects.
        self.forge.set_running(bool(runtime.get('alive', False)))
        self.bar.setRange(0, 0 if busy else 1)
        if not busy:
            self.bar.setValue(1 if complete else 0)
        self.bar.setAccessibleName(title)
