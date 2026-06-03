"""DB-обёртка для code_proposals."""
import os
import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("self_modify.db")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)


def conn():
    return psycopg2.connect(DSN, connect_timeout=8)


def insert_proposal(
    *, target_repo: str, target_files: List[str], title: str,
    rationale: str, proposed_diff: str,
    bug_id: Optional[str] = None, alert_id: Optional[int] = None,
) -> Optional[int]:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO code_proposals
                    (bug_id, alert_id, target_repo, target_files, title,
                     rationale, proposed_diff)
                VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s)
                RETURNING id
                """,
                (bug_id, alert_id, target_repo, json.dumps(target_files),
                 title, rationale, proposed_diff),
            )
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log.exception("insert_proposal failed: %s", e)
        return None


def set_sandbox_result(proposal_id: int, passed: bool, result: Dict[str, Any]) -> bool:
    new_status = "pending" if passed else "sandbox_failed"
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE code_proposals
                   SET sandbox_test_result=%s::jsonb,
                       sandbox_passed=%s,
                       status=%s
                 WHERE id=%s
                """,
                (json.dumps(result, ensure_ascii=False, default=str),
                 passed, new_status, proposal_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        log.exception("set_sandbox_result failed: %s", e)
        return False


def set_decision(proposal_id: int, *, approved: bool, admin_id: int) -> bool:
    status = "approved" if approved else "rejected"
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE code_proposals
                   SET status=%s, admin_decision_by=%s, admin_decision_at=NOW()
                 WHERE id=%s AND status='pending'
                """,
                (status, admin_id, proposal_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        log.exception("set_decision failed: %s", e)
        return False


def mark_applied(proposal_id: int, sha: str) -> bool:
    try:
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "UPDATE code_proposals SET status='applied', "
                "applied_commit_sha=%s, applied_at=NOW() WHERE id=%s",
                (sha, proposal_id),
            )
            return cur.rowcount > 0
    except Exception as e:  # noqa: BLE001
        log.exception("mark_applied failed: %s", e)
        return False


def list_by_status(status: str = "pending", limit: int = 20) -> List[Dict[str, Any]]:
    try:
        with conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM code_proposals WHERE status=%s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (status, limit),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.exception("list_by_status failed: %s", e)
        return []


def get_proposal(proposal_id: int) -> Optional[Dict[str, Any]]:
    try:
        with conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM code_proposals WHERE id=%s", (proposal_id,))
                row = cur.fetchone()
                return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        log.exception("get_proposal failed: %s", e)
        return None
