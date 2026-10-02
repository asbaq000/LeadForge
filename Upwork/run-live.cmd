@echo off
REM Watch for new video/YouTube/creator jobs as they get posted.
REM Appends to live.jsonl. Press Ctrl+C to stop.

cd /d "%~dp0"

echo Watching for new jobs. Press Ctrl+C to stop.
echo Results append to: %~dp0live.jsonl
echo.

.venv\Scripts\python.exe -m upwork_scraper ^
  --niche video ^
  --watch --interval 60 --pages 1 ^
  --skip-backlog ^
  --format jsonl --out live.jsonl

pause
