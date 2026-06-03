"""
approval_handler — Telegram callback handler for `bpm:*` callbacks.

Use from admin-bot-handler:
    from pattern_mining.approval_handler import handle_callback
    text, ok = handle_callback(callback.data)
"""
from __future__ import annotations

import json
import logging

from ._db import conn_cursor
from .bot_pattern_applier import apply_discovery

log = logging.getLogger(__name__)


def _get_discovery(did: int) -> dict | None:
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute("SELECT * FROM bot_pattern_discoveries WHERE id=%s", (did,))
        return cur.fetchone()


def _mark_rejected(did: int) -> None:
    with conn_cursor() as (_, cur):
        cur.execute(
            "UPDATE bot_pattern_discoveries SET status='rejected' WHERE id=%s",
            (did,),
        )


def handle_callback(data: str) -> tuple[str, bool]:
    """Returns (answer_text, success)."""
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "bpm":
        return ("Invalid callback", False)
    action = parts[1]
    try:
        did = int(parts[2])
    except Exception:
        return ("Bad discovery id", False)

    row = _get_discovery(did)
    if not row:
        return (f"Discovery #{did} not found", False)

    if action == "approve":
        if row["status"] == "applied":
            return (f"#{did} already applied", True)
        try:
            with conn_cursor() as (_, cur):
                cur.execute(
                    "UPDATE bot_pattern_discoveries SET status='approved' WHERE id=%s AND status='pending'",
                    (did,),
                )
            result = apply_discovery(did)
            return (f"✅ Applied: {json.dumps(result, ensure_ascii=False)[:200]}", True)
        except Exception as exc:
            log.warning("apply failed: %s", exc)
            return (f"❌ Apply failed: {exc}", False)

    if action == "reject":
        _mark_rejected(did)
        return (f"❌ Rejected #{did}", True)

    if action == "more":
        # show samples
        ev = row.get("evidence_json") or []
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except Exception:
                ev = []
        try:
            page = int(parts[3]) if len(parts) > 3 else 1
        except Exception:
            page = 1
        start = (page - 1) * 10
        chunk = ev[start:start + 10]
        if not chunk:
            return ("No more samples", True)
        return ("\n".join(f"• {s[:150]}" for s in chunk), True)

    return (f"Unknown action: {action}", False)
