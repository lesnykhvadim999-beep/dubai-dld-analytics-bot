"""PHASE BN N4 — Audit runner.

`run_audit(audit_type)`:
  1. Reads enabled checks for `audit_type` from `audit_check_registry`
     (falls back to AUDIT_TYPE_DEFAULTS if registry empty).
  2. Inserts a row into `audit_runs` (status='running').
  3. Executes checks; collects findings.
  4. Persists every finding into `audit_findings`.
  5. Updates registry (last_run_at, last_finding_at).
  6. Closes audit_runs (status='ok'/'failed', findings_count).
  7. CRITICAL / HIGH findings → admin_notify().

CLI: python -m shared.autonomous_audit.runner --type daily
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Ensure shared/ is on sys.path when run as standalone script.
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_PROJECT = _SHARED.parent
for p in (str(_PROJECT), str(_SHARED)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    import psycopg2  # type: ignore
    _PG_OK = True
except Exception:
    _PG_OK = False

try:
    from admin_notify import admin_notify  # type: ignore
except Exception:
    def admin_notify(text: str, **kw) -> bool:  # type: ignore
        print(f"[admin_notify-stub] {text}")
        return False

from autonomous_audit.checks import (  # noqa: E402
    CHECKS, AUDIT_TYPE_DEFAULTS, Finding,
)

log = logging.getLogger("autonomous_audit.runner")


# ── DSN ───────────────────────────────────────────────────────────────────
def _dsn() -> str:
    """DSN env vars only — no hard-coded URIs."""
    for k in ("RESALE_DATABASE_URL_PUBLIC", "RESALE_DATABASE_URL",
              "INTELLIGENCE_DATABASE_URL_PUBLIC", "INTELLIGENCE_DATABASE_URL",
              "LIVE_DATABASE_URL_PUBLIC", "LIVE_DATABASE_URL",
              "DATABASE_URL"):
        v = os.environ.get(k)
        if v:
            return v
    return ""


# ── Schema ────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.audit_runs (
  id BIGSERIAL PRIMARY KEY,
  audit_type     TEXT NOT NULL,
  started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at    TIMESTAMPTZ,
  status         TEXT,
  findings_count INT DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_runs_type_started_idx
  ON public.audit_runs(audit_type, started_at DESC);

CREATE TABLE IF NOT EXISTS public.audit_findings (
  id BIGSERIAL PRIMARY KEY,
  audit_run_id   BIGINT REFERENCES public.audit_runs(id) ON DELETE CASCADE,
  severity       TEXT,
  category       TEXT,
  description    TEXT,
  affected_files TEXT[],
  discovered_at  TIMESTAMPTZ DEFAULT NOW(),
  resolved_at    TIMESTAMPTZ,
  escalated      BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS audit_findings_run_idx
  ON public.audit_findings(audit_run_id);
CREATE INDEX IF NOT EXISTS audit_findings_severity_idx
  ON public.audit_findings(severity, discovered_at DESC);

CREATE TABLE IF NOT EXISTS public.audit_check_registry (
  check_name      TEXT PRIMARY KEY,
  audit_type      TEXT,
  enabled         BOOLEAN DEFAULT TRUE,
  last_run_at     TIMESTAMPTZ,
  last_finding_at TIMESTAMPTZ
);
"""


def ensure_schema(dsn: str | None = None) -> bool:
    d = dsn or _dsn()
    if not d or not _PG_OK:
        return False
    try:
        with psycopg2.connect(d, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
        return True
    except Exception as e:
        log.error("ensure_schema failed: %s", e)
        return False


def seed_registry(dsn: str | None = None) -> int:
    """Insert default check_name → audit_type rows (idempotent).

    For checks listed in multiple audit_types, the *narrowest* (most frequent
    = hourly < daily < weekly) wins, because the registry has one row per
    check. We map each check_name to the highest-frequency audit_type that
    includes it.
    """
    d = dsn or _dsn()
    if not d or not _PG_OK:
        return 0
    # priority: hourly > daily > weekly
    priority = ["hourly", "daily", "weekly"]
    best: dict[str, str] = {}
    for tier in priority:
        for chk in AUDIT_TYPE_DEFAULTS.get(tier, []):
            best.setdefault(chk, tier)
    n = 0
    try:
        with psycopg2.connect(d, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for chk, tier in best.items():
                    cur.execute("""
                        INSERT INTO public.audit_check_registry
                            (check_name, audit_type, enabled)
                        VALUES (%s, %s, TRUE)
                        ON CONFLICT (check_name) DO UPDATE
                            SET audit_type = EXCLUDED.audit_type
                    """, (chk, tier))
                    n += 1
    except Exception as e:
        log.error("seed_registry failed: %s", e)
    return n


# ── Run helpers ───────────────────────────────────────────────────────────
def _enabled_checks(dsn: str, audit_type: str) -> list[str]:
    """Pull enabled checks from registry; fall back to defaults."""
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                # weekly includes daily/hourly; daily includes hourly.
                if audit_type == "weekly":
                    cur.execute("""
                        SELECT check_name FROM public.audit_check_registry
                        WHERE enabled = TRUE
                    """)
                elif audit_type == "daily":
                    cur.execute("""
                        SELECT check_name FROM public.audit_check_registry
                        WHERE enabled = TRUE AND audit_type IN ('hourly','daily')
                    """)
                else:  # hourly
                    cur.execute("""
                        SELECT check_name FROM public.audit_check_registry
                        WHERE enabled = TRUE AND audit_type = 'hourly'
                    """)
                rows = cur.fetchall()
                if rows:
                    return [r[0] for r in rows]
    except Exception as e:
        log.warning("_enabled_checks fallback: %s", e)
    return list(AUDIT_TYPE_DEFAULTS.get(audit_type, []))


def _open_run(dsn: str, audit_type: str) -> int | None:
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.audit_runs
                        (audit_type, started_at, status)
                    VALUES (%s, NOW(), 'running')
                    RETURNING id
                """, (audit_type,))
                row = cur.fetchone()
                return int(row[0]) if row else None
    except Exception as e:
        log.error("_open_run failed: %s", e)
        return None


def _close_run(dsn: str, run_id: int, status: str, count: int) -> None:
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE public.audit_runs
                    SET finished_at = NOW(),
                        status = %s,
                        findings_count = %s
                    WHERE id = %s
                """, (status, count, run_id))
    except Exception as e:
        log.error("_close_run failed: %s", e)


def _insert_finding(dsn: str, run_id: int, f: Finding) -> None:
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO public.audit_findings
                        (audit_run_id, severity, category,
                         description, affected_files, escalated)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    run_id,
                    (f.get("severity") or "LOW")[:20],
                    (f.get("category") or "misc")[:80],
                    (f.get("description") or "")[:2000],
                    list(f.get("affected_files") or [])[:50],
                    (f.get("severity") or "").upper() in ("CRITICAL", "HIGH"),
                ))
    except Exception as e:
        log.error("_insert_finding failed: %s", e)


