@echo off
REM Story J2 - registers a Windows Task Scheduler job that runs
REM doctor-weekly.bat every Sunday at 09:00, writing a dated report into
REM _system\logs\. Idempotent: /F overwrites an existing task of the same
REM name instead of duplicating it.
REM
REM Run this one AS ADMINISTRATOR (right-click -^> Run as administrator).
REM The first attempt registers the task to run as SYSTEM, which is what
REM makes it fire whether or not lj is logged on. If that is refused
REM (not elevated), it falls back to a task that only runs while lj is
REM logged on, so the report still gets written either way.
setlocal
cd /d "%~dp0"

set "TASK=secondbrain-weekly-doctor"
set "SCRIPT=%~dp0doctor-weekly.bat"

if not exist "%SCRIPT%" (
  echo Could not find doctor-weekly.bat next to this script: "%SCRIPT%"
  pause
  exit /b 1
)

schtasks /Create /SC WEEKLY /D SUN /ST 09:00 /TN "%TASK%" /TR "\"%SCRIPT%\"" /RU SYSTEM /RL HIGHEST /F >nul 2>&1
if errorlevel 1 (
  echo Could not register to run as SYSTEM - this needs an elevated
  echo (Run as administrator) prompt. Retrying as your own account;
  echo this copy will only run while you are logged on.
  schtasks /Create /SC WEEKLY /D SUN /ST 09:00 /TN "%TASK%" /TR "\"%SCRIPT%\"" /F
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
echo   When: every Sunday at 09:00
echo   Writes: _system\logs\doctor-YYYYMMDD.txt  (last 8 kept)
echo.
schtasks /Query /TN "%TASK%"
echo.
echo The weekly review reports how old the newest report is, so a task
echo that stops firing shows up there rather than going unnoticed.
echo To remove it later, run uninstall-doctor-task.bat.
echo.
pause
exit /b 0
