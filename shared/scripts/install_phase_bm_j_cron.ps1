# PHASE BM Agent J — Windows Task Scheduler installer.
# Run from elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File C:\Projects\shared\scripts\install_phase_bm_j_cron.ps1

$pythonExe = (Get-Command python).Source
$projDir = "C:\Projects"
$env:RESALE_DATABASE_URL = "postgresql://postgres:zixtQCodkMoSpjrHicqkYcutfGUXiQCM@tramway.proxy.rlwy.net:23228/railway"

function Register-CronTask {
    param([string]$Name, [string]$Cmd, [TimeSpan]$Interval, [string]$AtTime)
    $action = New-ScheduledTaskAction -Execute $pythonExe `
        -Argument "-m shared.scripts.cron_phase_bm_j $Cmd" `
        -WorkingDirectory $projDir
    if ($AtTime) {
        $trigger = New-ScheduledTaskTrigger -Daily -At $AtTime
    } else {
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval $Interval -RepetitionDuration (New-TimeSpan -Days 365)
    }
    Register-ScheduledTask -TaskName "PhaseBMJ-$Name" -Action $action `
        -Trigger $trigger -Force | Out-Null
    Write-Host "registered: PhaseBMJ-$Name"
}

Register-CronTask -Name "RssPoll"      -Cmd "rss_poll"       -Interval (New-TimeSpan -Hours 3)
Register-CronTask -Name "TgScrape"     -Cmd "telegram_scrape" -Interval (New-TimeSpan -Hours 2)
Register-CronTask -Name "DailyDigest"  -Cmd "daily_digest"   -AtTime "08:00"
# Weekly meta_optimize: Sunday 03:00 via SchTasks since PS native lacks DOW-only daily trigger
schtasks /Create /TN "PhaseBMJ-MetaOpt" /SC WEEKLY /D SUN /ST 03:00 `
  /TR "`"$pythonExe`" -m shared.scripts.cron_phase_bm_j meta_optimize 14" /F | Out-Null
Write-Host "registered: PhaseBMJ-MetaOpt"

Write-Host "All PHASE BM J cron tasks installed."
