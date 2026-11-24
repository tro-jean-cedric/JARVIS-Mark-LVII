#!/bin/bash
# Double-click in Finder to start JARVIS (macOS opens .command files in Terminal).
# First run sets everything up; later runs start in a few seconds.
# Everything happens in run.py — this file only finds Python and calls it.

cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
    echo "Python 3 is not installed."
    echo "Install Python 3.12 from https://www.python.org/downloads/ and double-click this file again."
    read -r -p "Press Enter to close…" _
    exit 1
fi

"$PY" run.py "$@"
status=$?
if [ $status -ne 0 ] && [ $status -ne 130 ]; then
    echo
    read -r -p "JARVIS stopped with an error (code $status). Press Enter to close…" _
fi
exit $status
