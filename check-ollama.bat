@echo off
setlocal
cd /d "%~dp0"
REM Test Ollama step by step and start it if it is off.
REM   check-ollama.bat        test + start
REM   check-ollama.bat pull   test + start + pull missing models
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  pause
  exit /b 1
)
if /i "%~1"=="pull" (
  ".venv\Scripts\python.exe" run.py ollama --fix --pull
) else (
  ".venv\Scripts\python.exe" run.py ollama --fix
)
echo.
pause
