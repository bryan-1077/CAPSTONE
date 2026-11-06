"""Vector chip animation using the forge indicator's worker-liveness clock."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen

from .forge import ForgeIndicator
from .theme import theme_colors


class ChipIndicator(ForgeIndicator):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName('Chip with electrical sparks work indicator')

    def paintEvent(self, event):
        elapsed = self.clock.elapsed() if self.running else 0
        pulse = (1 + math.sin(elapsed / 1100 * math.tau)) / 2 if self.running else 0
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        # A restrained halo and metallic pins surround the dark package.
        for margin in (7, 4, 1):
            glow = QColor('#50cfff')
            glow.setAlpha(int((5 + pulse * 6) if self.running else 3))
            painter.setBrush(glow)
            painter.drawRoundedRect(QRectF(34 - margin, 18 - margin,
                                          52 + 2 * margin, 52 + 2 * margin), 9, 9)
        metal = QLinearGradient(0, 14, 0, 76)
        metal.setColorAt(0, QColor('#f1dfab'))
        metal.setColorAt(.5, QColor('#b99a54'))
        metal.setColorAt(1, QColor('#ddc185'))
        painter.setBrush(metal)
        for offset in range(6):
            position = 23 + offset * 7
            painter.drawRoundedRect(QRectF(26, position, 10, 3), 1, 1)
            painter.drawRoundedRect(QRectF(84, position, 10, 3), 1, 1)
            painter.drawRoundedRect(QRectF(position + 16, 10, 3, 10), 1, 1)
            painter.drawRoundedRect(QRectF(position + 16, 68, 3, 10), 1, 1)

        body = QLinearGradient(34, 18, 86, 70)
        body.setColorAt(0, QColor('#35465b'))
        body.setColorAt(1, QColor('#131e2e'))
        painter.setBrush(body)
        painter.setPen(QPen(QColor(theme_colors()['gold']), 1.2))
        painter.drawRoundedRect(QRectF(34, 18, 52, 52), 2, 2)
        painter.setBrush(QColor('#1c3045'))
        painter.setPen(QPen(QColor('#58bfe9'), 1))
        painter.drawRoundedRect(QRectF(46, 30, 28, 28), 3, 3)

        # Small traces and a pulsing die keep the silhouette readable at this size.
        painter.setPen(QPen(QColor('#527e9b'), 1))
        for offset in (0, 7, 14):
            for start, end in (
                ((39, 37 + offset), (46, 37 + offset)),
                ((74, 37 + offset), (81, 37 + offset)),
                ((53 + offset, 23), (53 + offset, 30)),
                ((53 + offset, 58), (53 + offset, 65)),
            ):
                painter.drawLine(QPointF(*start), QPointF(*end))
        core = QColor('#66d9ff')
        core.setAlpha(int(65 + pulse * 150) if self.running else 65)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawRoundedRect(QRectF(54, 38, 12, 12), 2, 2)
        painter.setBrush(QColor('#b3c8d9'))
        painter.drawEllipse(QPointF(40, 24), 1.5, 1.5)

        # Stagger short arcs around the pins, leaving quiet gaps between bursts.
        if self.running:
            burst = elapsed // 360
            phase = (elapsed % 360) / 360
            arcs = (
                [(29, 25), (20, 21), (23, 30), (15, 34), (28, 39)],
                [(56, 13), (51, 5), (62, 8), (68, 3), (76, 13)],
                [(91, 39), (103, 34), (99, 44), (108, 49), (92, 53)],
                [(45, 75), (50, 83), (58, 78), (65, 85), (73, 75)],
            )
            if phase < .72:
                path = QPainterPath(QPointF(*arcs[burst % len(arcs)][0]))
                for point in arcs[burst % len(arcs)][1:]:
                    path.lineTo(QPointF(*point))
                for width, color, opacity in ((6, '#27a9ff', .20),
                                               (2, '#65dcff', .90),
                                               (.8, '#effcff', 1)):
                    ink = QColor(color)
                    ink.setAlphaF(opacity * (1 - phase / .72))
                    painter.setPen(QPen(ink, width, Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
        painter.end()
