"""Small optional GUI layout preferences; contains no run or dialog state."""
import json
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / '.runtime/ui_preferences.json'


def load_preferences(path):
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw) if len(raw) <= 16384 else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def sizes(value):
    return value if (isinstance(value, list) and len(value) == 2 and
                     all(type(n) is int and 0 < n < 20000 for n in value)) else None


def save_preferences(path, value):
    # A read-only installation must still be fully usable.
    try:
        from .registry import atomic_write
        atomic_write(Path(path), value)
    except OSError:
        pass
