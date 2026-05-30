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

import functools
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


def log_event(level: str = "INFO", **fields: Any) -> None:
    """Emit a single-line JSON log. Always includes ts + bot + level + msg.

    Usage:
        log_event("INFO", msg="incoming", user_id=u.id, cmd="/start")
        log_event("ERROR", msg="db fail", err=str(e), table="listings")
    """
    payload: dict[str, Any] = {
        "ts": time.time(),
        "bot": _bot_name(),
        "level": level,
    }
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


__all__ = [
    "TTLCache", "ttl_cache",
    "log_event", "new_request_id", "set_bot_name", "get_logger",
    "retry_on_transient", "retry_on_transient_async",
    "ValidationError",
    "validate_price", "validate_bedrooms", "validate_area_name",
    "validate_lang", "validate_phone", "validate_user_text",
    "SUPPORTED_LANGS",
    "metrics", "timed", "start_metrics_server",
    "count_request", "count_error", "count_llm", "count_db",
]
