@echo off
REM Story B1 - removes the Startup-folder shortcut installed by
REM install-autostart.bat. Safe to run even if it was never installed.
setlocal
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-start.lnk"

if exist "%SHORTCUT%" (
  del /f /q "%SHORTCUT%"
  if exist "%SHORTCUT%" (
    echo Could not remove: "%SHORTCUT%"
    pause
    exit /b 1
  )
  echo Removed autostart shortcut:
  echo   %SHORTCUT%
) else (
  echo No autostart shortcut found at:
  echo   %SHORTCUT%
  echo Nothing to do.
)

echo.
pause
exit /b 0
