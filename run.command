#!/bin/bash
# Double-clickable launcher for jd_gdrive_refresh.py
# (or run it from a terminal: ./run.command --status)
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating virtual environment and installing dependencies..."
  python3 -m venv .venv \
    && .venv/bin/pip install --quiet --upgrade pip \
    && .venv/bin/pip install --quiet -r requirements.txt \
    || { echo "Setup failed. See README.md."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }
fi

if [ $# -eq 0 ]; then
  set -- --watch
fi
exec .venv/bin/python jd_gdrive_refresh.py "$@"
