"""Log output that follows new text until the reader scrolls away."""
from collections import Counter
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QMenu, QPlainTextEdit, QPushButton
from .node_graph import MAIN_NODES
from .run_reader import log_sections, preview, read_log_window

SECTION_LABELS = {
    **dict(MAIN_NODES),
    'specs': 'User Specs',
    'bist': 'BIST',
    'debug': 'Debug Mode',
    'lec': 'Logical Equivalence Check (LEC)',
    'remote-check': 'SSH / Slurm Preflight',
}


class LiveOutput(QPlainTextEdit):
    following_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.following = True
        self._updating = False
        self._log_path = None
        self._reading_section = False
        self.section_button = QPushButton('Jump to Section')
        self.section_button.setObjectName('runLogSections')
        self.section_button.setToolTip('Jump to the start of a section in the run log.')
        self.section_button.setEnabled(False)
        self.section_menu = QMenu(self.section_button)
        self.section_menu.aboutToShow.connect(self._populate_sections)
        self.section_menu.triggered.connect(self._jump_to_section)
        self.section_button.setMenu(self.section_menu)
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
        if self._reading_section:
            self._reading_section = False
            self.update_log(self._log_path)
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def update_log(self, path, *, missing=''):
        if path != self._log_path:
            self._log_path = path
            self._reading_section = False
            self._set_following(True)
        self.section_button.setEnabled(path is not None and path.is_file())
        if not self._reading_section:
            self.update_snapshot(preview(path) if path is not None else missing)

    def _populate_sections(self):
        self.section_menu.clear()
        path = self._log_path
        sections = log_sections(path) if path is not None else []
        totals = Counter(stage for stage, _ in sections)
        attempts = Counter()
        for stage, offset in sections:
            title = SECTION_LABELS.get(stage, stage)
            if totals[stage] > 1:
                attempts[stage] += 1
                title += f' ({attempts[stage]})'
            self.section_menu.addAction(title.replace('&', '&&')).setData((path, offset))
        if not sections:
            self.section_menu.addAction('No section headings found').setEnabled(False)

    def _jump_to_section(self, action):
        path, offset = action.data()
        if path != self._log_path:
            return
        text, block_number = read_log_window(path, section=offset)
        if block_number is None:
            return
        self._reading_section = True
        self._set_following(False)
        self.update_snapshot(text)
        block = self.document().findBlockByNumber(block_number)
        self.setTextCursor(QTextCursor(block))
        self.horizontalScrollBar().setValue(0)
        self.centerCursor()
        self.setFocus()

    def reset_output(self):
        self._log_path = None
        self._reading_section = False
        self.section_button.setEnabled(False)
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
