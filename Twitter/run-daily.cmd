@echo off
REM Daily lead pull: only what's new since the last run, as a dated CSV.
REM
REM --no-login is deliberate. On a schedule there is nobody to sign in, so an
REM expired session must fail loudly (exit 3) rather than wait at a browser
REM window nobody can see. When that happens, run `x-leads --login` once.

cd /d "%~dp0"

REM Leads reported by an earlier run are skipped by default, so each day's
REM file holds only what is genuinely new.
python -m x_leads ^
    --hours 24 ^
    --min-verdict warm ^
    --no-login ^
    --format csv ^
    --out auto

if errorlevel 3 (
    echo.
    echo The X session has expired. Run: python -m x_leads --login
    exit /b 3
)
if errorlevel 4 (
    echo.
    echo X rate limited the search. Try again later, or lower --concurrency.
    exit /b 4
)
exit /b %errorlevel%
