# PHASE BM Layer 12 — register Windows Scheduled Task that runs the
# agent_bus subscriber every minute in --once mode.
#
# Run once as Administrator:
#   powershell -ExecutionPolicy Bypass -File install_windows_task.ps1

$ErrorActionPreference = "Stop"

$python = (Get-Command python).Source
if (-not $python) { throw "python not found in PATH" }

$workDir = "C:\Projects\shared"
$action  = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m agent_bus.daemon --agent admin-notifier --once" `
    -WorkingDirectory $workDir

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "AgentBus_AdminNotifier" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "PHASE BM L12 agent_bus subscriber (admin-notifier)" `
    -Force

Write-Host "Registered task AgentBus_AdminNotifier (every 1 min)."
