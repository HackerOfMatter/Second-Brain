@echo off
REM Second Brain - push code to GitHub.
REM
REM This script used to run a blind `git add -A`. On 2026-08-28 that swept a
REM file of 2FA recovery codes into a commit. It was caught before the push.
REM Everything below exists so that cannot happen again.
setlocal EnableDelayedExpansion
cd /d "%~dp0"

git rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
  echo Not a git repository. Nothing to sync.
  exit /b 1
)

git add -A >nul 2>&1

REM --- secret guard -------------------------------------------------------
REM Refuse to commit if any staged path looks like a credential. Unstage
REM everything and report, rather than pushing and apologising afterwards.
set "BLOCKED="
for /f "delims=" %%F in ('git diff --cached --name-only') do (
  echo %%F | findstr /i /r "recovery.code authenticator secret password credential token \.pem$ \.key$ \.env$ \.text$ id_rsa" >nul && set "BLOCKED=!BLOCKED! %%F"
)

if defined BLOCKED (
  echo.
  echo ================ PUSH BLOCKED ================
  echo These staged paths look like secrets:
  for %%B in (!BLOCKED!) do echo    %%B
  echo.
  echo Nothing was committed and nothing was pushed.
  echo Add them to .gitignore, or move them out of this folder, then re-run.
  echo ==============================================
  echo.
  git reset >nul 2>&1
  pause
  exit /b 1
)

git diff --cached --quiet
if %errorlevel% NEQ 0 (
    git commit -m "auto-sync: %date% %time%" >nul 2>&1
    git push origin main
    echo Pushed.
) else (
    echo Nothing to sync.
)
