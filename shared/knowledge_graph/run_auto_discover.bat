@echo off
REM KG Auto-discover — daily 04:00 Asia/Dubai. Register with:
REM   schtasks /Create /SC DAILY /ST 04:00 /TN "KG Auto Discover" /TR "C:\Projects\shared\knowledge_graph\run_auto_discover.bat"
set PYTHONPATH=C:\Projects\shared;%PYTHONPATH%
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" -m shared.knowledge_graph.auto_discover >> "%TEMP%\kg_auto_discover.log" 2>&1
