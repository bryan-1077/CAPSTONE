"""Shared section-card layout for setup and saved evidence."""
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QTabBar


class AlignedTabBar(QTabBar):
    header_height = 0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDrawBase(False)

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        hint.setHeight(max(hint.height(), self.header_height))
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setHeight(max(hint.height(), self.header_height))
        return hint


class DirectoryHeader(QWidget):
    width_changed = Signal()

    def sizeHint(self):
        tabs = self.parentWidget()
        width = self.minimumWidth()
        if hasattr(tabs, 'tabBar'):
            width = max(width, tabs.width() - tabs.tabBar().sizeHint().width() - 12)
        return QSize(width, self.minimumHeight())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.width_changed.emit()


class DirectoryLabel(QLabel):
    """Show the complete path wrapped within the tab-header space."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)

    def set_path(self, path):
        self.path = str(path)
        self.setToolTip(self.path)
        self.setText(self.path)
        self.updateGeometry()

    def sizeHint(self):
        return QSize(254, self.heightForWidth(254))


class SectionCard(QFrame):
    def __init__(self, title, description='', parent=None):
        super().__init__(parent)
        self.setObjectName('sectionCard')
        self.default_description = description
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(16, 14, 16, 14)
        self.content.setSpacing(12)
        self.title = QLabel(title)
        self.title.setObjectName('sectionTitle')
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        heading = QHBoxLayout()
        heading.setSpacing(16)
        heading.addWidget(self.title, 1)
        from .decoration import TraceBand
        traces = TraceBand(compact=True)
        traces.setFixedWidth(150)
        heading.addWidget(traces)
        self.content.addLayout(heading)
        self.description = QLabel(description)
        self.description.setObjectName('sectionDescription')
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setWordWrap(True)
        self.content.addWidget(self.description)

    def add_widget(self, widget, stretch=0):
        if widget.objectName() == 'historyBadge':
            self.content.addWidget(widget, stretch, Qt.AlignmentFlag.AlignLeft)
        else:
            self.content.addWidget(widget, stretch)


def status_badge(status, text):
    label = QLabel(text)
    label.setObjectName('historyBadge')
    label.setProperty('historyState', status)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return label


def status_cell(status, text):
    """Fit a badge to its text within a table's wider evidence column."""
    cell = QFrame()
    cell.setObjectName('statusCell')
    cell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    row = QHBoxLayout(cell)
    row.setContentsMargins(4, 2, 4, 2)
    row.addStretch()
    row.addWidget(status_badge(status, text))
    return cell
