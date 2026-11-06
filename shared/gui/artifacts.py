"""Lazy folder browsing for run-local evidence and recorded external artifacts."""
from itertools import islice
from pathlib import Path
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QAbstractItemView
from .display_tree import NoCellFocusDelegate

PATH_ROLE = Qt.ItemDataRole.UserRole
LOADED_ROLE = int(PATH_ROLE) + 1
SORT_ROLE = int(PATH_ROLE) + 2


class ExplorerItem(QTreeWidgetItem):
    def __lt__(self, other):
        return (self.data(0, SORT_ROLE) or 0, self.text(0).casefold()) < (
            other.data(0, SORT_ROLE) or 0, other.text(0).casefold())


class ArtifactBrowser(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('artifactBrowser')
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setItemDelegate(NoCellFocusDelegate(self))
        self.setHeaderLabels(['Run Files and Artifact Locations'])
        self.setHeaderHidden(True)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setIndentation(16)
        self.setIconSize(QSize(16, 16))
        self.setUniformRowHeights(True)
        self.setAnimated(False)
        self.setExpandsOnDoubleClick(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.itemExpanded.connect(self.populate_folder)
        self.itemExpanded.connect(lambda item: self.update_folder_icon(item, True))
        self.itemCollapsed.connect(lambda item: self.update_folder_icon(item, False))
        self.itemClicked.connect(self.toggle_folder)

    def toggle_folder(self, item, column):
        path = item.data(0, PATH_ROLE)
        if (isinstance(path, Path) and path.is_dir() and not path.is_symlink()) or (
                path is None and item.childCount()):
            item.setExpanded(not item.isExpanded())

    def update_folder_icon(self, item, expanded):
        path = item.data(0, PATH_ROLE)
        if isinstance(path, Path) and path.is_dir():
            from .explorer_icons import explorer_icon
            item.setIcon(0, explorer_icon('folder-open' if expanded else 'folder'))

    def add_path(self, parent, path, label=None):
        item = ExplorerItem(parent, [label or path.name])
        item.setData(0, PATH_ROLE, path)
        item.setData(0, SORT_ROLE, 0 if path.is_dir() else 1)
        item.setToolTip(0, str(path))
        from .explorer_icons import explorer_icon
        kind = ('folder' if path.is_dir() else 'json' if path.suffix.lower() in ('.json', '.yaml', '.yml')
                else 'rtl' if path.suffix.lower() in ('.sv', '.v', '.svh', '.vh', '.tcl')
                else 'log' if path.suffix.lower() in ('.log', '.txt', '.rpt') else 'file')
        item.setIcon(0, explorer_icon(kind))
        if path.is_dir() and not path.is_symlink():
            item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        elif path.is_symlink():
            item.setText(0, item.text(0) + ' · Link')
        return item

    def populate_folder(self, item):
        path = item.data(0, PATH_ROLE)
        if not isinstance(path, Path) or not path.is_dir() or path.is_symlink() or item.data(0, LOADED_ROLE):
            return
        item.setData(0, LOADED_ROLE, True)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless)
        try:
            entries = list(islice(path.iterdir(), 1001))
            for entry in sorted(entries[:1000], key=lambda p: (not p.is_dir(), p.name.lower())):
                self.add_path(item, entry)
            if len(entries) > 1000:
                self.add_path(item, path, 'More than 1,000 entries · Open this folder externally').setChildIndicatorPolicy(
                    QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator)
        except OSError as exc:
            QTreeWidgetItem(item, ['Cannot list folder: ' + str(exc)])

    def load_run(self, run):
        self.clear()
        self.run_root = self.add_path(self, run.directory, run.directory.name)
        self.populate_folder(self.run_root)
        self.run_root.setExpanded(True)
        external = {}
        labels = {'mailbox': 'Published RTL Snapshot', 'rtl_dir': 'Frontend Working RTL (May Change)',
                  'manifest': 'Frontend Working Manifest (May Change)'}
        for result in run.results:
            for key, value in result.get('artifacts', {}).items():
                if not isinstance(value, str):
                    continue
                recorded = Path(value)
                # In-run paths need rebasing for moved runs. Existing external
                # snapshots must not be mistaken for a child of the run folder
                # just because their mailbox path also contains the run ID.
                path = run.resolve(value)
                if not path.exists() and recorded.is_absolute() and recorded.exists():
                    path = recorded
                if path.exists() and not path.is_relative_to(run.directory):
                    external.setdefault(path, labels.get(key, key.replace('_', ' ').title()))
        from .run_storage import lifecycle_path, worker_log_path
        for path in (lifecycle_path(run.directory), worker_log_path(run.directory)):
            if path.exists() and not path.is_relative_to(run.directory):
                external.setdefault(path, 'Legacy GUI Artifact · ' + path.name)
        if external:
            group = QTreeWidgetItem(self, ['External Artifacts'])
            group.setToolTip(0, 'Stored outside this run by the orchestrator. Published snapshots and working outputs are distinct.')
            for path, label in sorted(external.items(), key=lambda pair: pair[1]):
                self.add_path(group, path, label)
            group.setExpanded(True)
        state_item = next((self.run_root.child(i) for i in range(self.run_root.childCount())
                           if self.run_root.child(i).data(0, PATH_ROLE) == run.directory / 'state.json'), None)
        if state_item:
            self.setCurrentItem(state_item)

    def select_path(self, path):
        """Expand only the ancestors needed to reveal a result's exact evidence."""
        path = Path(path)

        def descend(item):
            candidate = item.data(0, PATH_ROLE)
            if candidate == path:
                if path.is_dir() and not path.is_symlink():
                    item.setExpanded(True)
                self.setCurrentItem(item)
                self.scrollToItem(item)
                return True
            if isinstance(candidate, Path):
                if not path.is_relative_to(candidate):
                    return False
                self.populate_folder(item)
                item.setExpanded(True)
            for index in range(item.childCount()):
                if descend(item.child(index)):
                    return True
            return False

        for index in range(self.topLevelItemCount()):
            if descend(self.topLevelItem(index)):
                return
        # A newly written report or a bounded directory listing may not be in
        # the tree yet. Keep the exact reference accessible without reloading it.
        item = self.add_path(self, path, 'Selected Evidence · ' + path.name)
        self.setCurrentItem(item)
        self.scrollToItem(item)

    def refresh_loaded_folders(self):
        """Reveal newly written stage files without resetting selection or scroll."""
        def refresh(item):
            path = item.data(0, PATH_ROLE)
            if isinstance(path, Path) and item.data(0, LOADED_ROLE) and not path.is_symlink():
                known = {item.child(i).data(0, PATH_ROLE) for i in range(item.childCount())}
                added = False
                try:
                    for entry in islice(path.iterdir(), 1000):
                        if entry not in known:
                            self.add_path(item, entry)
                            added = True
                except OSError:
                    pass
                if added:
                    item.sortChildren(0, Qt.SortOrder.AscendingOrder)
            for index in range(item.childCount()):
                refresh(item.child(index))
        for index in range(self.topLevelItemCount()):
            refresh(self.topLevelItem(index))
