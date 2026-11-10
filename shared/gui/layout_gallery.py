"""Side-by-side layout gallery and a separate image viewer."""
from PySide6.QtCore import Qt, QSize, QRect, Signal
from PySide6.QtGui import QIcon, QKeySequence, QShortcut, QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton,
                              QSizePolicy, QDialog, QApplication,
                              QStyleOptionToolButton, QStylePainter, QStyle)
from .image_preview import ImagePreview
from .navigation_icons import navigation_icon

STAGE_DESCRIPTIONS = {
    'Floorplan': 'Chip outline and power grid',
    'Placement': 'Positions of the design’s cells',
    'Routing': 'Connections between cells',
}

STAGE_EXPLANATIONS = {
    'Floorplan': 'The floorplan sets the chip’s outline and reserves space for the design. '
                 'This view shows the available area and power grid before the logic cells are placed.',
    'Placement': 'Placement assigns positions to the cells—the small building blocks that implement the design’s logic. '
                 'This view shows how those cells are arranged before their final connections are routed.',
    'Routing': 'Routing adds the metal paths that connect the cells. '
               'This view shows the routed layout, including the signal connections and power grid.',
}


class GalleryButton(QToolButton):
    """Keep a readable gap between the small outline icon and its label."""
    def sizeHint(self):
        hint = super().sizeHint()
        if not self.icon().isNull() and self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon:
            hint += QSize(6, 0)
        return hint

    def paintEvent(self, event):
        if self.icon().isNull() or self.toolButtonStyle() != Qt.ToolButtonStyle.ToolButtonTextBesideIcon:
            return super().paintEvent(event)
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        option.text = ''
        option.icon = QIcon()
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option)
        width = self.fontMetrics().horizontalAdvance(self.text())
        icon_size = self.iconSize()
        left = (self.width() - width - icon_size.width() - 8) // 2
        self.icon().paint(painter, QRect(left, (self.height() - icon_size.height()) // 2,
                                        icon_size.width(), icon_size.height()),
                          Qt.AlignmentFlag.AlignCenter,
                          QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled,
                          QIcon.State.On if self.isChecked() else QIcon.State.Off)
        from .theme import theme_colors
        colors = theme_colors()
        painter.setPen(QColor(colors['disabled'] if not self.isEnabled() else
                              colors['gold'] if self.isChecked() else colors['text']))
        painter.drawText(QRect(left + icon_size.width() + 8, 0, width, self.height()),
                         Qt.AlignmentFlag.AlignCenter, self.text())


