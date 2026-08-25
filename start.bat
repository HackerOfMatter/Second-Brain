@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  echo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py
if errorlevel 1 pause
