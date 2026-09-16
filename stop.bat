@echo off
REM Stop the running Second Brain (the copy started at login, or any other).
setlocal
cd /d "%~dp0"
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"
".venv\Scripts\python.exe" run.py stop
pause
