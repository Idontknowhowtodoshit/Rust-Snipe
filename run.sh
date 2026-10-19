#!/usr/bin/env sh
# Rust-Snipe launcher for macOS / Linux: sets itself up on first run, then opens the web UI.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    PY="${PYTHON:-python3}"
    command -v "$PY" >/dev/null 2>&1 || { echo "Python 3.9 or newer is needed (python3 not found)."; exit 1; }
    echo "First run: setting up Python packages (one time only)..."
    "$PY" -m venv .venv || { echo "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
    .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || { rm -rf .venv; exit 1; }
fi
exec .venv/bin/python rustloc.py ui "$@"
