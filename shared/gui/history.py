"""Readable saved-run cards with conservative process-status reporting."""
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QEvent, QSize
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QListWidget

from .registry import read_record, lifecycle_path, alive
from .platform_support import host_capabilities
from .problems import run_connection_error, CONNECTION_TITLE, CONNECTION_GUIDANCE

LABELS = {'complete': 'Complete', 'passed': 'Passed', 'cancelled': 'Stopped',
          'connection_error': CONNECTION_TITLE,
          'needs_attention': 'Needs attention', 'failed': 'Failed',
          'interrupted': 'Interrupted', 'running': 'Running', 'cancelling': 'Stopping',
          'unconfirmed': 'Unconfirmed', 'unknown': 'Unknown', 'unreadable': 'Unreadable'}


class RunHistoryList(QListWidget):
    """Fit wrapped run cards to the viewport instead of scrolling sideways."""
    def __init__(self, parent=None):
        super().__init__(parent)
        from .display_tree import NoCellFocusDelegate
        self.setItemDelegate(NoCellFocusDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.fit_timer = QTimer(self)
        self.fit_timer.setSingleShot(True)
        self.fit_timer.timeout.connect(self.fit_cards)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Resize and hasattr(self, 'fit_timer'):
            self.fit_timer.start(0)
        return super().viewportEvent(event)

    def fit_cards(self):
        width = max(1, self.viewport().width() - 2 * self.spacing())
        for index in range(self.count()):
            item = self.item(index)
            card = self.itemWidget(item)
            if card is None:
                continue
            card.ensurePolished()
            height = card.layout().totalHeightForWidth(width)
            size = QSize(width, max(height, card.minimumSizeHint().height()))
            if item.sizeHint() != size:
                item.setSizeHint(size)


def run_status(run):
    if run.error:
        return 'unreadable', run.error
    receipt = read_record(lifecycle_path(run.directory))
    outcome = receipt.get('lifecycle')
    if outcome in (None, 'failed') and run_connection_error(run.state):
        return 'connection_error', CONNECTION_GUIDANCE + ' Remote cleanup is recorded separately.'
    if outcome in ('complete', 'cancelled', 'failed', 'needs_attention', 'interrupted'):
        return outcome, 'Recorded worker outcome. Remote cleanup is recorded separately.'
    active = read_record()
    record = next((record for record in (active, receipt)
                   if record.get('run_dir') and Path(record['run_dir']).resolve() == run.directory.resolve()), {})
    if record and alive(record):
        status = 'cancelling' if outcome == 'cancelling' else 'running'
        return status, 'Worker process identity is verified alive on this machine.'
    if run.status in ('running', 'pending', 'waiting') or outcome in ('running', 'cancelling'):
        if not host_capabilities().local_execution:
            return 'unconfirmed', 'Worker liveness cannot be checked on this platform.'
        if record.get('identity'):
            return 'interrupted', 'The recorded worker is no longer alive. Remote cleanup is not implied.'
        return 'unconfirmed', 'Saved as active, but no living worker can be verified. This is not a live running status.'
    return run.status.lower(), 'Saved run outcome.'


def hardware_summary(run):
    import yaml
    spec = {}
    paths = [run.directory / name for name in ('gui_approved_specs.yaml', 'approved_specs.yaml')]
    source = run.config.get('input_yaml')
    if isinstance(source, str) and source:
        paths.append(run.resolve(source))
    for path in paths:
        try:
            with path.open('r', encoding='utf-8') as handle:
                candidate = yaml.safe_load(handle.read(65536))
            if isinstance(candidate, dict):
                spec = candidate
                break
        except (OSError, ValueError, yaml.YAMLError):
            continue
    def mapping(value):
        return value if isinstance(value, dict) else {}
    controller = mapping(spec.get('controller_config'))
    memory = mapping(spec.get('memory'))
    topology = mapping(spec.get('topology')) or mapping(controller.get('topology'))
    protocol = memory.get('protocol') or controller.get('protocol')
    banks = topology.get('banks')
    target = run.config.get('target_mhz')
    parts = []
    if isinstance(protocol, str):
        parts.append(protocol)
    if isinstance(banks, int) and not isinstance(banks, bool):
        parts.append(f'{banks} bank' + ('s' if banks != 1 else ''))
    if isinstance(target, (str, int, float)) and not isinstance(target, bool):
        try:
            parts.append(f'{float(target):g} MHz')
        except ValueError:
            pass
    return ' · '.join(parts) or 'Specification not recorded'


class RunCard(QFrame):
    def __init__(self, run, parent=None):
        super().__init__(parent)
        self.setObjectName('historyCard')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(7)
        self.name = QLabel(run.directory.name)
        self.name.setWordWrap(True)
        self.name.setObjectName('historyName')
        self.description = QLabel(hardware_summary(run))
        self.description.setWordWrap(True)
        self.description.setObjectName('historyDescription')
        self.badge = QLabel()
        self.badge.setObjectName('historyBadge')
        self.date = QLabel()
        self.date.setObjectName('historyDate')
        for label in (self.name, self.description, self.badge, self.date):
            label.setTextFormat(Qt.TextFormat.PlainText)
        heading = QHBoxLayout()
        heading.addWidget(self.name, 1)
        self.selected_label = QLabel('Selected')
        self.selected_label.setObjectName('selectedRunLabel')
        policy = self.selected_label.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.selected_label.setSizePolicy(policy)
        self.selected_label.hide()
        heading.addWidget(self.selected_label)
        layout.addLayout(heading)
        layout.addWidget(self.description)
        footer = QHBoxLayout()
        footer.addWidget(self.badge)
        footer.addStretch()
        footer.addWidget(self.date)
        layout.addLayout(footer)
        try:
            stamp = (run.directory / 'state.json').stat().st_mtime
            self.date.setText(datetime.fromtimestamp(stamp).strftime('%b %d, %H:%M'))
        except OSError:
            pass
        self.update_status(run)

    def set_selected(self, selected):
        self.selected_label.setVisible(selected)
        self.setProperty('selected', selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def update_status(self, run):
        status, explanation = run_status(run)
        self.badge.setText(LABELS.get(status, status.replace('_', ' ').capitalize()))
        self.badge.setProperty('historyState', status)
        self.badge.setToolTip(explanation)
        self.setToolTip(f'{run.directory}\n{explanation}')
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
