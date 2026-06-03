"""
Layer 19 — Weekly Top-10 resale deals.

Берёт топ-10 объектов из resale-БД (read-model), картинку через Pollinations,
ставит в approval queue.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from ._llm import chat
from ._assets import pollinations_image
from ._queue import enqueue

log = logging.getLogger("content_pipeline.weekly")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)
TARGET_CHAT = os.getenv("CHANNEL_TARGET", "@vadim_realty_channel")


def _fetch_top_deals(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Толерантно: пробуем несколько возможных view/таблиц. Никогда не падаем.
    """
    candidates = [
        ("SELECT listing_id, area, bedrooms, price_aed, size_sqft, "
         "url, posted_at FROM resale_top_deals_weekly "
         "ORDER BY score DESC LIMIT %s", (limit,)),
        ("SELECT id::text AS listing_id, area, bedrooms, price_aed, size_sqft, "
         "url, created_at AS posted_at FROM resale_listings "
         "WHERE created_at > NOW() - INTERVAL '7 days' "
         "AND price_aed IS NOT NULL AND area IS NOT NULL "
         "ORDER BY (price_aed::float / NULLIF(size_sqft,0)) ASC LIMIT %s", (limit,)),
    ]
    try:
        with psycopg2.connect(DSN, connect_timeout=8) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                for sql, params in candidates:
                    try:
                        cur.execute(sql, params)
                        rows = [dict(r) for r in cur.fetchall()]
                        if rows:
                            return rows
                    except Exception as e:
                        log.debug("candidate query skipped: %s", e)
                        c.rollback()
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_top_deals failed: %s", e)
    return []


def _format_deal(i: int, d: Dict[str, Any]) -> str:
    price = d.get("price_aed")
    size = d.get("size_sqft")
    ppsf = f" ({int(price/size):,} AED/sqft)" if price and size else ""
    return (
        f"{i}. *{d.get('area','?')}* · {d.get('bedrooms','?')}BR · "
        f"{int(price):,} AED{ppsf}"
        + (f" · [link]({d.get('url')})" if d.get("url") else "")
    )


def generate_weekly_top_deals(target_chat: Optional[str] = None) -> Optional[int]:
    deals = _fetch_top_deals(10)
    if not deals:
        log.warning("weekly_top_deals: no data, post skipped")
        return None
    week = datetime.now(timezone.utc).strftime("%V/%G")
    listing = "\n".join(_format_deal(i + 1, d) for i, d in enumerate(deals))
    prompt = (
        f"Сделай вступление (2-3 предложения) для еженедельного дайджеста "
        f"топ-10 ресейл-сделок Дубая (неделя {week}). Дальше будет список. "
        "Заверши краткой подписью 'Vadim Realty · RERA BRN 65011'. Без эмодзи-перебора."
    )
    intro = chat(prompt, max_tokens=250, temperature=0.5) or "🏙 Топ-10 ресейл-сделок недели:"
    body = f"{intro}\n\n{listing}\n\n_Vadim Realty · RERA BRN 65011_"

    img = pollinations_image(
        "Dubai resale property collage, premium apartments, marina view, editorial photo",
        width=1280, height=720, seed=int(datetime.now().strftime("%Y%V")),
    )
    qid = enqueue(
        kind="weekly_top",
        target_chat=target_chat or TARGET_CHAT,
        body_md=body,
        title=f"Weekly top-10 {week}",
        media_paths=[img] if img else [],
        payload={"count": len(deals), "deals": deals},
    )
    log.info("weekly_top_deals enqueued id=%s n=%s", qid, len(deals))
    return qid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("queue_id =", generate_weekly_top_deals())
