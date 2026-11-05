"""Editable specification review with typed fields and complete source coverage."""
from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QGridLayout, QFormLayout,
                              QGroupBox, QLineEdit, QComboBox, QCheckBox, QLabel)

CHOICES = {('memory', 'protocol'): ['DDR4'], ('memory', 'speed'): ['2400', '3200'],
           ('topology', 'banks'): [1, 2, 4],
           ('features', 'scheduler'): ['simple', 'round_robin'],
           ('features', 'page_policy'): ['open_page', 'close_page']}
LABELS = {'design_name': 'Design name', 'profile': 'Design profile', 'type': 'Profile',
          'memory': 'Memory', 'protocol': 'Memory standard', 'speed': 'DDR transfer rate (MT/s)',
          'topology': 'Organization', 'banks': 'Memory banks', 'features': 'Controller features',
          'scheduler': 'Scheduling policy', 'page_policy': 'Page policy',
          'refresh': 'Automatic refresh', 'tFAW': 'Four-activation timing (tFAW)',
          'tRRD': 'Row-to-row timing (tRRD)'}
DISPLAY = {'simple': 'Simple', 'round_robin': 'Round robin', 'open_page': 'Keep pages open',
           'close_page': 'Close pages after use', 'basic': 'Basic'}


class SpecificationEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.fields = {}
        self.candidate = {}

    def load(self, candidate):
        self.candidate = deepcopy(candidate)
        self.fields = {}
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        for index, (key, value) in enumerate(candidate.items()):
            group = QGroupBox(LABELS.get(key, key.replace('_', ' ').capitalize()))
            form = QFormLayout(group)
            if isinstance(value, dict):
                self.add_fields(form, value, (key,))
            else:
                self.add_fields(form, {key: value}, ())
            grid.addWidget(group, index // 2, index % 2)
        self.layout.addWidget(grid_host)

    def add_fields(self, form, values, prefix):
        for key, value in values.items():
            path = prefix + (key,)
            label = LABELS.get(key, key.replace('_', ' ').capitalize())
            if isinstance(value, dict):
                group = QGroupBox(label)
                nested = QFormLayout(group)
                self.add_fields(nested, value, path)
                form.addRow(group)
                continue
            if isinstance(value, bool):
                widget = QCheckBox(label)
                widget.setChecked(value)
                form.addRow(widget)
            elif path in CHOICES:
                widget = QComboBox()
                options = list(CHOICES[path])
                if value not in options:
                    options.append(value)
                for option in options:
                    widget.addItem(DISPLAY.get(str(option), str(option)), option)
                widget.setCurrentIndex(options.index(value))
                form.addRow(label, widget)
            elif isinstance(value, (str, int, float)):
                widget = QLineEdit(str(value))
                form.addRow(label, widget)
            else:
                widget = QLabel(str(value))
                widget.setTextFormat(Qt.TextFormat.PlainText)
                widget.setWordWrap(True)
                form.addRow(label, widget)
            self.fields[path] = (widget, value)

    def configuration(self):
        candidate = deepcopy(self.candidate)
        for path, (widget, original) in self.fields.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
                if type(original) in (int, float, str):
                    value = type(original)(value)
            elif isinstance(widget, QLineEdit):
                value = type(original)(widget.text())
            else:
                continue
            parent = candidate
            for key in path[:-1]:
                parent = parent[key]
            parent[path[-1]] = value
        return candidate
