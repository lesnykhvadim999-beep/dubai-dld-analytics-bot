"""Daily DLD digest -> @vadim_admin_bot.

Sends a concise daily summary of Dubai DLD sale transactions for the most
recent day available in the archive (DLD typically lags real-time by a few
days/weeks; we anchor to MAX(instance_date)).

Includes:
  - deal count for the "yesterday" day + % vs the prior day
  - top-5 areas and top-5 buildings by deal count (top-1 in the message)
  - 1BR median price for the day + % vs the previous 7 days
  - up to 3 "hot deals": price <= 80% of the trailing-30d median for the
    same rooms bucket, restricted to premium areas (Burj Khalifa / Marina /
    Downtown / Hills)

Run once per day. Suggested cron: `0 5 * * *` (05:00 UTC = 09:00 Dubai).

Env:
  ARCHIVE_DATABASE_URL or DATABASE_URL - DLD archive Postgres DSN
  ADMIN_BOT_TOKEN                      - token of @vadim_admin_bot
  ADMIN_CHAT_ID                        - defaults to Vadim (353806371)
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from admin_notify import admin_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("daily_digest")


# Premium areas where we search for hot deals (substring match, case-insensitive).
PREMIUM_AREA_KEYWORDS = [
    "burj khalifa",
    "marina",
    "downtown",
    "hills",
]


# DB
def _conn():
    dsn = (
        os.environ.get("ARCHIVE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not dsn:
        raise RuntimeError("ARCHIVE_DATABASE_URL/DATABASE_URL not set")
    return psycopg2.connect(dsn, cursor_factory=RealDictCursor)


# Helpers
def _fmt_price(aed: Optional[float]) -> str:
    if aed is None:
        return "-"
    aed = float(aed)
    if aed >= 1_000_000:
        return f"{aed / 1_000_000:.2f}M"
    if aed >= 1_000:
        return f"{aed / 1_000:.0f}K"
    return f"{aed:.0f}"


def _fmt_pct(curr: Optional[float], prev: Optional[float]) -> str:
    if not prev or curr is None:
        return "n/a"
    try:
        p = (float(curr) - float(prev)) / float(prev) * 100.0
    except Exception:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"


def _date_iso(d) -> str:
    return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)


# Queries
def _latest_available_date(cur):
    """DLD archive lags behind today. Return the most recent instance_date
    that actually has data; the digest is anchored to it.
    """
    cur.execute(
        """
        SELECT MAX(instance_date) AS d
          FROM dld_sale_archive
         WHERE instance_date IS NOT NULL
        """
    )
    r = cur.fetchone()
    raw = (r or {}).get("d")
    if not raw:
        return None
    s = str(raw)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _deal_count(cur, day) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS n
          FROM dld_sale_archive
         WHERE instance_date = %s
        """,
        (_date_iso(day),),
    )
    r = cur.fetchone()
    return int((r or {}).get("n") or 0)


def _top_areas(cur, day, limit: int = 5) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            COALESCE(NULLIF(TRIM(area_name_en),''), 'Unknown') AS name,
            COUNT(*) AS n
          FROM dld_sale_archive
         WHERE instance_date = %s
         GROUP BY name
         ORDER BY n DESC
         LIMIT %s
        """,
        (_date_iso(day), limit),
    )
    return list(cur.fetchall())


def _top_buildings(cur, day, limit: int = 5) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            COALESCE(NULLIF(TRIM(building_name_en),''), 'Unknown') AS name,
            COUNT(*) AS n
          FROM dld_sale_archive
         WHERE instance_date = %s
           AND NULLIF(TRIM(building_name_en),'') IS NOT NULL
         GROUP BY name
         ORDER BY n DESC
         LIMIT %s
        """,
        (_date_iso(day), limit),
    )
    return list(cur.fetchall())


def _median_1br(cur, day_start, day_end_exclusive) -> Optional[float]:
    """Median actual_worth for 1BR sales over [day_start, day_end_exclusive)."""
    cur.execute(
        """
        SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (
                 ORDER BY actual_worth::numeric
               ) AS med
          FROM dld_sale_archive
         WHERE instance_date >= %s
           AND instance_date <  %s
           AND rooms_en ILIKE '%%1 B/R%%'
           AND actual_worth ~ '^[0-9.]+$'
           AND actual_worth::numeric > 100000
        """,
        (_date_iso(day_start), _date_iso(day_end_exclusive)),
    )
    r = cur.fetchone()
    med = (r or {}).get("med")
    return float(med) if med is not None else None


