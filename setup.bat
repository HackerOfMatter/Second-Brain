@echo off
REM Second Brain - one-shot Windows setup.
REM Calls python.exe -m pip, never the pip.exe shim (antivirus
REM and cloud sync tools quarantine that launcher, so "pip" appears missing).
setlocal
cd /d "%~dp0"

echo ============================================
echo   Second Brain setup
echo   %CD%
echo ============================================
echo.

set "PYCMD="
py -3 --version >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD (
  python --version >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
  echo [X] No Python found. Install from python.org and tick "Add to PATH".
  goto :fail
)
echo [1/4] Using: %PYCMD%
%PYCMD% --version

if exist ".venv\Scripts\python.exe" (
  echo [2/4] Virtual environment already exists.
) else (
  echo [2/4] Creating virtual environment...
  %PYCMD% -m venv .venv
  if errorlevel 1 goto :fail
)
if not exist ".venv\Scripts\python.exe" (
  echo [X] .venv\Scripts\python.exe was not created.
  goto :fail
)

echo [3/4] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install google-api-python-client google-auth-oauthlib

echo [4/4] Creating vault folders...
".venv\Scripts\python.exe" run.py init
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   Done.  doctor.bat = health check
echo          sync.bat   = calendar / Google auth
echo          start.bat  = open the dashboard
echo ============================================
echo.
pause
exit /b 0

:fail
echo.
echo Setup did not complete. The error above is the real one.
echo.
pause
exit /b 1
