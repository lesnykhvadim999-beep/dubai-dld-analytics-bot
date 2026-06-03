# Phase BM L9 — nightly user memory consolidation (05:00)
# Invoked by Windows Task Scheduler.
$ErrorActionPreference = "Continue"
$logDir = "C:\Projects\shared\user_memory\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "consolidate_$ts.log"

$env:PYTHONPATH = "C:\Projects"
$env:RESALE_DATABASE_URL = "postgresql://postgres:zixtQCodkMoSpjrHicqkYcutfGUXiQCM@tramway.proxy.rlwy.net:23228/railway"

"=== START $(Get-Date -Format o) ===" | Out-File -FilePath $log -Encoding utf8 -Append
& py -m shared.user_memory.consolidator *>> $log
"=== END   $(Get-Date -Format o) exit=$LASTEXITCODE ===" | Out-File -FilePath $log -Encoding utf8 -Append

# Retention: keep last 30 logs
Get-ChildItem $logDir -Filter "consolidate_*.log" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue
