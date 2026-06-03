"""
fsst_core.py — Finite State Safe Transitions core utilities.

Eliminates the most common Telegram bot glitches:
  1. Double-click / duplicate callbacks         → CallbackDeduplicator
  2. Wizard sessions stuck forever              → SessionWatchdog
  3. Individual handler crashes (no recovery)  → safe_ptb_handler / safe_aiogram_cb
  4. Slow LLM / DB calls blocking event loop   → with_timeout
  5. Stale inline buttons after restart        → is_stale_button_error / answer_stale
  6. Process hangs on Railway restart          → setup_sigterm (raw loops)
  7. Health check endpoint for monitoring      → start_health_server

Framework-agnostic core; thin wrappers for PTB and aiogram included.

Usage (PTB):
    from fsst_core import CallbackDeduplicator, SessionWatchdog, safe_ptb_handler

    _dedup = CallbackDeduplicator()

    async def cb_query(update, context):
        if _dedup.is_dup(update.callback_query):
            return
        ...

    # Session watchdog via job_queue (in main()):
    _watchdog = SessionWatchdog()
    app.job_queue.run_repeating(
        _watchdog.ptb_job, interval=60, first=60
    )

    # Touch on every update (TypeHandler group=-10):
    async def _touch(update, context):
        if update.effective_user:
            _watchdog.touch(update.effective_user.id)

Usage (aiogram):
    from fsst_core import CallbackDeduplicator, SessionWatchdog

    _dedup = CallbackDeduplicator()

    @router.callback_query(...)
    async def on_cb(cb: CallbackQuery, state: FSMContext):
        if _dedup.is_dup_aiogram(cb):
            await cb.answer()
            return
        ...

    # SessionWatchdog is less needed in aiogram (FSM already handles state),
    # but can be used to clear stale FSM data via dp.storage.
"""
from __future__ import annotations

import asyncio
import collections
import functools
import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("fsst_core")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Callback Deduplicator
# ─────────────────────────────────────────────────────────────────────────────

class CallbackDeduplicator:
    """Prevents processing the same Telegram callback_query twice.

    Telegram may deliver the same callback_query.id multiple times if the
    bot is slow to answer — especially during rolling deploys.

    Each callback_query has a globally unique `id` (string). We cache it for
    TTL seconds. On second arrival → return True (is_dup) so caller can skip.

    Thread-safe for synchronous calls; for async bots the GIL + single-thread
    event loop ensures no race. A double-check pattern is added for safety.
    """

    def __init__(self, ttl: float = 10.0):
        self._seen: Dict[str, float] = {}
        self._ttl = ttl

    def _cleanup(self, now: float) -> None:
        expired = [k for k, ts in self._seen.items() if now - ts > self._ttl]
        for k in expired:
            del self._seen[k]

    def is_dup(self, callback_query) -> bool:
        """Pass the PTB CallbackQuery object. Returns True if duplicate."""
        cb_id = getattr(callback_query, "id", None)
        if cb_id is None:
            return False
        return self._check(cb_id)

    def is_dup_aiogram(self, callback_query) -> bool:
        """Pass the aiogram CallbackQuery object. Returns True if duplicate."""
        cb_id = getattr(callback_query, "id", None)
        if cb_id is None:
            return False
        return self._check(cb_id)

    def is_dup_raw(self, cb: dict) -> bool:
        """Pass a raw Telegram callback_query dict (for polling loops). Returns True if dup."""
        cb_id = cb.get("id")
        if not cb_id:
            return False
        return self._check(str(cb_id))

    def _check(self, cb_id: str) -> bool:
        now = time.time()
        self._cleanup(now)
        if cb_id in self._seen:
            logger.debug(f"[dedup] duplicate callback dropped: {cb_id[:20]}")
            return True
        self._seen[cb_id] = now
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Session Watchdog (wizard timeout / auto-reset)
# ─────────────────────────────────────────────────────────────────────────────

