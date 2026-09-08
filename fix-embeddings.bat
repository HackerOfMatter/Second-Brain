@echo off
setlocal
cd /d "%~dp0"
REM Fix: search matching words rather than meaning.
REM Cause: nomic-embed-text was never pulled, so /api/embeddings returns 404
REM and the indexer refuses a partial vector file (by design).
set "PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\secondbrain\pycache"

where ollama >nul 2>&1
if errorlevel 1 (
  echo Ollama is not on PATH. Install it from https://ollama.com/download
  echo.
  pause
  exit /b 1
)

echo Checking the Ollama server...
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1
if errorlevel 1 (
  echo   not running - starting it
  start "" /min ollama serve
  timeout /t 5 /nobreak >nul
) else (
  echo   already running
)

echo.
echo Pulling nomic-embed-text ^(~270MB^)...
ollama pull nomic-embed-text
if errorlevel 1 (
  echo.
  echo The pull failed. Check the network and try again.
  echo.
  pause
  exit /b 1
)

echo.
echo Installed models:
ollama list

echo.
if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found. Run setup.bat first.
  echo.
  pause
  exit /b 1
)
echo Checking the wiring...
".venv\Scripts\python.exe" run.py doctor

echo.
echo Done. Edit or capture any note - the next index build writes vectors
echo and the "matching words rather than meaning" banner clears itself.
echo.
pause
