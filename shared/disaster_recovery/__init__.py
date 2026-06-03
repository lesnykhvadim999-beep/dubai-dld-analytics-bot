"""Disaster Recovery / Backup automation (PHASE BO O1, simplified re-launch).

Modules:
  _config         — DSN env lookup + retention policy constants
  storage_adapter — abstract storage: GitHub releases / R2 / B2 / local
  backup          — main runner: pg_dump custom + gzip + upload + meta log
                    (psycopg2 JSONL fallback if pg_dump missing)
  restore         — download + optional pg_restore
  retention       — daily/weekly/monthly pruning via storage backend
  verify          — download + gzip integrity + pg_restore --list

Cron (see shared/scripts/phase_bm_master_cron.py):
  daily 01:00 UTC — backup intelligence
  weekly Sun 02:30 UTC — verify latest daily
  daily 04:00 UTC — retention prune (apply)

Vadim Realty + RERA BRN 65011.
"""
