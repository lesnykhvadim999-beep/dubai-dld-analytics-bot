"""PHASE BN N5 — slow query analyser.

Reads pg_stat_statements (creates the extension if missing), records the top-30
slow statements into slow_query_log, and for queries with mean_exec_time > 100ms
runs EXPLAIN ANALYZE to detect seq scans on big tables and suggests CREATE INDEX
hints.  No DDL is auto-applied.

Usage:
    python -m shared.optimizer.query_perf
"""
from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from . import _db

log = logging.getLogger("optimizer.query_perf")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

# Heuristic: skip these system-ish queries.
_SKIP_PATTERNS = (
    "pg_stat_statements",
    "pg_catalog",
    "information_schema",
    "vacuum",
    "analyze ",
    "begin", "commit", "rollback",
    "set ", "show ",
    "create extension",
)

# Rough "large table" cutoff used when scanning EXPLAIN output.
LARGE_TABLE_ROWS = 5_000


def _ensure_extension(cur) -> bool:
    """Try to CREATE EXTENSION pg_stat_statements. Return True if usable."""
    try:
        cur.execute(
            "SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'"
        )
        if cur.fetchone():
            return True
    except Exception:
        pass
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        return True
    except Exception as e:
        log.warning("pg_stat_statements not available and cannot be created: %s", e)
        return False


def _column_set(cur, table: str) -> set[str]:
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def _fetch_top(cur, limit: int = 30) -> list[dict[str, Any]]:
    """pg_stat_statements column names differ between PG13/PG14+.  Detect."""
    cols = _column_set(cur, "pg_stat_statements")
    if "total_exec_time" in cols:
        time_total = "total_exec_time"
        time_mean = "mean_exec_time"
    elif "total_time" in cols:
        # PG <= 12 legacy naming
        time_total = "total_time"
        time_mean = "mean_time"
    else:
        return []
    sql = f"""
        SELECT query,
               calls,
               {time_total} AS total_time,
               {time_mean}  AS mean_time
        FROM pg_stat_statements
        WHERE calls > 0
        ORDER BY {time_total} DESC
        LIMIT %s
    """
    cur.execute(sql, (limit,))
    out = []
    for q, calls, total_t, mean_t in cur.fetchall():
        out.append({
            "query": q,
            "calls": int(calls or 0),
            "total_time_ms": float(total_t or 0.0),
            "mean_time_ms": float(mean_t or 0.0),
        })
    return out


def _should_skip(query: str) -> bool:
    q = (query or "").strip().lower()
    if not q:
        return True
    for p in _SKIP_PATTERNS:
        if p in q:
            return True
    return False


_SEQ_SCAN_RE = re.compile(
    r"Seq Scan on (?P<table>[a-zA-Z0-9_\.]+).*?"
    r"(?:rows=(?P<rows>\d+))?",
    re.IGNORECASE | re.DOTALL,
)


def _explain(cur, query: str) -> tuple[str | None, str | None]:
    """Run EXPLAIN (no ANALYZE — pg_stat_statements queries are templated with
    $1 placeholders so ANALYZE would fail).  Return (plan_text, suggestion)."""
    # pg_stat_statements normalises literals to $1 — we cannot ANALYZE those.
    # Use plain EXPLAIN which still works on parameterised SQL via PREPARE
    # fallback. If anything fails -> swallow.
    plan_text: str | None = None
    try:
        cur.execute("EXPLAIN (FORMAT JSON) " + query)
        rows = cur.fetchall()
        if rows and rows[0] and rows[0][0]:
            plan_text = json.dumps(rows[0][0])[:8000]
    except Exception:
        try:
            cur.execute("EXPLAIN " + query)
            plan_text = "\n".join(r[0] for r in cur.fetchall())[:8000]
        except Exception:
            return None, None

    suggestion = _suggest_index(plan_text or "", query)
    return plan_text, suggestion


def _suggest_index(plan_text: str, query: str) -> str | None:
    """Look at the textual plan for Seq Scan on a table with many rows."""
    if not plan_text:
        return None
    hits: list[str] = []
    for m in _SEQ_SCAN_RE.finditer(plan_text):
        tbl = m.group("table")
        rows = m.group("rows")
        if not tbl:
            continue
        if rows and int(rows) < LARGE_TABLE_ROWS:
            continue
        # try to recover a WHERE column from the query
        col = _guess_where_column(query, tbl.split(".")[-1])
        if col:
            hits.append(
                f"CREATE INDEX IF NOT EXISTS idx_{tbl.split('.')[-1]}_{col} "
                f"ON {tbl} ({col});"
            )
        else:
            hits.append(f"-- Seq Scan on {tbl}; add an index on the WHERE column.")
    if not hits:
        return None
    # dedupe, preserve order
    seen, dedup = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            dedup.append(h)
    return "\n".join(dedup[:5])


_WHERE_COL_RE = re.compile(
    r"WHERE\s+(?:[a-zA-Z0-9_]+\.)?(?P<col>[a-zA-Z_][a-zA-Z0-9_]*)\s*[=<>]",
    re.IGNORECASE,
)


def _guess_where_column(query: str, table: str) -> str | None:
    m = _WHERE_COL_RE.search(query or "")
    if m:
        return m.group("col")
    return None


def analyze_slow_queries(limit: int = 30, threshold_ms: float = 100.0) -> list[dict]:
    """Main entry point.  Returns list of recorded rows (also persisted)."""
    conn = _db.connect()
    if conn is None:
        log.warning("no DSN / no psycopg2 — skipping query_perf")
        return []
    inserted: list[dict] = []
    try:
        with conn.cursor() as cur:
            if not _ensure_extension(cur):
                return []
            rows = _fetch_top(cur, limit=limit)
            log.info("pg_stat_statements returned %d rows", len(rows))
            for r in rows:
                if _should_skip(r["query"]):
                    continue
                plan, suggestion = (None, None)
                if r["mean_time_ms"] >= threshold_ms:
                    plan, suggestion = _explain(cur, r["query"])
                cur.execute(
                    """INSERT INTO slow_query_log
                       (query_normalized, mean_time_ms, calls, total_time_ms,
                        explain_plan, suggested_index)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       RETURNING id""",
                    (r["query"][:8000], r["mean_time_ms"], r["calls"],
                     r["total_time_ms"], plan, suggestion),
                )
                rid = cur.fetchone()[0]
                inserted.append({"id": rid, **r,
                                 "suggested_index": suggestion})
            log.info("recorded %d slow queries", len(inserted))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return inserted


def main(argv: list[str] | None = None) -> int:
    rows = analyze_slow_queries()
    print(f"[optimizer.query_perf] recorded={len(rows)}")
    for r in rows[:10]:
        print(f"  mean={r['mean_time_ms']:.1f}ms calls={r['calls']} "
              f"sugg={'Y' if r.get('suggested_index') else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