def tool_button(text, callback, *, checkable=False, icon_kind=None):
    button = GalleryButton()
    button.setObjectName('galleryAction')
    button.setText(text)
    button.setAccessibleName(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_kind:
        button.setIcon(navigation_icon(icon_kind))
        button.setIconSize(QSize(16, 16))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    button.setCheckable(checkable)
    button.clicked.connect(callback)
    return button


class LayoutViewer(QDialog):
    def __init__(self, images, index, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.images = list(images)
        self.index = index
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.previous = tool_button('Previous', lambda: self.navigate(-1), icon_kind='previous')
        self.previous.setToolTip('Previous layout view (Left arrow)')
        controls.addWidget(self.previous)
        self.title = QLabel()
        self.title.setObjectName('layoutTitle')
        controls.addWidget(self.title, 1, Qt.AlignmentFlag.AlignCenter)
        self.next = tool_button('Next', lambda: self.navigate(1), icon_kind='next')
        self.next.setToolTip('Next layout view (Right arrow)')
        controls.addWidget(self.next)
        self.close_button = tool_button('Close', self.close, icon_kind='close')
        self.close_button.setToolTip('Close image viewer (Esc)')
        controls.addWidget(self.close_button)
        layout.addLayout(controls)
        self.preview = ImagePreview()
        self.preview.view.wheel_zoom = True
        layout.addWidget(self.preview, 1)
        hint = QLabel('Scroll to zoom · Drag to pan · ← / → switch views · Esc to close')
        hint.setObjectName('layoutHint')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, activated=lambda: self.navigate(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, activated=lambda: self.navigate(1))
        self.resize(1200, 850)
        screen = QApplication.primaryScreen()
        if screen:
            self.resize(self.size().boundedTo(screen.availableGeometry().size()))
        self.display_image()

    def navigate(self, offset):
        self.index = max(0, min(len(self.images) - 1, self.index + offset))
        self.display_image()

    def display_image(self):
        title, path, _ = self.images[self.index]
        self.setWindowTitle(title + ' · Layout Images')
        self.title.setText(f'{title}  ·  {self.index + 1} of {len(self.images)}')
        self.previous.setEnabled(self.index > 0)
        self.next.setEnabled(self.index < len(self.images) - 1)
        self.preview.show_image(path)


class LayoutImageCard(QWidget):
    """One image, with its actions and status kept together."""
    def __init__(self, title, path, record, open_image, download_image):
        super().__init__()
        self.title, self.path, self.record = title, path, record
        self.info_window = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header_layout = QVBoxLayout()
        self.header_layout.setSpacing(3)
        heading = self.heading = QHBoxLayout()
        heading.setSpacing(3)
        stage = QLabel()
        stage.setPixmap(navigation_icon(title.lower()).pixmap(QSize(16, 16)))
        heading.addWidget(stage)
        name = QLabel(title)
        name.setObjectName('layoutTitle')
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        name.setToolTip(STAGE_DESCRIPTIONS.get(title, title))
        heading.addWidget(name, 1)
        self.open_button = tool_button('Open ' + title, open_image, icon_kind='external-link')
        self.open_button.setEnabled(path is not None)
        self.open_button.setToolTip('Open ' + title.lower() + ' in a separate window')
        self.info = tool_button(title + ' image info', self.toggle_info,
                                checkable=True, icon_kind='image-info')
        self.info.setToolTip('Information about the ' + title.lower() + ' image')
        self.download = tool_button('Download ' + title, download_image, icon_kind='download')
        self.download.setEnabled(False)
        self.download.setToolTip('Download the ' + title.lower() + ' image')
        self.actions = QWidget()
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(3)
        for button in (self.open_button, self.info, self.download):
            button.setObjectName('imageCardAction')
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            actions.addWidget(button)
        heading.addWidget(self.actions)
        self.header_layout.addLayout(heading)
        layout.addLayout(self.header_layout)
        self._narrow = False
        self.preview = None
        if path is not None:
            self.preview = ImagePreview(compact=True, tools=False)
            self.preview.view.setMinimumSize(0, 0)
            self.preview.view.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            self.preview.view.setToolTip('Click to open ' + title.lower() + ' in a separate window')
            self.preview.view.setAccessibleName(title + ' layout preview')
            self.preview.show_image(path)
            self.preview.view.clicked.connect(open_image)
            layout.addWidget(self.preview, 1)
        else:
            empty = QLabel('Not downloaded\nUse the download arrow for this image.')
            empty.setObjectName('layoutDetails')
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setWordWrap(True)
            layout.addWidget(empty, 1)
        self.status_text = QLabel()
        self.status_text.setObjectName('layoutHint')
        self.status_text.setTextFormat(Qt.TextFormat.PlainText)
        self.status_text.setWordWrap(True)
        self.status_text.hide()
        layout.addWidget(self.status_text)

    def set_status(self, text):
        self.status_text.setText(text)
        self.status_text.setVisible(bool(text))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        narrow = event.size().width() < 220
        if narrow != self._narrow:
            self._narrow = narrow
            if narrow:
                self.heading.removeWidget(self.actions)
                self.header_layout.addWidget(self.actions, alignment=Qt.AlignmentFlag.AlignRight)
            else:
                self.header_layout.removeWidget(self.actions)
                self.heading.addWidget(self.actions)

    def toggle_info(self, checked):
        if not checked:
            if self.info_window is not None:
                self.info_window.close()
            return
        dialog = self.info_window = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(self.title + ' image information')
        layout = QVBoxLayout(dialog)
        heading = QLabel(self.title + ' · ' + STAGE_DESCRIPTIONS.get(self.title, 'Layout image'))
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setObjectName('layoutTitle')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        explanation = QLabel(STAGE_EXPLANATIONS.get(self.title, 'A captured view of the physical layout.'))
        explanation.setTextFormat(Qt.TextFormat.PlainText)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        detail_heading = QLabel('Image details')
        detail_heading.setObjectName('fieldHeading')
        layout.addWidget(detail_heading)
        details = ['Saved image: ' + str(self.path) if self.path else 'Not downloaded to this machine.']
        if self.record.get('source_build'):
            details.append('Captured from: ' + str(self.record['source_build']))
        if self.record.get('origin') == 'ancestor_build':
            details.append('Reused from an earlier build in this implementation’s lineage.')
        if self.record.get('reason'):
            details.append(str(self.record['reason']))
        body = QLabel('\n\n'.join(details))
        body.setObjectName('layoutDetails')
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)
        close = tool_button('Close', dialog.close, icon_kind='close')
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.finished.connect(self.info_closed)
        dialog.resize(600, 360)
        dialog.show()

    def info_closed(self):
        self.info_window = None
        self.info.setChecked(False)


class LayoutGallery(QWidget):
    """Side-by-side image cards with no shared image-action toolbar."""
    download_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.images = []
        self.selected_index = -1
        self.viewer = None
        self.comparisons = []
        self.cards = {}
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(0)
        self.comparison = QWidget()
        self.comparison_layout = QHBoxLayout(self.comparison)
        self.comparison_layout.setContentsMargins(0, 0, 0, 0)
        self.comparison_layout.setSpacing(8)
        self.content.addWidget(self.comparison, 1)

    @property
    def selected_title(self):
        return self.images[self.selected_index][0] if 0 <= self.selected_index < len(self.images) else ''

    def set_images(self, images, *, reset=False, records=None):
        previous = self.selected_title if not reset else 'Routing'
        self.images = list(images)
        for card in self.cards.values():
            if card.info_window:
                card.info_window.close()
        while self.comparison_layout.count():
            widget = self.comparison_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self.cards = {}
        self.comparisons = []
        available = {title: (index, path, record) for index, (title, path, record) in enumerate(images)}
        for title in STAGE_DESCRIPTIONS:
            index, path, record = available.get(title, (-1, None, (records or {}).get(title, {})))
            card = LayoutImageCard(title, path, record,
                lambda checked=False, i=index: self.open_large(i) if i >= 0 else None,
                lambda checked=False, stage=title.lower(): self.download_requested.emit(stage))
            self.cards[title] = card
            self.comparison_layout.addWidget(card, 1)
            if card.preview is not None:
                self.comparisons.append(card.preview)
        self.selected_index = next((i for i, item in enumerate(images) if item[0] == previous), len(images) - 1)

    def open_large(self, index=None):
        if not self.images:
            return
        if self.viewer is not None:
            self.viewer.close()
        index = self.selected_index if index is None or isinstance(index, bool) else index
        self.selected_index = index
        self.viewer = LayoutViewer(self.images, index, self)
        viewer = self.viewer
        viewer.finished.connect(lambda: self._viewer_closed(viewer))
        viewer.show()

    def _viewer_closed(self, viewer):
        if self.viewer is viewer:
            self.viewer = None
