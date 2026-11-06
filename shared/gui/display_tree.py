"""Read-only evidence tables without cell selection or editing decorations."""
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate, QStyle, QTreeWidget


class NoCellFocusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, option, index)


class DisplayTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setItemDelegate(NoCellFocusDelegate(self))
