"""Read-only saved-run window."""
import json
from pathlib import Path
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog,
    QListWidget, QListWidgetItem, QSplitter, QTabWidget, QPlainTextEdit,
    QTreeWidget, QTreeWidgetItem, QLabel, QMessageBox, QSizePolicy,
)
from .run_reader import DEFAULT_RUNS, discover, load_run, preview, summary


class RunWindow(QMainWindow):
    def __init__(self, root=DEFAULT_RUNS):
        super().__init__()
        from PySide6.QtWidgets import QApplication
        from .theme import apply_theme
        apply_theme(QApplication.instance())
        self.root = Path(root)
        self.run = None
        self.setWindowTitle('CAPSTONE — Design Flow')
        self.resize(1150, 740)
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        self.history_toggle = QPushButton('Show history')
        self.history_toggle.setCheckable(True)
        self.history_toggle.toggled.connect(self.toggle_history)
        header = QHBoxLayout()
        header.addWidget(self.history_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        header.addStretch()
        from .decoration import TraceAccent
        self.trace_accent = TraceAccent()
        header.addWidget(self.trace_accent)
        layout.addLayout(header)
        self.history_tools = QWidget()
        self.history_tools.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        toolbar = QHBoxLayout(self.history_tools)
        for label, callback in [('History Folder…', self.choose_root), ('Open Run…', self.choose_run),
                                ('Refresh', self.refresh), ('Open Run Folder', self.open_folder)]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addWidget(self.history_tools)
        self.location = QLabel()
        self.location.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.location)
        split = QSplitter()
        layout.addWidget(split, 1)
        self.history = QListWidget()
        self.history.currentItemChanged.connect(self.select_run)
        split.addWidget(self.history)
        self.tabs = QTabWidget()
        from .setup import SetupPanel
        self.setup = SetupPanel(self)
        self.tabs.addTab(self.setup, 'Design')
        self.tabs.setTabBarAutoHide(True)
        split.addWidget(self.tabs)
        self.overview = QPlainTextEdit()
        self.overview.setReadOnly(True)
        self.tabs.addTab(self.overview, 'Overview')
        self.stages = QTreeWidget()
        self.stages.setHeaderLabels(['Stage / attempt', 'Saved status', 'Message'])
        self.tabs.addTab(self.stages, 'Stages')
        self.config = QPlainTextEdit()
        self.config.setReadOnly(True)
        self.tabs.addTab(self.config, 'Configuration')
        artifacts = QWidget()
        alayout = QVBoxLayout(artifacts)
        self.files = QListWidget()
        self.files.currentItemChanged.connect(self.select_file)
        alayout.addWidget(self.files)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        alayout.addWidget(self.text)
        open_button = QPushButton('Open Selected Externally')
        open_button.clicked.connect(self.open_selected)
        alayout.addWidget(open_button)
        self.tabs.addTab(artifacts, 'Logs & Artifacts')
        split.setSizes([320, 830])
        self.statusBar().showMessage('Describe your design or load a saved specification to begin.')
        self.refresh()
        self.toggle_history(False)

    def toggle_history(self, visible):
        self.history.setVisible(visible)
        self.history_tools.setVisible(visible)
        self.location.setVisible(visible)
        self.history_toggle.setText('Hide history' if visible else 'Show history')
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, visible)
        if not visible:
            self.tabs.setCurrentWidget(self.setup)

    def refresh(self):
        selected = self.run.directory if self.run else None
        self.history.clear()
        self.run = None
        self.overview.clear()
        self.config.clear()
        self.stages.clear()
        self.files.clear()
        self.text.clear()
        self.location.setText(str(self.root))
        for run in discover(self.root):
            self.add_run(run)
        match = next((i for i in range(self.history.count())
                      if self.history.item(i).data(Qt.ItemDataRole.UserRole).directory == selected), 0)
        if self.history.count():
            self.history.setCurrentRow(match)
        else:
            self.overview.setPlainText('No saved runs found. Select a history folder or open a run directory containing state.json.')

    def add_run(self, run):
        item = QListWidgetItem(f'{run.directory.name}\n{run.status}')
        item.setData(Qt.ItemDataRole.UserRole, run)
        self.history.addItem(item)
        return item

    def choose_root(self):
        value = QFileDialog.getExistingDirectory(self, 'Select history folder', str(self.root))
        if value:
            self.root = Path(value)
            self.refresh()

    def choose_run(self):
        value = QFileDialog.getExistingDirectory(self, 'Select saved run', str(self.root))
        if value:
            self.history.setCurrentItem(self.add_run(load_run(value)))

    def select_run(self, item, previous=None):
        if item is None:
            return
        self.run = item.data(Qt.ItemDataRole.UserRole)
        self.overview.setPlainText(summary(self.run))
        self.config.setPlainText(json.dumps(self.run.config, indent=2))
        self.stages.clear()
        for index, result in enumerate(self.run.results, 1):
            QTreeWidgetItem(self.stages, [f'{index}. {result.get("stage", "unknown")}',
                                        str(result.get('status', 'unknown')),
                                        str(result.get('message', ''))])
        current = self.run.state.get('current_stage')
        if current:
            QTreeWidgetItem(self.stages, [str(current), 'Saved as active; liveness unknown', ''])
        self.stages.resizeColumnToContents(0)
        self.files.clear()
        self.text.clear()
        for path in self.run.files():
            item = QListWidgetItem(str(path))
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.files.addItem(item)

    def select_file(self, item, previous=None):
        if item:
            path = item.data(Qt.ItemDataRole.UserRole)
            self.text.setPlainText('Directory: use Open Selected Externally.' if path.is_dir() else preview(path))

    def open_path(self, path):
        if not path.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, 'Cannot open artifact', f'Could not open {path}')

    def open_folder(self):
        if self.run:
            self.open_path(self.run.directory)

    def open_selected(self):
        item = self.files.currentItem()
        if item:
            self.open_path(item.data(Qt.ItemDataRole.UserRole))

    def closeEvent(self, event):
        if self.setup.process is None or self.setup.process.poll() is not None:
            event.accept()
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle('A design flow is still running')
        dialog.setText('Leave the run executing and reconnect when CAPSTONE reopens, or request a stop.')
        keep = dialog.addButton('Keep Window Open', QMessageBox.ButtonRole.RejectRole)
        leave = dialog.addButton('Leave Run Executing & Close', QMessageBox.ButtonRole.AcceptRole)
        stop = dialog.addButton('Stop Run…', QMessageBox.ButtonRole.DestructiveRole)
        dialog.setDefaultButton(keep)
        dialog.setEscapeButton(keep)
        dialog.exec()
        if dialog.clickedButton() == leave:
            self.setup.password.clear()
            self.setup.input_panel.value.clear()
            self.setup.timer.stop()
            event.accept()
        else:
            if dialog.clickedButton() == stop:
                self.setup.stop_run()
            event.ignore()
