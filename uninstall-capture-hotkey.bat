@echo off
REM Story F1 - removes the Startup-folder shortcut installed by
REM install-capture-hotkey.bat. Safe to run even if it was never installed.
REM Note: this only stops it running at the NEXT logon. A copy already
REM resident from this session keeps the hotkey registered until you log
REM off/reboot, or end it by hand in Task Manager (look for pythonw.exe
REM running capture_hotkey.pyw, under Details -> Command line).
setlocal
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-capture-hotkey.lnk"

if exist "%SHORTCUT%" (
  del /f /q "%SHORTCUT%"
  if exist "%SHORTCUT%" (
    echo Could not remove: "%SHORTCUT%"
    pause
    exit /b 1
  )
  echo Removed capture-hotkey autostart shortcut:
  echo   %SHORTCUT%
) else (
  echo No capture-hotkey autostart shortcut found at:
  echo   %SHORTCUT%
  echo Nothing to do.
)

echo.
echo If the capture box still opens on Ctrl+Alt+Space, a copy from this
echo session is still resident - log off/reboot, or end it in Task Manager.
echo.
pause
exit /b 0
