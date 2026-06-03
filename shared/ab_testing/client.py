"""
ab_testing.client — public API for bots.

Usage in bot:
    from shared.ab_testing import get_variant, record_outcome

    variant = get_variant("response_format_v2", user_id=uid, default="A")
    if variant == "B":
        response = new_response(...)
    else:
        response = old_response(...)

    # Later when user interacts:
    record_outcome("response_format_v2", uid, "follow_up", 1.0)

All calls are best-effort: any DB failure is swallowed so bot flow never breaks.
Toggle via env: AB_TESTING_ENABLED=1 (default 1).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from ._db import conn_cursor, ensure_schema as _ensure_schema

log = logging.getLogger(__name__)

_ENABLED_ENV = os.environ.get("AB_TESTING_ENABLED", "1")
_IS_ENABLED = _ENABLED_ENV.lower() not in ("0", "false", "no", "off")

# In-process cache for experiment configs (refreshed per process start)
_EXP_CACHE: dict[str, dict] = {}
_CACHE_VERSION = 0


def is_enabled() -> bool:
    return _IS_ENABLED


def ensure_schema() -> None:
    _ensure_schema()


def _invalidate_cache() -> None:
    global _CACHE_VERSION
    _CACHE_VERSION += 1
    _EXP_CACHE.clear()


def _load_experiment(experiment_key: str) -> dict | None:
    cached = _EXP_CACHE.get(experiment_key)
    if cached:
        return cached
    try:
        with conn_cursor(dict_cursor=True) as (_, cur):
            cur.execute(
                "SELECT * FROM ab_experiments WHERE experiment_key = %s",
                (experiment_key,),
            )
            row = cur.fetchone()
            if row:
                _EXP_CACHE[experiment_key] = dict(row)
            return row
    except Exception as exc:
        log.debug("ab._load_experiment failed: %s", exc)
        return None


def _deterministic_pick(user_id: int, experiment_key: str, split: dict[str, float]) -> str:
    """Stable bucket: hash(user_id + key) mod 10000, mapped to cumulative splits."""
    h = hashlib.sha1(f"{user_id}:{experiment_key}".encode("utf-8")).hexdigest()
    bucket = int(h[:8], 16) % 10000
    pct = bucket / 100.0   # 0..99.99
    cum = 0.0
    items = sorted(split.items())
    for variant, weight in items:
        cum += float(weight) * 100.0
        if pct < cum:
            return variant
    return items[-1][0] if items else "A"


def _persist_assignment(exp_id: int, user_id: int, variant: str) -> None:
    try:
        with conn_cursor() as (_, cur):
            cur.execute("""
                INSERT INTO ab_assignments (experiment_id, user_id, variant)
                VALUES (%s, %s, %s)
                ON CONFLICT (experiment_id, user_id) DO NOTHING
            """, (exp_id, user_id, variant))
    except Exception as exc:
        log.debug("ab._persist_assignment failed: %s", exc)


def _read_assignment(exp_id: int, user_id: int) -> str | None:
    try:
        with conn_cursor() as (_, cur):
            cur.execute("""
                SELECT variant FROM ab_assignments
                WHERE experiment_id=%s AND user_id=%s
            """, (exp_id, user_id))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.debug("ab._read_assignment failed: %s", exc)
        return None


def get_variant(experiment_key: str, user_id: int, default: str = "A") -> str:
    """Return the variant for this user, persistently.

    Behaviour:
      - Disabled globally → default
      - Experiment not found → default
      - Status = completed → winner (or default)
      - Status = paused → default
      - Otherwise deterministic pick and persist (if not yet assigned)
    """
    if not _IS_ENABLED or not user_id:
        return default
    try:
        exp = _load_experiment(experiment_key)
        if not exp:
            return default
        status = exp.get("status") or "running"
        if status == "completed":
            return exp.get("winner") or exp.get("default_variant") or default
        if status in ("paused", "abandoned"):
            return exp.get("default_variant") or default

        # Stable assignment
        existing = _read_assignment(exp["id"], user_id)
        if existing:
            return existing
        split = exp.get("traffic_split") or {"A": 0.5, "B": 0.5}
        if isinstance(split, str):
            try:
                split = json.loads(split)
            except Exception:
                split = {"A": 0.5, "B": 0.5}
        variant = _deterministic_pick(user_id, experiment_key, split)
        _persist_assignment(exp["id"], user_id, variant)
        return variant
    except Exception as exc:
        log.debug("ab.get_variant failed: %s", exc)
        return default


def record_outcome(
    experiment_key: str,
    user_id: int,
    outcome_type: str,
    outcome_value: float = 1.0,
    meta: dict | None = None,
) -> bool:
    """Record an outcome event. Best-effort, returns success bool."""
    if not _IS_ENABLED or not user_id:
        return False
    try:
        exp = _load_experiment(experiment_key)
        if not exp:
            return False
        variant = _read_assignment(exp["id"], user_id)
        if not variant:
            # No assignment → user never saw this experiment; skip
            return False
        with conn_cursor() as (_, cur):
            cur.execute("""
                INSERT INTO ab_outcomes (experiment_id, user_id, variant, outcome_type, outcome_value, meta)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """, (
                exp["id"], user_id, variant, outcome_type, outcome_value,
                json.dumps(meta or {}, ensure_ascii=False, default=str),
            ))
        return True
    except Exception as exc:
        log.debug("ab.record_outcome failed: %s", exc)
        return False


# ─────────── Admin API (used by CLI and analyze_experiments) ───────────
def register_experiment(
    experiment_key: str,
    description: str,
    variants: dict[str, str],
    traffic_split: dict[str, float] | None = None,
    bot_name: str | None = None,
    default_variant: str = "A",
    meta: dict | None = None,
) -> dict:
    """Create or update an experiment. Idempotent on `experiment_key`."""
    ensure_schema()
    traffic_split = traffic_split or {k: 1.0 / len(variants) for k in variants}
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute("""
            INSERT INTO ab_experiments
              (experiment_key, bot_name, description, variants, traffic_split,
               default_variant, status, meta)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, 'running', %s::jsonb)
            ON CONFLICT (experiment_key) DO UPDATE
              SET description = EXCLUDED.description,
                  variants = EXCLUDED.variants,
                  traffic_split = EXCLUDED.traffic_split,
                  default_variant = EXCLUDED.default_variant,
                  meta = EXCLUDED.meta
            RETURNING *
        """, (
            experiment_key, bot_name, description,
            json.dumps(variants, ensure_ascii=False),
            json.dumps(traffic_split, ensure_ascii=False),
            default_variant,
            json.dumps(meta or {}, ensure_ascii=False, default=str),
        ))
        row = cur.fetchone()
    _invalidate_cache()
    return dict(row)


def list_running() -> list[dict]:
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute("SELECT * FROM ab_experiments WHERE status='running' ORDER BY started_at DESC")
        return [dict(r) for r in cur.fetchall()]


def set_winner(experiment_key: str, winner: str) -> None:
    with conn_cursor() as (_, cur):
        cur.execute("""
            UPDATE ab_experiments
            SET winner=%s, status='completed', ended_at=NOW()
            WHERE experiment_key=%s
        """, (winner, experiment_key))
    _invalidate_cache()


def pause_experiment(experiment_key: str, reason: str = "") -> None:
    with conn_cursor() as (_, cur):
        cur.execute("""
            UPDATE ab_experiments
            SET status='paused',
                meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object('pause_reason', %s)
            WHERE experiment_key=%s
        """, (reason, experiment_key))
    _invalidate_cache()
