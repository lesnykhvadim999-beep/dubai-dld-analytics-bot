"""Stability utilities — TTL cache, structured logging, retry, validation, metrics.

Self-contained, no external deps beyond stdlib. Import what you need:

    from stability import (
        ttl_cache, log_event, retry_on_transient,
        validate_price, validate_bedrooms, validate_area_name,
        validate_lang, validate_phone, validate_user_text,
        metrics, start_metrics_server,
    )

Designed to be dropped into every bot in the ecosystem identically.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Optional

# ─── TTL CACHE ────────────────────────────────────────────────────────────────

class TTLCache:
    """Tiny thread-safe TTL cache. dict[key]=(expires_at, value).
    Lazy eviction on get/set; bounded by maxsize via LRU-ish drop of oldest expired.
    """

    def __init__(self, maxsize: int = 500, ttl: int = 300) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            exp, val = entry
            if exp < time.time():
                self._data.pop(key, None)
                return default
            return val

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            now = time.time()
            if len(self._data) >= self.maxsize:
                # Drop expired first
                dead = [k for k, (e, _) in self._data.items() if e < now]
                for k in dead:
                    self._data.pop(k, None)
                # If still full, drop oldest
                if len(self._data) >= self.maxsize:
                    oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
                    self._data.pop(oldest, None)
            self._data[key] = (now + self.ttl, value)

    def __contains__(self, key: Any) -> bool:
        return self.get(key, _SENTINEL) is not _SENTINEL

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_SENTINEL = object()


def ttl_cache(maxsize: int = 500, ttl: int = 300) -> Callable:
    """Decorator: simple TTL cache by positional args (must be hashable).

    Usage:
        @ttl_cache(maxsize=500, ttl=300)
        def get_area_stats(area: str): ...
    """
    cache = TTLCache(maxsize=maxsize, ttl=ttl)

    def decor(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            try:
                key = (args, tuple(sorted(kwargs.items())))
            except TypeError:
                # unhashable → bypass cache
                return fn(*args, **kwargs)
            hit = cache.get(key, _SENTINEL)
            if hit is not _SENTINEL:
                metrics.inc("cache_hits_total", fn=fn.__name__)
                return hit
            metrics.inc("cache_misses_total", fn=fn.__name__)
            result = fn(*args, **kwargs)
            cache.set(key, result)
            return result

        inner._cache = cache  # type: ignore[attr-defined]
        inner.cache_clear = cache.clear  # type: ignore[attr-defined]
        return inner

    return decor


# ─── STRUCTURED LOGGING ───────────────────────────────────────────────────────

_BOT_NAME = os.environ.get("BOT_NAME", "bot")


def _bot_name() -> str:
    return _BOT_NAME


def set_bot_name(name: str) -> None:
    global _BOT_NAME
    _BOT_NAME = name


_logger: Optional[logging.Logger] = None


def get_logger(name: str = "stability") -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    log = logging.getLogger(name)
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    _logger = log
    return log


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


# ─── DISTRIBUTED TRACING (request_id propagation) ────────────────────────────
#
# request_id is propagated across async/threading boundaries via ContextVar.
# Auto-injected into every log_event() call so Sentinel can grep one ID and
# see the full request lifecycle: handler entry → DB query → LLM call →
# response.
#
# Usage:
#     # entry point (Telegram handler):
#     async def on_message(update, ctx):
#         with request_context(user_id=update.effective_user.id) as rid:
#             ... rest of handler ...
#
#     # downstream code — no changes needed, log_event picks up rid:
#     log_event("INFO", msg="db query", table="listings")
#
#     # cross-bot deep link:
#     url = f"https://t.me/other_bot?start=foo__rid_{get_request_id()}"

_request_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None,
)
_user_id_var: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "user_id", default=None,
)


def get_request_id() -> Optional[str]:
    """Return current request_id from contextvar, or None."""
    return _request_id_var.get()


def set_request_id(rid: Optional[str]) -> contextvars.Token:
    """Set request_id for the current async/thread context. Returns token
    that can be passed to reset_request_id() to restore the previous value."""
    return _request_id_var.set(rid)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)


def get_user_id() -> Optional[int]:
    return _user_id_var.get()


class request_context:
    """Context manager / decorator that sets request_id (and optionally user_id)
    for everything inside it. New request_id auto-generated if not supplied.

    Usage as context manager:
        with request_context(user_id=12345) as rid:
            log_event("INFO", msg="start")  # rid is auto-attached
            do_work()

    Usage as decorator:
        @request_context()
        async def handler(update, ctx): ...

    External rid (e.g. from cross-bot deep link) can be passed in:
        with request_context(rid="abc123", user_id=u.id):
            ...
    """

    def __init__(self, rid: Optional[str] = None,
                 user_id: Optional[int] = None) -> None:
        self.rid = rid or new_request_id()
        self.user_id = user_id
        self._rid_token: Optional[contextvars.Token] = None
        self._uid_token: Optional[contextvars.Token] = None

    def __enter__(self) -> str:
        self._rid_token = _request_id_var.set(self.rid)
        if self.user_id is not None:
            self._uid_token = _user_id_var.set(self.user_id)
        return self.rid

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._rid_token is not None:
            _request_id_var.reset(self._rid_token)
        if self._uid_token is not None:
            _user_id_var.reset(self._uid_token)

    def __call__(self, fn: Callable) -> Callable:
        import asyncio
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def inner_async(*a, **kw):
                with request_context(self.rid, self.user_id):
                    return await fn(*a, **kw)
            return inner_async

        @functools.wraps(fn)
        def inner(*a, **kw):
            with request_context(self.rid, self.user_id):
                return fn(*a, **kw)
        return inner


def extract_rid_from_start_param(start_param: Optional[str]) -> Optional[str]:
    """Parse a Telegram /start payload like 'foo__rid_abc123' and return rid.

    Bots receive cross-bot deep links of the form:
        https://t.me/bot?start=<action>__rid_<requestid>

    Returns None if no rid suffix.
    """
    if not start_param or "__rid_" not in start_param:
        return None
    try:
        return start_param.rsplit("__rid_", 1)[1][:32] or None
    except Exception:
        return None


def append_rid_to_start_param(payload: str,
                              rid: Optional[str] = None) -> str:
    """Append current (or given) request_id to a Telegram start payload.

        append_rid_to_start_param("buy_listing_42")
        → "buy_listing_42__rid_a3f9c8e1b2d4"
    """
    rid = rid or get_request_id()
    if not rid:
        return payload
    return f"{payload}__rid_{rid}"


def log_event(level: str = "INFO", **fields: Any) -> None:
    """Emit a single-line JSON log. Always includes ts + bot + level + msg.

    Automatically injects request_id and user_id from the current context if
    they aren't already in `fields`. This is what enables single-grep tracing.

    Usage:
        log_event("INFO", msg="incoming", user_id=u.id, cmd="/start")
        log_event("ERROR", msg="db fail", err=str(e), table="listings")
    """
    payload: dict[str, Any] = {
        "ts": time.time(),
        "bot": _bot_name(),
        "level": level,
    }
    # Auto-inject tracing context (caller can still override).
    rid = _request_id_var.get()
    if rid is not None and "request_id" not in fields and "rid" not in fields:
        payload["request_id"] = rid
    uid_ctx = _user_id_var.get()
    if uid_ctx is not None and "user_id" not in fields:
        payload["user_id"] = uid_ctx
    payload.update(fields)
    log = get_logger()
    lvl = getattr(logging, level.upper(), logging.INFO)
    try:
        log.log(lvl, json.dumps(payload, default=str, ensure_ascii=False))
    except Exception:
        # never fail caller because of logging
        log.log(lvl, str(payload))


# ─── RETRY ON TRANSIENT ERRORS ────────────────────────────────────────────────

# Class-name based detection — works without importing telegram/db libs.
_TRANSIENT_NAMES = {
    "OperationalError", "InterfaceError", "DBAPIError",
    "InternalError", "DisconnectionError", "ConnectionError",
    "TimeoutError", "ReadTimeout", "ReadTimeoutError",
    "RemoteProtocolError", "NetworkError", "BadGateway",
    "RetryAfter", "TimedOut", "ConnectTimeout", "ConnectError",
    "ServerDisconnectedError", "ClientOSError",
}


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _TRANSIENT_NAMES:
        return True
    msg = str(exc).lower()
    if any(s in msg for s in (
        "connection reset", "connection refused", "timed out",
        "temporarily unavailable", "server disconnected", "broken pipe",
        "5xx", "502", "503", "504", "429",
    )):
        return True
    return False


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Extract retry_after from Telegram-like exceptions (sync or async)."""
    for attr in ("retry_after", "retry_after_seconds"):
        v = getattr(exc, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    m = re.search(r"retry[_ ]after[^\d]*(\d+(?:\.\d+)?)", str(exc).lower())
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def retry_on_transient(retries: int = 3, base_delay: float = 0.5,
                       max_delay: float = 30.0) -> Callable:
    """Sync decorator. Retries up to N times on transient errors.

    Respects retry_after if present (e.g. Telegram 429).
    Exponential backoff with jitter otherwise.
    """

    def decor(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            last: Optional[BaseException] = None
            for attempt in range(retries):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if not _is_transient(e) or attempt == retries - 1:
                        raise
                    ra = _retry_after_seconds(e)
                    if ra is not None:
                        sleep_for = min(max_delay, ra + random.uniform(0, 0.5))
                    else:
                        sleep_for = min(max_delay, base_delay * (2 ** attempt))
                        sleep_for += random.uniform(0, 0.3)
                    metrics.inc("retries_total", fn=fn.__name__)
                    log_event("WARNING", msg="retry", fn=fn.__name__,
                              attempt=attempt + 1, sleep=sleep_for,
                              err=type(e).__name__)
                    time.sleep(sleep_for)
            if last is not None:
                raise last
            return None

        return inner

    return decor


def retry_on_transient_async(retries: int = 3, base_delay: float = 0.5,
                             max_delay: float = 30.0) -> Callable:
    """Async version of retry_on_transient."""
    import asyncio

    def decor(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            last: Optional[BaseException] = None
            for attempt in range(retries):
                try:
                    return await fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
                    if not _is_transient(e) or attempt == retries - 1:
                        raise
                    ra = _retry_after_seconds(e)
                    if ra is not None:
                        sleep_for = min(max_delay, ra + random.uniform(0, 0.5))
                    else:
                        sleep_for = min(max_delay, base_delay * (2 ** attempt))
                        sleep_for += random.uniform(0, 0.3)
                    metrics.inc("retries_total", fn=fn.__name__)
                    log_event("WARNING", msg="retry_async", fn=fn.__name__,
                              attempt=attempt + 1, sleep=sleep_for,
                              err=type(e).__name__)
                    await asyncio.sleep(sleep_for)
            if last is not None:
                raise last
            return None

        return inner

    return decor


# ─── INPUT VALIDATION ─────────────────────────────────────────────────────────

class ValidationError(ValueError):
    """Raised when user input fails validation."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


SUPPORTED_LANGS = {"ru", "en", "ar"}

# Allow letters/digits/space/dash/apostrophe/comma/period/&/parens
_AREA_NAME_RE = re.compile(r"[^\w\s\-',.\&()/]", re.UNICODE)


def validate_price(value: Any, lang: str = "en") -> float:
    """Price in AED. Must be numeric, 1_000..1_000_000_000."""
    try:
        s = str(value).strip().replace(",", "").replace(" ", "")
        # tolerate k/m suffixes
        mult = 1.0
        if s.lower().endswith("m"):
            mult, s = 1_000_000.0, s[:-1]
        elif s.lower().endswith("k"):
            mult, s = 1_000.0, s[:-1]
        v = float(s) * mult
    except (TypeError, ValueError):
        raise ValidationError("price_not_numeric",
                              _msg(lang, "price_not_numeric"))
    if not (1_000.0 <= v <= 1_000_000_000.0):
        raise ValidationError("price_out_of_range",
                              _msg(lang, "price_out_of_range"))
    return v


def validate_bedrooms(value: Any, lang: str = "en") -> int:
    """Bedrooms 0..15. 'studio' → 0."""
    if value is None:
        raise ValidationError("br_required", _msg(lang, "br_required"))
    s = str(value).strip().lower()
    if s in {"studio", "студия", "ستوديو"}:
        return 0
    try:
        v = int(float(s))
    except (TypeError, ValueError):
        raise ValidationError("br_not_int", _msg(lang, "br_not_int"))
    if not (0 <= v <= 15):
        raise ValidationError("br_out_of_range",
                              _msg(lang, "br_out_of_range"))
    return v


def validate_area_name(value: Any, lang: str = "en", max_len: int = 150) -> str:
    """Area/building name. Strip dangerous chars, cap at max_len."""
    if value is None:
        raise ValidationError("area_required", _msg(lang, "area_required"))
    s = str(value).strip()
    if not s:
        raise ValidationError("area_required", _msg(lang, "area_required"))
    if len(s) > max_len:
        s = s[:max_len]
    # remove dangerous chars (keep letters/digits/common punct)
    s = _AREA_NAME_RE.sub("", s)
    s = s.strip()
    if not s:
        raise ValidationError("area_invalid", _msg(lang, "area_invalid"))
    return s


def validate_lang(value: Any, default: str = "en") -> str:
    s = str(value or "").strip().lower()
    if s in SUPPORTED_LANGS:
        return s
    return default


def validate_phone(value: Any, lang: str = "en") -> str:
    """Phone digits only, 7..15 length."""
    if value is None:
        raise ValidationError("phone_required", _msg(lang, "phone_required"))
    digits = re.sub(r"\D", "", str(value))
    if not (7 <= len(digits) <= 15):
        raise ValidationError("phone_invalid", _msg(lang, "phone_invalid"))
    return digits


def validate_user_text(value: Any, lang: str = "en",
                       max_len: int = 4000) -> str:
    """Generic user text. Strip control chars, cap at 4000 (Telegram limit)."""
    if value is None:
        return ""
    s = str(value)
    # Strip control chars except \n \t
    s = "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if len(s) > max_len:
        s = s[:max_len]
    return s


_VAL_MESSAGES = {
    "en": {
        "price_not_numeric": "Please send a numeric price (e.g. 2500000 or 2.5m).",
        "price_out_of_range": "Price must be between 1,000 and 1,000,000,000 AED.",
        "br_required": "Please specify bedrooms (0 for studio, up to 15).",
        "br_not_int": "Bedrooms must be a whole number (or 'studio').",
        "br_out_of_range": "Bedrooms must be between 0 and 15.",
        "area_required": "Please specify the area or building name.",
        "area_invalid": "That area name looks invalid. Please use plain text.",
        "phone_required": "Please send a phone number.",
        "phone_invalid": "Phone must contain 7–15 digits.",
    },
    "ru": {
        "price_not_numeric": "Пришлите числовую цену (например 2500000 или 2.5m).",
        "price_out_of_range": "Цена должна быть от 1 000 до 1 000 000 000 AED.",
        "br_required": "Укажите количество спален (0 — студия, до 15).",
        "br_not_int": "Спальни — целое число (или 'studio').",
        "br_out_of_range": "Спальни — от 0 до 15.",
        "area_required": "Укажите район или название здания.",
        "area_invalid": "Название района выглядит некорректно. Используйте обычный текст.",
        "phone_required": "Пришлите номер телефона.",
        "phone_invalid": "Телефон должен содержать 7–15 цифр.",
    },
    "ar": {
        "price_not_numeric": "يرجى إرسال سعر رقمي.",
        "price_out_of_range": "يجب أن يكون السعر بين 1,000 و 1,000,000,000 درهم.",
        "br_required": "يرجى تحديد عدد غرف النوم (0 للستوديو، حتى 15).",
        "br_not_int": "يجب أن يكون عدد غرف النوم رقمًا صحيحًا.",
        "br_out_of_range": "يجب أن تكون غرف النوم بين 0 و 15.",
        "area_required": "يرجى تحديد المنطقة أو اسم المبنى.",
        "area_invalid": "اسم المنطقة يبدو غير صالح.",
        "phone_required": "يرجى إرسال رقم هاتف.",
        "phone_invalid": "يجب أن يحتوي الهاتف على 7–15 رقمًا.",
    },
}


def _msg(lang: str, key: str) -> str:
    lang = validate_lang(lang, default="en")
    return _VAL_MESSAGES.get(lang, _VAL_MESSAGES["en"]).get(key, key)


# ─── IDEMPOTENCY KEYS FOR WRITES ──────────────────────────────────────────────
#
# Telegram retries, double-taps, network glitches and bot restarts mean the
# SAME user write request can arrive 2-5 times in a few seconds. Without
# idempotency keys this creates duplicate listings, duplicate leads,
# double-charged subscriptions, etc.
#
# Pattern: caller computes a deterministic key from
# (user_id, action, time_bucket_minute) and wraps the write in
# idempotent_write(). If the key already exists in `idempotency_keys`, the
# previously stored response is returned without re-running fn.
#
# Required DB table (see shared/migrations/idempotency_keys.sql):
#     CREATE TABLE idempotency_keys (
#         key        TEXT PRIMARY KEY,
#         response   JSONB,
#         created_at TIMESTAMPTZ NOT NULL DEFAULT now()
#     );
#     CREATE INDEX idempotency_keys_created_at_idx
#         ON idempotency_keys(created_at);
#
# A background job (or pg_cron) deletes rows older than 24h.

_IDEMPOTENCY_BUCKET_SECONDS = 60  # 1-minute resolution by default


def make_idempotency_key(user_id: Any,
                         action: str,
                         bucket_seconds: int = _IDEMPOTENCY_BUCKET_SECONDS,
                         extra: Optional[str] = None) -> str:
    """Build a deterministic idempotency key.

    Key = sha1(bot|user_id|action|bucket|extra)[:24]

    bucket = floor(now / bucket_seconds) — so two identical requests inside
    the same minute share a key, but the same user can repeat the action
    legitimately in the next minute.

    `extra` lets you scope the key to a particular target (e.g. a listing
    id, a search query hash) so unrelated writes never collide.
    """
    bucket = int(time.time() // max(1, bucket_seconds))
    parts = [
        _bot_name(),
        str(user_id),
        str(action),
        str(bucket),
        str(extra or ""),
    ]
    raw = "|".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()[:24]


def idempotent_write(conn: Any,
                     key: str,
                     fn: Callable[[], Any],
                     ttl_hours: int = 24,
                     table: str = "idempotency_keys") -> Any:
    """Run `fn` exactly once for the given idempotency key.

    Args:
        conn: an open psycopg2-compatible connection (auto-commit or managed
              externally — we use a SAVEPOINT-free pattern via short txns).
        key:  the idempotency key (see make_idempotency_key()).
        fn:   zero-arg callable that performs the write and returns a
              JSON-serialisable response.
        ttl_hours: rows older than this can be GC'd by the cleanup job. Does
              NOT affect the duplicate-detection logic itself (PRIMARY KEY
              does that).
        table: idempotency table name (override for tests).

    Returns:
        cached response (dict / list / scalar) if key already present;
        otherwise fn()'s return value (also stored).

    On any DB error the helper falls back to calling fn() directly — losing
    idempotency is better than losing the user's write. Metrics tagged
    `idempotent_writes_total{result=hit|miss|error}` so we can monitor it.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT response FROM {table} WHERE key = %s",  # noqa: S608
                (key,),
            )
            row = cur.fetchone()
        if row is not None:
            metrics.inc("idempotent_writes_total", result="hit")
            log_event("INFO", msg="idempotency_hit", key=key)
            cached = row[0]
            if isinstance(cached, (str, bytes, bytearray)):
                try:
                    cached = json.loads(cached)
                except Exception:
                    pass
            return cached
    except Exception as e:
        metrics.inc("idempotent_writes_total", result="error")
        log_event("WARNING", msg="idempotency_lookup_failed",
                  key=key, err=str(e))
        return fn()  # fall back: better to write than to lose data

    # Miss → run the write, then persist response.
    result = fn()
    try:
        payload = json.dumps(result, default=str, ensure_ascii=False)
    except Exception:
        payload = json.dumps({"_unserialisable": True}, ensure_ascii=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table}(key, response) VALUES (%s, %s) "  # noqa: S608
                f"ON CONFLICT (key) DO NOTHING",
                (key, payload),
            )
        metrics.inc("idempotent_writes_total", result="miss")
    except Exception as e:
        metrics.inc("idempotent_writes_total", result="error")
        log_event("WARNING", msg="idempotency_insert_failed",
                  key=key, err=str(e))
    return result


def cleanup_idempotency_keys(conn: Any, older_than_hours: int = 24,
                             table: str = "idempotency_keys") -> int:
    """Delete idempotency rows older than the cutoff. Returns rows removed."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} "  # noqa: S608
                f"WHERE created_at < now() - INTERVAL '%s hours'",
                (older_than_hours,),
            )
            return cur.rowcount or 0
    except Exception as e:
        log_event("WARNING", msg="idempotency_cleanup_failed", err=str(e))
        return 0


# ─── METRICS ──────────────────────────────────────────────────────────────────

class Metrics:
    """In-process Prometheus-style counters + simple histograms.

    Thread-safe. Exposes /metrics in plain text (Prometheus exposition format).
    """

    def __init__(self) -> None:
        self._counters: dict[str, dict[tuple, float]] = defaultdict(dict)
        self._histograms: dict[str, dict[tuple, list[float]]] = defaultdict(dict)
        self._lock = threading.Lock()

    @staticmethod
    def _label_key(labels: dict) -> tuple:
        return tuple(sorted(labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        with self._lock:
            key = self._label_key({k: str(v) for k, v in labels.items()})
            d = self._counters[name]
            d[key] = d.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        with self._lock:
            key = self._label_key({k: str(v) for k, v in labels.items()})
            d = self._histograms[name]
            d.setdefault(key, []).append(float(value))
            # bound memory: keep last 1000 samples per series
            if len(d[key]) > 1000:
                d[key] = d[key][-1000:]

    def render(self) -> str:
        out: list[str] = []
        with self._lock:
            for name, series in self._counters.items():
                out.append(f"# TYPE {name} counter")
                for label_key, val in series.items():
                    if label_key:
                        labels = ",".join(f'{k}="{v}"' for k, v in label_key)
                        out.append(f"{name}{{{labels}}} {val}")
                    else:
                        out.append(f"{name} {val}")
            for name, series in self._histograms.items():
                out.append(f"# TYPE {name} summary")
                for label_key, samples in series.items():
                    if not samples:
                        continue
                    n = len(samples)
                    s = sum(samples)
                    label_str = ""
                    if label_key:
                        label_str = "{" + ",".join(
                            f'{k}="{v}"' for k, v in label_key) + "}"
                    out.append(f"{name}_count{label_str} {n}")
                    out.append(f"{name}_sum{label_str} {s}")
                    out.append(f"{name}_avg{label_str} {s / n:.3f}")
        return "\n".join(out) + "\n"


metrics = Metrics()


def timed(handler_name: str) -> Callable:
    """Decorator: measures latency_ms into response_latency_ms{handler=...}."""

    def decor(fn: Callable) -> Callable:
        import asyncio
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def inner_async(*a, **kw):
                t0 = time.time()
                try:
                    return await fn(*a, **kw)
                finally:
                    metrics.observe("response_latency_ms",
                                    (time.time() - t0) * 1000.0,
                                    handler=handler_name)
            return inner_async

        @functools.wraps(fn)
        def inner(*a, **kw):
            t0 = time.time()
            try:
                return fn(*a, **kw)
            finally:
                metrics.observe("response_latency_ms",
                                (time.time() - t0) * 1000.0,
                                handler=handler_name)
        return inner

    return decor


# ─── /metrics HTTP server ─────────────────────────────────────────────────────

def start_metrics_server(port: Optional[int] = None) -> Optional[int]:
    """Start a tiny background HTTP server exposing /metrics.

    Returns the port used, or None if disabled.
    Reads PORT env if not provided. If PORT is unset and port is None, picks
    a free port via :0 binding. Safe to call multiple times (idempotent).
    """
    global _metrics_thread, _metrics_port
    if _metrics_thread is not None and _metrics_thread.is_alive():
        return _metrics_port
    if port is None:
        env_port = os.environ.get("METRICS_PORT") or os.environ.get("PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                port = 0
        else:
            port = 0

    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("/metrics", "/metrics/"):
                body = metrics.render().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type",
                                 "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.rstrip("/") in ("", "/", "/health"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok\n")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):  # silence default access log
            return

    try:
        srv = HTTPServer(("0.0.0.0", port), _Handler)
    except OSError as e:
        log_event("WARNING", msg="metrics_server_bind_failed",
                  port=port, err=str(e))
        return None

    actual_port = srv.server_address[1]
    _metrics_port = actual_port

    def _serve() -> None:
        try:
            srv.serve_forever()
        except Exception as e:  # noqa: BLE001
            log_event("ERROR", msg="metrics_server_crash", err=str(e))

    t = threading.Thread(target=_serve, daemon=True, name="metrics-http")
    t.start()
    _metrics_thread = t
    log_event("INFO", msg="metrics_server_started", port=actual_port)
    return actual_port


_metrics_thread: Optional[threading.Thread] = None
_metrics_port: Optional[int] = None


# ─── HELPERS for common bot patterns ──────────────────────────────────────────

def count_request(command: str = "unknown") -> None:
    metrics.inc("requests_total", bot=_bot_name(), command=command)


def count_error(err_type: str = "unknown") -> None:
    metrics.inc("errors_total", bot=_bot_name(), type=err_type)


def count_llm(provider: str, status: str = "ok") -> None:
    metrics.inc("llm_calls_total", provider=provider, status=status)


def count_db(table: str, status: str = "ok") -> None:
    metrics.inc("db_queries_total", table=table, status=status)


# ─── SLOW QUERY LOG ───────────────────────────────────────────────────────────

import hashlib  # noqa: E402
from collections import deque as _ev_deque  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402

SLOW_QUERY_THRESHOLD_MS = float(os.environ.get("SLOW_QUERY_MS", "500"))
SLOW_QUERY_LOG_PATH = os.environ.get("SLOW_QUERY_LOG", "slow_queries.log")
SLOW_QUERY_DB_TABLE = "slow_queries"
_SLOW_QUERY_MAX_TEXT = 500

_slow_q_logger: Optional[logging.Logger] = None
_slow_q_lock = threading.Lock()
_slow_q_db_table_ready = False
_slow_q_db_disabled = False  # disabled after first DB failure


def _get_slow_query_logger() -> logging.Logger:
    global _slow_q_logger
    if _slow_q_logger is not None:
        return _slow_q_logger
    lg = logging.getLogger("stability.slow_query")
    if not lg.handlers:
        try:
            h = RotatingFileHandler(
                SLOW_QUERY_LOG_PATH,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=3,
                encoding="utf-8",
            )
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
            lg.propagate = False
        except Exception:
            lg.addHandler(logging.NullHandler())
    _slow_q_logger = lg
    return lg


def _query_hash(query_text: str) -> str:
    norm = re.sub(r"\s+", " ", (query_text or "").strip().lower())[:2000]
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def _record_slow_query_db(ts: float, bot: str, qhash: str,
                          duration_ms: float, query_text: str) -> None:
    global _slow_q_db_table_ready, _slow_q_db_disabled
    if _slow_q_db_disabled:
        return
    dsn = (os.environ.get("INTELLIGENCE_DATABASE_URL")
           or os.environ.get("INTEL_DATABASE_URL")
           or os.environ.get("DATABASE_URL"))
    if not dsn:
        _slow_q_db_disabled = True
        return
    try:
        import psycopg2  # type: ignore
        with psycopg2.connect(dsn, connect_timeout=3) as c, c.cursor() as cur:
            if not _slow_q_db_table_ready:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {SLOW_QUERY_DB_TABLE} (
                        id BIGSERIAL PRIMARY KEY,
                        ts DOUBLE PRECISION NOT NULL,
                        bot TEXT NOT NULL,
                        query_hash TEXT NOT NULL,
                        duration_ms DOUBLE PRECISION NOT NULL,
                        query_text TEXT
                    );
                    CREATE INDEX IF NOT EXISTS slow_queries_ts_idx
                        ON {SLOW_QUERY_DB_TABLE}(ts DESC);
                    CREATE INDEX IF NOT EXISTS slow_queries_bot_idx
                        ON {SLOW_QUERY_DB_TABLE}(bot, ts DESC);
                    """
                )
                _slow_q_db_table_ready = True
            cur.execute(
                f"INSERT INTO {SLOW_QUERY_DB_TABLE} "
                f"(ts, bot, query_hash, duration_ms, query_text) "
                f"VALUES (%s, %s, %s, %s, %s)",
                (ts, bot, qhash, duration_ms,
                 query_text[:_SLOW_QUERY_MAX_TEXT]),
            )
    except Exception as e:  # noqa: BLE001
        _slow_q_db_disabled = True
        log_event("WARNING", msg="slow_query_db_disabled", err=str(e)[:200])


