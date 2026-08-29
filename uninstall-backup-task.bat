@echo off
REM Story B2 - removes the scheduled task installed by
REM install-backup-task.bat. Safe to run even if it was never installed.
setlocal

set "TASK=secondbrain-nightly-backup"

schtasks /Query /TN "%TASK%" >nul 2>&1
if errorlevel 1 (
  echo No scheduled task named "%TASK%" found. Nothing to do.
  pause
  exit /b 0
)

schtasks /Delete /TN "%TASK%" /F
if errorlevel 1 (
  echo Could not delete task "%TASK%". Try running this as Administrator.
  pause
  exit /b 1
)

echo Removed scheduled task: %TASK%
pause
exit /b 0
