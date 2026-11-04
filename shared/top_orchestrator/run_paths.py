"""Readable names for automatically created orchestrator runs."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def run_name(target_mhz: float | None) -> str:
    frequency = f"{target_mhz:g}MHz" if target_mhz is not None else "unknown_frequency"
    return f"internal_{frequency}_{datetime.now(ZoneInfo('America/Chicago')):%Y%m%d_%H%M%S_%f}"


def frequency_run_dir(directory: Path, target_mhz: float | None) -> Path:
    """Keep the original timestamp when selection resolves an unknown target."""
    if target_mhz is None or not directory.name.startswith("internal_unknown_frequency_"):
        return directory
    return directory.with_name(directory.name.replace(
        "internal_unknown_frequency_", f"internal_{target_mhz:g}MHz_", 1))
