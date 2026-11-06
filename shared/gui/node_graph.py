"""Four orchestrator nodes connected by PCB-style routes."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget, QLabel, QGridLayout, QFrame, QVBoxLayout, QHBoxLayout
from .theme import theme_colors

MAIN_NODES = [('generate', 'RTL Generation'), ('validation', 'Verification'),
              ('backend', 'Physical Design'), ('gdsii-validation', 'GDSII Validation')]


class StatusIcon(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(24, 24)
        self.status = 'pending'
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = {'passed': '#8bd6a5', 'running': '#79d6e8', 'waiting': '#ffc47e',
                  'needs_attention': '#ffc47e', 'failed': '#f29ca6'}
        painter.setPen(QPen(QColor(colors.get(self.status, theme_colors()['muted'])), 1.8,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.status == 'needs_attention':
            triangle = QPainterPath(QPointF(12, 3))
            triangle.lineTo(22, 21)
            triangle.lineTo(2, 21)
            triangle.closeSubpath()
            painter.drawPath(triangle)
            painter.drawLine(QPointF(12, 9), QPointF(12, 14))
            painter.drawPoint(QPointF(12, 18))
        else:
            painter.drawEllipse(QRectF(3, 3, 18, 18))
            if self.status == 'passed':
                path = QPainterPath(QPointF(7, 12))
                path.lineTo(10.5, 15.5)
                path.lineTo(17, 8.5)
                painter.drawPath(path)
            elif self.status == 'failed':
                painter.drawLine(QPointF(8.5, 8.5), QPointF(15.5, 15.5))
                painter.drawLine(QPointF(15.5, 8.5), QPointF(8.5, 15.5))
            elif self.status == 'waiting':
                painter.drawLine(QPointF(12, 7), QPointF(12, 12))
                painter.drawLine(QPointF(12, 12), QPointF(16, 14))
            elif self.status == 'running':
                path = QPainterPath(QPointF(10, 7.5))
                path.lineTo(16, 12)
                path.lineTo(10, 16.5)
                path.closeSubpath()
                painter.drawPath(path)
            elif self.status in ('skipped', 'not_run'):
                painter.drawLine(QPointF(8, 12), QPointF(16, 12))
            else:
                painter.drawPoint(QPointF(12, 12))
        painter.end()


class FlowNode(QFrame):
    clicked = Signal()

    def __init__(self, caption, parent=None, *, number=None):
        super().__init__(parent)
        self.caption = caption
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty('orchestratorNode', True)
        self.setProperty('stageNavigation', True)
        self.setMinimumHeight(88)
        frame_layout = QGridLayout(self)
        frame_layout.setContentsMargins(12, 6, 12, 6)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        frame_layout.addLayout(layout, 0, 0)
        title = QLabel(caption)
        title.setObjectName('flowNodeTitle')
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        title.setMinimumHeight(title.fontMetrics().lineSpacing() * 2)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.number = QLabel(f'{number:02d}' if number is not None else '')
        self.number.setObjectName('flowNodeNumber')
        self.number.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.number.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.number.setVisible(number is not None)
        # The stage number is a corner annotation, not a row that pushes the
        # title/status group below the center of the node.
        frame_layout.addWidget(self.number, 0, 0,
                               Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(title)
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        status_row.addStretch()
        self.icon = StatusIcon()
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        status_row.addWidget(self.icon)
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        layout.addLayout(status_row)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        self.setProperty('keyboardFocus', event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason))
        self.style().unpolish(self)
        self.style().polish(self)
        super().focusInEvent(event)

    def set_status(self, status):
        text = 'Connection error' if status == 'connection_error' else status.replace('_', ' ').capitalize()
        if status == 'connection_error':
            status = 'needs_attention'
        self.status_label.setText(text)
        self.status_label.setProperty('nodeStatus', status)
        self.setProperty('flowState', status)
        self.setAccessibleName(f'{self.caption}: {text}')
        self.icon.status = status
        self.icon.update()
        for widget in (self, self.status_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class NodeGraph(QWidget):
    stage_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.nodes = {}
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setHorizontalSpacing(24)
        for index, (key, caption) in enumerate(MAIN_NODES):
            label = FlowNode(caption, number=index + 1)
            label.setToolTip({
                'generate': 'Orchestrator: generate. Includes RTL generation and configured BIST checks.',
            }.get(key, f'Orchestrator: {key}'))
            self.nodes[key] = label
            label.clicked.connect(lambda stage=key: self.stage_clicked.emit(stage))
            label.setToolTip(label.toolTip() + '\nClick to view stage details.')
            layout.addWidget(label, 0, index)
        for column in range(4):
            layout.setColumnStretch(column, 1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        def route(points, *, conditional=False, color=None):
            ink = QColor(color or theme_colors()['trace'])
            painter.setPen(QPen(ink, 1.4, Qt.PenStyle.DashLine if conditional else Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath(points[0])
            for point in points[1:]:
                path.lineTo(point)
            painter.drawPath(path)
            end, previous = points[-1], points[-2]
            delta = end - previous
            length = max(1, (delta.x() ** 2 + delta.y() ** 2) ** .5)
            unit = QPointF(delta.x() / length, delta.y() / length)
            side = QPointF(-unit.y(), unit.x())
            painter.setPen(QPen(ink, 1.4))
            for direction in (-1, 1):
                painter.drawLine(end, end - unit * 5 + side * direction * 3)

        for index in range(3):
            start = self.nodes[MAIN_NODES[index][0]].geometry()
            finish = self.nodes[MAIN_NODES[index + 1][0]].geometry()
            route([QPointF(start.right() + 2, start.center().y()),
                   QPointF(finish.left() - 3, finish.center().y())])
        painter.end()
