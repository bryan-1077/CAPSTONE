"""Read-only access to the specification saved with a run."""
from pathlib import Path
import json
import math
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QPlainTextEdit,
                              QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout)
from .spec_editor import LABELS, DISPLAY, HIDDEN_FIELDS
from .display_tree import DisplayTree


def approved_spec_path(directory):
    if directory is None:
        return None
    for name in ('gui_approved_specs.yaml', 'approved_specs.yaml'):
        path = Path(directory) / name
        if path.is_file():
            return path
    return None


def target_speed_text(directory):
    if directory is not None:
        for filename in ('state.json', 'frequency_selection.json', 'gui_frequency_review.json'):
            try:
                data = json.loads((Path(directory) / filename).read_text(encoding='utf-8'))
                value = data.get('config', {}).get('target_mhz') if filename == 'state.json' else data.get('target_mhz')
                number = float(value) if not isinstance(value, bool) else float('nan')
                if math.isfinite(number) and number > 0:
                    return f'{number:g} MHz'
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    return 'Not recorded'


def approved_spec_button_text(directory):
    speed = target_speed_text(directory)
    return 'Approved Specification' + (f' · {speed}' if speed != 'Not recorded' else '')


class ApprovedSpecification(QDialog):
    def __init__(self, directory, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Approved Specification')
        self.resize(800, 580)
        layout = QVBoxLayout(self)
        name = QLabel('Run: ' + Path(directory).name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setObjectName('sectionTitle')
        name.setWordWrap(True)
        layout.addWidget(name)
        self.source = QLabel()
        self.source.setTextFormat(Qt.TextFormat.PlainText)
        self.source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        self.target_speed = QLabel('Target controller speed: ' + target_speed_text(directory))
        self.target_speed.setObjectName('sectionTitle')
        self.target_speed.setToolTip('Selected clock target for this run, not measured achieved frequency.')
        layout.addWidget(self.target_speed)
        tabs = QTabWidget()
        self.fields = DisplayTree()
        self.fields.setHeaderLabels(['Specification', 'Approved Value'])
        self.fields.setWordWrap(True)
        self.yaml = QPlainTextEdit()
        self.yaml.setReadOnly(True)
        self.yaml.setProperty('technicalText', True)
        tabs.addTab(self.fields, 'Specification')
        tabs.addTab(self.yaml, 'YAML')
        layout.addWidget(tabs, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.close)
        layout.addWidget(close)
        path = approved_spec_path(directory)
        if path is None:
            self.source.setText('No saved approved specification is available for this run.')
            return
        self.source.setText('Saved specification · Read only\n' + str(path))
        import yaml
        try:
            with path.open(encoding='utf-8') as handle:
                text = handle.read(512 * 1024 + 1)
            if len(text) > 512 * 1024:
                raise ValueError('Specification is too large to preview. Open it from Logs and Artifacts.')
            self.yaml.setPlainText(text)
            spec = yaml.safe_load(text)
            if not isinstance(spec, dict):
                raise ValueError('The saved specification is not a mapping.')
            self.add_fields(self.fields, {k: v for k, v in spec.items() if k not in HIDDEN_FIELDS})
            self.fields.expandToDepth(1)
            self.fields.setColumnWidth(0, 320)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.source.setText(f'Cannot display saved specification: {exc}')
            tabs.setCurrentWidget(self.yaml)

    def add_fields(self, parent, values, *, budget=None):
        budget = budget if budget is not None else [512]
        for key, value in values.items():
            if budget[0] <= 0:
                QTreeWidgetItem(parent, ['More fields', 'See the complete YAML tab.'])
                break
            budget[0] -= 1
            label = LABELS.get(str(key), str(key).replace('_', ' ').capitalize())
            rendered = ('Enabled' if value else 'Disabled') if isinstance(value, bool) else DISPLAY.get(str(value), str(value))
            item = QTreeWidgetItem(parent, [label, '' if isinstance(value, dict) else rendered])
            if isinstance(value, dict):
                self.add_fields(item, value, budget=budget)
