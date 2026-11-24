#!/usr/bin/env bash
# Start JARVIS on Linux (or macOS from a terminal):  ./run_jarvis.sh
# To double-click it in a Linux file manager, allow "Run as program" /
# "Allow executing file as program" in the file's properties.
# First run sets everything up; later runs start in a few seconds.

cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
    echo "Python 3 is not installed. Install python3 (3.11 or newer) and python3-venv, then run this again."
    [ -t 0 ] && read -r -p "Press Enter to close…" _
    exit 1
fi

"$PY" run.py "$@"
status=$?
if [ $status -ne 0 ] && [ $status -ne 130 ] && [ -t 0 ]; then
    echo
    read -r -p "JARVIS stopped with an error (code $status). Press Enter to close…" _
fi
exit $status
