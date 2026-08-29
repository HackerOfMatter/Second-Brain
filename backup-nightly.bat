@echo off
REM Story B2 - zips _decks and _labels (which together cover
REM _decks\_reviews.jsonl, the FSRS review log - see sb/cards.py
REM REVIEW_LOG) to _system\backups\backup-YYYYMMDD.zip, then keeps only
REM the newest 7 backup-*.zip files.
REM
REM Must succeed even when _decks/_labels do not exist yet - as of this
REM writing 0 decks have ever been generated, so a first run writes a
REM small placeholder zip rather than erroring out.
REM
REM No "pause" below on purpose: install-backup-task.bat runs this
REM unattended at 02:00 via Task Scheduler, and a paused console would
REM sit there forever with nobody to press a key. Double-click this once
REM by hand if you want to watch it work; the output still prints either way.
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=(Get-Location).Path; $backupDir=Join-Path $root '_system\backups'; New-Item -ItemType Directory -Force -Path $backupDir | Out-Null; $stamp=Get-Date -Format 'yyyyMMdd'; $zip=Join-Path $backupDir ('backup-' + $stamp + '.zip'); if (Test-Path $zip) { Remove-Item $zip -Force }; $sources=@('_decks','_labels') | ForEach-Object { Join-Path $root $_ } | Where-Object { Test-Path $_ }; if ($sources.Count -eq 0) { $tmp=Join-Path $env:TEMP ('sb-backup-' + [guid]::NewGuid()); New-Item -ItemType Directory -Path $tmp | Out-Null; Set-Content -Path (Join-Path $tmp 'NOTE.txt') -Value ('No _decks or _labels folder existed yet at backup time: ' + (Get-Date -Format 'u')); Compress-Archive -Path (Join-Path $tmp '*') -DestinationPath $zip -Force; Remove-Item $tmp -Recurse -Force; Write-Host ('Backed up: nothing yet, wrote placeholder -> ' + $zip) } else { Compress-Archive -Path $sources -DestinationPath $zip -Force; Write-Host ('Backed up: ' + ($sources -join ', ') + ' -> ' + $zip) }; Get-ChildItem -Path $backupDir -Filter 'backup-*.zip' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 7 | ForEach-Object { Write-Host ('Pruned old backup: ' + $_.Name); Remove-Item $_.FullName -Force }; Write-Host ('backup-*.zip files now kept: ' + (Get-ChildItem -Path $backupDir -Filter 'backup-*.zip').Count)"

set CODE=%ERRORLEVEL%
if not "%CODE%"=="0" echo BACKUP FAILED - exit code %CODE%
exit /b %CODE%