def _tier_medians(cur, day) -> Dict[str, float]:
    """Median actual_worth per rooms_en bucket over the trailing 30 days
    excluding the digest day itself. Used as the reference for hot-deal
    discounts on the digest day.
    """
    end = day  # exclusive
    start = day - timedelta(days=30)
    cur.execute(
        """
        SELECT
            rooms_en,
            PERCENTILE_CONT(0.5) WITHIN GROUP (
              ORDER BY actual_worth::numeric
            ) AS med
          FROM dld_sale_archive
         WHERE instance_date >= %s
           AND instance_date <  %s
           AND rooms_en IS NOT NULL
           AND actual_worth ~ '^[0-9.]+$'
           AND actual_worth::numeric > 100000
         GROUP BY rooms_en
        """,
        (_date_iso(start), _date_iso(end)),
    )
    out: Dict[str, float] = {}
    for r in cur.fetchall():
        rooms = (r.get("rooms_en") or "").strip()
        med = r.get("med")
        if rooms and med is not None:
            out[rooms] = float(med)
    return out


def _hot_deals(cur, day, tier_medians: Dict[str, float], limit: int = 3) -> List[Dict[str, Any]]:
    """Deals at >=20% below the rooms-bucket median, in premium areas only."""
    if not tier_medians:
        return []

    pairs = []
    for rooms, med in tier_medians.items():
        if med and med > 0:
            pairs.append((rooms, med * 0.8))
    if not pairs:
        return []

    # Premium-area ILIKE filter (matches area, master project, or building).
    area_clauses = []
    area_args: List[Any] = []
    for kw in PREMIUM_AREA_KEYWORDS:
        area_clauses.append(
            "(area_name_en ILIKE %s OR master_project_en ILIKE %s OR building_name_en ILIKE %s)"
        )
        kw_like = f"%{kw}%"
        area_args += [kw_like, kw_like, kw_like]
    area_filter = "(" + " OR ".join(area_clauses) + ")"

    rooms_list = [p[0] for p in pairs]
    case_parts = []
    case_args: List[Any] = []
    for rooms, thr in pairs:
        case_parts.append("WHEN rooms_en = %s THEN %s")
        case_args += [rooms, thr]
    threshold_case = "CASE " + " ".join(case_parts) + " ELSE NULL END"

    sql = f"""
        SELECT
            building_name_en,
            area_name_en,
            master_project_en,
            rooms_en,
            actual_worth::numeric AS price,
            {threshold_case}      AS threshold
          FROM dld_sale_archive
         WHERE instance_date = %s
           AND rooms_en = ANY(%s)
           AND actual_worth ~ '^[0-9.]+$'
           AND actual_worth::numeric > 100000
           AND {area_filter}
           AND actual_worth::numeric <= {threshold_case}
         ORDER BY (
            ({threshold_case}) - actual_worth::numeric
         ) DESC
         LIMIT %s
    """
    params: List[Any] = []
    params += case_args                   # SELECT threshold_case
    params += [_date_iso(day)]            # instance_date = %s
    params += [rooms_list]                # rooms_en = ANY(%s)
    params += area_args                   # area_filter ILIKEs
    params += case_args                   # WHERE threshold_case
    params += case_args                   # ORDER threshold_case
    params += [limit]                     # LIMIT

    cur.execute(sql, params)
    rows = cur.fetchall()
    out = []
    for r in rows:
        price = float(r["price"]) if r.get("price") is not None else None
        thr = float(r["threshold"]) if r.get("threshold") is not None else None
        if not price or not thr or thr <= 0:
            continue
        median = thr / 0.8  # back to true median
        discount_pct = (median - price) / median * 100.0
        out.append({
            "building": (r.get("building_name_en") or "").strip(),
            "area": (r.get("area_name_en") or r.get("master_project_en") or "").strip(),
            "rooms": (r.get("rooms_en") or "").strip(),
            "price": price,
            "discount_pct": discount_pct,
        })
    return out


