@echo off
REM Run the capture tool with a VISIBLE console, so a failure says why.
REM install-capture-hotkey.bat runs it under pythonw.exe with no window,
REM which is right for daily use and useless for debugging.
setlocal
cd /d "%~dp0"
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"

if not exist ".venv\Scripts\python.exe" (
  echo [X] No virtual environment. Run setup.bat first.
  pause
  exit /b 1
)
echo [ok] found .venv\Scripts\python.exe

".venv\Scripts\python.exe" -c "import tkinter" 2>nul
if errorlevel 1 (
  echo [X] tkinter is NOT available in this Python. The capture box cannot draw.
  echo     Reinstall Python from python.org with the 'tcl/tk and IDLE' option ticked.
  pause
  exit /b 1
)
echo [ok] tkinter imports

echo.
echo Starting capture tool in the foreground.
echo Press the hotkey to test it. Ctrl+C here to stop.
echo -----------------------------------------------------------
".venv\Scripts\python.exe" capture_hotkey.pyw
echo -----------------------------------------------------------
echo Exited with code %ERRORLEVEL%.
pause
