"""
behavior_tracking.tracker — interaction logger.

Resilient: every DB op wrapped in try/except. Never raises out to the bot.
Async-friendly: writes happen in background tasks where possible.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    import psycopg2
    import psycopg2.extras
    _PG_OK = True
except Exception:
    _PG_OK = False


# ───────────────────────────── config ─────────────────────────────

def is_enabled() -> bool:
    return os.environ.get("BEHAVIOR_TRACKING_ENABLED", "1") == "1" and _PG_OK


_DSN_ENV_CANDIDATES = ("RESALE_DATABASE_URL", "DATABASE_URL", "BEHAVIOR_DATABASE_URL")


def _dsn() -> Optional[str]:
    for k in _DSN_ENV_CANDIDATES:
        v = os.environ.get(k)
        if v:
            return v
    return None


# ───────────────────────────── PII redact ─────────────────────────────
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s()]{7,}\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


def _redact(text: Optional[str], limit: int = 500) -> Optional[str]:
    if not text:
        return text
    s = str(text)
    s = _PHONE_RE.sub("[PHONE]", s)
    s = _EMAIL_RE.sub("[EMAIL]", s)
    if len(s) > limit:
        s = s[:limit] + "…"
    return s


# ───────────────────────────── connection pool (lazy, simple) ─────────────────────────────
_conn_lock = threading.Lock()
_conn: Optional["psycopg2.extensions.connection"] = None


def _get_conn():
    global _conn
    if not _PG_OK:
        return None
    dsn = _dsn()
    if not dsn:
        return None
    with _conn_lock:
        try:
            if _conn is None or _conn.closed:
                _conn = psycopg2.connect(dsn, connect_timeout=6)
                _conn.autocommit = True
            return _conn
        except Exception:
            _conn = None
            return None


_schema_ready = False


def ensure_schema() -> bool:
    """Run migrations.sql once per process. Returns True on success."""
    global _schema_ready
    if _schema_ready:
        return True
    if not is_enabled():
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        path = os.path.join(os.path.dirname(__file__), "migrations.sql")
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
        _schema_ready = True
        return True
    except Exception as e:
        try:
            print(f"[behavior_tracking] ensure_schema failed: {e}")
        except Exception:
            pass
        return False


# ───────────────────────────── tracking context ─────────────────────────────
@dataclass
class TrackingContext:
    bot_name: str
    user_id: Optional[int] = None
    user_message: Optional[str] = None
    user_message_type: Optional[str] = None
    query_type: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    response_preview: Optional[str] = None
    result_count: Optional[int] = None
    outcome: Optional[str] = None
    error_msg: Optional[str] = None
    session_id: Optional[str] = None
    language: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    # set by tracker
    interaction_id: Optional[int] = None
    _t0: float = field(default_factory=time.monotonic)

    @property
    def latency_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


def _insert_interaction(ctx: TrackingContext) -> Optional[int]:
    """Synchronous DB insert. Returns id or None on failure."""
    if not is_enabled():
        return None
    ensure_schema()
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_interactions
                  (bot_name, user_id, user_message, user_message_type,
                   bot_response_preview, query_type, query_params,
                   latency_ms, result_count, outcome, error_msg,
                   session_id, language, meta)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id;
                """,
                (
                    ctx.bot_name,
                    ctx.user_id,
                    _redact(ctx.user_message, 1000),
                    ctx.user_message_type,
                    _redact(ctx.response_preview, 500),
                    ctx.query_type,
                    json.dumps(ctx.params, ensure_ascii=False) if ctx.params else None,
                    ctx.latency_ms,
                    ctx.result_count,
                    ctx.outcome,
                    _redact(ctx.error_msg, 500),
                    ctx.session_id,
                    ctx.language,
                    json.dumps(ctx.meta, ensure_ascii=False) if ctx.meta else None,
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        try:
            print(f"[behavior_tracking] insert_interaction failed: {e}")
        except Exception:
            pass
        return None


def log_interaction(**kwargs) -> Optional[int]:
    """Fire-and-forget logging. Returns interaction_id (or None)."""
    if not is_enabled():
        return None
    try:
        ctx = TrackingContext(**kwargs)
        if ctx.outcome is None:
            ctx.outcome = "success"
        return _insert_interaction(ctx)
    except Exception:
        return None


def update_outcome(interaction_id: int, *, outcome: Optional[str] = None,
                   result_count: Optional[int] = None,
                   error_msg: Optional[str] = None,
                   response_preview: Optional[str] = None,
                   meta_patch: Optional[Dict[str, Any]] = None) -> bool:
    if not is_enabled() or not interaction_id:
        return False
    conn = _get_conn()
    if conn is None:
        return False
    try:
        sets, vals = [], []
        if outcome is not None:
            sets.append("outcome=%s"); vals.append(outcome)
        if result_count is not None:
            sets.append("result_count=%s"); vals.append(result_count)
        if error_msg is not None:
            sets.append("error_msg=%s"); vals.append(_redact(error_msg, 500))
        if response_preview is not None:
            sets.append("bot_response_preview=%s"); vals.append(_redact(response_preview, 500))
        if meta_patch:
            sets.append("meta = COALESCE(meta,'{}'::jsonb) || %s::jsonb")
            vals.append(json.dumps(meta_patch, ensure_ascii=False))
        if not sets:
            return False
        vals.append(interaction_id)
        with conn.cursor() as cur:
            cur.execute(f"UPDATE bot_interactions SET {', '.join(sets)} WHERE id=%s", vals)
        return True
    except Exception:
        return False


# ───────────────────────────── context manager ─────────────────────────────
class _AsyncTracker:
    def __init__(self, **kwargs):
        self.ctx = TrackingContext(**kwargs)

    async def __aenter__(self) -> TrackingContext:
        return self.ctx

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None:
            self.ctx.outcome = self.ctx.outcome or "error"
            self.ctx.error_msg = f"{exc_type.__name__}: {exc}" if exc_type else str(exc)
        elif self.ctx.outcome is None:
            self.ctx.outcome = "success"
        # background write — never block caller
        try:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, _insert_interaction, self.ctx)
        except Exception:
            try:
                _insert_interaction(self.ctx)
            except Exception:
                pass
        # do not suppress
        return False


@contextlib.contextmanager
def _SyncTracker(**kwargs):
    ctx = TrackingContext(**kwargs)
    try:
        yield ctx
        if ctx.outcome is None:
            ctx.outcome = "success"
    except Exception as e:
        ctx.outcome = ctx.outcome or "error"
        ctx.error_msg = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:
            t = threading.Thread(target=_insert_interaction, args=(ctx,), daemon=True)
            t.start()
        except Exception:
            pass


def track(*, async_mode: bool = True, **kwargs):
    """
    Universal tracker.

    Usage (async):
        async with track(bot_name="resale", user_id=uid, query_type="search") as ctx:
            ctx.params = {"area": "Marina"}
            rows = await search(...)
            ctx.result_count = len(rows)
            ctx.outcome = "success" if rows else "empty"

    Usage (sync):
        with track(async_mode=False, bot_name="currency", user_id=uid) as ctx:
            ...
    """
    if async_mode:
        return _AsyncTracker(**kwargs)
    return _SyncTracker(**kwargs)