def _stamp_registry(dsn: str, check_name: str, had_finding: bool) -> None:
    try:
        with psycopg2.connect(dsn, connect_timeout=8) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                if had_finding:
                    cur.execute("""
                        UPDATE public.audit_check_registry
                        SET last_run_at = NOW(),
                            last_finding_at = NOW()
                        WHERE check_name = %s
                    """, (check_name,))
                else:
                    cur.execute("""
                        UPDATE public.audit_check_registry
                        SET last_run_at = NOW()
                        WHERE check_name = %s
                    """, (check_name,))
    except Exception as e:
        log.warning("_stamp_registry(%s) failed: %s", check_name, e)


# ── Public API ────────────────────────────────────────────────────────────
def run_audit(audit_type: str = "daily") -> dict:
    """Execute checks for `audit_type`. Returns summary dict.

    Summary keys: run_id, audit_type, findings_count, critical, high,
                  by_category, status, elapsed_sec.
    """
    started = time.time()
    if audit_type not in ("hourly", "daily", "weekly"):
        raise ValueError(f"unknown audit_type {audit_type!r}")
    dsn = _dsn()
    if not dsn:
        return {"status": "no_dsn", "findings_count": 0,
                "audit_type": audit_type}
    if not _PG_OK:
        return {"status": "no_psycopg2", "findings_count": 0,
                "audit_type": audit_type}

    ensure_schema(dsn)
    # seed if registry is empty
    seed_registry(dsn)

    run_id = _open_run(dsn, audit_type)
    if run_id is None:
        return {"status": "open_failed", "findings_count": 0,
                "audit_type": audit_type}

    enabled = _enabled_checks(dsn, audit_type)
    all_findings: list[Finding] = []
    by_category: dict[str, int] = {}
    crit = high = 0
    for chk_name in enabled:
        fn = CHECKS.get(chk_name)
        if fn is None:
            log.warning("unknown check in registry: %s", chk_name)
            continue
        chk_findings: list[Finding] = []
        try:
            chk_findings = list(fn(dsn) or [])
        except Exception as e:
            chk_findings = [{
                "severity": "LOW", "category": "runner",
                "description": f"check {chk_name} crashed: {e}",
                "affected_files": [],
            }]
            log.error("check %s crashed: %s\n%s",
                      chk_name, e, traceback.format_exc()[-400:])

        for f in chk_findings:
            _insert_finding(dsn, run_id, f)
            all_findings.append(f)
            sev = (f.get("severity") or "").upper()
            cat = f.get("category") or "misc"
            by_category[cat] = by_category.get(cat, 0) + 1
            if sev == "CRITICAL":
                crit += 1
            elif sev == "HIGH":
                high += 1
        _stamp_registry(dsn, chk_name, bool(chk_findings))

    status = "ok" if (crit + high) == 0 else "issues"
    _close_run(dsn, run_id, status, len(all_findings))

    # ── escalate CRITICAL / HIGH ──
    if crit or high:
        head = (
            f"<b>🚨 Audit {audit_type} — issues found</b>\n"
            f"run #{run_id}\n"
            f"CRITICAL: <b>{crit}</b>   HIGH: <b>{high}</b>   "
            f"total: <b>{len(all_findings)}</b>\n\n"
        )
        body_lines: list[str] = []
        for f in all_findings:
            if (f.get("severity") or "").upper() not in ("CRITICAL", "HIGH"):
                continue
            body_lines.append(
                f"• <b>{f.get('severity')}</b> [{f.get('category')}] "
                f"{(f.get('description') or '')[:180]}"
            )
            if len(body_lines) >= 20:
                body_lines.append(f"… (+{crit + high - 20} more)")
                break
        try:
            admin_notify(head + "\n".join(body_lines),
                         parse_mode="HTML", priority="critical")
        except Exception as e:
            log.warning("admin_notify failed: %s", e)

    summary = {
        "run_id": run_id,
        "audit_type": audit_type,
        "findings_count": len(all_findings),
        "critical": crit,
        "high": high,
        "by_category": by_category,
        "status": status,
        "elapsed_sec": round(time.time() - started, 2),
    }
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--type", default="daily",
                   choices=["hourly", "daily", "weekly"])
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    summary = run_audit(args.type)
    print(f"[autonomous_audit] {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
