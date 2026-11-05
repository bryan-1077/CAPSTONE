"""Lightweight PCB trace ornament drawn with Qt vectors."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget


class TraceAccent(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(34)
        self.setMinimumWidth(140)
        self.setMaximumWidth(320)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#b99a50'), 1.5, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        right = self.width() - 8
        for start, turn, finish in [(3, 70, 11), (12, 100, 20), (21, 130, 29)]:
            pad = QPointF(right - turn - 28, start)
            path = QPainterPath(pad)
            path.lineTo(right - turn, start)
            path.lineTo(right - turn + 8, finish)
            path.lineTo(right, finish)
            painter.drawPath(path)
            painter.drawEllipse(pad, 3, 3)
            painter.drawEllipse(QPointF(right, finish), 2, 2)
        painter.end()
