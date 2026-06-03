"""
shared.proactive_agent.scheduler — periodic trigger evaluator.

Runs every ~30 minutes via Windows Task Scheduler (see install_cron.ps1).
For each active trigger past its cooldown:
  1. evaluate condition
  2. if matched → format message → notifier.send_notification
  3. mark fired (regardless of delivery success — to respect cooldown)
"""
from __future__ import annotations
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from ._db import get_conn
from . import triggers as _triggers
from .notifier import send_notification


# ───────────────────────── message formatters ─────────────────────────

def _fmt_new_listing_match(t: Dict[str, Any], payload: Dict[str, Any],
                            lang: str = "ru") -> str:
    p = payload
    price = int(p.get("price", 0))
    area = p.get("area", "")
    br = p.get("bedrooms", "?")
    if lang.startswith("ru"):
        return (f"<b>Новое объявление по вашему запросу</b>\n"
                f"Район: <b>{area}</b>, спален: {br}\n"
                f"Цена: <b>{price:,} AED</b>\n\n"
                f"Vadim Realty (RERA BRN 65011)")
    return (f"<b>New listing matching your search</b>\n"
            f"Area: <b>{area}</b>, beds: {br}\n"
            f"Price: <b>{price:,} AED</b>\n\n"
            f"Vadim Realty (RERA BRN 65011)")


def _fmt_price_drop(t, p, lang="ru"):
    pct = p.get("drop_pct", 0)
    old_p = int(p.get("old_price", 0))
    new_p = int(p.get("new_price", 0))
    if lang.startswith("ru"):
        return (f"<b>Снижение цены −{pct}%</b>\n"
                f"Было: {old_p:,} → Сейчас: <b>{new_p:,} AED</b>\n\n"
                f"Vadim Realty (RERA BRN 65011)")
    return (f"<b>Price drop −{pct}%</b>\n"
            f"Was {old_p:,} → Now <b>{new_p:,} AED</b>\n\n"
            f"Vadim Realty (RERA BRN 65011)")


def _fmt_roi_change(t, p, lang="ru"):
    pct = p.get("change_pct", 0)
    old_r = p.get("old_roi", 0)
    new_r = p.get("new_roi", 0)
    if lang.startswith("ru"):
        return (f"<b>ROI здания изменился на {pct}%</b>\n"
                f"Было: {old_r}% → Стало: <b>{new_r}%</b>\n\n"
                f"Vadim Realty (RERA BRN 65011)")
    return (f"<b>Building ROI changed by {pct}%</b>\n"
            f"Was {old_r}% → Now <b>{new_r}%</b>\n\n"
            f"Vadim Realty (RERA BRN 65011)")


def _fmt_reengagement(t, p, lang="ru"):
    days = p.get("inactive_days", 14)
    if lang.startswith("ru"):
        return (f"Давно не общались — прошло {days} дн.\n"
                f"Если ещё интересна недвижимость в Дубае, я подобрал свежие варианты.\n\n"
                f"Vadim Realty (RERA BRN 65011)")
    return (f"It's been {days} days. If you're still looking at Dubai real estate, "
            f"I have fresh options.\n\nVadim Realty (RERA BRN 65011)")


FORMATTERS = {
    "new_listing_match": _fmt_new_listing_match,
    "price_drop": _fmt_price_drop,
    "roi_change": _fmt_roi_change,
    "reengagement": _fmt_reengagement,
}


def _user_language(user_id: int, bot_name: str) -> str:
    conn = get_conn()
    if not conn:
        return "ru"
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT language FROM user_profiles
                WHERE user_id = %s AND bot_name = %s
            """, (user_id, bot_name))
            r = cur.fetchone()
            if r and r[0]:
                return r[0]
            cur.execute("""
                SELECT language FROM bot_interactions
                WHERE user_id = %s AND bot_name = %s
                  AND language IS NOT NULL
                ORDER BY ts DESC LIMIT 1
            """, (user_id, bot_name))
            r = cur.fetchone()
            return (r[0] if r and r[0] else "ru")
    except Exception:
        return "ru"


def _start_run() -> Optional[int]:
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO proactive_scheduler_runs DEFAULT VALUES
                RETURNING id
            """)
            return cur.fetchone()[0]
    except Exception:
        return None


def _finish_run(run_id: Optional[int], stats: Dict[str, Any]) -> None:
    if run_id is None:
        return
    conn = get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE proactive_scheduler_runs
                SET finished_at = NOW(),
                    triggers_evaluated = %s,
                    triggers_matched = %s,
                    notifications_sent = %s,
                    notifications_blocked = %s,
                    errors = %s,
                    details = %s::jsonb
                WHERE id = %s
            """, (stats.get("evaluated", 0),
                  stats.get("matched", 0),
                  stats.get("sent", 0),
                  stats.get("blocked", 0),
                  stats.get("errors", 0),
                  json.dumps(stats), run_id))
    except Exception:
        pass


def _due_triggers(max_batch: int = 200) -> List[Dict[str, Any]]:
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, bot_name, trigger_type, condition_jsonb,
                       cooldown_hours, last_fired
                FROM proactive_triggers
                WHERE enabled = TRUE
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND (last_fired IS NULL
                       OR last_fired < NOW() - (cooldown_hours || ' hours')::interval)
                ORDER BY last_fired NULLS FIRST
                LIMIT %s
            """, (max_batch,))
            rows = cur.fetchall()
        return [
            {"id": r[0], "user_id": r[1], "bot_name": r[2],
             "trigger_type": r[3], "condition": r[4] or {},
             "cooldown_hours": r[5], "last_fired": r[6]}
            for r in rows
        ]
    except Exception as e:
        sys.stderr.write(f"[scheduler] due_triggers err: {e}\n")
        return []


def run_scheduler_tick(max_batch: int = 200,
                       dry_run: bool = False) -> Dict[str, Any]:
    """Single scheduler iteration. Returns stats dict."""
    stats = {"evaluated": 0, "matched": 0, "sent": 0,
             "blocked": 0, "errors": 0}
    run_id = _start_run()
    try:
        for tr in _due_triggers(max_batch):
            stats["evaluated"] += 1
            try:
                payload = _triggers.evaluate(tr)
                if not payload:
                    continue
                stats["matched"] += 1
                fmt = FORMATTERS.get(tr["trigger_type"])
                if not fmt:
                    continue
                lang = _user_language(tr["user_id"], tr["bot_name"])
                text = fmt(tr, payload, lang)
                res = send_notification(
                    user_id=tr["user_id"],
                    bot_name=tr["bot_name"],
                    text=text,
                    trigger_type=tr["trigger_type"],
                    trigger_id=tr["id"],
                    payload=payload,
                    language=lang,
                    dry_run=dry_run,
                )
                if res.get("sent") or res.get("status") == "dry_run":
                    stats["sent"] += 1
                else:
                    stats["blocked"] += 1
                # mark fired in all cases to respect cooldown
                _triggers.mark_fired(tr["id"])
            except Exception as e:
                stats["errors"] += 1
                sys.stderr.write(f"[scheduler] trigger {tr.get('id')} err: {e}\n")
    finally:
        _finish_run(run_id, stats)
    return stats


if __name__ == "__main__":
    dry = os.environ.get("PROACTIVE_DRY_RUN", "0") == "1"
    out = run_scheduler_tick(dry_run=dry)
    print(json.dumps(out, indent=2))
