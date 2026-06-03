"""Кэш виртуальных туров в таблице virtual_tours."""
import os
import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("virtual_tours.cache")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)


def get_cached_tour(listing_id: str, bot: str) -> Optional[Dict[str, Any]]:
    try:
        with psycopg2.connect(DSN, connect_timeout=8) as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM virtual_tours WHERE listing_id=%s AND bot=%s",
                    (listing_id, bot),
                )
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE virtual_tours SET cache_hits=cache_hits+1, last_used_at=NOW() "
                        "WHERE id=%s",
                        (row["id"],),
                    )
                    return dict(row)
    except Exception as e:  # noqa: BLE001
        log.warning("get_cached_tour failed: %s", e)
    return None


def save_tour_cache(
    listing_id: str, bot: str,
    rooms: List[Dict[str, Any]],
    script_md: str,
    audio_path: Optional[str],
    video_path: Optional[str],
    cover_image: Optional[str],
) -> Optional[int]:
    try:
        with psycopg2.connect(DSN, connect_timeout=8) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO virtual_tours
                    (listing_id, bot, rooms, script_md, audio_path, video_path, cover_image)
                VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT (bot, listing_id) DO UPDATE
                  SET rooms = EXCLUDED.rooms,
                      script_md = EXCLUDED.script_md,
                      audio_path = EXCLUDED.audio_path,
                      video_path = EXCLUDED.video_path,
                      cover_image = EXCLUDED.cover_image,
                      last_used_at = NOW()
                RETURNING id
                """,
                (listing_id, bot, json.dumps(rooms, ensure_ascii=False),
                 script_md, audio_path, video_path, cover_image),
            )
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log.exception("save_tour_cache failed: %s", e)
        return None
