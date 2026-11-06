"""Native Qt action button with an explicit, consistent icon-to-label gap."""
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QIcon, QPalette, QColor
from PySide6.QtWidgets import QPushButton, QStyle, QStyleOptionButton, QStylePainter


class ActionButton(QPushButton):
    icon_gap = 8

    def __init__(self, text='', parent=None, *, icon_kind=None):
        super().__init__(text, parent)
        if icon_kind is not None:
            from .navigation_icons import navigation_icon
            self.setIcon(navigation_icon(icon_kind))
            self.setIconSize(QSize(18, 18))

    def sizeHint(self):
        hint = super().sizeHint()
        if not self.icon().isNull() and self.text():
            hint += QSize(self.icon_gap, 0)
        return hint

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        icon, text = QIcon(option.icon), option.text
        option.icon = QIcon()
        option.text = ''
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        area = self.style().subElementRect(QStyle.SubElement.SE_PushButtonContents, option, self)
        icon_size = self.iconSize() if not icon.isNull() else QSize(0, 0)
        text_width = self.fontMetrics().horizontalAdvance(text)
        gap = self.icon_gap if text and not icon.isNull() else 0
        width = icon_size.width() + gap + text_width
        left = area.x() + (area.width() - width) // 2
        icon_rect = QRect(left, area.center().y() - icon_size.height() // 2,
                          icon_size.width(), icon_size.height())
        text_rect = QRect(left + icon_size.width() + gap, area.y(), text_width, area.height())
        if self.isDown():
            dx = self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftHorizontal, option, self)
            dy = self.style().pixelMetric(QStyle.PixelMetric.PM_ButtonShiftVertical, option, self)
            icon_rect.translate(dx, dy)
            text_rect.translate(dx, dy)
        if not icon.isNull():
            mode = QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled
            state = QIcon.State.On if self.isChecked() else QIcon.State.Off
            icon.paint(painter, icon_rect, Qt.AlignmentFlag.AlignCenter, mode, state)
        from .theme import theme_colors
        colors = theme_colors()
        foreground = (colors['disabled'] if not self.isEnabled() else
                      colors['field'] if self.objectName() == 'primaryAction' or (
                          self.objectName() == 'historyToggle' and self.isChecked()) else colors['text'])
        palette = QPalette(option.palette)
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(foreground))
        self.style().drawItemText(painter, text_rect, Qt.AlignmentFlag.AlignCenter,
                                 palette, self.isEnabled(), text, QPalette.ColorRole.ButtonText)