# Message
def _build_message(stats: Dict[str, Any]) -> str:
    yesterday = stats["yesterday"]
    n_y = stats["deals_yesterday"]
    n_p = stats["deals_prev"]
    top_areas = stats["top_areas"]
    top_buildings = stats["top_buildings"]
    med_1br = stats["median_1br_yesterday"]
    med_1br_lw = stats["median_1br_last_week"]
    hot = stats["hot_deals"]

    deal_change = _fmt_pct(n_y, n_p)
    med_change = _fmt_pct(med_1br, med_1br_lw)

    top_area = top_areas[0] if top_areas else None
    top_bldg = top_buildings[0] if top_buildings else None

    lines: List[str] = []
    lines.append(f"\U0001F4CA <b>DLD сводка за {yesterday.strftime('%d.%m')}</b>")
    lines.append("")
    lines.append(f"\U0001F4BC Сделок: <b>{n_y}</b> ({deal_change} vs позавчера)")
    if top_area:
        lines.append(
            f"\U0001F3D8 Топ-район: <b>{top_area['name']}</b> ({top_area['n']} сделок)"
        )
    if top_bldg:
        lines.append(
            f"\U0001F3E2 Топ-здание: <b>{top_bldg['name']}</b> ({top_bldg['n']})"
        )
    lines.append("")
    if med_1br:
        lines.append(
            f"\U0001F4B0 Медиана 1BR: <b>{_fmt_price(med_1br)} AED</b> "
            f"({med_change} vs прошлая неделя)"
        )
    else:
        lines.append("\U0001F4B0 Медиана 1BR: -")
    lines.append("")

    if hot:
        lines.append(f"\U0001F525 <b>Hot deals ({len(hot)})</b>:")
        for i, h in enumerate(hot, 1):
            bldg = h["building"] or "-"
            area = h["area"] or "-"
            rooms = h["rooms"] or "-"
            price = _fmt_price(h["price"])
            disc = h["discount_pct"]
            lines.append(
                f"{i}. {bldg} {area} {rooms} - {price} AED (-{disc:.0f}%)"
            )
    else:
        lines.append("\U0001F525 <b>Hot deals</b>: ничего >=-20% сегодня")

    lines.append("")
    lines.append("\U0001F4C8 Полная аналитика - @dubai_analitik_sys")
    lines.append("")
    lines.append("— Vadim Realty · RERA BRN 65011")
    return "\n".join(lines)


# Orchestrator
def run() -> bool:
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Anchor to the latest day actually present in the archive.
                latest = _latest_available_date(cur)
                today_utc = datetime.now(timezone.utc).date()
                yesterday = latest or (today_utc - timedelta(days=1))
                prev = yesterday - timedelta(days=1)

                # "last week" for 1BR median = 7 days ending the day before
                # "yesterday" (no overlap).
                lw_end = yesterday                # exclusive
                lw_start = yesterday - timedelta(days=7)

                # 1BR median window for "yesterday" itself: [day, day+1).
                y_start = yesterday
                y_end = yesterday + timedelta(days=1)

                deals_y = _deal_count(cur, yesterday)
                deals_p = _deal_count(cur, prev)
                top_areas = _top_areas(cur, yesterday, limit=5)
                top_buildings = _top_buildings(cur, yesterday, limit=5)
                med_y = _median_1br(cur, y_start, y_end)
                med_lw = _median_1br(cur, lw_start, lw_end)
                tier_med = _tier_medians(cur, yesterday)
                hot = _hot_deals(cur, yesterday, tier_med, limit=3)

        stats = {
            "yesterday": yesterday,
            "deals_yesterday": deals_y,
            "deals_prev": deals_p,
            "top_areas": top_areas,
            "top_buildings": top_buildings,
            "median_1br_yesterday": med_y,
            "median_1br_last_week": med_lw,
            "hot_deals": hot,
        }
        msg = _build_message(stats)
        log.info(
            "digest built: day=%s deals_y=%s deals_p=%s med_1br=%s hot=%d",
            yesterday, deals_y, deals_p, med_y, len(hot),
        )
        ok = admin_notify(msg)
        log.info("admin_notify ok=%s", ok)
        return bool(ok)
    except Exception as e:
        log.exception("digest failed")
        try:
            admin_notify(f"⚠️ Digest failed: {e}")
        except Exception:
            pass
        return False


if __name__ == "__main__":
    import sys
    ok = run()
    sys.exit(0 if ok else 1)