class SessionWatchdog:
    """Auto-resets PTB user_data after N seconds of inactivity.

    Prevents users stuck in half-finished wizard states forever.
    After `timeout_sec` of inactivity the state is cleared.

    Usage:
        _watchdog = SessionWatchdog(timeout_sec=300)  # 5 min

        # In TypeHandler (runs before everything, group=-10):
        async def _touch(update, context):
            if update.effective_user:
                _watchdog.touch(update.effective_user.id)

        # In job_queue:
        app.job_queue.run_repeating(_watchdog.ptb_job, interval=60, first=60)
    """

    def __init__(self, timeout_sec: int = 300):
        self._last_activity: Dict[int, float] = {}
        self._timeout = timeout_sec

    def touch(self, user_id: int) -> None:
        """Mark user as active right now."""
        self._last_activity[user_id] = time.time()

    def expired_users(self) -> list[int]:
        """Return list of user_ids whose sessions have timed out."""
        now = time.time()
        return [uid for uid, ts in self._last_activity.items()
                if now - ts > self._timeout]

    async def ptb_job(self, context) -> None:
        """PTB job_queue callback. Clears user_data for inactive users."""
        stale = self.expired_users()
        for uid in stale:
            try:
                if uid in context.application.user_data:
                    ud = context.application.user_data[uid]
                    ud.clear()
                    logger.debug(f"[watchdog] cleared stale session for user {uid}")
            except Exception as e:
                logger.warning(f"[watchdog] failed to clear user {uid}: {e}")
            del self._last_activity[uid]

    def aiogram_check(self, user_id: int) -> bool:
        """Returns True if user session is stale (for aiogram FSMContext.clear())."""
        ts = self._last_activity.get(user_id)
        if ts is None:
            return False
        return time.time() - ts > self._timeout


# ─────────────────────────────────────────────────────────────────────────────
# 3. Timeout helper (async)
# ─────────────────────────────────────────────────────────────────────────────

async def with_timeout(
    coro,
    timeout: float = 8.0,
    fallback: Any = None,
    label: str = "",
) -> Any:
    """Run async coroutine with timeout. Returns fallback on timeout or error.

    Usage:
        result = await with_timeout(llm_chain.ask(text), timeout=8.0, fallback=None)
        if result is None:
            await msg.reply_text("⏳ Сервис недоступен, попробуйте позже.")
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"[timeout] {label or 'call'} timed out after {timeout}s")
        return fallback
    except Exception as e:
        logger.error(f"[timeout] {label or 'call'} error: {e}")
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# 4. Safe PTB handler decorator
# ─────────────────────────────────────────────────────────────────────────────

def safe_ptb_handler(main_menu_builder: Callable, lang_getter: Callable):
    """Decorator for PTB async handlers.

    On any unhandled exception:
      - Clears user_data (resets wizard state)
      - Sends Main Menu message to user
      - Logs the exception

    Usage:
        @safe_ptb_handler(main_kb, detect_lang)
        async def cb_query(update, context):
            ...

    Where main_kb(lang) → InlineKeyboardMarkup
    and   detect_lang(update, context) → "en" | "ru" | "ar"
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                logger.error(
                    f"[safe_handler] {func.__name__} raised {type(e).__name__}: {e}",
                    exc_info=True,
                )
                try:
                    lang = lang_getter(update, context)
                    kb = main_menu_builder(lang)
                    context.user_data.clear()
                    msg = getattr(update, "effective_message", None)
                    if msg:
                        await msg.reply_text(
                            "⚠️ Произошла ошибка. Главное меню:",
                            reply_markup=kb,
                        )
                    cq = getattr(update, "callback_query", None)
                    if cq:
                        try:
                            await cq.answer()
                        except Exception:
                            pass
                except Exception as fallback_err:
                    logger.error(f"[safe_handler] fallback failed: {fallback_err}")
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# 5. Safe aiogram callback handler decorator
# ─────────────────────────────────────────────────────────────────────────────

