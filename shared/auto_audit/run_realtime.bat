@echo off
REM Realtime Auto-Audit wrapper — invoked every 15 minutes.
set PYTHONPATH=C:\Projects\shared;%PYTHONPATH%
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" "C:\Projects\shared\auto_audit\audit_realtime.py" >> "%TEMP%\auto_audit_realtime.log" 2>&1
