#!/usr/bin/env bash
set -euo pipefail
GUI_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPOSITORY_DIR="$(cd -- "$GUI_DIR/../.." && pwd)"
if [[ -n "${CAPSTONE_GUI_PYTHON:-}" ]]; then
    GUI_PYTHON="$CAPSTONE_GUI_PYTHON"
elif [[ -x "$GUI_DIR/.venv/bin/python" ]]; then
    GUI_PYTHON="$GUI_DIR/.venv/bin/python"
else
    GUI_PYTHON="$(command -v python3)"
fi
cd -- "$REPOSITORY_DIR"
exec "$GUI_PYTHON" -m shared.gui "$@"
