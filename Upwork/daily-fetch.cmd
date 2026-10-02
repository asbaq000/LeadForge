@echo off
REM Daily video/YouTube/creator job fetch with client details.
REM
REM   daily-fetch.cmd        -> 8 jobs (default)
REM   daily-fetch.cmd 15     -> 15 jobs
REM
REM Output:  data\jobs-YYYY-MM-DD.csv
REM Log:     logs\daily-YYYY-MM-DD.log
REM
REM Paced at 2-3.5 minutes between client lookups, so 8 jobs takes ~20 minutes.
REM That slowness is deliberate - it keeps the footprint small.
REM
REM Jobs posted from India, Pakistan, Bangladesh, Egypt and the Philippines are
REM dropped, so the CSV usually holds fewer rows than COUNT - the country is
REM only known after the lookup, so an excluded job is fetched and then
REM discarded. Ask for a few extra if you need a full N. See GUIDE.md 7b.

setlocal
cd /d "%~dp0"

set COUNT=%1
if "%COUNT%"=="" set COUNT=8

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

if not exist data mkdir data
if not exist logs mkdir logs

echo Fetching %COUNT% jobs into data\jobs-%TODAY%.csv
echo Progress is written to logs\daily-%TODAY%.log

.venv\Scripts\python.exe -m upwork_scraper ^
  --niche video ^
  --pages 1 ^
  --max-age-days 2 ^
  --limit %COUNT% ^
  --enrich-clients ^
  --enrich-delay 120 210 ^
  --format csv ^
  --out "data\jobs-%TODAY%.csv" >> "logs\daily-%TODAY%.log" 2>&1

if errorlevel 1 (
  echo FAILED - see logs\daily-%TODAY%.log
  exit /b 1
)

echo Done: data\jobs-%TODAY%.csv
exit /b 0
