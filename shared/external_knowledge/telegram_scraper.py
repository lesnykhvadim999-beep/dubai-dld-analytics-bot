"""Telethon read-only scraper for PUBLIC Dubai property channels.

Runs every 2 hours. Requires TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION
env vars (existing channel-bot-new session reused if available).

If Telethon is not installed or no session — silently skips.
We never write to channels, never join private chats, never DM users.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

from ._db import conn_cursor, ensure_schema
from ._embed import embed
from .relevance_filter import classify

log = logging.getLogger(__name__)

PUBLIC_CHANNELS = [
    "dubai_real_estate_news",
    "propertyfinderae",
    "bayut_dubai",
    "dubai_property_market",
    "dxbpropertynews",
]


def _fingerprint(channel: str, msg_id: int, text: str) -> str:
    h = hashlib.sha1()
    h.update(f"{channel}:{msg_id}".encode("utf-8"))
    h.update(b"|")
    h.update((text[:200] or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()


async def _scrape_async(limit_per_channel: int = 30) -> dict:
    try:
        from telethon import TelegramClient  # type: ignore
        from telethon.sessions import StringSession  # type: ignore
    except ImportError:
        log.warning("Telethon not installed; skipping telegram scrape")
        return {"inserted": 0, "channels": 0, "error": "telethon missing"}

    api_id = os.environ.get("TELETHON_API_ID")
    api_hash = os.environ.get("TELETHON_API_HASH")
    session_str = os.environ.get("TELETHON_SESSION") or os.environ.get("TG_SESSION_STRING")

    if not (api_id and api_hash and session_str):
        log.warning("Telethon env vars missing; skipping telegram scrape")
        return {"inserted": 0, "channels": 0, "error": "no session"}

    inserted = 0
    channels_done = 0

    client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        log.warning("Telethon session not authorized; skipping")
        await client.disconnect()
        return {"inserted": 0, "channels": 0, "error": "not authorized"}

    try:
        for ch in PUBLIC_CHANNELS:
            try:
                async for msg in client.iter_messages(ch, limit=limit_per_channel):
                    text = (msg.message or "").strip()
                    if not text or len(text) < 30:
                        continue
                    pub = msg.date or datetime.now(timezone.utc)
                    fp = _fingerprint(ch, msg.id, text)

                    cls = classify(text[:200], text)
                    if cls["score"] < 0.3:
                        continue
                    vec = embed(text)
                    facts = {"areas": cls.get("areas", []),
                             "summary": cls.get("summary", "")}
                    url = f"https://t.me/{ch}/{msg.id}"

                    with conn_cursor() as (_, cur):
                        cur.execute(
                            """
                            INSERT INTO external_signals
                              (source, source_kind, signal_type, url, title, raw_text,
                               extracted_facts, embedding, relevance_score,
                               published_at, fingerprint, language)
                            VALUES (%s, 'telegram', %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
                            ON CONFLICT (fingerprint) DO NOTHING
                            """,
                            (
                                f"tg:{ch}", cls.get("signal_type", "news"),
                                url, text[:200], text[:8000],
                                json.dumps(facts, ensure_ascii=False),
                                json.dumps(vec),
                                cls["score"], pub, fp, "en",
                            ),
                        )
                        if cur.rowcount > 0:
                            inserted += 1
                channels_done += 1
            except Exception as e:
                log.debug("channel %s failed: %s", ch, e)
                continue
    finally:
        await client.disconnect()

    return {"inserted": inserted, "channels": channels_done}


def scrape(limit_per_channel: int = 30) -> dict:
    ensure_schema()
    try:
        return asyncio.run(_scrape_async(limit_per_channel=limit_per_channel))
    except Exception as e:
        log.warning("telegram scrape failed: %s", e)
        return {"inserted": 0, "channels": 0, "error": str(e)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = scrape()
    print(f"telegram_scraper: channels={s.get('channels', 0)} inserted={s.get('inserted', 0)} "
          f"err={s.get('error', '-')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
