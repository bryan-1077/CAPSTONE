"""Flow activity bar with a clock independent of Qt style animations."""
from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProgressBar
from .theme import theme_colors


class ActivityBar(QProgressBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.paused = False
        self.paused_total = 0
        self.pause_started = 0
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.update)

    def set_running(self, running, *, complete=False, paused=False):
        if running != self.running:
            self.running = running
            if running:
                self.clock.start()
                self.paused_total = 0
                self.paused = False
                self.setRange(0, 0)
            else:
                self.timer.stop()
                self.setRange(0, 1)
        paused = bool(running and paused)
        if paused != self.paused:
            if paused:
                self.pause_started = self.clock.elapsed()
            elif running:
                self.paused_total += self.clock.elapsed() - self.pause_started
            self.paused = paused
        if running:
            if self.paused:
                self.timer.stop()
            elif self.isVisible() and not self.timer.isActive():
                self.timer.start()
        else:
            self.setValue(1 if complete else 0)
        self.update()

    def showEvent(self, event):
        if self.running and not self.paused:
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        if not self.running:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        colors = theme_colors()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        painter.setPen(QPen(QColor('#626971' if self.paused else colors['trace']), 1))
        painter.setBrush(QColor('#30353b' if self.paused else colors['field']))
        painter.drawRoundedRect(track, 3, 3)
        interior = track.adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(interior, 2, 2)
        painter.setClipPath(clip)
        # Wrap the segment continuously; refreshes never reset this clock.
        width = max(24, interior.width() * .22)
        elapsed = (self.pause_started if self.paused else self.clock.elapsed()) - self.paused_total
        position = (elapsed % 1800) / 1800 * (interior.width() + width)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#858c95' if self.paused else '#79d6e8'))
        painter.drawRoundedRect(QRectF(interior.left() + position - width,
                                      interior.top(), width, interior.height()), 2, 2)
        painter.end()
