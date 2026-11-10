"""Compatibility import for backend execution outside the repository root."""
from services.ssh_executor import SSHExecutor  # Establishes the repository import path.
from shared.remote.layout_images import collect_layout_images

__all__ = ['collect_layout_images']
