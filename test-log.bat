@echo off
REM Same as test.bat, but writes the whole run to
REM _system\logs\test-run.txt so it can be read back.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  pause
  exit /b 1
)
if not exist "_system\logs" mkdir "_system\logs"
".venv\Scripts\python.exe" tests\test_all.py > "_system\logs\test-run.txt" 2>&1
set CODE=%ERRORLEVEL%
type "_system\logs\test-run.txt"
echo.
echo ---- saved to _system\logs\test-run.txt  (exit %CODE%) ----
pause
exit /b %CODE%
