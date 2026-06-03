@echo off
REM Weekly predictor backtest — Sundays 05:00.
cd /d C:\Projects\shared
py -3 C:\Projects\shared\auto_audit\predictive_audit.py --backtest >> C:\Projects\shared\auto_audit\predictive_backtest.log 2>&1
