@echo off
REM Run the whole suite on THIS machine. Four phases shipped green from a
REM Linux cloud run; a green suite that never ran where the code runs is
REM not green. Double-click this after every change.
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
".venv\Scripts\python.exe" tests\test_all.py
set CODE=%ERRORLEVEL%
echo.
if not "%CODE%"=="0" echo SUITE FAILED - exit code %CODE%
pause
exit /b %CODE%
