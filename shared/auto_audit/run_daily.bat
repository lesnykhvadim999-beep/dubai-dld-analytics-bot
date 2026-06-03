@echo off
REM Daily Auto-Audit wrapper — invoked by Windows Task Scheduler at 06:00 Asia/Dubai.
set PYTHONPATH=C:\Projects\shared;%PYTHONPATH%
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" "C:\Projects\shared\auto_audit\daily_audit.py" >> "%TEMP%\auto_audit_daily.log" 2>&1
