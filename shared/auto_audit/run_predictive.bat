@echo off
REM Predictive Failure Detection — runs every 4h via Task Scheduler.
cd /d C:\Projects\shared
py -3 C:\Projects\shared\auto_audit\predictive_audit.py >> C:\Projects\shared\auto_audit\predictive_audit.log 2>&1
