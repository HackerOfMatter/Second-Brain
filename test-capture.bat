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

REM `import tkinter` passing proves nothing -- it imports fine without the
REM Tcl runtime. Only creating a window exercises what actually failed here.
".venv\Scripts\python.exe" -c "import tkinter as t; r=t.Tk(); r.destroy()" 2>nul
if errorlevel 1 (
  echo [!] tkinter cannot open a window on its own settings.
  echo     capture_hotkey.pyw sets TCL_LIBRARY/TK_LIBRARY itself, so this
  echo     may still work. Watch the log line below.
) else (
  echo [ok] tkinter opens a window
)

echo.
echo Starting capture tool in the foreground.
echo Press the hotkey to test it. Ctrl+C here to stop.
echo -----------------------------------------------------------
".venv\Scripts\python.exe" capture_hotkey.pyw
echo -----------------------------------------------------------
echo Exited with code %ERRORLEVEL%.
pause
