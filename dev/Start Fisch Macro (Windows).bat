@echo off
REM Double-click to open Fisch Macro. First run installs what it needs (about a minute).
title Fisch Macro
cd /d "%~dp0"
echo Fisch Macro
echo -----------

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; assert sys.version_info >= (3, 8)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo Python 3 isn't installed yet.
  echo Install it from the page that just opened. During setup, tick
  echo "Add python.exe to PATH". Then double-click this launcher again.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Couldn't create the environment. & pause & exit /b 1 )
)

".venv\Scripts\python.exe" -c "import mss, numpy, pynput" >nul 2>nul
if errorlevel 1 (
  echo Installing mss, numpy and pynput ^(only needed once^)...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q mss numpy pynput
  if errorlevel 1 ( echo Install failed. Check your internet connection and try again. & pause & exit /b 1 )
)

echo Opening the Fisch Macro window in your browser.
echo Keep this window open while you use it (you can minimise it).
echo.
".venv\Scripts\python.exe" app.py
pause
