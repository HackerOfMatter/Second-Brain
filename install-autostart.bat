@echo off
REM Story B1 - installs a Startup-folder shortcut that launches start.bat
REM minimized at logon, so the dashboard is up at 127.0.0.1:8787 with no
REM manual step after a reboot. Idempotent: overwrites the shortcut if run
REM again rather than creating a duplicate.
setlocal
cd /d "%~dp0"

set "TARGET=%~dp0start.bat"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-start.lnk"

if not exist "%TARGET%" (
  echo Could not find start.bat next to this script: "%TARGET%"
  echo.
  pause
  exit /b 1
)

if not exist "%STARTUP%" (
  echo Startup folder not found: "%STARTUP%"
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='Second Brain dashboard - starts minimized at login'; $s.Save()"

if errorlevel 1 (
  echo PowerShell failed to create the shortcut.
  pause
  exit /b 1
)

if not exist "%SHORTCUT%" (
  echo Shortcut was not created: "%SHORTCUT%"
  pause
  exit /b 1
)

echo.
echo Installed autostart shortcut:
echo   %SHORTCUT%
echo   -^> %TARGET%   (WindowStyle=7, minimized)
echo.
echo Reboot (or log off/on) to verify: the dashboard should be reachable at
echo http://127.0.0.1:8787 with no manual step. To undo, run uninstall-autostart.bat.
echo.
pause
exit /b 0
