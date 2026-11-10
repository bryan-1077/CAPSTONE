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
               colors['field'] if self.kind == 'new-design' else
               colors['gold'] if state == QIcon.State.On else colors['text'])
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
        elif self.kind == 'refresh':
            shape.moveTo(17, 11)
            shape.cubicTo(16.5, 17, 9, 19, 5, 15)
            shape.cubicTo(1, 11, 3, 4, 8, 3)
            shape.cubicTo(11.5, 2, 14.5, 3.5, 16, 6)
            painter.drawPath(shape)
            arrow = QPainterPath(QPointF(12, 6))
            arrow.lineTo(16, 6)
            arrow.lineTo(16, 2)
            painter.drawPath(arrow)
        elif self.kind == 'approved-spec':
            shape.moveTo(4, 2)
            for x, y in [(12, 2), (16, 6), (16, 18), (4, 18), (4, 2)]:
                shape.lineTo(x, y)
            shape.moveTo(12, 2)
            shape.lineTo(12, 6)
            shape.lineTo(16, 6)
            painter.drawPath(shape)
            check = QPainterPath(QPointF(7, 12))
            check.lineTo(9.5, 14.5)
            check.lineTo(13.5, 10)
            painter.drawPath(check)
        elif self.kind == 'run-log':
            painter.drawRoundedRect(2, 3, 16, 14, 1, 1)
            painter.drawLine(QPointF(2, 7), QPointF(18, 7))
            for y, end in [(10, 15), (13, 12)]:
                painter.drawPoint(QPointF(5, y))
                painter.drawLine(QPointF(8, y), QPointF(end, y))
        elif self.kind == 'compare':
            painter.drawRoundedRect(2, 3, 6, 14, 1, 1)
            painter.drawRoundedRect(12, 3, 6, 14, 1, 1)
        elif self.kind == 'external-link':
            shape = QPainterPath(QPointF(9, 4))
            for point in ((3, 4), (3, 17), (16, 17), (16, 11)):
                shape.lineTo(*point)
            painter.drawPath(shape)
            painter.drawLine(QPointF(9, 11), QPointF(18, 2))
            painter.drawLine(QPointF(12, 2), QPointF(18, 2))
            painter.drawLine(QPointF(18, 2), QPointF(18, 8))
        elif self.kind == 'expand':
            for points in (((3, 8), (3, 3), (8, 3)), ((12, 3), (17, 3), (17, 8)),
                           ((3, 12), (3, 17), (8, 17)), ((12, 17), (17, 17), (17, 12))):
                shape = QPainterPath(QPointF(*points[0]))
                for point in points[1:]:
                    shape.lineTo(*point)
                painter.drawPath(shape)
        elif self.kind == 'image-info':
            painter.drawEllipse(2, 2, 16, 16)
            painter.drawPoint(QPointF(10, 6))
            painter.drawLine(QPointF(10, 9), QPointF(10, 14))
        elif self.kind == 'download':
            painter.drawLine(QPointF(10, 2), QPointF(10, 12))
            for point in ((6, 8), (14, 8)):
                painter.drawLine(QPointF(*point), QPointF(10, 12))
            shape = QPainterPath(QPointF(3, 13))
            for point in ((3, 17), (17, 17), (17, 13)):
                shape.lineTo(*point)
            painter.drawPath(shape)
        elif self.kind == 'fit-image':
            painter.drawRoundedRect(2, 3, 16, 14, 1, 1)
            painter.drawRect(6, 6, 8, 8)
        elif self.kind in ('zoom-in', 'zoom-out'):
            painter.drawEllipse(2, 2, 11, 11)
            painter.drawLine(QPointF(12, 12), QPointF(17, 17))
            painter.drawLine(QPointF(5, 7.5), QPointF(10, 7.5))
            if self.kind == 'zoom-in':
                painter.drawLine(QPointF(7.5, 5), QPointF(7.5, 10))
        elif self.kind in ('previous', 'next'):
            middle, end = (6, 12) if self.kind == 'previous' else (14, 8)
            shape = QPainterPath(QPointF(end, 4))
            shape.lineTo(middle, 10)
            shape.lineTo(end, 16)
            painter.drawPath(shape)
        elif self.kind == 'close':
            painter.drawLine(QPointF(5, 5), QPointF(15, 15))
            painter.drawLine(QPointF(5, 15), QPointF(15, 5))
        elif self.kind == 'floorplan':
            painter.drawRect(2, 2, 16, 16)
            painter.drawLine(QPointF(8, 2), QPointF(8, 18))
            painter.drawLine(QPointF(8, 9), QPointF(18, 9))
        elif self.kind == 'placement':
            for x in (3, 11):
                for y in (3, 11):
                    painter.drawRoundedRect(x, y, 6, 6, 0.5, 0.5)
        elif self.kind == 'routing':
            for points in (((2, 4), (8, 4), (8, 12), (18, 12)),
                           ((2, 16), (5, 16), (5, 8), (14, 8), (14, 3), (18, 3))):
                shape = QPainterPath(QPointF(*points[0]))
                for point in points[1:]:
                    shape.lineTo(*point)
                painter.drawPath(shape)
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
