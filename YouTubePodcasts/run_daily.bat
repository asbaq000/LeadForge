@echo off
REM Daily podcaster-lead run. Uses the project's own .venv so it does not
REM depend on whatever "python" happens to be on PATH for the scheduled task.
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" main.py run >> "logs\run_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log" 2>&1
