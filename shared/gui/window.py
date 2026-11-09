"""Selected-run workspace and independent active-worker monitoring."""
from pathlib import Path
from PySide6.QtCore import Qt, QUrl, QTimer, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog,
    QListWidgetItem, QSplitter, QTabWidget, QPlainTextEdit,
    QLabel, QMessageBox, QSizePolicy,
    QFrame, QToolButton, QMenu, QGridLayout, QStackedWidget,
)
from .run_reader import DEFAULT_RUNS, discover, load_run, preview


class RunWindow(QMainWindow):
    def __init__(self, root=DEFAULT_RUNS, *, preferences_path=None):
        super().__init__()
        from PySide6.QtWidgets import QApplication
        from .theme import apply_theme
        apply_theme(QApplication.instance(), 'blue')
        self.root = Path(root)
        from .preferences import DEFAULT_PATH, load_preferences
        self.preferences_path = preferences_path or DEFAULT_PATH
        self.preferences = load_preferences(self.preferences_path)
        self.run = None
        self._new_design_mode = True
        self.close_when_stopped = False
        self.close_timer = QTimer(self)
        self.close_timer.setInterval(250)
        self.close_timer.timeout.connect(self.finish_close)
        self.setWindowTitle('Ramageddon - LLM MC2')
        self.resize(1150, 740)
        from .decoration import BoardSurface, TraceBand
        container = BoardSurface()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(22, 14, 22, 14)
        layout.setSpacing(10)
        from .action_button import ActionButton
        self.new_design = ActionButton('New Design')
        self.new_design.setObjectName('primaryAction')
        self.new_design.setToolTip('Open a fresh design setup.')
        self.new_design.clicked.connect(self.open_design)
        self.history_toggle = ActionButton('Saved Runs')
        self.history_toggle.setObjectName('historyToggle')
        self.history_toggle.setToolTip('Show or hide saved design runs.')
        self.history_toggle.setCheckable(True)
        self.history_toggle.toggled.connect(self.toggle_history)
        from .navigation_icons import navigation_icon
        self.new_design.setIcon(navigation_icon('new-design'))
        self.history_toggle.setIcon(navigation_icon('saved-runs'))
        for button in (self.new_design, self.history_toggle):
            button.setProperty('headerAction', True)
            button.setIconSize(QSize(18, 18))
        header = QGridLayout()
        header.setColumnStretch(0, 1)
        header.setColumnStretch(2, 1)
        brand = QVBoxLayout()
        brand.setSpacing(2)
        name = QLabel('Ramageddon - LLM MC2')
        name.setObjectName('brand')
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption = QLabel('Agentic AI DDR4 Memory Controller Chip Design Flow')
        caption.setObjectName('brandCaption')
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(name)
        brand.addWidget(caption)
        header.addLayout(brand, 0, 1)
        header_controls = QWidget()
        controls = QHBoxLayout(header_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        controls.addWidget(self.new_design)
        controls.addWidget(self.history_toggle)
        header.addWidget(header_controls, 0, 2, Qt.AlignmentFlag.AlignRight)
        for column in (0, 2):
            header.setColumnMinimumWidth(column, header_controls.sizeHint().width())
        console_caption = QLabel('MC2 / DESIGN CONSOLE')
        console_caption.setObjectName('consoleCaption')
        header.addWidget(console_caption, 0, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(header)
        layout.addWidget(TraceBand())
        context = QHBoxLayout()
        self.selection_label = QLabel('New Design')
        self.selection_label.setObjectName('selectedRunName')
        self.selection_label.setWordWrap(True)
        self.selection_label.setTextFormat(Qt.TextFormat.PlainText)
        selection_info = QVBoxLayout()
        selection_info.setSpacing(3)
        selection_info.addWidget(self.selection_label)
        self.selection_details = QLabel()
        self.selection_details.setObjectName('selectedRunDetails')
        self.selection_details.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_details.setWordWrap(True)
        self.selection_details.hide()
        selection_info.addWidget(self.selection_details)
        context.addLayout(selection_info, 1)
        self.selection_status = QLabel('Ready to Start')
        self.selection_status.setObjectName('historyBadge')
        context.setSpacing(12)
        context.addWidget(self.selection_status, 0, Qt.AlignmentFlag.AlignVCenter)
        self.active_run = QPushButton('Active Run')
        self.active_run.setObjectName('primaryAction')
        self.active_run.clicked.connect(self.return_to_active_run)
        self.active_run.hide()
        context.addWidget(self.active_run)
        layout.addLayout(context)
        from .cards import DirectoryLabel, DirectoryHeader, AlignedTabBar
        self.location = DirectoryLabel()
        self.location.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.location.setMinimumWidth(50)
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        self.location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.location_row = DirectoryHeader()
        location_layout = QVBoxLayout(self.location_row)
        location_layout.setContentsMargins(0, 4, 32, 4)
        location_layout.setSpacing(2)
        location_name = self.location_caption = QLabel('History Directory:')
        location_name.setObjectName('fieldHeading')
        location_layout.addWidget(location_name)
        location_layout.addWidget(self.location, 1)
        self.location_row.setMinimumWidth(270)
        split = self.workspace_split = QSplitter()
        self._history_width_adjusting = False
        self._history_width_manual = False
        split.splitterMoved.connect(self.history_divider_moved)
        split.setHandleWidth(8)
        layout.addWidget(split, 1)
        from .history import RunHistoryList
        self.history = RunHistoryList()
        self.history.setObjectName('runHistory')
        self.history.setSpacing(5)
        self.history.setMinimumWidth(340)
        self.history.currentItemChanged.connect(self.select_run)
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self.history_context_menu)
        self.history.itemClicked.connect(self.open_history_run)
        self.history.itemActivated.connect(self.open_history_run)
        self.tabs = QTabWidget()
        self.tabs.setTabBar(AlignedTabBar())
        self.tabs.setCornerWidget(self.location_row, Qt.Corner.TopRightCorner)
        self.tabs.setObjectName('workspaceTabs')
        from .setup import SetupPanel
        self.setup = SetupPanel(self)
        self.setup.run_started.connect(lambda directory: self.display_run(load_run(directory)))
        self.setup.setObjectName('designPage')
        from .run_review import RunReview
        self.review = RunReview()
        self.review.specification_requested.connect(self.show_saved_specification)
        self.run_page = QStackedWidget()
        self.run_page.addWidget(self.setup)
        self.run_page.addWidget(self.review)
        self.tabs.addTab(self.run_page, 'Run')
        self.tabs.setTabBarAutoHide(False)
        split.addWidget(self.tabs)
        self.history_panel = QWidget()
        history_layout = QVBoxLayout(self.history_panel)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(0)
        self.history_heading = QFrame()
        self.history_heading.setObjectName('historyHeading')
        heading_layout = QHBoxLayout(self.history_heading)
        heading_layout.setContentsMargins(12, 0, 8, 0)
        heading_label = QLabel('Saved Runs')
        heading_label.setObjectName('historyHeadingLabel')
        heading_layout.addWidget(heading_label, 1)
        self.history_options = QToolButton()
        self.history_options.setObjectName('historyOptions')
        self.history_options.setAccessibleName('History options')
        self.history_options.setToolTip('History options')
        from .decoration import options_icon
        self.history_options.setIcon(options_icon())
        self.history_options.setFixedSize(32, 30)
        self.history_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.history_menu = QMenu(self.history_options)
        self.open_run_folder_action = self.history_menu.addAction('Open Run Folder', self.open_folder)
        self.open_run_folder_action.setEnabled(False)
        self.history_menu.addSeparator()
        self.history_menu.addAction('Change History Folder…', self.choose_root)
        self.history_options.setMenu(self.history_menu)
        self.history_refresh = ActionButton('Refresh', icon_kind='refresh')
        self.history_refresh.setToolTip('Reload saved runs from disk.')
        self.history_refresh.clicked.connect(self.refresh)
        heading_layout.addWidget(self.history_refresh)
        heading_layout.addWidget(self.history_options)
        history_layout.addWidget(self.history_heading)
        history_layout.addWidget(self.history, 1)
        split.addWidget(self.history_panel)
        self.location_row.width_changed.connect(self.sync_history_header, Qt.ConnectionType.QueuedConnection)
        from .results import ResultsPanel
        self.results = ResultsPanel()
        self.results.stage_requested.connect(self.open_stage)
        self.results.evidence_requested.connect(self.show_evidence)
        self.results.specification_requested.connect(self.show_saved_specification)
        self.tabs.addTab(self.results, 'Results')
        self.spec_dialog = None
        from .cards import SectionCard
        artifacts = QWidget()
        alayout = QVBoxLayout(artifacts)
        alayout.setContentsMargins(0, 0, 0, 0)
        self.artifact_split = QSplitter(Qt.Orientation.Horizontal)
        self.artifact_split.setHandleWidth(8)
        self.artifact_split.setChildrenCollapsible(False)
        from .artifacts import ArtifactBrowser
        self.files = ArtifactBrowser()
        self.files.setMinimumSize(160, 80)
        self.files.currentItemChanged.connect(self.select_file)
        self.files.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self.file_context_menu)
        explorer = QWidget()
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(0, 0, 0, 0)
        explorer_layout.setSpacing(0)
        explorer_header = QFrame()
        explorer_header.setObjectName('explorerHeader')
        explorer_controls = QHBoxLayout(explorer_header)
        explorer_controls.setContentsMargins(8, 4, 4, 4)
        explorer_heading = QLabel('EXPLORER')
        explorer_heading.setObjectName('explorerHeading')
        explorer_controls.addWidget(explorer_heading, 1)
        from .explorer_icons import explorer_icon
        for tooltip, icon, callback in [
                ('Refresh Files', 'refresh', self.refresh_files),
                ('Collapse All Folders', 'collapse', self.files.collapseAll)]:
            action = QToolButton()
            action.setObjectName('explorerAction')
            action.setToolTip(tooltip)
            action.setAccessibleName(tooltip)
            action.setIcon(explorer_icon(icon))
            action.clicked.connect(callback)
            explorer_controls.addWidget(action)
        explorer_layout.addWidget(explorer_header)
        explorer_layout.addWidget(self.files, 1)
        self.artifact_split.addWidget(explorer)
        preview_panel = QWidget()
        preview_panel.setMinimumWidth(160)
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        preview_header = QFrame()
        preview_header.setObjectName('explorerHeader')
        preview_controls = QHBoxLayout(preview_header)
        preview_controls.setContentsMargins(10, 4, 6, 4)
        self.preview_title = QLabel('Select a File')
        self.preview_title.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_title.setObjectName('fieldHeading')
        self.preview_title.setWordWrap(True)
        self.preview_title.setMinimumWidth(0)
        preview_controls.addWidget(self.preview_title, 1)
        self.preview_open = QPushButton('Open Externally')
        self.preview_open.setEnabled(False)
        self.preview_open.clicked.connect(self.open_preview)
        preview_controls.addWidget(self.preview_open)
        preview_layout.addWidget(preview_header)
        self.preview_path = None
        self.preview_location = QLabel('Choose a file in Explorer to preview its contents.')
        self.preview_location.setObjectName('fileBreadcrumb')
        self.preview_location.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_location.setWordWrap(True)
        self.preview_location.setMinimumWidth(0)
        preview_layout.addWidget(self.preview_location)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setProperty('technicalText', True)
        self.text.setMinimumHeight(100)
        preview_layout.addWidget(self.text, 1)
        self.artifact_split.addWidget(preview_panel)
        self.artifact_split.setStretchFactor(0, 0)
        self.artifact_split.setStretchFactor(1, 1)
        self.artifact_split.setSizes([280, 620])
        alayout.addWidget(self.artifact_split, 1)
        self.files_card = SectionCard('Files', 'Select a run to browse logs, reports, configuration, and generated outputs.')
        self.files_card.add_widget(artifacts, 1)
        self.tabs.addTab(self.files_card, 'Files')
        self.sync_history_header()
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([810, 340])
        self.statusBar().showMessage('Describe your design or load a saved specification to begin.')
        self.refresh()
        if self.setup.process is not None and self.setup.process.poll() is None:
            self.return_to_active_run()
        else:
            self.open_design()
        self.toggle_history(False)
        self.history_timer = QTimer(self)
        self.history_timer.setInterval(1000)
        self.history_timer.timeout.connect(self.update_history_statuses)
        self.history_timer.start()
        self.update_design_action()
        QTimer.singleShot(0, self.restore_layout)

    def restore_layout(self):
        from .preferences import sizes
        for key, splitter in [('files_split', self.artifact_split),
                              ('live_split', self.setup.module_split), ('review_split', self.review.split)]:
            # The previous Files layout stacked panes vertically; those sizes
            # do not describe the new Explorer sidebar.
            value = sizes(self.preferences.get(key)) if key != 'files_split' or self.preferences.get('explorer_layout') == 1 else None
            if value:
                splitter.setSizes(value)
        self._saved_history_sizes = sizes(self.preferences.get('history_split'))
        if self._saved_history_sizes:
            self._history_width_manual = True
        log_visible = self.preferences.get('show_run_log')
        if isinstance(log_visible, bool):
            self.setup._log_preference = log_visible
            if self.setup.directory is not None and not self.setup._pending_decision:
                self.setup.details_toggle.setChecked(log_visible)
            self.review.log_toggle.setChecked(log_visible)

    def remember_layout(self):
        from .preferences import save_preferences
        value = dict(self.preferences)
        value['explorer_layout'] = 1
        for key, splitter in [('files_split', self.artifact_split),
                              ('live_split', self.setup.module_split), ('review_split', self.review.split)]:
            from .preferences import sizes
            if sizes(splitter.sizes()):
                value[key] = splitter.sizes()
        if self._history_width_manual and not self.history_panel.isHidden():
            value['history_split'] = self.workspace_split.sizes()
        value['show_run_log'] = (self.review.log_toggle.isChecked() if self.run_page.currentWidget() is self.review
                                 else self.setup.details_toggle.isChecked())
        save_preferences(self.preferences_path, value)

    def update_design_action(self):
        running = self.setup.process is not None and self.setup.process.poll() is None
        pending = bool(self.setup.input_panel.request) if running else False
        self.new_design.setText('New Design')
        self.new_design.setEnabled(not running)
        self.new_design.setToolTip('A design is running. Return to Active Run to monitor it.' if running
                                  else 'Open a fresh design setup.')
        self.active_run.setVisible(running)
        self.active_run.setText('Waiting for You — Open Active Run' if pending else 'Active Run')
        self.active_run.setProperty('historyState', 'needs_attention' if pending else 'running')
        self.active_run.style().unpolish(self.active_run)
        self.active_run.style().polish(self.active_run)

    def open_design(self):
        self.update_design_action()
        if self.setup.process is not None and self.setup.process.poll() is None:
            self.return_to_active_run()
            return
        self.setup.reset_design()
        self._new_design_mode = True
        self.run = None
        self.history.clearSelection()
        for index in range(self.history.count()):
            self.history.itemWidget(self.history.item(index)).set_selected(False)
        self.run_page.setCurrentWidget(self.setup)
        self.tabs.setCurrentWidget(self.run_page)
        self.selection_label.setText('New Design')
        self.selection_details.hide()
        self.selection_status.setText('Ready to Start')
        self.selection_status.setProperty('historyState', 'idle')
        self.selection_status.style().unpolish(self.selection_status)
        self.selection_status.style().polish(self.selection_status)
        self.results.clear()
        self.files.clear()
        self.clear_preview()
        self.files_card.description.setText(self.files_card.default_description)
        self.open_run_folder_action.setEnabled(False)
        self.setup.scroll.verticalScrollBar().setValue(0)
        self.setup.description.setFocus()
        self.statusBar().showMessage('Describe your design or load a saved specification to begin.')

    def return_to_active_run(self):
        if self.setup.directory is None:
            return
        self._new_design_mode = False
        self.display_run(load_run(self.setup.directory))
        self.tabs.setCurrentWidget(self.run_page)

    def display_run(self, run):
        changed = self.run is None or self.run.directory != run.directory
        self.run = run
        self._new_design_mode = False
        from .history import hardware_summary, run_status, LABELS
        status, _ = run_status(run)
        self.selection_label.setText('Selected Run · ' + run.directory.name)
        self.selection_details.setText(hardware_summary(run).replace(' MHz', ' MHz Target'))
        self.selection_details.show()
        self.selection_status.setText(LABELS.get(status, status.replace('_', ' ').title()))
        if (self.setup.directory == run.directory and self.setup.process is not None
                and self.setup.process.poll() is None and self.setup.input_panel.request):
            self.selection_status.setText('Waiting for You')
            status = 'needs_attention'
        self.selection_status.setProperty('historyState', status)
        self.selection_status.style().unpolish(self.selection_status)
        self.selection_status.style().polish(self.selection_status)
        own_worker = self.setup.directory is not None and self.setup.directory.resolve() == run.directory.resolve()
        self.run_page.setCurrentWidget(self.setup if own_worker else self.review)
        if not own_worker:
            self.review.display(run)
        self.results.display(run)
        self.open_run_folder_action.setEnabled(True)
        self.files_card.description.setText('Selected run: ' + run.directory.name)
        if changed:
            self.clear_preview()
            self.files.load_run(run)
        else:
            self.files.refresh_loaded_folders()
        self.statusBar().showMessage('Selected run: ' + run.directory.name)
        for index in range(self.history.count()):
            item = self.history.item(index)
            selected = item.data(Qt.ItemDataRole.UserRole).directory == run.directory
            self.history.itemWidget(item).set_selected(selected)

    def open_stage(self, stage):
        if self.run is None:
            return
        self.tabs.setCurrentWidget(self.run_page)
        panel = self.run_page.currentWidget()
        panel.open_stage(stage)

    def show_evidence(self, path):
        if path is None or self.run is None:
            return
        self.tabs.setCurrentWidget(self.files_card)
        self.files.select_path(path)

    def toggle_history(self, visible):
        self.history_panel.setVisible(visible)
        self.location_row.setVisible(visible)
        self.sync_history_header()
        signals_blocked = self.history_toggle.blockSignals(True)
        self.history_toggle.setChecked(visible)
        self.history_toggle.blockSignals(signals_blocked)
        if visible:
            self.refresh()
            if getattr(self, '_saved_history_sizes', None):
                self.workspace_split.setSizes(self._saved_history_sizes)
            self.size_history_sidebar()
            self.history.fit_timer.start(0)

    def history_divider_moved(self, position, index):
        if not self._history_width_adjusting:
            self._history_width_manual = True
            self._saved_history_sizes = self.workspace_split.sizes()

    def size_history_sidebar(self):
        if self._history_width_manual or self.history_panel.isHidden():
            return
        preferred = 400
        for index in range(self.history.count()):
            card = self.history.itemWidget(self.history.item(index))
            if card:
                card.ensurePolished()
                preferred = max(preferred, card.name.fontMetrics().horizontalAdvance(card.name.text())
                                + card.selected_label.sizeHint().width() + 64)
        available = self.workspace_split.width() - self.workspace_split.handleWidth()
        workspace_minimum = max(520, self.tabs.minimumSizeHint().width())
        maximum = max(self.history.minimumWidth(), min(int(available * .4), available - workspace_minimum))
        width = min(preferred, maximum)
        self._history_width_adjusting = True
        try:
            self.workspace_split.setSizes([max(0, available - width), width])
        finally:
            self._history_width_adjusting = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'history_panel'):
            QTimer.singleShot(0, self.size_history_sidebar)

    def sync_history_header(self):
        bar = self.tabs.tabBar()
        bar.setStyleSheet('')
        bar.header_height = 0
        bar.setMinimumHeight(0)
        height = bar.sizeHint().height()
        tab_chrome = height - bar.fontMetrics().height()
        if not self.location_row.isHidden():
            margins = self.location_row.layout().contentsMargins()
            path_width = self.location_row.sizeHint().width() - margins.left() - margins.right()
            required = self.location_caption.sizeHint().height() + self.location.heightForWidth(path_width) + 10
            self.location_row.setFixedHeight(required)
            height = max(height, required)
        bar.setMinimumHeight(height)
        bar.header_height = height
        bar.setStyleSheet(f'QTabBar::tab {{ height: {max(bar.fontMetrics().height(), height - tab_chrome)}px; }}')
        bar.updateGeometry()
        self.history_heading.setFixedHeight(height)

    def refresh(self):
        selected = self.run.directory if self.run else None
        self.history.blockSignals(True)
        self.history.clear()
        self.location.set_path(self.root)
        self.sync_history_header()
        for run in discover(self.root):
            self.add_run(run)
        self.history.blockSignals(False)
        if selected:
            for index in range(self.history.count()):
                if self.history.item(index).data(Qt.ItemDataRole.UserRole).directory == selected:
                    self.history.setCurrentRow(index)
                    break
            self.display_run(load_run(selected))

    def add_run(self, run):
        from .history import RunCard
        card = RunCard(run)
        item = QListWidgetItem()
        item.setText(f'{run.directory.name}\n{card.description.text()}\n{card.badge.text()}')
        item.setData(Qt.ItemDataRole.UserRole, run)
        item.setToolTip(card.toolTip())
        item.setSizeHint(card.sizeHint())
        self.history.addItem(item)
        self.history.setItemWidget(item, card)
        self.history.fit_timer.start(0)
        return item

    def update_history_statuses(self):
        self.update_design_action()
        # Selecting history never reattaches it to the active worker. Follow only
        # an explicitly selected active run, or the run just launched from setup.
        if self._new_design_mode and self.setup.directory is not None:
            self.display_run(load_run(self.setup.directory))
        elif self.run is not None:
            self.display_run(load_run(self.run.directory))
        if not self.history.isVisible():
            return
        known = {self.history.item(i).data(Qt.ItemDataRole.UserRole).directory
                 for i in range(self.history.count())}
        if self.setup.directory and self.setup.directory not in known and (self.setup.directory / 'state.json').exists():
            self.add_run(load_run(self.setup.directory))
        for index in range(self.history.count()):
            item = self.history.item(index)
            run = load_run(item.data(Qt.ItemDataRole.UserRole).directory)
            item.setData(Qt.ItemDataRole.UserRole, run)
            card = self.history.itemWidget(item)
            card.update_status(run)
            item.setToolTip(card.toolTip())
            item.setText(f'{run.directory.name}\n{card.description.text()}\n{card.badge.text()}')
        self.history.fit_timer.start(0)

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
        if item is not None:
            self.display_run(item.data(Qt.ItemDataRole.UserRole))

    def open_history_run(self, item):
        self.select_run(item)
        self.statusBar().showMessage('Selected run: ' + self.run.directory.name)

    def history_context_menu(self, position):
        item = self.history.itemAt(position)
        if item is None:
            return
        self.history.setCurrentItem(item)
        menu = QMenu(self.history)
        menu.addAction('Open Run Folder', self.open_folder)
        menu.exec(self.history.viewport().mapToGlobal(position))

    def show_saved_specification(self):
        if self.run is None:
            return
        from .approved_spec import ApprovedSpecification
        if self.spec_dialog is not None:
            self.spec_dialog.close()
        self.spec_dialog = ApprovedSpecification(self.run.directory, self)
        self.spec_dialog.show()
        self.spec_dialog.raise_()
        self.spec_dialog.activateWindow()

    def select_file(self, item, previous=None):
        if item:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            # Opening or collapsing a folder leaves the currently open file in
            # the preview, just as an editor's Explorer does.
            if not isinstance(path, Path) or path.is_dir():
                return
            self.preview_path = path
            self.preview_title.setText(path.name)
            self.preview_title.setToolTip(str(path))
            relative = path.relative_to(self.run.directory) if self.run and path.is_relative_to(self.run.directory) else path
            self.preview_location.setText(' › '.join(relative.parts))
            self.preview_location.setToolTip(str(path))
            self.preview_open.setEnabled(path.exists())
            self.text.setPlainText(preview(path))

    def clear_preview(self):
        self.preview_path = None
        self.preview_title.setText('Select a File')
        self.preview_title.setToolTip('')
        self.preview_location.setText('Choose a file in Explorer to preview its contents.')
        self.preview_location.setToolTip('')
        self.preview_open.setEnabled(False)
        self.text.clear()

    def open_preview(self):
        if self.preview_path is not None:
            self.open_path(self.preview_path)

    def refresh_files(self):
        if self.run is not None:
            self.files.refresh_loaded_folders()
            if self.preview_path is not None:
                self.text.setPlainText(preview(self.preview_path))

    def file_context_menu(self, position):
        item = self.files.itemAt(position)
        path = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(path, Path):
            return
        menu = QMenu(self.files)
        menu.addAction('Open Folder Externally' if path.is_dir() else 'Open File Externally',
                       lambda: self.open_path(path))
        from PySide6.QtWidgets import QApplication
        menu.addAction('Copy Path', lambda: QApplication.clipboard().setText(str(path)))
        menu.exec(self.files.viewport().mapToGlobal(position))

    def open_path(self, path):
        if not path.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, 'Cannot open artifact', f'Could not open {path}')

    def open_folder(self):
        if self.run:
            self.open_path(self.run.directory)

    def open_selected(self):
        item = self.files.currentItem()
        if item:
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(path, Path):
                self.open_path(path)

    def closeEvent(self, event):
        if self.setup.process is None or self.setup.process.poll() is not None:
            self.close_timer.stop()
            self.setup.timer.stop()
            self.setup.input_panel.value.clear()
            self.remember_layout()
            event.accept()
            return
        if self.close_when_stopped:
            event.ignore()
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Close?')
        dialog.setText('Are you sure you want to close? A design is still running.')
        stop = dialog.addButton('Stop Run and Close', QMessageBox.ButtonRole.DestructiveRole)
        leave = dialog.addButton('Maintain Run and Close', QMessageBox.ButtonRole.AcceptRole)
        keep = dialog.addButton('Cancel', QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(keep)
        dialog.setEscapeButton(keep)
        dialog.exec()
        if dialog.clickedButton() == leave:
            self.setup.input_panel.value.clear()
            self.setup.timer.stop()
            self.remember_layout()
            event.accept()
        else:
            if dialog.clickedButton() == stop:
                if self.setup.stop_run(confirm=False):
                    self.close_when_stopped = True
                    self.setup.start.setEnabled(False)
                    self.statusBar().showMessage('Stopping the design and waiting for cleanup before closing…')
                    self.close_timer.start()
                elif self.setup.process.poll() is not None:
                    self.close_timer.start()
            event.ignore()

    def finish_close(self):
        if self.setup.process is None or self.setup.process.poll() is not None:
            self.close_timer.stop()
            self.close()
