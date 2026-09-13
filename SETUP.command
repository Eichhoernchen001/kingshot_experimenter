#!/bin/bash
# macOS setup. Also callable from Terminal with: bash SETUP.command
set -e

finish_setup() {
    setup_status=$?
    trap - EXIT
    if [ "$setup_status" -ne 0 ]; then
        echo
        echo "Setup failed. See the error above and README.txt."
    fi
    if [ -t 0 ]; then
        read -r -p "Press Return to close..." || true
    fi
    exit "$setup_status"
}
trap finish_setup EXIT
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

echo "Kingshot setup"
echo "Packages are installed into .venv inside this folder."
echo "An internet connection is needed for packages and Chromium."

if [ ! -x ".venv/bin/python" ]; then
    base_python="${KINGSHOT_PYTHON:-python3}"
    if ! command -v "$base_python" >/dev/null 2>&1; then
        echo "Python was not found. Install Python 3.10+ from python.org."
        exit 1
    fi
    if ! "$base_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
        echo "Python 3.10 or newer is required."
        exit 1
    fi
    if ! "$base_python" -c 'import tkinter'; then
        echo "This Python has no Tkinter. Use the standard macOS installer from python.org."
        exit 1
    fi
    "$base_python" -m venv ".venv"
fi

venv_python=".venv/bin/python"
"$venv_python" -c 'import sys, tkinter; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install -r "app/requirements.txt"
"$venv_python" -m playwright install chromium

echo
echo "Setup complete. Open START.command or run: bash START.command"
