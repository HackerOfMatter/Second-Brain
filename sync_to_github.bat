@echo off
cd /d "%~dp0"
git add -A >nul 2>&1
git diff --cached --quiet
if %errorlevel% NEQ 0 (
    git commit -m "auto-sync: %date% %time%" >nul 2>&1
    git push origin main
)
