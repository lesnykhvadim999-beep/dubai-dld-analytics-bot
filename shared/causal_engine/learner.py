"""Weekly causal-pattern learner.

Pulls free news/blog RSS + a few public Telegram channels (via Telethon if
configured), asks the LLM to extract candidate causal patterns, and stores
them in causal_learning_log for review/auto-accept.

Run via cron Sunday 06:00.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Tuple

import psycopg2

logger = logging.getLogger(__name__)

DSN = os.getenv(
    "INTELLIGENCE_DB_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)

# Free RSS sources only (no paid sites)
RSS_FEEDS = [
    "https://www.arabianbusiness.com/feed",
    "https://gulfnews.com/rss",
    "https://www.propertyfinder.ae/blog/feed/",
    "https://www.zawya.com/en/rss/news/real-estate",
]

# Optional public Telegram channels (requires TELEGRAM_API_ID / API_HASH / SESSION)
TG_CHANNELS = [
    "@DubaiRealEstateMarket",
    "@dubaipropertynews",
]

AUTO_ACCEPT_CONF = 0.75


def _fetch_rss(url: str, limit: int = 10) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        import feedparser  # type: ignore
        f = feedparser.parse(url)
        for e in f.entries[:limit]:
            items.append({
                "title":   getattr(e, "title", "") or "",
                "summary": re.sub("<[^>]+>", " ", getattr(e, "summary", "") or "")[:1500],
                "link":    getattr(e, "link", "") or url,
            })
    except Exception as ex:
        logger.warning("rss fetch failed %s: %s", url, ex)
    return items


def _fetch_telegram(channel: str, limit: int = 20) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    api_id   = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    session  = os.getenv("TELEGRAM_SESSION_STRING")
    if not (api_id and api_hash and session):
        return items
    try:
        from telethon.sync import TelegramClient  # type: ignore
        from telethon.sessions import StringSession  # type: ignore
        with TelegramClient(StringSession(session), int(api_id), api_hash) as client:
            for msg in client.iter_messages(channel, limit=limit):
                if msg.text:
                    items.append({"title": "", "summary": msg.text[:1500], "link": f"https://t.me/{channel.strip('@')}/{msg.id}"})
    except Exception as ex:
        logger.warning("telegram fetch failed %s: %s", channel, ex)
    return items


def _llm_extract(excerpt: str) -> List[Dict[str, Any]]:
    """Ask LLM to extract causal pairs from a news excerpt."""
    try:
        from shared import llm_router  # type: ignore
        prompt = (
            "Extract causal relationships about Dubai real estate from this excerpt. "
            "Return STRICT JSON array, each item: "
            '{"cause":"...","effect":"...","confidence":0..1,"domain":"price|roi|rent|supply|demand|macro"}. '
            "If none, return [].\n\nExcerpt:\n" + excerpt[:1500]
        )
        text, _ = llm_router.complete(prompt, max_tokens=400)
        m = re.search(r"\[.*\]", text or "", re.S)
        if not m:
            return []
        arr = json.loads(m.group(0))
        return [x for x in arr if isinstance(x, dict) and x.get("cause") and x.get("effect")]
    except Exception as e:
        logger.warning("llm_extract failed: %s", e)
        return []


def _save_pending(url: str, excerpt: str, pairs: List[Dict[str, Any]], conn) -> Tuple[int, int]:
    """Insert pending; auto-accept high-confidence pairs into causal_facts."""
    pending = accepted = 0
    with conn.cursor() as cur:
        for p in pairs:
            try:
                cur.execute(
                    """INSERT INTO causal_learning_log
                       (source_url, raw_excerpt, proposed_cause, proposed_effect, llm_confidence, status)
                       VALUES (%s,%s,%s,%s,%s,%s)""",
                    (url, excerpt[:500], p["cause"], p["effect"],
                     float(p.get("confidence", 0.5)),
                     "accepted" if float(p.get("confidence", 0)) >= AUTO_ACCEPT_CONF else "pending"),
                )
                pending += 1
                if float(p.get("confidence", 0)) >= AUTO_ACCEPT_CONF:
                    cur.execute(
                        """INSERT INTO causal_facts
                           (cause_text, effect_text, evidence_source, confidence, domain, source_url)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (cause_text, effect_text) DO NOTHING""",
                        (p["cause"], p["effect"], "news", float(p["confidence"]),
                         p.get("domain"), url),
                    )
                    if cur.rowcount:
                        accepted += 1
            except Exception as e:
                logger.warning("save_pending row failed: %s", e)
    return pending, accepted


def run_weekly() -> Dict[str, int]:
    sources: List[Tuple[str, List[Dict[str, str]]]] = []
    for url in RSS_FEEDS:
        for item in _fetch_rss(url):
            sources.append((item["link"], [item]))
    for ch in TG_CHANNELS:
        for item in _fetch_telegram(ch):
            sources.append((item["link"], [item]))

    total_pending = total_accepted = 0
    try:
        with psycopg2.connect(DSN) as conn:
            conn.autocommit = True
            for link, items in sources:
                for it in items:
                    excerpt = (it.get("title", "") + ". " + it.get("summary", "")).strip()
                    if len(excerpt) < 80:
                        continue
                    pairs = _llm_extract(excerpt)
                    if not pairs:
                        continue
                    p, a = _save_pending(link, excerpt, pairs, conn)
                    total_pending += p
                    total_accepted += a
    except Exception as e:
        logger.error("learner DB error: %s", e)

    print(f"[learner] sources={len(sources)} pending={total_pending} auto_accepted={total_accepted}")
    return {"sources": len(sources), "pending": total_pending, "accepted": total_accepted}


if __name__ == "__main__":
    run_weekly()
