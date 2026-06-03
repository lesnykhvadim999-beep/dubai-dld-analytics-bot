"""
Layer 21 — Daily scan (cron 02:00).

Берёт открытые bug_kb entries и свежие auto_audit alerts,
для каждого вызывает propose_fix → run_sandbox → request_approval.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import psycopg2
import psycopg2.extras

from .proposer import propose_fix
from .sandbox import run_sandbox
from .approval import request_approval

log = logging.getLogger("self_modify.daily_scan")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)
MAX_PROPOSALS_PER_RUN = int(os.getenv("SELF_MODIFY_MAX_PER_RUN", "3"))


def _fetch_candidates() -> List[Dict[str, Any]]:
    """Толерантный сборщик: пробует bug_kb, потом auto_audit_alerts."""
    out: List[Dict[str, Any]] = []
    since = datetime.now(timezone.utc) - timedelta(days=2)
    try:
        with psycopg2.connect(DSN, connect_timeout=8) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                try:
                    cur.execute(
                        "SELECT id::text AS bug_id, title, description, "
                        "target_repo, target_files "
                        "FROM bug_kb WHERE status='open' "
                        "ORDER BY severity DESC, created_at DESC LIMIT %s",
                        (MAX_PROPOSALS_PER_RUN,),
                    )
                    for r in cur.fetchall():
                        d = dict(r)
                        d["source"] = "bug_kb"
                        out.append(d)
                except Exception as e:
                    log.info("bug_kb skipped: %s", e); c.rollback()
                if len(out) < MAX_PROPOSALS_PER_RUN:
                    try:
                        cur.execute(
                            "SELECT id AS alert_id, title, details AS description, "
                            "target_repo, target_files "
                            "FROM auto_audit_alerts "
                            "WHERE resolved_at IS NULL AND created_at > %s "
                            "ORDER BY severity DESC, created_at DESC LIMIT %s",
                            (since, MAX_PROPOSALS_PER_RUN - len(out)),
                        )
                        for r in cur.fetchall():
                            d = dict(r); d["source"] = "auto_audit"
                            out.append(d)
                    except Exception as e:
                        log.info("auto_audit skipped: %s", e)
    except Exception as e:
        log.warning("fetch_candidates failed: %s", e)
    return out


def run_daily_scan() -> Dict[str, Any]:
    cands = _fetch_candidates()
    stats = {"candidates": len(cands), "proposed": 0, "sandbox_ok": 0, "requested": 0}
    for c in cands:
        try:
            files = c.get("target_files") or []
            if isinstance(files, str):
                import json
                try:
                    files = json.loads(files)
                except Exception:
                    files = [files]
            pid = propose_fix(
                bug_id=c.get("bug_id"),
                alert_id=c.get("alert_id"),
                title=c.get("title") or "auto-patch",
                bug_description=c.get("description") or "",
                target_repo=c.get("target_repo") or "shared",
                target_files=files,
            )
            if not pid:
                continue
            stats["proposed"] += 1
            sb = run_sandbox(pid)
            if sb.get("ok"):
                stats["sandbox_ok"] += 1
                if request_approval(pid):
                    stats["requested"] += 1
        except Exception as e:  # noqa: BLE001
            log.exception("candidate failed: %s", e)
    log.info("daily_scan stats=%s", stats)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_daily_scan())
