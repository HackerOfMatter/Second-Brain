@echo off
REM Story J2 - the unattended half of `doctor`. Runs the health check and
REM leaves a dated report in _system\logs\doctor-YYYYMMDD.txt, which the
REM weekly review then reports the age of. Unlike doctor.bat this one is
REM unattended: no window, no test suite, and nothing that waits for a
REM keypress, because Task Scheduler is not there to press one.
setlocal
cd /d "%~dp0"
REM Keep Python bytecode out of the vault: Obsidian indexes and watches
REM every file under it, including the temp files Python writes while importing.
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.>>"_system\logs\doctor-task.log"
  exit /b 1
)
".venv\Scripts\python.exe" run.py doctor --write >>"_system\logs\doctor-task.log" 2>&1
exit /b %errorlevel%
