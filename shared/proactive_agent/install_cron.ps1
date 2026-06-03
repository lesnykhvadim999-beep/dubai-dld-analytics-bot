# Phase BM — install Windows Task Scheduler entries
# Run as Administrator (or use schtasks /RU SYSTEM).

$consolidate = @{
  Name   = "ClaudeAgentBM_L9_ConsolidateMemory"
  Action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\Projects\shared\user_memory\run_consolidate.ps1`""
  Sched  = "/SC DAILY /ST 05:00"
}

$tick = @{
  Name   = "ClaudeAgentBM_L10_ProactiveTick"
  Action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"C:\Projects\shared\proactive_agent\run_tick.ps1`""
  Sched  = "/SC MINUTE /MO 30"
}

foreach ($t in @($consolidate, $tick)) {
  $exists = (schtasks /Query /TN $t.Name 2>$null)
  if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing task: $($t.Name)"
    schtasks /Delete /TN $t.Name /F | Out-Null
  }
  $cmd = "schtasks /Create /TN `"$($t.Name)`" /TR `"$($t.Action)`" $($t.Sched) /F /RL HIGHEST"
  Write-Host "Creating: $cmd"
  Invoke-Expression $cmd
}

Write-Host ""
Write-Host "=== Installed tasks ==="
schtasks /Query /TN "ClaudeAgentBM_L9_ConsolidateMemory" /FO LIST
schtasks /Query /TN "ClaudeAgentBM_L10_ProactiveTick" /FO LIST
