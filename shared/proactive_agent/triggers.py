"""
shared.proactive_agent.triggers — CRUD + evaluation for proactive triggers.

Trigger types (built-in):
  - new_listing_match   condition={"area": "JVC", "bedrooms": 2, "price_max": 1200000, "lookback_days": 30}
  - price_drop          condition={"area": "JVC", "min_drop_pct": 10, "bedrooms": 2}
  - roi_change          condition={"building_id": 42, "min_change_pct": 15}
  - reengagement        condition={"inactive_days": 14}
  - custom              condition={"sql": "<read-only SELECT>", "params": []}  (admin only)
"""
from __future__ import annotations
import json
import sys
from typing import Any, Dict, List, Optional

from ._db import get_conn


ALLOWED_TYPES = {
    "new_listing_match", "price_drop", "roi_change",
    "reengagement", "custom",
}


def register_trigger(user_id: int, bot_name: str, trigger_type: str,
                     condition: Dict[str, Any],
                     cooldown_hours: int = 24,
                     expires_days: Optional[int] = 90) -> Optional[int]:
    """Insert a new trigger. Returns its id (or None on failure)."""
    if trigger_type not in ALLOWED_TYPES:
        return None
    conn = get_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO proactive_triggers
                  (user_id, bot_name, trigger_type, condition_jsonb,
                   cooldown_hours, expires_at)
                VALUES (%s, %s, %s, %s::jsonb, %s,
                        CASE WHEN %s IS NULL THEN NULL
                             ELSE NOW() + (%s || ' days')::interval END)
                RETURNING id
            """, (user_id, bot_name, trigger_type, json.dumps(condition),
                  cooldown_hours, expires_days, expires_days))
            return cur.fetchone()[0]
    except Exception as e:
        sys.stderr.write(f"[triggers.register] err: {e}\n")
        return None


def list_triggers(user_id: Optional[int] = None,
                  bot_name: Optional[str] = None,
                  enabled_only: bool = True) -> List[Dict[str, Any]]:
    conn = get_conn()
    if not conn:
        return []
    try:
        where = []
        params: List[Any] = []
        if enabled_only:
            where.append("enabled = TRUE")
            where.append("(expires_at IS NULL OR expires_at > NOW())")
        if user_id is not None:
            where.append("user_id = %s")
            params.append(user_id)
        if bot_name:
            where.append("bot_name = %s")
            params.append(bot_name)
        sql = ("SELECT id, user_id, bot_name, trigger_type, condition_jsonb, "
               "cooldown_hours, last_fired, fire_count, expires_at "
               "FROM proactive_triggers")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_fired NULLS FIRST, id"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [
            {"id": r[0], "user_id": r[1], "bot_name": r[2],
             "trigger_type": r[3], "condition": r[4] or {},
             "cooldown_hours": r[5], "last_fired": r[6],
             "fire_count": r[7], "expires_at": r[8]}
            for r in rows
        ]
    except Exception as e:
        sys.stderr.write(f"[triggers.list] err: {e}\n")
        return []


def disable_trigger(trigger_id: int) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE proactive_triggers SET enabled = FALSE, "
                "updated_at = NOW() WHERE id = %s", (trigger_id,))
        return True
    except Exception:
        return False


def mark_fired(trigger_id: int) -> None:
    conn = get_conn()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE proactive_triggers
                SET last_fired = NOW(), fire_count = fire_count + 1,
                    updated_at = NOW()
                WHERE id = %s
            """, (trigger_id,))
    except Exception:
        pass


# ───────────────────────── condition evaluators ─────────────────────────

