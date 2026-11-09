#!/bin/bash
# Double-click to open Fisch Macro. First run installs what it needs (about a minute).
cd "$(dirname "$0")/../macro" || exit 1
clear
echo "Fisch Macro"
echo "-----------"

if ! /usr/bin/env python3 -c "import sys; assert sys.version_info >= (3, 8)" >/dev/null 2>&1; then
  echo
  echo "Python 3 isn't installed yet."
  echo "If a pop-up asks to install 'command line developer tools', click Install,"
  echo "wait for it to finish, then double-click this launcher again."
  echo "(Or install Python from https://www.python.org/downloads/ )"
  xcode-select --install >/dev/null 2>&1
  read -r -p "Press Enter to close..."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "First run: setting things up..."
  python3 -m venv .venv || { echo "Couldn't create the environment."; read -r -p "Press Enter to close..."; exit 1; }
fi

if ! .venv/bin/python -c "import mss, numpy, pynput, AppKit, Vision, Quartz" >/dev/null 2>&1; then
  echo "Installing mss, numpy, pynput and pyobjc (only needed once, about a minute)..."
  .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip >/dev/null 2>&1
  if ! .venv/bin/python -m pip install --disable-pip-version-check -q mss numpy pynput pyobjc-framework-Cocoa pyobjc-framework-Quartz pyobjc-framework-Vision; then
    echo "Install failed. Check your internet connection and try again."
    read -r -p "Press Enter to close..."
    exit 1
  fi
fi

# fast (60 per second) screen reading; optional - the macro still works without it
if ! .venv/bin/python -c "import ScreenCaptureKit, CoreMedia, dispatch" >/dev/null 2>&1; then
  echo "Installing the fast screen reader (only needed once)..."
  .venv/bin/python -m pip install --disable-pip-version-check -q pyobjc-framework-ScreenCaptureKit pyobjc-framework-CoreMedia pyobjc-framework-libdispatch \
    || echo "(Couldn't install it - the macro will use the slower screen reading.)"
fi

echo "Opening the Fisch Macro window in your browser."
echo "Keep this Terminal window open while you use it (you can minimise it)."
echo
.venv/bin/python app.py