def record_slow_query(query_text: str, duration_ms: float) -> None:
    """Log a slow query (file + best-effort DB). No-op if below threshold."""
    if duration_ms < SLOW_QUERY_THRESHOLD_MS:
        return
    ts = time.time()
    bot = _bot_name()
    qtext = (query_text or "")[:_SLOW_QUERY_MAX_TEXT]
    qhash = _query_hash(query_text or "")
    with _slow_q_lock:
        try:
            _get_slow_query_logger().info(json.dumps({
                "ts": ts, "bot": bot, "query_hash": qhash,
                "duration_ms": round(duration_ms, 1), "query_text": qtext,
            }, ensure_ascii=False))
        except Exception:
            pass
    metrics.inc("slow_queries_total", bot=bot)
    threading.Thread(
        target=_record_slow_query_db,
        args=(ts, bot, qhash, duration_ms, qtext),
        daemon=True, name="slow-query-db",
    ).start()


class TimedCursor:
    """psycopg2 cursor wrapper that times execute() / executemany().

    Usage:
        with conn.cursor() as raw:
            cur = TimedCursor(raw)
            cur.execute("SELECT ...")
    """

    __slots__ = ("_c",)

    def __init__(self, cursor: Any) -> None:
        self._c = cursor

    def execute(self, query, vars=None):  # noqa: A002
        t0 = time.perf_counter()
        try:
            if vars is None:
                return self._c.execute(query)
            return self._c.execute(query, vars)
        finally:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            try:
                qtxt = (query.decode("utf-8", "replace")
                        if isinstance(query, (bytes, bytearray))
                        else str(query))
                record_slow_query(qtxt, dur_ms)
            except Exception:
                pass

    def executemany(self, query, vars_list):  # noqa: A002
        t0 = time.perf_counter()
        try:
            return self._c.executemany(query, vars_list)
        finally:
            dur_ms = (time.perf_counter() - t0) * 1000.0
            try:
                record_slow_query(f"[executemany] {query}", dur_ms)
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __iter__(self):
        return iter(self._c)

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *a):
        return self._c.__exit__(*a)


