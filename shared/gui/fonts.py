"""Optional app-owned font registration; never downloads or installs fonts."""
from pathlib import Path

FONT_DIRECTORY = Path(__file__).parent / 'assets' / 'fonts'


def bundled_font_paths():
    return sorted(path for path in FONT_DIRECTORY.rglob('*')
                  if path.is_file() and path.suffix.lower() in ('.ttf', '.otf'))


def load_bundled_fonts(app):
    """Register once per QApplication, retaining diagnostics for failed assets."""
    from PySide6.QtGui import QFontDatabase
    result = app.property('capstoneBundledFonts')
    if result is None:
        result = {str(path.relative_to(FONT_DIRECTORY)):
                  QFontDatabase.addApplicationFont(str(path)) >= 0
                  for path in bundled_font_paths()}
        app.setProperty('capstoneBundledFonts', result)
    return result