def _eval_new_listing_match(cur, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Probe resale.listings for new matches.

    Conservative: returns at most 1 listing per evaluation. Schema-tolerant
    (handles missing optional cols).
    """
    cond = trigger.get("condition") or {}
    area = (cond.get("area") or "").strip()
    bedrooms = cond.get("bedrooms")
    price_max = cond.get("price_max")
    price_min = cond.get("price_min")
    lookback = int(cond.get("since_hours", 24))

    # Look for a `listings` or `resale_listings` table — schema-tolerant
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('listings','resale_listings','classifieds')
        LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        return None
    tbl = row[0]

    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
    """, (tbl,))
    cols = {r[0] for r in cur.fetchall()}
    needed = {"id", "area", "bedrooms", "price"}
    if not needed.issubset(cols):
        return None
    ts_col = "created_at" if "created_at" in cols else (
        "first_seen_at" if "first_seen_at" in cols else None)
    if not ts_col:
        return None

    where = [f"{ts_col} > NOW() - INTERVAL %s"]
    params: List[Any] = [f"{lookback} hours"]
    if area:
        where.append("LOWER(area) = LOWER(%s)")
        params.append(area)
    if bedrooms is not None:
        where.append("bedrooms = %s")
        params.append(int(bedrooms))
    if price_max is not None:
        where.append("price <= %s")
        params.append(float(price_max))
    if price_min is not None:
        where.append("price >= %s")
        params.append(float(price_min))

    sql = (f"SELECT id, area, bedrooms, price FROM {tbl} "
           f"WHERE " + " AND ".join(where) +
           f" ORDER BY {ts_col} DESC LIMIT 1")
    try:
        cur.execute(sql, params)
        r = cur.fetchone()
        if not r:
            return None
        return {"listing_id": r[0], "area": r[1],
                "bedrooms": r[2], "price": float(r[3])}
    except Exception:
        return None


def _eval_reengagement(cur, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cond = trigger.get("condition") or {}
    inactive_days = int(cond.get("inactive_days", 14))
    uid = trigger["user_id"]
    bot = trigger["bot_name"]
    cur.execute("""
        SELECT MAX(ts) FROM bot_interactions
        WHERE user_id = %s AND bot_name = %s
    """, (uid, bot))
    r = cur.fetchone()
    last_ts = r[0] if r else None
    if not last_ts:
        return None
    cur.execute("""
        SELECT EXTRACT(EPOCH FROM (NOW() - %s))/86400.0
    """, (last_ts,))
    days = float(cur.fetchone()[0] or 0)
    if days < inactive_days:
        return None
    return {"inactive_days": int(days), "last_seen": str(last_ts)}


def _eval_price_drop(cur, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Detect price drops in saved areas (uses listings/price_history if present)."""
    cond = trigger.get("condition") or {}
    min_drop = float(cond.get("min_drop_pct", 10))
    area = (cond.get("area") or "").strip()
    # Try a price_history table; otherwise fall back to listings.previous_price
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_name='listing_price_history'
    """)
    has_hist = cur.fetchone() is not None
    if has_hist:
        sql = """
          SELECT listing_id, old_price, new_price,
                 ROUND(100.0*(old_price-new_price)/NULLIF(old_price,0),1) AS pct
          FROM listing_price_history
          WHERE changed_at > NOW() - INTERVAL '24 hours'
            AND old_price > new_price
            AND (%s = '' OR LOWER(area) = LOWER(%s))
          ORDER BY pct DESC LIMIT 1
        """
        try:
            cur.execute(sql, (area, area))
            r = cur.fetchone()
            if r and float(r[3] or 0) >= min_drop:
                return {"listing_id": r[0], "old_price": float(r[1]),
                        "new_price": float(r[2]), "drop_pct": float(r[3])}
        except Exception:
            return None
    return None


def _eval_roi_change(cur, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reads building_roi_history if present."""
    cond = trigger.get("condition") or {}
    bld = cond.get("building_id")
    min_pct = float(cond.get("min_change_pct", 15))
    if not bld:
        return None
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_name='building_roi_history'
    """)
    if not cur.fetchone():
        return None
    try:
        cur.execute("""
            SELECT roi_pct, ts FROM building_roi_history
            WHERE building_id = %s
            ORDER BY ts DESC LIMIT 2
        """, (bld,))
        rows = cur.fetchall()
        if len(rows) < 2:
            return None
        new_roi, old_roi = float(rows[0][0]), float(rows[1][0])
        if old_roi == 0:
            return None
        pct = abs((new_roi - old_roi) / old_roi) * 100
        if pct < min_pct:
            return None
        return {"building_id": bld, "old_roi": old_roi,
                "new_roi": new_roi, "change_pct": round(pct, 1)}
    except Exception:
        return None


EVALUATORS = {
    "new_listing_match": _eval_new_listing_match,
    "reengagement": _eval_reengagement,
    "price_drop": _eval_price_drop,
    "roi_change": _eval_roi_change,
}


def evaluate(trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate one trigger. Returns payload dict if matched, else None."""
    conn = get_conn()
    if not conn:
        return None
    ttype = trigger.get("trigger_type")
    fn = EVALUATORS.get(ttype)
    if not fn:
        return None
    try:
        with conn.cursor() as cur:
            return fn(cur, trigger)
    except Exception as e:
        sys.stderr.write(f"[triggers.evaluate {ttype}] err: {e}\n")
        return None