def timed_cursor(connection) -> "TimedCursor":
    """Open a TimedCursor on a psycopg2 connection."""
    return TimedCursor(connection.cursor())


# ─── REPLAY BUFFER ────────────────────────────────────────────────────────────

class EventBuffer:
    """Thread-safe ring buffer of recent bot events for post-mortem replay.

    Each event: ts, user_id, command, response_summary, latency_ms, error.
    Bounded by maxlen (default 1000).
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self.maxlen = maxlen
        self._buf: "_ev_deque[dict[str, Any]]" = _ev_deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, user_id: Any = None, command: str = "",
               response_summary: str = "", latency_ms: float = 0.0,
               error: Optional[str] = None, **extra: Any) -> None:
        ev: dict[str, Any] = {
            "ts": time.time(),
            "user_id": user_id,
            "command": (command or "")[:120],
            "response_summary": (response_summary or "")[:240],
            "latency_ms": round(float(latency_ms or 0), 1),
            "error": None if error is None else str(error)[:240],
        }
        for k, v in (extra or {}).items():
            try:
                json.dumps(v, default=str)
                ev[k] = v
            except Exception:
                ev[k] = str(v)[:120]
        with self._lock:
            self._buf.append(ev)

    def recent(self, n: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if n <= 0 or n >= len(self._buf):
                return list(self._buf)
            return list(self._buf)[-n:]

    def serialize(self, n: Optional[int] = None) -> str:
        items = self.recent(n or self.maxlen)
        return json.dumps({
            "bot": _bot_name(), "count": len(items), "events": items,
        }, default=str, ensure_ascii=False)

    def __len__(self) -> int:
        return len(self._buf)


# Module-level singleton, bots usually need only one buffer.
event_buffer = EventBuffer(maxlen=1000)


def record_event(user_id: Any = None, command: str = "",
                 response_summary: str = "", latency_ms: float = 0.0,
                 error: Optional[str] = None, **extra: Any) -> None:
    """Append to the default replay buffer. Never raises."""
    try:
        event_buffer.append(
            user_id=user_id, command=command,
            response_summary=response_summary,
            latency_ms=latency_ms, error=error, **extra,
        )
    except Exception:
        pass


def replay_buffer_json(n: int = 100) -> str:
    return event_buffer.serialize(n)


def replay_handler_dispatch(fn: Callable, command_label: str = "") -> Callable:
    """Decorator: records each handler call in event_buffer."""
    import asyncio

    def _extract_user_id(args):
        for a in args[:3]:
            try:
                uid = (getattr(getattr(a, "effective_user", None), "id", None)
                       or getattr(getattr(a, "from_user", None), "id", None)
                       or getattr(a, "user_id", None))
                if uid:
                    return uid
            except Exception:
                pass
        return None

    if asyncio.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def inner_a(*a, **kw):
            t0 = time.perf_counter()
            err = None
            try:
                return await fn(*a, **kw)
            except Exception as e:  # noqa: BLE001
                err = type(e).__name__ + ": " + str(e)[:200]
                raise
            finally:
                record_event(
                    user_id=_extract_user_id(a),
                    command=command_label or fn.__name__,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    error=err,
                )
        return inner_a

    @functools.wraps(fn)
    def inner(*a, **kw):
        t0 = time.perf_counter()
        err = None
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__ + ": " + str(e)[:200]
            raise
        finally:
            record_event(
                user_id=_extract_user_id(a),
                command=command_label or fn.__name__,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                error=err,
            )
    return inner


def start_replay_endpoint(port: int) -> Optional[int]:
    """Standalone /replay HTTP server. Returns port or None."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _RH(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = self.path.split("?", 1)[0].rstrip("/")
            if p in ("/replay", "/replay/"):
                try:
                    n = 100
                    if "?" in self.path:
                        from urllib.parse import parse_qs
                        qs = parse_qs(self.path.split("?", 1)[1])
                        n = int((qs.get("n") or ["100"])[0])
                except Exception:
                    n = 100
                body = replay_buffer_json(n).encode("utf-8")
                self.send_response(200)
                self.send_header(
                    "Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a, **kw):  # silence
            return

    try:
        srv = HTTPServer(("0.0.0.0", port), _RH)
    except OSError as e:
        log_event("WARNING", msg="replay_server_bind_failed",
                  port=port, err=str(e))
        return None
    actual = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="replay-http").start()
    log_event("INFO", msg="replay_server_started", port=actual)
    return actual


