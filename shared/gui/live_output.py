"""Log output that follows new text until the reader scrolls away."""
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit


class LiveOutput(QPlainTextEdit):
    following_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.following = True
        self._updating = False
        bar = self.verticalScrollBar()
        bar.actionTriggered.connect(lambda _: self._scroll_action(bar.sliderPosition()))
        bar.sliderMoved.connect(self._scroll_action)
        bar.rangeChanged.connect(self._range_changed)

    def _scroll_action(self, position):
        if not self._updating and position < self.verticalScrollBar().maximum():
            self._set_following(False)

    def _set_following(self, following):
        if self.following != following:
            self.following = following
            self.following_changed.emit(following)

    def _range_changed(self, minimum, maximum):
        # Opening or resizing the details panel can change its scroll range.
        if self.following and not self._updating:
            self.verticalScrollBar().setValue(maximum)

    def wheelEvent(self, event):
        if (event.angleDelta().y() > 0 or event.pixelDelta().y() > 0) and self.verticalScrollBar().maximum() > 0:
            self._set_following(False)
        super().wheelEvent(event)
        self._scroll_action(self.verticalScrollBar().value())

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Home) and self.verticalScrollBar().maximum() > 0:
            self._set_following(False)
        super().keyPressEvent(event)
        if event.key() == Qt.Key.Key_Home and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        self._scroll_action(self.verticalScrollBar().value())

    def jump_to_latest(self):
        self._set_following(True)
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def reset_output(self):
        self._updating = True
        try:
            self.clear()
        finally:
            self._updating = False
        self.jump_to_latest()

    def update_snapshot(self, text):
        previous = self.toPlainText()
        if text == previous:
            return
        vertical = self.verticalScrollBar().value()
        horizontal = self.horizontalScrollBar().value()
        selection = self.textCursor()
        anchor, position = selection.anchor(), selection.position()
        self._updating = True
        try:
            if text.startswith(previous):
                cursor = QTextCursor(self.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text[len(previous):])
            else:
                # Technical summaries change above the log on each poll.
                self.setPlainText(text)
                limit = self.document().characterCount() - 1
                cursor = self.textCursor()
                cursor.setPosition(min(anchor, limit))
                cursor.setPosition(min(position, limit), QTextCursor.MoveMode.KeepAnchor)
                self.setTextCursor(cursor)
            if self.following and not self.textCursor().hasSelection():
                self.moveCursor(QTextCursor.MoveOperation.End)
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().maximum() if self.following else vertical)
            self.horizontalScrollBar().setValue(horizontal)
        finally:
            self._updating = False
