#!/bin/sh
# Double-click to start Photo Manager and open it in the browser (macOS).
#
# Finder runs a .command file in a Terminal window. That window is the app:
# closing it stops the app, and anything the app prints lands in it. Nothing
# runs in the background once it is closed. Drag it to the Dock, or keep an
# alias on the Desktop — it finds the project through its own real path.
#
# If the app is already running, this just opens the browser on it.

cd "$(dirname "$(realpath "$0")")/.." || exit 1
URL="http://127.0.0.1:8000/"

up() { curl -s -o /dev/null -m 1 "${URL}api/health"; }

if up; then
  echo "Photo Manager is already running."
  open "$URL"
  exit 0
fi

if [ ! -x .venv/bin/python ]; then
  echo "No .venv here. Set it up first - see 'On macOS' in the README."
  printf "Press Return to close."; read -r _
  exit 1
fi

# Open the browser as soon as the app answers, not before.
( i=0; while [ $i -lt 100 ]; do
    if up; then open "$URL"; exit 0; fi
    sleep 0.2; i=$((i + 1))
  done ) &

echo "Photo Manager - close this window to stop it."
.venv/bin/python tools/serve.py
echo
printf "Photo Manager has stopped. Press Return to close."; read -r _