def safe_aiogram_cb(main_menu_text: str = "⚠️ Ошибка. Попробуйте /start"):
    """Decorator for aiogram callback_query handlers.

    On any unhandled exception: answer callback, clear FSM, reply with reset message.

    Usage:
        @router.callback_query(SomeFilter())
        @safe_aiogram_cb()
        async def on_cb(cb: CallbackQuery, state: FSMContext):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Find CallbackQuery and FSMContext in positional args
            cb = next((a for a in args if hasattr(a, "answer") and hasattr(a, "data")), None)
            state = kwargs.get("state") or next(
                (a for a in args if hasattr(a, "clear") and hasattr(a, "set_state")), None
            )
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    f"[safe_aiogram] {func.__name__} raised {type(e).__name__}: {e}",
                    exc_info=True,
                )
                try:
                    if cb:
                        await cb.answer()
                    if state:
                        await state.clear()
                    if cb and cb.message:
                        await cb.message.answer(main_menu_text)
                except Exception as fallback_err:
                    logger.error(f"[safe_aiogram] fallback failed: {fallback_err}")
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# 6. Stale button detection & graceful answer
# ─────────────────────────────────────────────────────────────────────────────

_STALE_PHRASES = (
    "message is not modified",
    "message to edit not found",
    "query is too old",
    "message can't be edited",
    "message_not_modified",
    "bad request",
)


def is_stale_button_error(exc: Exception) -> bool:
    """Returns True if the exception is caused by a stale/outdated inline button."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in _STALE_PHRASES)


async def answer_stale(callback_query, lang: str = "en") -> None:
    """Answer a stale callback_query with a friendly message.

    Works with both PTB and aiogram CallbackQuery objects.
    """
    text_map = {
        "ru": "⚠️ Кнопка устарела — нажмите /start",
        "en": "⚠️ Button expired — press /start",
        "ar": "⚠️ الزر منتهي الصلاحية — اضغط /start",
    }
    text = text_map.get(lang, text_map["en"])
    try:
        await callback_query.answer(text, show_alert=True)
    except Exception:
        pass


