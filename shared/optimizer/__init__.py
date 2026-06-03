"""PHASE BN N5 — Continuous Performance Optimizer.

Three pillars:
  * query_perf  — pg_stat_statements -> slow_query_log + EXPLAIN suggestions
  * dead_code   — vulture wrapper -> dead_code_candidates
  * dep_scan    — pip-audit wrapper -> vulnerability_findings (+ admin alert)

All DSN reads go through `_db.connect()` (env-only, never hardcoded).
No auto-apply: every finding is recorded for manual review.
"""
from __future__ import annotations

__all__ = ["query_perf", "dead_code", "dep_scan"]
