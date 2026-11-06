"""Small Explorer icons drawn consistently across desktop platforms."""
from functools import lru_cache
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


@lru_cache(maxsize=16)
def explorer_icon(kind):
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2, 2)
    color = {'folder': '#dfbb67', 'folder-open': '#e9c775', 'json': '#e6c875',
             'rtl': '#79d6e8', 'log': '#9bb8c9'}.get(kind, '#c5d6df')
    painter.setPen(QPen(QColor(color), 1.1, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == 'collapse':
        painter.drawRoundedRect(QRectF(5, 2, 9, 9), 1, 1)
        painter.setBrush(QColor('#213b50'))
        painter.drawRoundedRect(QRectF(2, 5, 9, 9), 1, 1)
        painter.drawLine(QPointF(4.5, 9.5), QPointF(8.5, 9.5))
    elif kind == 'refresh':
        painter.drawArc(QRectF(3, 3, 10, 10), 35 * 16, 285 * 16)
        arrow = QPainterPath(QPointF(12.5, 2.5))
        arrow.lineTo(12.5, 6)
        arrow.lineTo(9, 5)
        painter.drawPath(arrow)
    elif kind.startswith('folder'):
        shape = QPainterPath(QPointF(1.5, 4))
        for x, y in [(6, 4), (7.5, 6), (14.5, 6), (14.5, 13), (1.5, 13), (1.5, 4)]:
            shape.lineTo(x, y)
        painter.drawPath(shape)
        if kind == 'folder-open':
            painter.setBrush(QColor('#213b50'))
            flap = QPainterPath(QPointF(1.5, 13))
            for x, y in [(4, 8), (15, 8), (12.5, 13), (1.5, 13)]:
                flap.lineTo(x, y)
            painter.drawPath(flap)
    elif kind == 'json':
        for sign, center in [(-1, 5.5), (1, 10.5)]:
            path = QPainterPath(QPointF(center, 3))
            path.cubicTo(center + sign * 3, 3, center, 7, center + sign * 3, 8)
            path.cubicTo(center, 9, center + sign * 3, 13, center, 13)
            painter.drawPath(path)
    else:
        painter.drawRoundedRect(QRectF(3, 1.5, 10, 13), 1, 1)
        if kind == 'rtl':
            painter.drawLine(QPointF(5, 9), QPointF(7, 7))
            painter.drawLine(QPointF(7, 7), QPointF(5, 5))
            painter.drawLine(QPointF(9, 10), QPointF(11, 10))
        else:
            for y in (5, 8, 11):
                painter.drawLine(QPointF(5.5, y), QPointF(10.5, y))
    painter.end()
    return QIcon(pixmap)
