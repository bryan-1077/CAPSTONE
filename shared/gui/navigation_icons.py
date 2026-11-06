"""Resolution-independent outline icons for the main navigation actions."""
from PySide6.QtCore import QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap
from .theme import theme_colors


class NavigationIcon(QIconEngine):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    def clone(self):
        return NavigationIcon(self.kind)

    def paint(self, painter, rect, mode, state):
        colors = theme_colors()
        ink = (colors['disabled'] if mode == QIcon.Mode.Disabled else
               colors['field'] if self.kind == 'new-design' or state == QIcon.State.On else colors['text'])
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.translate(rect.x(), rect.y())
        painter.scale(rect.width() / 20, rect.height() / 20)
        painter.setPen(QPen(QColor(ink), 1.4, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        shape = QPainterPath()
        if self.kind == 'new-design':
            shape.moveTo(11, 17)
            for x, y in [(3, 17), (3, 2), (11, 2), (15, 6), (15, 9)]:
                shape.lineTo(x, y)
            shape.moveTo(11, 2)
            shape.lineTo(11, 6)
            shape.lineTo(15, 6)
            painter.drawPath(shape)
            painter.drawLine(QPointF(15, 12), QPointF(15, 18))
            painter.drawLine(QPointF(12, 15), QPointF(18, 15))
        else:
            shape.moveTo(2, 5)
            for x, y in [(8, 5), (10, 7), (18, 7), (18, 17), (2, 17), (2, 5)]:
                shape.lineTo(x, y)
            painter.drawPath(shape)
            for y in (10, 12.5, 15):
                painter.drawLine(QPointF(6, y), QPointF(14, y))
        painter.restore()

    def pixmap(self, size, mode, state):
        pixmap = QPixmap(size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap

    def scaledPixmap(self, size, mode, state, scale):
        physical = QSize(round(size.width() * scale), round(size.height() * scale))
        pixmap = self.pixmap(physical, mode, state)
        pixmap.setDevicePixelRatio(scale)
        return pixmap


def navigation_icon(kind):
    return QIcon(NavigationIcon(kind))
