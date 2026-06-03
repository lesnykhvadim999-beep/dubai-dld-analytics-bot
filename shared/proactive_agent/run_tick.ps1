# Phase BM L10 — proactive scheduler tick (every 30 minutes)
$ErrorActionPreference = "Continue"
$logDir = "C:\Projects\shared\proactive_agent\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "tick_$ts.log"

$env:PYTHONPATH = "C:\Projects"
$env:RESALE_DATABASE_URL = "postgresql://postgres:zixtQCodkMoSpjrHicqkYcutfGUXiQCM@tramway.proxy.rlwy.net:23228/railway"

"=== START $(Get-Date -Format o) ===" | Out-File -FilePath $log -Encoding utf8 -Append
& py -m shared.proactive_agent.scheduler *>> $log
"=== END   $(Get-Date -Format o) exit=$LASTEXITCODE ===" | Out-File -FilePath $log -Encoding utf8 -Append

# Retention: keep last 200 logs
Get-ChildItem $logDir -Filter "tick_*.log" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 200 | Remove-Item -Force -ErrorAction SilentlyContinue
