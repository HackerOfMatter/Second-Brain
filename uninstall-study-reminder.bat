@echo off
REM Story H3 - removes the Startup-folder shortcut installed by
REM install-study-reminder.bat. Safe to run even if it was never installed.
REM Note: this only stops it running at the NEXT logon. A copy already
REM resident from this session keeps polling until you log off/reboot, or
REM end it by hand in Task Manager (look for pythonw.exe running
REM study_reminder.pyw, under Details -^> Command line).
setlocal
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-study-reminder.lnk"

if exist "%SHORTCUT%" (
  del /f /q "%SHORTCUT%"
  if exist "%SHORTCUT%" (
    echo Could not remove: "%SHORTCUT%"
    pause
    exit /b 1
  )
  echo Removed study-reminder autostart shortcut:
  echo   %SHORTCUT%
) else (
  echo No study-reminder autostart shortcut found at:
  echo   %SHORTCUT%
  echo Nothing to do.
)

echo.
echo _system\study-reminder.json is left in place on purpose: it is the
echo record of what has already been announced, and deleting it is how a
echo reinstall ends up repeating today's reminder.
echo.
echo If a notification still appears, a copy from this session is still
echo resident - log off/reboot, or end it in Task Manager.
echo.
pause
exit /b 0
