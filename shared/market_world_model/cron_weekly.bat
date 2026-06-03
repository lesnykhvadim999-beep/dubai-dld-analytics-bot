@echo off
REM Layer 13 — weekly retrain (Sunday 02:00)
REM Free, runs locally. Re-seeds snapshots + retrains top-200 entities x 2 metrics.

cd /d C:\Projects
set PYTHONIOENCODING=utf-8
set MWM_LOG_LEVEL=INFO

REM Try Prophet first; falls back to linear automatically if cmdstanpy fails.
python -m shared.market_world_model.builder --weekly >> C:\Projects\shared\market_world_model\cron.log 2>&1
exit /b %ERRORLEVEL%
