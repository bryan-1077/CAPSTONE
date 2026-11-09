"""Application-owned Qt styling independent of the desktop theme."""
import json
from pathlib import Path
from string import Template
from PySide6.QtGui import QColor, QPalette, QFont, QFontDatabase

SETTINGS = Path(__file__).with_name('settings.local.json')
THEMES = {
    'green': dict(board='#19532f', panel='#153f28', field='#112219', button='#245235',
                  border='#50794d', hover='#306640', trace='#7b9e63', text='#f1f4e7',
                  muted='#cbdcc0', disabled='#a3baa0', disabled_border='#476d45',
                  die='#142d28', package='#202923', gold='#dfbb67', warning='#ffc47e'),
    'blue': dict(board='#142638', panel='#1b3144', field='#10202e', button='#284358',
                 border='#456074', hover='#304d62', trace='#7892a7', text='#f0f4f7',
                 muted='#b0c4d0', disabled='#9bafbd', disabled_border='#425e72',
                 die='#18374b', package='#202932', gold='#dfbb67', warning='#ffc47e'),
}


def load_theme():
    try:
        value = json.loads(SETTINGS.read_text())
        name = value.get('theme') if isinstance(value, dict) else None
        return name if isinstance(name, str) and name in THEMES else 'blue'
    except (OSError, ValueError):
        return 'blue'


def save_theme(name):
    if name not in THEMES:
        raise ValueError('Unknown theme')
    from .registry import read_record, atomic_write
    settings = read_record(SETTINGS)
    settings['theme'] = name
    atomic_write(SETTINGS, settings)


def theme_colors():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    name = app.property('capstoneTheme') if app else None
    return THEMES.get(name, THEMES['blue'])


