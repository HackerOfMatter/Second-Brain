@echo off
REM Story F1 - installs a Startup-folder shortcut that launches the global
REM capture hotkey (Ctrl+Alt+Z) at logon, resident and with no console
REM window, so the capture box is available with no manual step after a
REM reboot. Idempotent: overwrites the shortcut if run again rather than
REM creating a duplicate. Mirrors install-autostart.bat's approach.
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0capture_hotkey.pyw"
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-capture-hotkey.lnk"

if not exist "%SCRIPT%" (
  echo Could not find capture_hotkey.pyw next to this script: "%SCRIPT%"
  echo.
  pause
  exit /b 1
)

if not exist "%PYTHONW%" (
  echo No virtual environment found at "%PYTHONW%". Run setup.bat first.
  echo.
  pause
  exit /b 1
)

if not exist "%STARTUP%" (
  echo Startup folder not found: "%STARTUP%"
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%PYTHONW%'; $s.Arguments='\"%SCRIPT%\"'; $s.WorkingDirectory='%~dp0'; $s.Description='Second Brain capture hotkey - Ctrl+Alt+Z, resident, no console'; $s.Save()"

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
echo Installed capture-hotkey autostart shortcut:
echo   %SHORTCUT%
echo   -^> %PYTHONW% "%SCRIPT%"   (pythonw.exe: no console window)
echo.
echo Starting it now, so it is live without a reboot...
start "" "%PYTHONW%" "%SCRIPT%"
echo.
echo Press Ctrl+Alt+Z anywhere to capture. It also survives reboot/logon.
echo A toast on startup confirms which key it actually got.
echo Log file (if something looks wrong): %LOCALAPPDATA%\secondbrain\capture-hotkey.log
echo To undo, run uninstall-capture-hotkey.bat.
echo.
pause
exit /b 0
