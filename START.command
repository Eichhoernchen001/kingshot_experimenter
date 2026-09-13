#!/bin/bash
# macOS launcher. Also callable from Terminal with: bash START.command
set -e

finish_start() {
    start_status=$?
    trap - EXIT
    if [ "$start_status" -ne 0 ]; then
        echo
        echo "Kingshot could not start. See the error above and README.txt."
        if [ -t 0 ]; then
            read -r -p "Press Return to close..." || true
        fi
    fi
    exit "$start_status"
}
trap finish_start EXIT
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [ ! -x ".venv/bin/python" ]; then
    echo "Run SETUP.command first: bash SETUP.command"
    exit 1
fi

".venv/bin/python" "app/kingshot_gui.py"