def apply_theme(app, name=None):
    from .fonts import load_bundled_fonts
    load_bundled_fonts(app)
    name = name or app.property('capstoneTheme') or load_theme()
    if name not in THEMES:
        raise ValueError('Unknown theme')
    available = set(QFontDatabase.families())
    ui_font = next((family for family in ('Red Hat Text', 'Noto Sans', 'Segoe UI', 'Helvetica Neue', 'DejaVu Sans') if family in available),
                   QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family())
    mono_font = next((family for family in ('JetBrains Mono', 'Noto Sans Mono', 'Cascadia Mono', 'Menlo', 'DejaVu Sans Mono') if family in available),
                     QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family())
    tokens = dict(THEMES[name], ui_font=ui_font, mono_font=mono_font)
    app.setProperty('capstoneTheme', name)
    app.setStyleSheet('')
    app.setStyle('Fusion')
    font = QFont(ui_font)
    font.setPixelSize(14)
    font.setWeight(QFont.Weight.Normal)
    font.setItalic(False)
    app.setFont(font)
    base_style = app.style().objectName()
    palette = QPalette()
    colors = {'Window': '$board', 'WindowText': '$text', 'Base': '$field',
              'AlternateBase': '$panel', 'Text': '$text', 'Button': '$button',
              'ButtonText': '$text', 'Highlight': '$gold', 'HighlightedText': '$field',
              'ToolTipBase': '$button', 'ToolTipText': '$text', 'PlaceholderText': '$muted'}
    for role, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(Template(color).substitute(tokens)))
    app.setPalette(palette)
    stylesheet = Template('''
        QPushButton { padding: 7px 12px; border: 1px solid $border; border-radius: 2px; background: $button; color: $text; }
        QPushButton:hover { background: $hover; border-color: $gold; }
        QPushButton:pressed, QPushButton:checked { background: $border; }
        QPushButton#historyToggle { min-width: 64px; padding: 7px 16px; font-weight: 600; }
        QPushButton#historyToggle[headerAction="true"], QPushButton#primaryAction[headerAction="true"] { padding: 8px 14px; min-height: 20px; }
        QPushButton#historyToggle:checked { background: #7892a7; color: $field; border-color: #a9c0d0; }
        QPushButton:disabled { background: $panel; color: $disabled; border-color: $disabled_border; }
        QPushButton#primaryAction { background: $gold; color: $field; border-color: $gold; font-weight: 700; }
        QPushButton[runControl="true"] { font-size: 16px; font-weight: 700; min-width: 160px; min-height: 48px; padding: 10px 16px; }
        QPushButton#primaryAction:hover { background: #e2c577; }
        QPushButton#primaryAction:pressed { background: #b99747; }
        QPushButton#primaryAction:disabled { background: $panel; color: $disabled; border-color: $border; }
        QPushButton#approvalAction { background: $gold; color: $field; border: 1px solid $gold; font-weight: 700; padding: 8px 12px; }
        QPushButton#approvalAction:hover { background: #e5c779; }
        QPushButton#approvalAction:pressed { background: #b99747; }
        QPushButton#approvalAction:disabled { background: #344957; color: #a2b4bf; border-color: #617782; }
        QPushButton#dangerAction { background: #321d22; color: #ffc4cb; border-color: #d87987; }
        QPushButton#dangerAction:hover { background: #502830; }
        QPushButton#dangerAction:disabled { background: #403438; color: #ad979c; border-color: #665157; }
        QFrame#decisionPanel { background: $panel; border: 1px solid $border; border-left: 4px solid $warning; border-radius: 2px; padding: 8px; }
        QLabel#decisionHeading { color: $warning; font-size: 20px; font-weight: 600; padding: 8px 0; }
        QLabel[activityState="waiting"] { color: $warning; }
        QLabel[activityState="running"] { color: #79d6e8; }
        QLabel[activityState="passed"] { color: #8bd6a5; }
        QLabel[activityState="failed"], QLabel[activityState="needs_attention"], QLabel[activityState="interrupted"] { color: #f29ca6; }
        QFrame#runLogHeader { background: $button; border: 1px solid $border; border-bottom: none; border-top-left-radius: 2px; border-top-right-radius: 2px; }
        QLabel#runLogTitle { color: $text; font-size: 14px; font-weight: 600; }
        QLabel#runLogStatus { font-size: 12px; color: $muted; }
        QLabel#runLogStatus[activityState="running"] { color: #79d6e8; }
        QLabel#runLogStatus[activityState="waiting"] { color: $warning; }
        QPushButton#runLogLatest { padding: 3px 7px; font-size: 12px; background: transparent; }
        QPushButton#runLogLatest:hover { background: $hover; }
        QPlainTextEdit#runLogOutput { border-top-left-radius: 0; border-top-right-radius: 0; }
        QLineEdit, QPlainTextEdit, QListWidget, QTreeWidget { background: $field; color: $text; border: 1px solid $border; border-radius: 2px; }
        QTreeWidget#artifactBrowser { selection-background-color: $hover; selection-color: $text; }
        QTreeWidget#artifactBrowser::item:selected { background: $hover; color: $text; }
        QTreeWidget#artifactBrowser::item:hover:!selected { background: $button; color: $text; }
        QTreeWidget#artifactBrowser { border-radius: 0; outline: none; }
        QTreeWidget#artifactBrowser::item { height: 26px; padding: 0 4px; border: none; }
        QFrame#explorerHeader { background: $button; border: 1px solid $border; }
        QLabel#explorerHeading { color: $muted; font-size: 11px; font-weight: 600; letter-spacing: 1px; }
        QToolButton#explorerAction { background: transparent; border: none; padding: 4px; }
        QToolButton#explorerAction:hover { background: $hover; }
        QToolButton#explorerAction:focus { border: 1px solid #79d6e8; }
        QLabel#fileBreadcrumb { background: $field; color: $muted; padding: 7px 10px; font-size: 12px; }
        QLineEdit:focus, QPlainTextEdit:focus { border-color: $gold; }
        QPlainTextEdit[readOnly="true"]:focus { border-color: $border; }
        QFrame#flowStatus { background: $panel; border: 1px solid $border; border-radius: 2px; }
        QFrame[orchestratorNode="true"] { background: $field; border: 1px solid $border; border-radius: 2px; }
        QFrame[orchestratorNode="true"][flowState="passed"] { background: $field; border-color: #365b50; }
        QFrame[orchestratorNode="true"][flowState="running"] { background: $button; border: 1px solid #79d6e8; }
        QFrame[orchestratorNode="true"][flowState="waiting"], QFrame[orchestratorNode="true"][flowState="needs_attention"] { background: #3c3026; border-color: $warning; }
        QFrame[orchestratorNode="true"][flowState="failed"] { background: #321d22; border-color: #75434c; }
        QFrame[orchestratorNode="true"][stageNavigation="true"]:hover { border-color: $gold; }
        QFrame[orchestratorNode="true"][keyboardFocus="true"]:focus { border: 1px dashed #79d6e8; }
        QFrame[orchestratorNode="true"][inspecting="true"] { border: 2px solid $gold; }
        QLabel#flowNodeTitle { color: $text; font-size: 14px; font-weight: 600; }
        QLabel[nodeStatus] { color: $muted; font-size: 13px; font-weight: 500; }
        QLabel[nodeStatus="passed"] { color: #8bd6a5; }
        QLabel[nodeStatus="running"] { color: #79d6e8; }
        QLabel[nodeStatus="waiting"], QLabel[nodeStatus="needs_attention"] { color: $warning; }
        QLabel[nodeStatus="failed"] { color: #f29ca6; }
        QProgressBar { border: 1px solid $border; border-radius: 2px; background: $field; }
        QProgressBar::chunk { background: #79d6e8; border-radius: 2px; }

        QWidget { font-family: "$ui_font"; font-size: 14px; font-style: normal; }
        QLabel#brand { font-size: 24px; font-weight: 700; color: $text; letter-spacing: 0.5px; }
        QLabel#consoleCaption { color: $gold; font-size: 11px; font-weight: 600; letter-spacing: 1px; }
        QLabel#flowNodeNumber { color: $gold; font-family: "$mono_font"; font-size: 11px; font-weight: 600; }
        QLabel#selectedRunName { color: $muted; font-family: "$mono_font"; font-size: 12px; }
        QLabel#selectedRunDetails { color: $text; font-size: 19px; font-weight: 600; }
        QLabel#brandCaption { font-size: 12px; color: $muted; letter-spacing: 1px; }
        QLabel#flowHeading { font-size: 18px; font-weight: 600; }
        QLabel#flowActivity { font-size: 18px; font-weight: 600; }
        QLabel#elapsed { font-family: "$mono_font"; font-size: 12px; color: $muted; }
        QLabel#interpretationNotes { padding: 10px; color: $muted; background: $field; border: 1px solid $border; border-radius: 2px; }
        QPushButton:focus { border-color: #79d6e8; }
        QLineEdit, QComboBox { padding: 6px 8px; min-height: 18px; }
        QComboBox { background: $field; color: $text; border: 1px solid $border; border-radius: 2px; }
        QComboBox:focus { border-color: #79d6e8; }
        QComboBox QAbstractItemView { background: $panel; color: $text; selection-background-color: $button; selection-color: #79d6e8; }
        QGroupBox { background: $panel; border: 1px solid $border; border-radius: 2px; margin-top: 13px; padding: 12px 8px 8px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: $muted; }
        QCheckBox { spacing: 8px; padding: 3px 0; }
        QCheckBox::indicator { width: 15px; height: 15px; }
        QScrollArea { border: none; }
        QScrollBar:vertical { background: $board; width: 10px; margin: 0; }
        QScrollBar::handle:vertical { background: $border; min-height: 30px; border-radius: 2px; }
        QScrollBar::handle:vertical:hover { background: $trace; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        QTabWidget::pane { background: $panel; border: none; border-top: 1px solid $border; }
        QTabBar { background: transparent; border: none; qproperty-drawBase: 0; }
        QTabBar::tab { background: transparent; color: $muted; font-size: 14px; font-weight: 600; padding: 9px 14px; border: none; border-bottom: 3px solid $border; margin-right: 4px; }
        QTabBar::tab:hover { background: $button; color: $text; }
        QTabBar::tab:selected { background: transparent; color: $gold; border-bottom-color: $gold; }
        QWidget#designPage, QWidget#designBody { background: $panel; }
        QFrame#historyHeading { background: $panel; border: 1px solid $border; border-bottom: 3px solid $border; }
        QLabel#historyHeadingLabel { color: $text; font-weight: 700; }
        QToolButton#historyOptions { background: transparent; border: 1px solid transparent; border-radius: 2px; }
        QToolButton#historyOptions:hover, QToolButton#historyOptions:pressed { background: $hover; border-color: $trace; }
        QToolButton#historyOptions::menu-indicator { image: none; }
        QMenu { background: $panel; color: $text; border: 1px solid $border; padding: 5px; }
        QMenu::item { padding: 8px 22px 8px 12px; }
        QMenu::item:selected { background: $trace; color: $field; }
        QMenu::item:disabled { color: $disabled; }
        QMenu::separator { height: 1px; background: $border; margin: 5px 8px; }
        QSplitter::handle { background: $board; }
        QSplitter::handle:hover { background: $gold; }
        QHeaderView::section { background: $panel; color: $muted; padding: 8px; border: none; border-bottom: 1px solid $border; font-size: 12px; font-weight: 600; }
        QStatusBar { color: $muted; border-top: 1px solid $border; }
        QStatusBar::item { border: none; }
        QToolTip { background: $button; color: $text; border: 1px solid $trace; padding: 5px; }
        QListWidget#runHistory { background: $panel; padding: 5px; border: 1px solid $border; border-radius: 0; }
        QListWidget#runHistory::item { border: none; }
        QFrame#historyCard { background: $panel; border: 1px solid $border; border-radius: 2px; }
        QFrame#historyCard[selected="true"] { background: $hover; border: 1px solid $gold; border-left: 3px solid $gold; }
        QFrame#sectionCard { background: $panel; border: 1px solid $border; border-radius: 2px; }
        QFrame#metricCard { background: $panel; border: 1px solid $border; border-radius: 2px; }
        QFrame#statusCell { background: $field; border: none; }
        QFrame#sectionCard[emphasis="attention"] { border-left: 3px solid $warning; }
        QLabel#metricHeading { color: $gold; font-size: 11px; font-weight: 600; letter-spacing: 0.5px; }
        QLabel#metricValue { color: $text; font-size: 24px; font-weight: 700; }
        QLabel#metricDetail { color: $muted; font-size: 12px; }
        QLabel#sectionTitle { font-size: 18px; font-weight: 600; color: $text; }
        QLabel#sectionDescription { font-size: 14px; color: $muted; }
        QLabel#fieldHeading { font-weight: 700; color: $text; }
        QPlainTextEdit[readableText="true"] { font-size: 14px; padding: 10px; }
        QPlainTextEdit[technicalText="true"] { font-family: "$mono_font"; font-size: 13px; padding: 10px; }
        QTreeWidget::item { padding: 8px 8px; border-bottom: 1px solid $border; }
        QListWidget::item { padding: 7px; }
        QListWidget#runHistory::item { padding: 0; }
        QLabel#historyName { color: $text; font-family: "$mono_font"; font-size: 12px; font-weight: 600; }
        QLabel#historyDescription { color: $text; font-size: 13px; }
        QLabel#historyDate { color: $muted; font-size: 11px; }
        QLabel#historyBadge { background: #30353b; color: #d5dbe0; padding: 3px 7px; border-radius: 2px; font-size: 11px; font-weight: 600; }
        QLabel#historyBadge[historyState="complete"], QLabel#historyBadge[historyState="passed"] { background: #204d35; color: #b4f1cb; }
        QLabel#historyBadge[historyState="running"] { background: #194657; color: #9eeafa; }
        QLabel#historyBadge[historyState="needs_attention"], QLabel#historyBadge[historyState="cancelling"] { background: #624126; color: #ffd7a0; }
        QLabel#historyBadge[historyState="connection_error"] { background: #584423; color: #ffda8b; }
        QLabel#selectedRunLabel { color: $gold; font-size: 11px; font-weight: 700; padding: 2px 4px; }
        QLabel#historyBadge[historyState="failed"], QLabel#historyBadge[historyState="interrupted"], QLabel#historyBadge[historyState="unreadable"] { background: #512a30; color: #ffc4cb; }
    ''').substitute(tokens)
    app.setStyleSheet(stylesheet)
    for widget in app.allWidgets():
        widget.update()
    return base_style
