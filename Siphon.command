#!/bin/sh
# Siphon launcher for macOS and Linux (double-click on macOS, or run ./Siphon.command)
cd "$(dirname "$0")" || exit 1
VENV=".venv"

# A Siphon downloaded from a browser arrives marked as such, file by file, and
# macOS would stop at every one of them. Opening this script was the consent:
# lift the mark from the rest of the folder.
if [ "$(uname)" = Darwin ]; then xattr -dr com.apple.quarantine . 2>/dev/null; fi

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }

# Rebuild the private environment if the Python it was made from is gone.
if [ -e "$VENV/bin/python" ] && ! "$VENV/bin/python" -c 'import sys' >/dev/null 2>&1; then
  echo " Python changed, rebuilding Siphon's environment..."
  rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/python" ]; then
  BASE=""
  # a downloaded Siphon carries its own Python: nothing to install first
  if [ -x runtime/bin/python3 ] && py_ok runtime/bin/python3; then BASE="runtime/bin/python3"; fi
  [ -z "$BASE" ] && for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && py_ok "$c"; then BASE="$c"; break; fi
  done
  if [ -z "$BASE" ]; then
    echo " Siphon needs Python 3.10 or newer: https://www.python.org/downloads/"
    exit 1
  fi
  echo
  echo " Siphon - first launch setup"
  echo " Creating a private Python environment..."
  if ! "$BASE" -m venv "$VENV"; then
    echo " Could not create the environment (on Debian/Ubuntu: sudo apt install python3-venv)."
    exit 1
  fi
fi

"$VENV/bin/python" siphon/setup_env.py || exit 1
nohup "$VENV/bin/python" siphon/app.py >/dev/null 2>&1 &
