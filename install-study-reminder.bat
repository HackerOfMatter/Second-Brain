@echo off
REM Story H3 - installs a Startup-folder shortcut that launches the study
REM session reminder at logon, resident and with no console window, so a
REM session that is due and unstarted gets one desktop notification without
REM anyone remembering to start anything. Idempotent: overwrites the
REM shortcut if run again rather than creating a duplicate. Mirrors
REM install-capture-hotkey.bat's approach.
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0study_reminder.pyw"
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\secondbrain-study-reminder.lnk"

if not exist "%SCRIPT%" (
  echo Could not find study_reminder.pyw next to this script: "%SCRIPT%"
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

powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%PYTHONW%'; $s.Arguments='\"%SCRIPT%\"'; $s.WorkingDirectory='%~dp0'; $s.Description='Second Brain study reminder - resident, no console'; $s.Save()"

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
echo Installed study-reminder autostart shortcut:
echo   %SHORTCUT%
echo   -^> %PYTHONW% "%SCRIPT%"   (pythonw.exe: no console window)
echo.
echo Starting it now, so it is live without a reboot...
start "" "%PYTHONW%" "%SCRIPT%"
echo.
echo It checks once a minute and speaks at most once an hour, and at most
echo once a day unless you snooze it. It only fires when cards are actually
echo due and nothing has been answered today - so it stays silent on a day
echo with an empty queue, and after you have started studying.
echo.
echo To see what it would do right now, without waiting:
echo   python run.py study-reminder
echo Log file (if something looks wrong): _system\logs\study-reminder.log
echo State it remembers:                  _system\study-reminder.json
echo To undo, run uninstall-study-reminder.bat.
echo.
pause
exit /b 0
