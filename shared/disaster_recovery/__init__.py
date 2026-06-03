"""Disaster Recovery / Backup automation module (PHASE BO O1).

Modules:
  _config         — list of DSNs + retention policy constants
  storage_adapter — abstract storage: R2 / B2 / GitHub releases / local
  backup          — main runner (pg_dump + GPG encrypt + upload)
  restore         — restore helper (download + GPG decrypt + pg_restore)
  retention       — daily/weekly/monthly/yearly pruning
  verify          — weekly verify-on-restore + monthly DR drill
  schema_commit   — daily schema-only pg_dump → git commit
"""
