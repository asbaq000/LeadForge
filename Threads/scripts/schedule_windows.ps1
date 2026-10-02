# Registers a Windows Scheduled Task that runs the scraper 4x/day.
#
# Four passes of up to ~20 leads each comfortably clears the 70/day target while
# keeping each individual session short and human-paced.
#
# Usage (run PowerShell as Administrator):
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1
#
# Remove it later with:
#   Unregister-ScheduledTask -TaskName "ThreadsLeadScraper" -Confirm:$false

param(
    [string]$TaskName = "ThreadsLeadScraper",
    [string[]]$Times  = @("08:15", "12:15", "16:15", "20:15"),
    [int]$PerRun      = 20
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython  = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Python      = if (Test-Path $VenvPython) { $VenvPython } else { "python" }
$RunScript   = Join-Path $ProjectRoot "run.py"

if (-not (Test-Path $RunScript)) {
    throw "Could not find run.py at $RunScript"
}

Write-Host "Project : $ProjectRoot"
Write-Host "Python  : $Python"
Write-Host "Times   : $($Times -join ', ')  ($PerRun leads per run)"

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$RunScript`" scrape --limit $PerRun" `
    -WorkingDirectory $ProjectRoot

# RandomDelay matters: firing at exactly 08:15:00 every single day is a pattern
# no human produces. This smears each run across the following 25 minutes.
$triggers = foreach ($t in $Times) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $t
    $trigger.RandomDelay = "PT25M"
    $trigger
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "Scrapes Threads for AI/automation leads and appends them to Google Sheets." | Out-Null

Write-Host ""
Write-Host "Registered '$TaskName'."
Write-Host "Run it right now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check history with:     Get-ScheduledTaskInfo -TaskName $TaskName"
