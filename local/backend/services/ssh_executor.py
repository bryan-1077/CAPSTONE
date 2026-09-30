"""Compatibility import for backend scripts launched outside the repository root."""

import sys
from pathlib import Path

_repository = str(Path(__file__).resolve().parents[3])
if _repository not in sys.path:
    sys.path.insert(0, _repository)

from shared.remote.ssh_executor import SSHExecutor

__all__ = ["SSHExecutor"]
