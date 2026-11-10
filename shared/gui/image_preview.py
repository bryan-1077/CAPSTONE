"""Local raster previews with fit, zoom, and bounded image decoding."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                              QGraphicsView, QGraphicsScene, QLabel)

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.bmp', '.webp'}


class ImageCanvas(QGraphicsView):
    double_clicked = Signal()
    clicked = Signal()
    zoom_requested = Signal(float)
    wheel_zoom = False

    def wheelEvent(self, event):
        if self.wheel_zoom and event.angleDelta().y():
            self.zoom_requested.emit(1.25 if event.angleDelta().y() > 0 else 0.8)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        self._press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.MouseButton.LeftButton and
                (event.position().toPoint() - getattr(self, '_press_position', event.position().toPoint())).manhattanLength() < 5):
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class ImagePreview(QWidget):
    def __init__(self, parent=None, *, compact=False, tools=True):
        super().__init__(parent)
        self.fitting = True
        self.compact = compact
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        from .navigation_icons import navigation_icon
        for title, callback, icon, hint in [
                ('Fit', self.fit_image, 'fit-image', 'Fit the entire image'),
                ('', lambda: self.zoom(0.8), 'zoom-out', 'Zoom out'),
                ('', lambda: self.zoom(1.25), 'zoom-in', 'Zoom in'),
                ('100%', self.actual_size, None, 'Show the image at its original size')]:
            button = QPushButton(title)
            if icon:
                button.setIcon(navigation_icon(icon))
            button.setToolTip(hint)
            button.setAccessibleName(hint)
            button.clicked.connect(callback)
            controls.addWidget(button)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        if compact:
            controls.addStretch()
            self.message.hide()
        else:
            controls.addWidget(self.message, 1)
        layout.addLayout(controls)
        if compact:
            layout.addWidget(self.message)
        if not tools:
            for index in range(controls.count()):
                widget = controls.itemAt(index).widget()
                if widget:
                    widget.hide()
        self.view = ImageCanvas()
        self.view.setObjectName('layoutCanvas')
        self.view.zoom_requested.connect(self.zoom)
        self.view.setMinimumSize(80, 70)
        self.scene = QGraphicsScene(self.view)
        self.view.setScene(self.scene)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        layout.addWidget(self.view, 1)

    def clear(self):
        self.scene.clear()
        self.message.clear()
        self.message.setVisible(not self.compact)
        self.view.resetTransform()
        self.view.setSceneRect(0, 0, 0, 0)

    def show_image(self, path):
        self.clear()
        reader = QImageReader(str(path))
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 32_000_000:
            self.message.show()
            self.message.setText('Image is unreadable or exceeds the 32 megapixel preview limit. Use Open Externally.')
            return False
        decoded = reader.read()
        if decoded.isNull():
            self.message.show()
            self.message.setText('Image could not be decoded. Use Open Externally.')
            return False
        self.scene.addPixmap(QPixmap.fromImage(decoded))
        self.scene.setSceneRect(self.scene.itemsBoundingRect())
        self.message.setText(f'{size.width()} × {size.height()} · Drag to pan')
        self.fit_image()
        return True

    def fit_image(self):
        self.fitting = True
        if self.scene.items():
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor):
        self.fitting = False
        scale = self.view.transform().m11() * factor
        if 0.01 <= scale <= 32:
            self.view.scale(factor, factor)

    def actual_size(self):
        self.fitting = False
        self.view.resetTransform()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fitting:
            self.fit_image()

    def showEvent(self, event):
        super().showEvent(event)
        if self.fitting:
            self.fit_image()