__all__ = [
    "TTLCache", "ttl_cache",
    "log_event", "new_request_id", "set_bot_name", "get_logger",
    # tracing
    "get_request_id", "set_request_id", "reset_request_id",
    "get_user_id", "request_context",
    "extract_rid_from_start_param", "append_rid_to_start_param",
    # idempotency
    "make_idempotency_key", "idempotent_write", "cleanup_idempotency_keys",
    "retry_on_transient", "retry_on_transient_async",
    "ValidationError",
    "validate_price", "validate_bedrooms", "validate_area_name",
    "validate_lang", "validate_phone", "validate_user_text",
    "SUPPORTED_LANGS",
    "metrics", "timed", "start_metrics_server",
    "count_request", "count_error", "count_llm", "count_db",
    # slow query
    "record_slow_query", "TimedCursor", "timed_cursor",
    "SLOW_QUERY_THRESHOLD_MS",
    # replay buffer
    "EventBuffer", "event_buffer", "record_event", "replay_buffer_json",
    "replay_handler_dispatch", "start_replay_endpoint",
]


# ─── CIRCUIT BREAKER ──────────────────────────────────────────────────────────
#
# Simple state machine: CLOSED → OPEN (after N failures in window) → HALF_OPEN
# (after cooldown, 1 probe) → CLOSED on success / OPEN on failure.
# No external deps. Thread-safe. Notifies @vadim_admin_bot on OPEN transitions.
#
# Usage:
#     db_cb = get_breaker("db_main")
#     try:
#         conn = db_cb.call(psycopg2.connect, DATABASE_URL)
#     except CircuitOpenError:
#         conn = None  # degraded path
#
#     # or with fallback:
#     result = llm_cb.call(llm_call, prompt, fallback=None)

