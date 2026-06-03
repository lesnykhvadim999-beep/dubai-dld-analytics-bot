"""
shared.ux_metrics.tracker — async event logger (≤ 2 ms overhead on caller).

API:
    track_event(bot_name, user_id, event_type, properties=None, variant=None)
        → fire-and-forget; never raises out to bot code

Events recognised by analyzer / rollback (free-form is allowed):
    menu_shown, button_clicked, action_completed, drop_off, error_seen

Implementation notes:
- DSN order: LIVE_DATABASE_URL → RESALE_DATABASE_URL → DATABASE_URL → INTELLIGENCE_DATABASE_URL.
  Never hardcoded.
- Writes are dispatched to a daemon thread (sync caller) or executor (async
  caller). The hot path only does a dict copy + queue.put — well under 2 ms.
- Opt-out cache: 5-minute in-process TTL so we don't hammer DB on every click.
- ensure_schema() applies schema.sql once per process; cheap NO-OP afterwards.

Vadim Realty | RERA BRN 65011
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, Optional

log = logging.getLogger("ux_metrics.tracker")

try:
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    _PG_OK = True
except Exception:
    _PG_OK = False


# ── DSN resolution (env only) ──────────────────────────────────────────────
_DSN_ENVS = (
    "LIVE_DATABASE_URL",
    "RESALE_DATABASE_URL",
    "DATABASE_URL",
    "INTELLIGENCE_DATABASE_URL",
)


def _dsn() -> Optional[str]:
    for k in _DSN_ENVS:
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return None


def is_enabled() -> bool:
    if os.environ.get("UX_METRICS_DISABLED") == "1":
        return False
    return _PG_OK and _dsn() is not None


# ── connection (lazy, persistent) ──────────────────────────────────────────
_conn_lock = threading.Lock()
_conn = None  # type: ignore


def _get_conn():
    global _conn
    if not _PG_OK:
        return None
    dsn = _dsn()
    if not dsn:
        return None
    with _conn_lock:
        try:
            if _conn is None or getattr(_conn, "closed", 1):
                _conn = psycopg2.connect(dsn, connect_timeout=5)
                _conn.autocommit = True
            return _conn
        except Exception as e:
            log.warning("ux_metrics: connect failed: %s", e)
            _conn = None
            return None


# ── schema bootstrap ───────────────────────────────────────────────────────
_schema_ready = False
_schema_lock = threading.Lock()


def ensure_schema() -> bool:
    global _schema_ready
    if _schema_ready:
        return True
    if not is_enabled():
        return False
    with _schema_lock:
        if _schema_ready:
            return True
        conn = _get_conn()
        if conn is None:
            return False
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            sql_path = os.path.join(here, "schema.sql")
            with open(sql_path, "r", encoding="utf-8") as f:
                sql = f.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            _schema_ready = True
            return True
        except Exception as e:
            log.warning("ux_metrics: schema apply failed: %s", e)
            return False


# ── opt-out cache (5-min TTL) ──────────────────────────────────────────────
_OPT_TTL = 300.0
_opt_cache: Dict[tuple, tuple] = {}  # (user_id, bot_name) -> (opted_bool, expires_at)
_opt_lock = threading.Lock()


def is_opted_out(user_id: int, bot_name: str) -> bool:
    if not is_enabled() or user_id is None:
        return False
    now = time.monotonic()
    key = (int(user_id), bot_name)
    with _opt_lock:
        cached = _opt_cache.get(key)
        if cached and cached[1] > now:
            return cached[0]
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM ux_opt_outs WHERE user_id=%s AND bot_name=%s",
                (int(user_id), bot_name),
            )
            opted = cur.fetchone() is not None
    except Exception:
        # Schema not applied yet? Fall through as not-opted.
        opted = False
    with _opt_lock:
        _opt_cache[key] = (opted, now + _OPT_TTL)
    return opted


def set_opt_out(user_id: int, bot_name: str, opted: bool = True) -> bool:
    """Add (or remove) opt-out row. Returns True on success."""
    if not is_enabled() or user_id is None:
        return False
    ensure_schema()
    conn = _get_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            if opted:
                cur.execute(
                    """
                    INSERT INTO ux_opt_outs (user_id, bot_name)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """,
                    (int(user_id), bot_name),
                )
            else:
                cur.execute(
                    "DELETE FROM ux_opt_outs WHERE user_id=%s AND bot_name=%s",
                    (int(user_id), bot_name),
                )
        with _opt_lock:
            _opt_cache[(int(user_id), bot_name)] = (
                opted, time.monotonic() + _OPT_TTL,
            )
        return True
    except Exception as e:
        log.warning("ux_metrics: set_opt_out failed: %s", e)
        return False


# ── background writer (single-thread queue) ────────────────────────────────
_QUEUE_MAX = 5000
_q: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_writer_started = False
_writer_lock = threading.Lock()


def _writer_loop():
    while True:
        batch: list[dict] = []
        try:
            item = _q.get(timeout=2.0)
            batch.append(item)
        except queue.Empty:
            continue
        # drain quickly to batch-insert
        for _ in range(199):
            try:
                batch.append(_q.get_nowait())
            except queue.Empty:
                break
        try:
            _flush_batch(batch)
        except Exception as e:
            log.warning("ux_metrics: flush failed: %s", e)


def _flush_batch(batch: list[dict]) -> None:
    if not batch:
        return
    conn = _get_conn()
    if conn is None:
        return
    rows = []
    for ev in batch:
        try:
            props = ev.get("properties") or {}
            props_json = json.dumps(props, ensure_ascii=False, default=str)
        except Exception:
            props_json = "{}"
        rows.append((
            ev.get("bot_name"),
            ev.get("user_id"),
            ev.get("event_type"),
            props_json,
            ev.get("variant"),
        ))
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO ux_events
                  (bot_name, user_id, event_type, properties, variant)
                VALUES %s
                """,
                rows,
                template="(%s,%s,%s,%s::jsonb,%s)",
                page_size=200,
            )
    except Exception as e:
        log.warning("ux_metrics: batch insert failed (%d rows): %s",
                    len(rows), e)


