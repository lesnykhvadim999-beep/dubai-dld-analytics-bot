"""
confidence_calibration — Phase BK Phase 2 (weekly cron).

Цель:
    Найти listings с погранично-низкой confidence (0.5..0.7) и попробовать
    переразобрать их через current parser_v2. Если новая confidence > 0.7 —
    UPDATE listing (lift to higher-confidence row), пишем audit-row.

Алгоритм:
    1. SELECT 500 listings с confidence ∈ [0.5, 0.7], created_at > NOW()-30d.
    2. Для каждого:
         - re-parse original_text через parser_v2.parse_message_v2
         - если new_confidence > 0.7 AND ключевые поля совпадают →
             UPDATE listings_v2 SET confidence = new, parsed_at = NOW()
             INSERT audit_history metric=parser.calibration_lift
         - если new_confidence < old → пропустить (deterioration)
         - если ключевые поля изменились → пропустить (drift, нужен human)
    3. Сводка → admin_notifier.send_summary

Cron:
    Windows Task Scheduler — Wednesday 04:00 Asia/Dubai
        schtasks /Create /SC WEEKLY /D WED /TN "parser_calibration" \
                 /TR "py C:\\Projects\\shared\\parser_self_improve\\confidence_calibration.py" \
                 /ST 04:00 /F

Manual run:
    py C:/Projects/shared/parser_self_improve/confidence_calibration.py
    py C:/Projects/shared/parser_self_improve/confidence_calibration.py --dry-run
    py C:/Projects/shared/parser_self_improve/confidence_calibration.py --limit 100

Статус:
    BOILERPLATE — full re-parse loop wired, but execution is gated behind
    `--apply` flag. First production run is manual review only.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Any

# Поддержка `python confidence_calibration.py` (без пакета)
if __name__ == "__main__" and __package__ in (None, ""):
    import os as _os
    import sys as _sys
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _parent = _os.path.dirname(_here)
    if _parent not in _sys.path:
        _sys.path.insert(0, _parent)
    __package__ = "parser_self_improve"  # noqa: F841

from ._db import conn_cursor, ensure_schema  # type: ignore

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ────────────────────────── Tunables ──────────────────────────
CONFIDENCE_LO = 0.5
CONFIDENCE_HI = 0.7
LIFT_THRESHOLD = 0.7
DEFAULT_LIMIT = 500
RECENT_DAYS = 30

KEY_FIELDS = ("deal_type", "property_type", "bedrooms", "price")
# Used to detect "drift" — если новая re-parse меняет KEY_FIELDS, не UPDATE автоматом.


# ────────────────────────── Parser import (lazy) ──────────────────────────
def _import_parser():
    """parser_v2 живёт в resale-bot. Импортируем по абсолютному пути."""
    import importlib.util
    import os
    candidates = [
        r"C:\Projects\resale-bot\parser_v2.py",
        os.path.expanduser(r"~/Projects/resale-bot/parser_v2.py"),
    ]
    for path in candidates:
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("parser_v2", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise RuntimeError("parser_v2.py not found in any expected location")


# ────────────────────────── Core logic ──────────────────────────
def fetch_candidates(limit: int) -> list[dict[str, Any]]:
    """Pull listings with borderline confidence + raw text."""
    sql = """
        SELECT
          id, raw_text, confidence,
          deal_type, property_type, bedrooms, price,
          area, building, created_at
        FROM public.listings_v2
        WHERE confidence BETWEEN %s AND %s
          AND raw_text IS NOT NULL
          AND created_at > NOW() - INTERVAL '%s days'
        ORDER BY confidence ASC, created_at DESC
        LIMIT %s
    """
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute(sql, (CONFIDENCE_LO, CONFIDENCE_HI, RECENT_DAYS, limit))
        return [dict(r) for r in cur.fetchall()]


def reparse(parser_mod, text: str) -> dict[str, Any] | None:
    """Re-run current parse_message_v2 on raw text. Returns best (single) result."""
    try:
        results = parser_mod.parse_message_v2(text)
    except Exception as e:
        log.warning("re-parse failed: %s", e)
        return None
    if not results:
        return None
    # Pick highest-confidence result (parse may return multi-property splits).
    return max(results, key=lambda r: r.get("confidence") or 0)


def _extract_conf(parsed: dict[str, Any] | None) -> float | None:
    if not parsed:
        return None
    for key in ("confidence", "v2_classify_confidence", "overall_confidence"):
        v = parsed.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def fields_drifted(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    drifted = []
    for f in KEY_FIELDS:
        ov, nv = old.get(f), new.get(f)
        # Normalize None / 0 / '' to same bucket
        if (ov or None) != (nv or None):
            drifted.append(f)
    return drifted


def apply_lift(listing_id: int, new_confidence: float, new_meta: dict[str, Any]) -> bool:
    """UPDATE listings_v2 lifting confidence. Returns True on success."""
    sql = """
        UPDATE public.listings_v2
        SET confidence = %s,
            updated_at = NOW()
        WHERE id = %s
    """
    try:
        with conn_cursor() as (_, cur):
            cur.execute(sql, (new_confidence, listing_id))
        return True
    except Exception as e:
        log.warning("apply_lift(%s) failed: %s", listing_id, e)
        return False


def record_audit(listing_id: int, old_conf: float, new_conf: float,
                  drift: list[str], applied: bool) -> None:
    """Write a row into audit_history (best-effort)."""
    try:
        from auto_audit._common import record_metric  # type: ignore
        record_metric(
            "parser.calibration_lift",
            new_conf - old_conf,
            meta={
                "listing_id": listing_id,
                "old_confidence": round(old_conf, 3),
                "new_confidence": round(new_conf, 3),
                "drift_fields": drift,
                "applied": applied,
            },
        )
    except Exception:
        pass  # auto_audit may not be installed in caller env


# ────────────────────────── Main ──────────────────────────
def run(limit: int, dry_run: bool, apply_changes: bool) -> dict[str, int]:
    """
    Returns counters:
        scanned, parser_failed, lifted, deteriorated, drifted, applied
    """
    parser_mod = _import_parser()
    candidates = fetch_candidates(limit)
    log.info("Fetched %d candidates (confidence %.2f..%.2f, recent %dd)",
             len(candidates), CONFIDENCE_LO, CONFIDENCE_HI, RECENT_DAYS)

    counters = {
        "scanned": 0,
        "parser_failed": 0,
        "lifted": 0,
        "deteriorated": 0,
        "drifted": 0,
        "applied": 0,
    }

    for row in candidates:
        counters["scanned"] += 1
        old_conf = float(row["confidence"])
        new_parsed = reparse(parser_mod, row["raw_text"])
        if not new_parsed:
            counters["parser_failed"] += 1
            continue
        new_conf = _extract_conf(new_parsed)
        if new_conf is None or new_conf <= old_conf:
            counters["deteriorated"] += 1
            continue

        drift = fields_drifted(row, new_parsed)
        if drift:
            counters["drifted"] += 1
            log.info("listing %s drifted on fields: %s — skip", row["id"], drift)
            record_audit(row["id"], old_conf, new_conf, drift, applied=False)
            continue

        if new_conf >= LIFT_THRESHOLD:
            counters["lifted"] += 1
            if apply_changes and not dry_run:
                ok = apply_lift(row["id"], new_conf, new_parsed)
                if ok:
                    counters["applied"] += 1
            record_audit(row["id"], old_conf, new_conf, drift,
                          applied=apply_changes and not dry_run)

    log.info("Calibration done: %s", counters)

    # Summary to admin (best-effort)
    try:
        from .admin_notifier import send_summary  # type: ignore
        send_summary(
            "parser.confidence_calibration",
            counters,
            timestamp=datetime.utcnow().isoformat(),
        )
    except Exception:
        pass
    return counters


def main() -> int:
    p = argparse.ArgumentParser(description="Phase BK Phase 2 — confidence calibration")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"max candidates to scan (default {DEFAULT_LIMIT})")
    p.add_argument("--dry-run", action="store_true",
                   help="report only — never UPDATE")
    p.add_argument("--apply", action="store_true",
                   help="actually UPDATE listings (required to lift)")
    args = p.parse_args()

    try:
        ensure_schema()
    except Exception as e:
        log.warning("ensure_schema failed (continuing): %s", e)

    counters = run(
        limit=args.limit,
        dry_run=args.dry_run,
        apply_changes=args.apply,
    )
    print("\n=== Confidence Calibration Summary ===")
    for k, v in counters.items():
        print(f"  {k:>18}: {v}")
    return 0 if counters["scanned"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
