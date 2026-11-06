"""Local diagnostics only: no credential values, model calls, or SSH connections."""
from importlib import metadata
import os
import platform
import shutil
import sys
from .run_reader import REPO_ROOT, DEFAULT_RUNS


def inspect_environment():
    from .platform_support import host_capabilities
    from .fonts import bundled_font_paths
    host = host_capabilities()
    packages = {}
    for distribution, module in [('PySide6', 'PySide6'), ('PyYAML', 'yaml'),
                                 ('requests', 'requests'), ('paramiko', 'paramiko'),
                                 ('langgraph', 'langgraph')]:
        try:
            packages[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            packages[distribution] = 'missing'
    return dict(python=sys.version.split()[0], platform=platform.system(),
                repository=str(REPO_ROOT), history_available=DEFAULT_RUNS.is_dir(),
                packages=packages,
                capabilities=host.as_dict(),
                bundled_fonts=[path.name for path in bundled_font_paths()],
                tools={name: bool(shutil.which(name)) for name in
                       ('bash', 'make', 'g++', 'verilator', 'iverilog', 'ssh')},
                desktop_display_available=(bool(os.getenv('DISPLAY') or os.getenv('WAYLAND_DISPLAY'))
                                           if host.system == 'Linux' else None),
                note='Local inspection only. Remote access, provider credentials, PDK, and EDA tools are not verified.')


def smoke_test():
    """Create, populate, and close the real window without external application opens."""
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from .window import RunWindow
    # No active worker is attached during a diagnostic smoke test.
    from unittest.mock import patch
    with patch('shared.gui.registry.discover_worker', return_value=None), patch(
            'shared.gui.registry.read_record', return_value={}):
        app = QApplication.instance() or QApplication([])
        window = RunWindow()
        window.show()
        app.processEvents()
        result = dict(window_created=True, history_entries=window.history.count(),
                      tabs=window.tabs.count(), display_plugin=app.platformName())
        window.close()
        return result
