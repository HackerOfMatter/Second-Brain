@echo off
REM Story B2 - registers a Windows Task Scheduler job that runs
REM backup-nightly.bat every day at 02:00. Idempotent: /F overwrites an
REM existing task of the same name instead of duplicating it.
REM
REM Run this one AS ADMINISTRATOR (right-click -^> Run as administrator).
REM The first attempt registers the task to run as SYSTEM, which is what
REM makes it fire whether or not lj is logged on. If that is refused
REM (not elevated), it falls back to a task that only runs while lj is
REM logged on, so the backup still happens either way.
setlocal
cd /d "%~dp0"

set "TASK=secondbrain-nightly-backup"
set "SCRIPT=%~dp0backup-nightly.bat"

if not exist "%SCRIPT%" (
  echo Could not find backup-nightly.bat next to this script: "%SCRIPT%"
  pause
  exit /b 1
)

schtasks /Create /SC DAILY /ST 02:00 /TN "%TASK%" /TR "\"%SCRIPT%\"" /RU SYSTEM /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
  echo Could not register to run as SYSTEM - this needs an elevated
  echo (Run as administrator) prompt. Retrying as your own account;
  echo this copy will only run while you are logged on.
  schtasks /Create /SC DAILY /ST 02:00 /TN "%TASK%" /TR "\"%SCRIPT%\"" /F
)

if errorlevel 1 (
  echo.
  echo Task Scheduler registration failed entirely.
  pause
  exit /b 1
)

echo.
echo Scheduled task installed: %TASK%
echo   Runs: %SCRIPT%
echo   When: daily at 02:00
echo.
schtasks /Query /TN "%TASK%"
echo.
echo To remove it later, run uninstall-backup-task.bat.
echo.
pause
exit /b 0
