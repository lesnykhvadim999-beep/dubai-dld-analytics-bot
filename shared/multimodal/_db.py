"""Тонкая обёртка над psycopg2 для multimodal_inputs."""
import os
import json
import logging
import psycopg2
from typing import Optional, Dict, Any

log = logging.getLogger("multimodal.db")

DSN = os.getenv(
    "RAILWAY_PG_DSN",
    os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
)


def _conn():
    return psycopg2.connect(DSN, connect_timeout=8)


def save_multimodal(
    user_id: int,
    bot: str,
    input_type: str,
    file_id: str,
    extracted: Dict[str, Any],
    *,
    file_unique_id: Optional[str] = None,
    mime_type: Optional[str] = None,
    transcript: Optional[str] = None,
    intent: Optional[str] = None,
    confidence: Optional[float] = None,
    llm_provider: Optional[str] = None,
    error: Optional[str] = None,
) -> Optional[int]:
    """Идемпотентная запись по (bot, file_unique_id)."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO multimodal_inputs
                    (user_id, bot, input_type, file_id, file_unique_id, mime_type,
                     transcript, extracted, intent, confidence, llm_provider, error)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT (bot, file_unique_id) DO UPDATE
                  SET extracted = EXCLUDED.extracted,
                      transcript = EXCLUDED.transcript,
                      intent = EXCLUDED.intent,
                      confidence = EXCLUDED.confidence,
                      processed_at = NOW(),
                      error = EXCLUDED.error
                RETURNING id
                """,
                (
                    user_id, bot, input_type, file_id, file_unique_id, mime_type,
                    transcript, json.dumps(extracted, ensure_ascii=False),
                    intent, confidence, llm_provider, error,
                ),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        log.exception("save_multimodal failed: %s", e)
        return None
