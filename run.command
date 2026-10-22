#!/bin/bash
# Double-clickable launcher for jd_gdrive_refresh.py
# (or run it from a terminal: ./run.command --status)
cd "$(dirname "$0")" || exit 1

# urllib3 2 (needed for the security fixes) requires OpenSSL 1.1.1+.
# macOS system Python 3.9 uses LibreSSL, so prefer a modern Homebrew Python.
PYTHON_BIN="$(command -v python3.13 || command -v python3.12 || command -v python3)"

# Rebuild a stale venv created with an older Python.
if [ -x .venv/bin/python ] && ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Rebuilding virtual environment with a newer Python..."
  rm -rf .venv
fi

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating virtual environment and installing dependencies..."
  "$PYTHON_BIN" -m venv .venv \
    && .venv/bin/pip install --quiet --upgrade pip \
    && .venv/bin/pip install --quiet -r requirements.txt \
    || { echo "Setup failed. See README.md."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
fi

if [ $# -eq 0 ]; then
  set -- --watch
fi
exec .venv/bin/python jd_gdrive_refresh.py "$@"
