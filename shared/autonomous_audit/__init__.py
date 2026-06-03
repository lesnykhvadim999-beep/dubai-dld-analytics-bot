"""PHASE BN N4 — Autonomous Audit Loop.

Self-driving audit cycle:
  scheduler  → cron-based runner triggers checks at intervals
  checks     → registry of 10 baseline checks (heartbeats, schema, brand, ...)
  runner     → executes audit_type, persists audit_runs + audit_findings
  reporter   → daily digest to admin

DSN: env vars only — RESALE_DATABASE_URL / INTELLIGENCE_DATABASE_URL /
LIVE_DATABASE_URL / DATABASE_URL (first found).

Integration: cron jobs registered in
shared/scripts/phase_bm_master_cron.py — hourly/daily/weekly.
"""
