"""Small vector-drawn work indicator; no image assets or desktop dependencies."""
import math
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class ForgeIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(120, 88)
        self.setAccessibleName('Hammer and anvil work indicator')
        self.setToolTip('Working on your design')
        self.clock = QElapsedTimer()
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.advance_frame)
        self.running = False

    def set_running(self, running):
        if self.running == running:
            if running and self.isVisible() and not self.timer.isActive():
                self.timer.start()
            return
        self.running = running
        if running:
            self.clock.start()
            if self.isVisible():
                self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def advance_frame(self):
        if self.running:
            self.update()

    def showEvent(self, event):
        if self.running:
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        phase = (self.clock.elapsed() % 1000) / 1000 if self.running else 0
        if phase < .65:
            angle = 75 * (1 - math.cos(math.pi * phase / .65)) / 2
        elif phase < .82:
            angle = 75 * (1 - ((phase - .65) / .17) ** 2)
        else:
            angle = 0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#d1ad58'))
        anvil = QPainterPath()
        anvil.moveTo(25, 54)
        for x, y in [(83, 54), (106, 57), (84, 64), (69, 64), (66, 73),
                     (80, 78), (80, 82), (28, 82), (28, 78), (43, 73),
                     (40, 64), (25, 64)]:
            anvil.lineTo(x, y)
        anvil.closeSubpath()
        painter.drawPath(anvil)
        painter.setPen(QPen(QColor('#f1d58c'), 2))
        painter.drawLine(QPointF(27, 55), QPointF(81, 55))
        painter.save()
        painter.translate(78, 44)
        painter.rotate(angle)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#af7847'))
        painter.drawRoundedRect(QRectF(-30, -3, 34, 6), 2, 2)
        painter.setBrush(QColor('#eef0e6'))
        painter.drawRoundedRect(QRectF(-42, -10, 17, 20), 3, 3)
        painter.restore()
        if self.running and .82 <= phase < .98:
            spread = (phase - .82) / .16
            painter.setPen(QPen(QColor('#f1d58c'), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for dx, dy in [(-15, -12), (-5, -20), (12, -16), (22, -6)]:
                start = QPointF(43 + dx * spread, 51 + dy * spread)
                painter.drawLine(start, start + QPointF(dx * .18, dy * .18))
        painter.end()
