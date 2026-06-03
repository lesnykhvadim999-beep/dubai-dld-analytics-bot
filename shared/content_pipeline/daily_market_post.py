"""
Layer 19 — Daily market post.

Подтягивает данные:
  • market_world_model (PHASE BL Level 13) — общая температура рынка
  • external_signals (PHASE BL Level 16) — новости / макро
Генерирует пост через Cerebras, картинку через Pollinations,
ставит в content_pipeline_queue со статусом pending.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras

from ._llm import chat
from ._assets import pollinations_image
from ._queue import enqueue

log = logging.getLogger("content_pipeline.daily")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)
TARGET_CHAT = os.getenv("CHANNEL_TARGET", "@vadim_realty_channel")


def _fetch_market_snapshot() -> Dict[str, Any]:
    """Толерантно: если таблиц нет — вернём пустой dict."""
    snap: Dict[str, Any] = {}
    try:
        with psycopg2.connect(DSN, connect_timeout=8) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                try:
                    cur.execute(
                        "SELECT area, median_price_aed, deals_24h, trend_7d "
                        "FROM market_world_model "
                        "ORDER BY updated_at DESC NULLS LAST LIMIT 5"
                    )
                    snap["areas"] = [dict(r) for r in cur.fetchall()]
                except Exception:
                    snap["areas"] = []
                try:
                    since = datetime.now(timezone.utc) - timedelta(hours=24)
                    cur.execute(
                        "SELECT title, source, sentiment "
                        "FROM external_signals WHERE created_at > %s "
                        "ORDER BY created_at DESC LIMIT 5",
                        (since,),
                    )
                    snap["signals"] = [dict(r) for r in cur.fetchall()]
                except Exception:
                    snap["signals"] = []
    except Exception as e:  # noqa: BLE001
        log.warning("market snapshot fallback: %s", e)
    return snap


def _build_prompt(snap: Dict[str, Any]) -> str:
    areas_txt = "; ".join(
        f"{a.get('area')}: median {a.get('median_price_aed')} AED, deals {a.get('deals_24h')}"
        for a in (snap.get("areas") or [])[:5]
    ) or "нет свежих данных по районам"
    sig_txt = "; ".join(
        f"[{s.get('source')}] {s.get('title')} ({s.get('sentiment')})"
        for s in (snap.get("signals") or [])[:5]
    ) or "новостей за сутки нет"
    today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    return (
        f"Сегодня {today}. Напиши короткий пост (~120-160 слов) для Telegram-канала "
        f"о рынке недвижимости Дубая.\n\n"
        f"Данные по топ-районам: {areas_txt}\n"
        f"Внешние сигналы за сутки: {sig_txt}\n\n"
        f"Структура:\n"
        f"1) Хук — 1 фраза.\n"
        f"2) 3 буллета с цифрами/инсайтами.\n"
        f"3) Мини-вывод 'что это значит для покупателя/инвестора'.\n"
        f"4) Подпись: Vadim Realty · RERA BRN 65011.\n\n"
        f"Markdown, без эмодзи в каждой строке (макс 3 на пост)."
    )


def generate_daily_market_post(target_chat: Optional[str] = None) -> Optional[int]:
    """Создаёт pending-пост. Возвращает queue id или None."""
    snap = _fetch_market_snapshot()
    body = chat(_build_prompt(snap), max_tokens=600, temperature=0.55)
    if not body:
        log.error("daily_market_post: LLM вернул None")
        return None

    img_prompt = (
        "Dubai skyline at golden hour, Marina + Downtown, ultra-realistic, "
        "editorial real-estate cover, no text"
    )
    img = pollinations_image(img_prompt, width=1280, height=720,
                             seed=int(datetime.now().strftime("%Y%m%d")))

    qid = enqueue(
        kind="daily_market",
        target_chat=target_chat or TARGET_CHAT,
        body_md=body,
        title=f"Daily market {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        media_paths=[img] if img else [],
        payload={"snapshot": snap},
    )
    log.info("daily_market_post enqueued id=%s", qid)
    return qid


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("queue_id =", generate_daily_market_post())
