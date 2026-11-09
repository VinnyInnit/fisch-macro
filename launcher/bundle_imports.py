"""Never run - it lists every module the macro code uses, so the app builder (PyInstaller)
packs them into the app. Updates can only use modules that are in here; adding a new
library means building a new app (bump SHELL_VERSION)."""
# flake8: noqa
import argparse, collections, ctypes, ctypes.util, difflib, http.server, json, math, re, signal
import socketserver, subprocess, threading, urllib.request, webbrowser, zipfile, zlib
import numpy
import mss, mss.tools
import pynput, pynput.mouse, pynput.keyboard
import certifi
import sys

if sys.platform == "darwin":
    import objc, Foundation, AppKit, Quartz, Vision, ScreenCaptureKit, CoreMedia, dispatch
elif sys.platform == "win32":
    import tkinter
