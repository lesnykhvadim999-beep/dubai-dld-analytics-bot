"""
failed_collector — пишет failed extractions в parser_failed_log.

Использование из parser_v2.py (опционально, soft import):
    try:
        from parser_self_improve.failed_collector import maybe_log_failure
        maybe_log_failure(listing_id, original_text, parsed_result)
    except Exception:
        pass  # никогда не ломаем основной парсер

Логика:
- confidence < CONFIDENCE_THRESHOLD (0.7) → log
- ИЛИ хотя бы 1 critical field == NULL → log
- Дедупликация по sha256(original_text) — уникальный индекс idx_pfl_text_hash.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ._db import conn_cursor

log = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.7
CRITICAL_FIELDS = ("deal_type", "property_type", "price", "area", "bedrooms")


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _extract_confidence(parsed: dict) -> float | None:
    if not isinstance(parsed, dict):
        return None
    # Несколько возможных мест
    for key in (
        "confidence",
        "v2_classify_confidence",
        "overall_confidence",
    ):
        v = parsed.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    # Иначе — min из per-field confidences
    field_confs = []
    for k, v in parsed.items():
        if k.endswith("_confidence") and isinstance(v, (int, float)):
            field_confs.append(float(v))
    if field_confs:
        return min(field_confs)
    return None


def _missing_critical(parsed: dict) -> list[str]:
    missing = []
    for f in CRITICAL_FIELDS:
        v = parsed.get(f)
        if v is None or v == "" or v == "null":
            missing.append(f)
    return missing


def should_log(parsed_result: dict) -> tuple[bool, float | None, list[str]]:
    conf = _extract_confidence(parsed_result)
    missing = _missing_critical(parsed_result)
    flag = False
    if conf is not None and conf < CONFIDENCE_THRESHOLD:
        flag = True
    if missing:
        flag = True
    return flag, conf, missing


def maybe_log_failure(
    listing_id: int | None,
    original_text: str,
    parsed_result: dict[str, Any],
) -> bool:
    """Безопасный wrapper. True если запись добавлена."""
    if not original_text or not isinstance(parsed_result, dict):
        return False
    try:
        flag, conf, missing = should_log(parsed_result)
        if not flag:
            return False
        text_hash = _hash_text(original_text)
        with conn_cursor() as (_, cur):
            cur.execute(
                """
                INSERT INTO parser_failed_log
                    (listing_id, original_text, parsed_result, confidence,
                     missing_fields, text_hash)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (text_hash) DO NOTHING
                """,
                (
                    listing_id,
                    original_text[:8000],
                    json.dumps(parsed_result, ensure_ascii=False, default=str),
                    conf,
                    missing,
                    text_hash,
                ),
            )
        return True
    except Exception as exc:
        log.warning("failed_collector.maybe_log_failure error: %s", exc)
        return False


def backfill_from_listings(limit: int = 2000) -> int:
    """
    Однократный backfill: пройти по listings, у которых критические поля NULL
    или confidence низкий → записать в parser_failed_log.
    Используется для первого запуска discovery.

    Оптимизация: один connection, execute_values bulk insert.
    """
    from psycopg2.extras import execute_values

    with conn_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT id, original_text, deal_type, property_type, price, area,
                   bedrooms, area_confidence, building_confidence, location_confidence
            FROM listings
            WHERE original_text IS NOT NULL
              AND LENGTH(original_text) BETWEEN 80 AND 4000
              AND (
                    deal_type IS NULL
                 OR property_type IS NULL
                 OR price IS NULL
                 OR area IS NULL
                 OR COALESCE(area_confidence, 1.0) < 0.7
                 OR COALESCE(location_confidence, 1.0) < 0.7
              )
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

        batch = []
        for row in rows:
            (
                lid, text, deal_type, ptype, price, area, beds,
                area_conf, bld_conf, loc_conf,
            ) = row
            parsed = {
                "deal_type": deal_type,
                "property_type": ptype,
                "price": price,
                "area": area,
                "bedrooms": beds,
                "area_confidence": area_conf,
                "building_confidence": bld_conf,
                "location_confidence": loc_conf,
            }
            flag, conf, missing = should_log(parsed)
            if not flag:
                continue
            batch.append((
                lid,
                text[:8000],
                json.dumps(parsed, ensure_ascii=False, default=str),
                conf,
                missing,
                _hash_text(text),
            ))

        if not batch:
            return 0

        # Используем INSERT ... ON CONFLICT (text_hash) DO NOTHING
        execute_values(
            cur,
            """
            INSERT INTO parser_failed_log
                (listing_id, original_text, parsed_result, confidence, missing_fields, text_hash)
            VALUES %s
            ON CONFLICT (text_hash) DO NOTHING
            """,
            batch,
            template="(%s, %s, %s::jsonb, %s, %s, %s)",
        )
        return cur.rowcount or len(batch)
