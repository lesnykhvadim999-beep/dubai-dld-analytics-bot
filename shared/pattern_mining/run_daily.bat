@echo off
REM Bot Pattern Mining — daily 03:30 Asia/Dubai (after daily audit at 06:00).
set PYTHONPATH=C:\Projects\shared;%PYTHONPATH%
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" "C:\Projects\shared\pattern_mining\discover_bot_patterns.py" --days 7 >> "%TEMP%\bot_pattern_mining.log" 2>&1
