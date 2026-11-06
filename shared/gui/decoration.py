"""Lightweight PCB trace ornament drawn with Qt vectors."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QIcon, QPixmap
from PySide6.QtWidgets import QWidget
from .theme import theme_colors


def options_icon():
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme_colors()['text']))
    for y in (4, 10, 16):
        painter.drawEllipse(QPointF(10, y), 1.7, 1.7)
    painter.end()
    return QIcon(pixmap)


def password_eye_icon(revealed=False):
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(theme_colors()['text']), 1.5,
                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    eye = QPainterPath(QPointF(2, 10))
    eye.cubicTo(6, 3, 14, 3, 18, 10)
    eye.cubicTo(14, 17, 6, 17, 2, 10)
    painter.drawPath(eye)
    painter.drawEllipse(QPointF(10, 10), 2.3, 2.3)
    if revealed:
        painter.drawLine(QPointF(3, 3), QPointF(17, 17))
    painter.end()
    return QIcon(pixmap)


class TraceBand(QWidget):
    """Routed buses and plated vias used as quiet section dividers."""
    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.setFixedHeight(16 if compact else 20)
        self.setMinimumWidth(80)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = theme_colors()
        ink = QColor(colors['trace'])
        ink.setAlpha(145 if self.compact else 180)
        painter.setPen(QPen(ink, 1, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.compact:
            for index in range(2):
                y = 3 + index * 5
                bend = self.width() * .36 + index * 10
                path = QPainterPath(QPointF(3, y))
                path.lineTo(bend, y)
                path.lineTo(bend + 4, y + 4)
                path.lineTo(self.width() - 5 - index * 14, y + 4)
                painter.drawPath(path)
            painter.setPen(QPen(QColor(colors['gold']), 1.2))
            painter.drawEllipse(QPointF(3, 3), 2, 2)
            painter.drawEllipse(QPointF(self.width() - 5, 7), 2, 2)
        else:
            # Mirrored buses run toward a short row of exposed contact pads.
            # All geometry stays inside this dedicated decoration strip.
            for mirror in (False, True):
                painter.save()
                if mirror:
                    painter.translate(self.width(), 0)
                    painter.scale(-1, 1)
                for index in range(3):
                    y = 3 + index * 5
                    bend = self.width() * .24 + index * 10
                    end = self.width() * .43 - index * 14
                    painter.setPen(QPen(ink, 1.1))
                    path = QPainterPath(QPointF(3, y))
                    path.lineTo(bend, y)
                    path.lineTo(bend + 4, y + 4)
                    path.lineTo(end, y + 4)
                    painter.drawPath(path)
                    painter.setPen(QPen(QColor(colors['gold']), 1))
                    painter.drawEllipse(QPointF(end, y + 4), 1.8, 1.8)
                painter.restore()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(colors['gold']))
            for index in range(6):
                painter.drawRect(QRectF(self.width() / 2 - 28 + index * 10, 6, 3, 9))
        painter.end()


class BoardSurface(QWidget):
    """Trace routing confined to the workspace's otherwise empty outer margins."""
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme_colors()['board']))
        painter.fillRect(QRectF(0, 0, self.width(), 5), QColor(theme_colors()['gold']))
        ink = QColor(theme_colors()['trace'])
        ink.setAlpha(115)
        painter.setPen(QPen(ink, 1.1, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for mirror in (False, True):
            painter.save()
            if mirror:
                painter.translate(self.width(), 0)
                painter.scale(-1, 1)
            for index in range(3):
                x = 5 + index * 6
                top = 78 + index * 18
                bottom = max(top + 30, self.height() - 38 - index * 16)
                path = QPainterPath(QPointF(x, top))
                path.lineTo(x, top + 48)
                path.lineTo(x + 4, top + 52)
                path.lineTo(x + 4, bottom - 12)
                path.lineTo(x, bottom - 8)
                path.lineTo(x, bottom)
                painter.drawPath(path)
                painter.drawEllipse(QPointF(x, top), 2, 2)
                painter.drawEllipse(QPointF(x, bottom), 2, 2)
                # A staggered plated via and short branch distinguish these
                # routes from a simple border without entering the content.
                junction = top + (bottom - top) * (.34 + index * .13)
                painter.save()
                copper = QColor(theme_colors()['gold'])
                copper.setAlpha(175)
                painter.setPen(QPen(copper, 1))
                painter.drawLine(QPointF(x + 4, junction), QPointF(x, junction + 4))
                painter.drawEllipse(QPointF(x, junction + 4), 2.1, 2.1)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(copper)
                painter.drawRect(QRectF(x - 1, bottom + 5, 2, 6))
                painter.restore()
            painter.restore()
        painter.end()
