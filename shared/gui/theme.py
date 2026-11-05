"""Application-owned Qt styling independent of the desktop theme."""
from PySide6.QtGui import QColor, QPalette


def apply_theme(app):
    app.setStyleSheet('')
    app.setStyle('Fusion')
    base_style = app.style().objectName()
    palette = QPalette()
    colors = {'Window': '#125234', 'WindowText': '#eef0e6', 'Base': '#0c3021',
              'AlternateBase': '#19643e', 'Text': '#eef0e6', 'Button': '#217447',
              'ButtonText': '#eef0e6', 'Highlight': '#d1ad58', 'HighlightedText': '#102419',
              'ToolTipBase': '#217447', 'ToolTipText': '#eef0e6', 'PlaceholderText': '#b9cdbd'}
    for role, color in colors.items():
        palette.setColor(getattr(QPalette.ColorRole, role), QColor(color))
    app.setPalette(palette)
    app.setStyleSheet('''
        QPushButton { padding: 5px 10px; border: 1px solid #489565; border-radius: 4px; background: #217447; color: #eef0e6; }
        QPushButton:hover { background: #298652; border-color: #d1ad58; }
        QPushButton:pressed, QPushButton:checked { background: #489565; }
        QPushButton:disabled { background: #19643e; color: #a2bba8; border-color: #346b49; }
        QPushButton#primaryAction { background: #d1ad58; color: #102419; border-color: #d1ad58; font-weight: 600; }
        QPushButton#primaryAction:hover { background: #e2c577; }
        QPushButton#primaryAction:pressed { background: #b99747; }
        QPushButton#primaryAction:disabled { background: #3d4430; color: #a3ac94; border-color: #489565; }
        QLineEdit, QPlainTextEdit, QListWidget, QTreeWidget { background: #0c3021; color: #eef0e6; border: 1px solid #489565; border-radius: 3px; }
        QLineEdit:focus, QPlainTextEdit:focus { border-color: #d1ad58; }
        QFrame#flowStatus { background: #19643e; border: 1px solid #489565; border-radius: 6px; }
        QLabel[flowState] { padding: 5px; border-radius: 4px; background: #217447; color: #e0eadc; }
        QLabel[flowState="passed"] { background: #1e6640; color: #9ce2bb; }
        QLabel[flowState="running"] { background: #493e22; color: #f1d58c; }
        QLabel[flowState="waiting"] { background: #493c20; color: #f5d58a; }
        QLabel[flowState="failed"], QLabel[flowState="needs_attention"] { background: #512a30; color: #ffb3ba; }
        QProgressBar { border: 1px solid #489565; border-radius: 3px; background: #0c3021; }
        QProgressBar::chunk { background: #d1ad58; border-radius: 2px; }
    ''')
    return base_style
