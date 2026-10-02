@echo off
cd /d "C:\Users\pc\OneDrive\Desktop\facebook_scraper"

echo. >> daily_run.log
echo ==== Daily run started %DATE% %TIME% ==== >> daily_run.log

rem Start (or reuse) the debug Chrome window with the persistent profile.
rem If a Facebook session is already logged into this profile, it stays
rem logged in across days, same as any normal browser profile -- this does
rem NOT log in for you.
rem The throttling flags matter: Chrome pauses rendering for minimized or
rem occluded windows, which makes Facebook serve an empty feed and the
rem scrape silently return zero leads with no error. These keep the page
rem live even when the window isn't in the foreground.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-fb-debug-profile" --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion "https://www.facebook.com/"

rem Give Chrome time to finish launching before Selenium attaches.
timeout /t 15 /nobreak > nul

"C:\Users\pc\AppData\Local\Programs\Python\Python312\python.exe" scrape_multi_groups.py >> daily_run.log 2>&1

echo ==== Daily run finished %DATE% %TIME% ==== >> daily_run.log
