@echo off
REM A/B Auto-analysis — daily 04:30 Asia/Dubai.
set PYTHONPATH=C:\Projects\shared;%PYTHONPATH%
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" "C:\Projects\shared\ab_testing\analyze_experiments.py" >> "%TEMP%\ab_testing_analyze.log" 2>&1
