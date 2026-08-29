@echo off
REM Story B2 - restore test. Extracts a backup zip into
REM _system\restore-test\ so it can be eyeballed. NEVER writes over the
REM live _decks or _labels folders - that directory is wiped and
REM recreated fresh on each run, and nothing outside it is touched.
REM
REM Usage:  restore-backup.bat backup-20260829.zip
REM     or:  restore-backup.bat C:\full\path\to\some.zip
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage: restore-backup.bat ^<zip-filename-or-path^>
  echo.
  echo Available backups in _system\backups:
  dir /b "_system\backups\backup-*.zip" 2>nul
  echo.
  pause
  exit /b 1
)

set "ZIPARG=%~1"
set "ZIP=%ZIPARG%"
if not exist "%ZIP%" set "ZIP=%~dp0_system\backups\%ZIPARG%"

if not exist "%ZIP%" (
  echo Could not find zip: "%ZIPARG%"
  echo   looked at "%ZIPARG%"
  echo   looked at "%~dp0_system\backups\%ZIPARG%"
  pause
  exit /b 1
)

set "DEST=%~dp0_system\restore-test"
if exist "%DEST%" rmdir /s /q "%DEST%"
mkdir "%DEST%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%ZIP%' -DestinationPath '%DEST%' -Force"

if errorlevel 1 (
  echo Extraction FAILED.
  pause
  exit /b 1
)

echo.
echo Restored INTO A TEST FOLDER ONLY, not the live vault:
echo   %DEST%
echo.
echo Contents:
dir /s /b "%DEST%"
echo.
echo Nothing under _decks\ or _labels\ was touched - this is a copy under
echo _system\restore-test only. Eyeball it, then delete that folder when done.
echo.
pause
exit /b 0