def _ensure_writer():
    global _writer_started
    if _writer_started:
        return
    with _writer_lock:
        if _writer_started:
            return
        t = threading.Thread(target=_writer_loop, name="ux_metrics_writer",
                             daemon=True)
        t.start()
        _writer_started = True


# ── PUBLIC: track_event ─────────────────────────────────────────────────────
# ── PII whitelist (audit B5): properties must contain ONLY these keys ──────
_PII_SAFE_KEYS = frozenset({
    "label", "action_name", "variant", "screen", "step",
    "duration_ms", "result", "error_code", "count", "value",
})


def _sanitize_properties(props: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strip any non-whitelisted keys (names/emails/phones never reach DB)."""
    if not props:
        return None
    return {k: v for k, v in props.items() if k in _PII_SAFE_KEYS}


def track_event(
    bot_name: str,
    user_id: Optional[int],
    event_type: str,
    properties: Optional[Dict[str, Any]] = None,
    variant: Optional[str] = None,
) -> None:
    """Fire-and-forget event logger.

    Never raises. Drops silently when:
      - UX_METRICS_DISABLED=1
      - psycopg2 not installed / DSN missing
      - user has opted out for this bot
      - queue is full (backpressure → drop oldest semantics ignored, we just skip)
    """
    if not is_enabled():
        return
    try:
        if user_id is not None and is_opted_out(user_id, bot_name):
            return
        ev = {
            "bot_name": bot_name,
            "user_id": int(user_id) if user_id is not None else None,
            "event_type": event_type,
            "properties": _sanitize_properties(properties),
            "variant": variant,
        }
        _ensure_writer()
        # schema bootstrap is best-effort; do it in background to keep hot path fast
        if not _schema_ready:
            try:
                threading.Thread(target=ensure_schema, daemon=True).start()
            except Exception:
                pass
        try:
            _q.put_nowait(ev)
        except queue.Full:
            # backpressure: drop oldest one
            try:
                _q.get_nowait()
                _q.put_nowait(ev)
            except Exception:
                pass
    except Exception:
        # Never raise out to caller.
        pass


async def track_event_async(
    bot_name: str,
    user_id: Optional[int],
    event_type: str,
    properties: Optional[Dict[str, Any]] = None,
    variant: Optional[str] = None,
) -> None:
    """Async wrapper — still non-blocking (hands off to thread pool)."""
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            track_event,
            bot_name, user_id, event_type, properties, variant,
        )
    except RuntimeError:
        track_event(bot_name, user_id, event_type, properties, variant)


# ── friction logger (convenience) ──────────────────────────────────────────
def log_friction(
    bot_name: str,
    user_id: Optional[int],
    friction_type: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort friction log. Used by drop_off / repeated-error detectors."""
    if not is_enabled():
        return
    if user_id is not None and is_opted_out(user_id, bot_name):
        return
    ensure_schema()
    conn = _get_conn()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ux_friction_log
                  (bot_name, user_id, friction_type, context)
                VALUES (%s,%s,%s,%s::jsonb)
                """,
                (
                    bot_name,
                    int(user_id) if user_id is not None else None,
                    friction_type,
                    json.dumps(_sanitize_properties(context) or {}, ensure_ascii=False, default=str),
                ),
            )
    except Exception as e:
        log.warning("ux_metrics: friction insert failed: %s", e)


# ── synchronous flush (used by tests / smoke) ──────────────────────────────
def flush_now(timeout: float = 3.0) -> None:
    """Block until the queue is empty (or timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _q.empty():
            return
        time.sleep(0.05)