from collections import deque as _cb_deque


class CircuitOpenError(RuntimeError):
    """Raised when a breaker is OPEN and no fallback was supplied."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit breaker '{name}' is OPEN")
        self.name = name


_BREAKER_ADMIN_NOTIFY_BOT_TOKEN = os.environ.get("ADMIN_NOTIFY_BOT_TOKEN") or \
    os.environ.get("VADIM_ADMIN_BOT_TOKEN") or os.environ.get("BOT_TOKEN_ADMIN")
_BREAKER_ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or \
    os.environ.get("VADIM_ADMIN_CHAT_ID")


def _breaker_notify_admin(text: str) -> None:
    """Best-effort notification to @vadim_admin_bot. Never raises."""
    try:
        token = _BREAKER_ADMIN_NOTIFY_BOT_TOKEN
        chat_id = _BREAKER_ADMIN_CHAT_ID
        if not token or not chat_id:
            return
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        # never let admin notification break the breaker
        pass


class CircuitBreaker:
    """Per-service circuit breaker.

    States:
      CLOSED    — calls pass through, failures counted in rolling window.
      OPEN      — calls short-circuited to fallback (or CircuitOpenError).
      HALF_OPEN — exactly one probe call allowed; success closes, fail re-opens.
    """

    def __init__(self, name: str, threshold: int = 5, window: int = 60,
                 cooldown: int = 60) -> None:
        self.name = name
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self.failures: "_cb_deque[float]" = _cb_deque()
        self.state = "CLOSED"
        self.opened_at = 0.0
        self._lock = threading.Lock()
        self._notified_open_at = 0.0  # de-dup admin notifications

    def _open(self) -> None:
        prev = self.state
        self.state = "OPEN"
        self.opened_at = time.time()
        metrics.inc("circuit_breaker_open_total", breaker=self.name)
        log_event("WARNING", msg="circuit_breaker_open", breaker=self.name,
                  failures=len(self.failures), threshold=self.threshold)
        # One admin notification per OPEN transition (not per cooldown cycle)
        if prev != "OPEN" and (self.opened_at - self._notified_open_at) > 300:
            self._notified_open_at = self.opened_at
            _breaker_notify_admin(
                f"⚠️ circuit OPEN: {_bot_name()}/{self.name} "
                f"({len(self.failures)} fails in {self.window}s) — "
                f"degraded mode for {self.cooldown}s"
            )

    def _close(self) -> None:
        if self.state != "CLOSED":
            log_event("INFO", msg="circuit_breaker_close", breaker=self.name)
            metrics.inc("circuit_breaker_close_total", breaker=self.name)
        self.state = "CLOSED"
        self.failures.clear()

    def call(self, fn: Callable, *args: Any, fallback: Any = _SENTINEL,
             **kwargs: Any) -> Any:
        """Invoke fn through the breaker.

        If OPEN: returns fallback (callable invoked, else value). If fallback
        was not supplied, raises CircuitOpenError.
        If CLOSED/HALF_OPEN: invokes fn, accounts for failures, may transition.
        """
        with self._lock:
            now = time.time()
            if self.state == "OPEN":
                if now - self.opened_at < self.cooldown:
                    metrics.inc("circuit_breaker_short_circuit_total",
                                breaker=self.name)
                    if fallback is _SENTINEL:
                        raise CircuitOpenError(self.name)
                    return fallback() if callable(fallback) else fallback
                # cooldown elapsed → allow a probe
                self.state = "HALF_OPEN"
                log_event("INFO", msg="circuit_breaker_half_open",
                          breaker=self.name)

        # Outside the lock for the actual call (don't serialize traffic)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                now = time.time()
                self.failures.append(now)
                # drop failures older than window
                while self.failures and now - self.failures[0] > self.window:
                    self.failures.popleft()
                if self.state == "HALF_OPEN":
                    # probe failed → re-open
                    self._open()
                elif len(self.failures) >= self.threshold:
                    self._open()
                metrics.inc("circuit_breaker_fail_total", breaker=self.name,
                            err=type(e).__name__)
            raise

        with self._lock:
            if self.state == "HALF_OPEN":
                self._close()
            metrics.inc("circuit_breaker_ok_total", breaker=self.name)
        return result

    def is_open(self) -> bool:
        with self._lock:
            if self.state == "OPEN":
                if time.time() - self.opened_at < self.cooldown:
                    return True
            return False

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self.state,
                "failures": len(self.failures),
                "threshold": self.threshold,
                "opened_at": self.opened_at,
            }


_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def get_breaker(name: str, threshold: int = 5, window: int = 60,
                cooldown: int = 60) -> CircuitBreaker:
    """Get or create a named breaker. Defaults: 5 fails / 60s → OPEN for 60s."""
    with _BREAKERS_LOCK:
        cb = _BREAKERS.get(name)
        if cb is None:
            cb = CircuitBreaker(name, threshold=threshold, window=window,
                                cooldown=cooldown)
            _BREAKERS[name] = cb
        return cb


def breaker_stats_all() -> list:
    with _BREAKERS_LOCK:
        return [cb.stats() for cb in _BREAKERS.values()]


# ─── FEATURE FLAGS ────────────────────────────────────────────────────────────
#
# Env-var driven. Convention: FF_<NAME>_ENABLED = "1"/"true"/"yes"/"on" (case-
# insensitive). Defaults ON — disabling must never break the bot, just degrade.
#
# Usage:
#     if ff("VOICE_SEARCH"):
#         transcribe(audio)
#     else:
#         return _msg(lang, "feature_unavailable")

_FF_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FF_FALSE = {"0", "false", "no", "off", "n", "f"}


def ff(name: str, default: bool = True) -> bool:
    """Read FF_<NAME>_ENABLED env var. Defaults True if unset.

    name can be passed without the FF_ prefix and _ENABLED suffix.
    """
    key = name.upper()
    if not key.startswith("FF_"):
        key = "FF_" + key
    if not key.endswith("_ENABLED"):
        key = key + "_ENABLED"
    val = os.environ.get(key)
    if val is None:
        return default
    v = val.strip().lower()
    if v in _FF_TRUE:
        return True
    if v in _FF_FALSE:
        return False
    return default


# Pre-baked common flags (no-op helpers — explicit names for grep-ability)
def ff_voice_search() -> bool: return ff("VOICE_SEARCH")
def ff_ai_consultant() -> bool: return ff("AI_CONSULTANT")
def ff_heatmap() -> bool: return ff("HEATMAP")
def ff_translate() -> bool: return ff("TRANSLATE")
def ff_pdf_report() -> bool: return ff("PDF_REPORT")
def ff_telethon_parser() -> bool: return ff("TELETHON_PARSER")
def ff_llm_extra_pass() -> bool: return ff("LLM_EXTRA_PASS")


# ─── GRACEFUL DEGRADATION HELPERS ─────────────────────────────────────────────

_DEGRADE_MESSAGES = {
    "en": {
        "feature_unavailable": "This feature is temporarily unavailable. Please try again later.",
        "data_updating": "📊 Data updating… showing cached snapshot.",
        "llm_unavailable": "AI assistant is busy. Try a structured search instead.",
        "photo_unavailable": "🖼️ Photo unavailable",
        "stats_unavailable": "—",
    },
    "ru": {
        "feature_unavailable": "Эта функция временно недоступна. Попробуйте позже.",
        "data_updating": "📊 Данные обновляются… показан кешированный снапшот.",
        "llm_unavailable": "AI-ассистент занят. Попробуйте обычный поиск.",
        "photo_unavailable": "🖼️ Фото недоступно",
        "stats_unavailable": "—",
    },
    "ar": {
        "feature_unavailable": "هذه الميزة غير متاحة مؤقتاً. حاول لاحقاً.",
        "data_updating": "📊 جارٍ تحديث البيانات…",
        "llm_unavailable": "المساعد الذكي مشغول.",
        "photo_unavailable": "🖼️ الصورة غير متاحة",
        "stats_unavailable": "—",
    },
}


def degrade_msg(key: str, lang: str = "en") -> str:
    """Get a user-facing degraded-mode message in user's language."""
    lang = validate_lang(lang, default="en")
    return _DEGRADE_MESSAGES.get(lang, _DEGRADE_MESSAGES["en"]).get(
        key, _DEGRADE_MESSAGES["en"].get(key, key))


def safe_call(breaker_name: str, fn: Callable, *args: Any,
              fallback: Any = None, **kwargs: Any) -> Any:
    """Convenience wrapper: get_breaker(name).call(fn, *a, fallback=..., **kw).

    Catches all exceptions when fallback is provided (treats them like an
    open circuit). Returns fallback on any failure.
    """
    cb = get_breaker(breaker_name)
    try:
        return cb.call(fn, *args, fallback=fallback, **kwargs)
    except CircuitOpenError:
        return fallback() if callable(fallback) else fallback
    except Exception:
        return fallback() if callable(fallback) else fallback


# extend __all__
try:
    __all__ += [  # type: ignore[name-defined]
        "CircuitBreaker", "CircuitOpenError", "get_breaker", "breaker_stats_all",
        "ff", "ff_voice_search", "ff_ai_consultant", "ff_heatmap", "ff_translate",
        "ff_pdf_report", "ff_telethon_parser", "ff_llm_extra_pass",
        "degrade_msg", "safe_call",
    ]
except NameError:
    pass
