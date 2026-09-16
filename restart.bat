@echo off
REM Stop the running Second Brain and start it again, so code updates load.
setlocal
cd /d "%~dp0"
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run.py serve --restart
if errorlevel 1 pause
