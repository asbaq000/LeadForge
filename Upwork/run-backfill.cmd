@echo off
REM Fetch video/YouTube/creator jobs already posted, into jobs.csv
REM Double-click to run, or: run-backfill.cmd

cd /d "%~dp0"

.venv\Scripts\python.exe -m upwork_scraper ^
  --niche video ^
  --backfill --backfill-pages 20 ^
  --format csv --out jobs.csv

echo.
echo Done. Results are in: %~dp0jobs.csv
pause
