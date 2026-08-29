@echo off
setlocal
cd /d "%~dp0"
REM Keep Python bytecode out of the vault: Obsidian indexes and watches
REM every file under it, including the temp files Python writes while importing.
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