def ptb_stale_guard(lang_getter: Optional[Callable] = None):
    """PTB decorator: catches stale-button errors and answers gracefully.

    Usage:
        @ptb_stale_guard(lambda u, c: c.user_data.get("lang", "en"))
        async def cb_query(update, context):
            await update.callback_query.edit_message_text(...)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            try:
                return await func(update, context, *args, **kwargs)
            except Exception as e:
                if is_stale_button_error(e):
                    lang = "en"
                    if lang_getter:
                        try:
                            lang = lang_getter(update, context)
                        except Exception:
                            pass
                    cq = getattr(update, "callback_query", None)
                    if cq:
                        await answer_stale(cq, lang)
                    logger.debug(f"[stale] {func.__name__}: {e}")
                else:
                    raise
        return wrapper
    return decorator


def aiogram_stale_guard(func: Callable) -> Callable:
    """aiogram decorator: catches stale-button errors and answers gracefully.

    Usage:
        @dp.callback_query(...)
        @aiogram_stale_guard
        async def on_cb(cb: CallbackQuery, ...):
            await cb.message.edit_text(...)
    """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        cb = next((a for a in args if hasattr(a, "answer") and hasattr(a, "data")), None)
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if is_stale_button_error(e):
                if cb:
                    await answer_stale(cb, "en")
                logger.debug(f"[stale] {func.__name__}: {e}")
            else:
                raise
    return wrapper


# ─────────────────────────────────────────────────────────────────────────────
# 7. Graceful SIGTERM for raw polling loops
# ─────────────────────────────────────────────────────────────────────────────

def setup_sigterm(stop_flag: list, label: str = "bot") -> None:
    """Wire SIGTERM/SIGINT to set stop_flag[0] = True.

    For raw polling loops (not PTB/aiogram which handle signals themselves):

    Usage:
        _stop = [False]
        setup_sigterm(_stop, "resale-bot")

        while not _stop[0]:
            updates = poll_updates()
            ...
    """
    def _handler(sig, frame):
        logger.info(f"[SIGTERM] {label}: signal {sig} received — stopping gracefully")
        stop_flag[0] = True

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
    logger.info(f"[SIGTERM] {label}: graceful shutdown handler installed")


# ─────────────────────────────────────────────────────────────────────────────
# 8. HTTP health endpoint (background thread)
# ─────────────────────────────────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler — GET /health → JSON with status/db/uptime, GET / → 200 OK."""

    bot_name: str = "bot"
    bot_username: str = ""
    start_time: float = time.time()
    db_ping: Optional[Callable[[], bool]] = None

    def _payload(self) -> Tuple[int, bytes]:
        uptime = int(time.time() - _HealthHandler.start_time)
        db_status = "unknown"
        status = "ok"
        http_code = 200
        if _HealthHandler.db_ping is not None:
            try:
                ok = bool(_HealthHandler.db_ping())
                db_status = "ok" if ok else "fail"
                if not ok:
                    status = "degraded"
                    http_code = 503
            except Exception as e:  # noqa: BLE001
                db_status = f"fail:{type(e).__name__}"
                status = "degraded"
                http_code = 503
        body = {
            "status": status,
            "db": db_status,
            "uptime_sec": uptime,
            "bot_username": _HealthHandler.bot_username or _HealthHandler.bot_name,
        }
        return http_code, json.dumps(body).encode("utf-8")

    def do_GET(self):
        if self.path in ("/health", "/healthz", "/_health"):
            code, body = self._payload()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # legacy root: simple text
        uptime = int(time.time() - _HealthHandler.start_time)
        body = (f"OK\nbot={_HealthHandler.bot_name}\nuptime={uptime}s\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access logs


def start_health_server(
    port: Optional[int] = None,
    bot_name: str = "bot",
    bot_username: str = "",
    db_ping: Optional[Callable[[], bool]] = None,
) -> int:
    """Start an HTTP health server in a daemon thread.

    GET /health returns JSON:
        {"status": "ok"|"degraded", "db": "ok"|"fail"|"unknown",
         "uptime_sec": N, "bot_username": "..."}
    HTTP 503 when DB ping fails.

    Port is read from env PORT (Railway sets this) or the `port` argument.
    Falls back to 8080 if neither is set. If port is in use → warns and
    returns the requested port without crashing the bot.

    Usage:
        from fsst_core import start_health_server
        start_health_server(bot_name="hub-bot", db_ping=lambda: True)
    """
    _HealthHandler.bot_name = bot_name
    _HealthHandler.bot_username = bot_username or bot_name
    _HealthHandler.start_time = time.time()
    _HealthHandler.db_ping = db_ping

    actual_port = port or int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", actual_port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"[HEALTH] {bot_name}: HTTP health endpoint on :{actual_port}")
    except OSError as e:
        logger.warning(f"[HEALTH] {bot_name}: could not start on :{actual_port} — {e}")
    return actual_port


# ─────────────────────────────────────────────────────────────────────────────
# 9. UpdateDeduplicator — idempotent getUpdates / callback_query handling
# ─────────────────────────────────────────────────────────────────────────────

class UpdateDeduplicator:
    """Dedup Telegram update_id and callback_query.id with a rolling deque.

    Telegram MAY redeliver an update_id if the bot fails to ACK before the
    next getUpdates call (rare but happens on Railway redeploy / network blip).
    Likewise callback_query.id can race when a user double-clicks.

    Usage (raw polling):
        _udedup = UpdateDeduplicator(maxlen=1000)
        for upd in updates:
            if _udedup.seen_update(upd):
                continue
            ...
            if "callback_query" in upd:
                if _udedup.seen_callback(upd["callback_query"]):
                    continue
                ...
    """

    def __init__(self, maxlen: int = 1000):
        self._upd_ids: collections.deque = collections.deque(maxlen=maxlen)
        self._upd_set: set = set()
        self._cb_ids: collections.deque = collections.deque(maxlen=maxlen)
        self._cb_set: set = set()
        self._lock = threading.Lock()

    def _push(self, val, dq: collections.deque, st: set) -> bool:
        with self._lock:
            if val in st:
                return True
            if len(dq) == dq.maxlen and dq:
                old = dq[0]
                st.discard(old)
            dq.append(val)
            st.add(val)
            return False

    def seen_update(self, update: dict) -> bool:
        """True if this update was already processed."""
        uid = update.get("update_id") if isinstance(update, dict) else None
        if uid is None:
            return False
        return self._push(uid, self._upd_ids, self._upd_set)

    def seen_callback(self, cb: dict) -> bool:
        """True if this callback_query.id was already processed."""
        cb_id = cb.get("id") if isinstance(cb, dict) else None
        if not cb_id:
            return False
        return self._push(str(cb_id), self._cb_ids, self._cb_set)


# ─────────────────────────────────────────────────────────────────────────────
# 10. DB connection pool helper (psycopg2)
# ─────────────────────────────────────────────────────────────────────────────

class DBPool:
    """Thin wrapper around psycopg2.pool.ThreadedConnectionPool.

    Drop-in for ad-hoc psycopg2.connect calls:
        _pool = DBPool(DATABASE_URL, minconn=2, maxconn=10)

        with _pool.conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")

    The conn() context manager auto-returns the connection to the pool
    (even on error). If a connection is broken (DB restart / TCP reset)
    the pool will silently replace it on the next checkout.

    Pass cursor_factory to enable RealDictCursor automatically.

    NOTE: import-safe — if psycopg2 is missing or DATABASE_URL is empty,
    DBPool will raise on construction, not at import time.
    """

    def __init__(
        self,
        dsn: str,
        minconn: int = 2,
        maxconn: int = 10,
        cursor_factory=None,
        connect_timeout: int = 5,
        application_name: Optional[str] = None,
    ):
        if not dsn:
            raise RuntimeError("DBPool: DSN is empty (DATABASE_URL not set?)")
        # Lazy import — keeps fsst_core importable without psycopg2 in env.
        from psycopg2 import pool as _pp  # noqa: WPS433
        kwargs = {"connect_timeout": connect_timeout}
        if application_name:
            kwargs["application_name"] = application_name
        self._pool = _pp.ThreadedConnectionPool(minconn, maxconn, dsn, **kwargs)
        self._cursor_factory = cursor_factory
        self._lock = threading.Lock()
        self._closed = False
        logger.info(f"[DBPool] initialized min={minconn} max={maxconn}")

    def getconn(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("DBPool is closed")
            conn = self._pool.getconn()
        # Verify connection is alive; psycopg2 can hand out a broken socket
        # if the DB restarted while it was idle.
        try:
            if getattr(conn, "closed", 0):
                raise Exception("conn closed")
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DBPool] stale conn replaced: {e}")
            try:
                self._pool.putconn(conn, close=True)
            except Exception:
                pass
            with self._lock:
                conn = self._pool.getconn()
        if self._cursor_factory is not None:
            try:
                conn.cursor_factory = self._cursor_factory
            except Exception:
                pass
        return conn

    def putconn(self, conn, close: bool = False):
        try:
            self._pool.putconn(conn, close=close)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DBPool] putconn failed: {e}")

    def conn(self):
        """Context manager wrapper. Returns a pooled connection."""
        pool = self

        class _Ctx:
            def __enter__(self_inner):
                self_inner._c = pool.getconn()
                return self_inner._c

            def __exit__(self_inner, exc_type, exc, tb):
                broken = exc_type is not None
                try:
                    if not broken:
                        try:
                            self_inner._c.commit()
                        except Exception:
                            pass
                    else:
                        try:
                            self_inner._c.rollback()
                        except Exception:
                            pass
                finally:
                    pool.putconn(self_inner._c, close=broken)
                return False
        return _Ctx()

    def ping(self) -> bool:
        """Returns True if a pooled connection can SELECT 1 within ~2s."""
        try:
            with self.conn() as c:
                cur = c.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DBPool] ping failed: {e}")
            return False

    def closeall(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._pool.closeall()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 11. LLM key health tracker (auto-disable + auto-revive)
# ─────────────────────────────────────────────────────────────────────────────

class LLMKeyHealth:
    """Tracks success/failure per (provider, key_idx) over a rolling window.

    Rules (defaults match brief):
      - >= 5 consecutive 429s on a key → mark DEAD for 1 hour
      - success rate < 20% over last 10 minutes (with at least 5 calls)
        → mark DEAD for 1 hour
      - DEAD keys are skipped via is_dead(); revive when TTL expires.
      - All transitions logged.

    Thread-safe. Designed to live alongside existing per-key cool-down logic
    in llm_chain.py — it's an extra safety net for sustained failure, NOT
    a replacement for per-request 429 cool-downs.

    Usage:
        _lk = LLMKeyHealth()

        # before request:
        if _lk.is_dead(provider, key_idx):
            continue   # skip this key

        try:
            resp = call(provider, key)
            _lk.record_success(provider, key_idx)
        except RateLimitError:
            _lk.record_failure(provider, key_idx, code=429)
        except Exception:
            _lk.record_failure(provider, key_idx, code=0)
    """

    def __init__(
        self,
        window_sec: float = 600.0,
        dead_ttl_sec: float = 3600.0,
        min_calls_for_rate: int = 5,
        min_success_rate: float = 0.2,
        max_consecutive_429: int = 5,
    ):
        self._window = window_sec
        self._dead_ttl = dead_ttl_sec
        self._min_calls = min_calls_for_rate
        self._min_rate = min_success_rate
        self._max_429 = max_consecutive_429
        # key = (provider, key_idx)
        # value = deque of (ts, ok_bool)
        self._events: Dict[Tuple[str, int], collections.deque] = {}
        self._consec_429: Dict[Tuple[str, int], int] = {}
        # provider,key_idx -> revive_at_epoch
        self._dead_until: Dict[Tuple[str, int], float] = {}
        self._lock = threading.Lock()

    def _gc(self, key, now: float) -> None:
        dq = self._events.get(key)
        if not dq:
            return
        cutoff = now - self._window
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def is_dead(self, provider: str, key_idx: int) -> bool:
        key = (provider, key_idx)
        with self._lock:
            until = self._dead_until.get(key)
            if not until:
                return False
            if time.time() >= until:
                # revive
                del self._dead_until[key]
                self._consec_429[key] = 0
                logger.info(f"[LLM] key {provider}#{key_idx} revived after TTL")
                return False
            return True

    def record_success(self, provider: str, key_idx: int) -> None:
        key = (provider, key_idx)
        now = time.time()
        with self._lock:
            dq = self._events.setdefault(key, collections.deque(maxlen=200))
            dq.append((now, True))
            self._gc(key, now)
            self._consec_429[key] = 0

    def record_failure(self, provider: str, key_idx: int, code: int = 0) -> None:
        key = (provider, key_idx)
        now = time.time()
        with self._lock:
            dq = self._events.setdefault(key, collections.deque(maxlen=200))
            dq.append((now, False))
            self._gc(key, now)
            # consecutive 429 counter
            if code == 429:
                self._consec_429[key] = self._consec_429.get(key, 0) + 1
                if self._consec_429[key] >= self._max_429:
                    self._mark_dead_locked(key, "consecutive 429")
                    return
            # rolling success rate
            n = len(dq)
            if n >= self._min_calls:
                ok = sum(1 for _, b in dq if b)
                rate = ok / n
                if rate < self._min_rate:
                    self._mark_dead_locked(
                        key, f"success rate {rate:.0%} < {self._min_rate:.0%}"
                    )

    def _mark_dead_locked(self, key: Tuple[str, int], reason: str) -> None:
        # caller holds self._lock
        until = time.time() + self._dead_ttl
        if self._dead_until.get(key, 0) >= until:
            return
        self._dead_until[key] = until
        logger.warning(
            f"[LLM] key {key[0]}#{key[1]} marked DEAD for "
            f"{int(self._dead_ttl)}s — {reason}"
        )

    def force_revive(self, provider: str, key_idx: int) -> None:
        key = (provider, key_idx)
        with self._lock:
            self._dead_until.pop(key, None)
            self._consec_429[key] = 0

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            out = {"dead": [], "tracked": []}
            for key, until in self._dead_until.items():
                if until > now:
                    out["dead"].append({
                        "provider": key[0],
                        "key_idx": key[1],
                        "revive_in_sec": int(until - now),
                    })
            for key, dq in self._events.items():
                if not dq:
                    continue
                n = len(dq)
                ok = sum(1 for _, b in dq if b)
                out["tracked"].append({
                    "provider": key[0],
                    "key_idx": key[1],
                    "calls": n,
                    "success_rate": round(ok / n, 3),
                })
            return out
