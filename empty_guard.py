"""Empty-result Sanity Guard.

Problem this solves
-------------------
A SQL filter chops everything → user sees "no data" → but the underlying
table is actually full of rows. Silent bug. Hard to detect because the
function returns successfully (just empty).

This module wraps such functions: if the result is empty AND the base
table has data above a threshold, we log it and (above another threshold)
notify the admin bot.

Usage
-----
    from empty_guard import guarded_query

    @guarded_query(label="analytics.get_latest_deals",
                   base_count_sql="SELECT COUNT(*) FROM public.dld_transactions_full",
                   dsn_env="LIVE_DATABASE_URL",
                   alert_threshold=1000)
    def get_latest_deals(scope, name, ...):
        ...

Throttling
----------
Same (label, args_signature) cannot fire more than 1 alert per hour.

Fail-safe
---------
ALL guard logic is wrapped in try/except. If anything inside the guard
errors out (DB unreachable, alert fails, etc.) we still return the
original `rows` from the wrapped function. NEVER breaks the caller.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("empty_guard")

# Persistent throttle / audit dirs
_STATE_DIR = Path(os.environ.get(
    "EMPTY_GUARD_STATE_DIR",
    str(Path.home() / ".claude" / "empty_guard"),
))
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

_THROTTLE_PATH = _STATE_DIR / "throttle.json"
_AUDIT_PATH = _STATE_DIR / "audit.jsonl"

THROTTLE_WINDOW_SEC = 3600          # 1h between alerts for same key
BASE_COUNT_CACHE_SEC = 300          # 5 min cache for base_count
DEFAULT_BASE_MIN = 100              # below this, ignore — table just empty
DEFAULT_ALERT_MIN = 1000            # above this base, raise an alert

_lock = threading.Lock()
_base_cache: dict[str, tuple[float, int]] = {}


# ── persistence helpers ─────────────────────────────────────────────────
def _load_throttle() -> dict:
    try:
        if _THROTTLE_PATH.exists():
            return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_throttle(d: dict) -> None:
    try:
        _THROTTLE_PATH.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def _audit(entry: dict) -> None:
    """Append-only audit jsonl. Used by /empty_log admin command."""
    try:
        entry = {**entry, "ts": time.time()}
        with _AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        log.debug("empty_guard audit write fail: %s", e)


def recent_empty_events(limit: int = 20, bot: Optional[str] = None) -> list[dict]:
    """Used by admin /empty_log command. Returns most-recent first."""
    try:
        if not _AUDIT_PATH.exists():
            return []
        lines = _AUDIT_PATH.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if bot and str(obj.get("bot") or "").lower() != bot.lower():
                continue
            out.append(obj)
            if len(out) >= limit:
                break
        return out
    except Exception as e:
        log.debug("recent_empty_events fail: %s", e)
        return []


# ── base count cache ────────────────────────────────────────────────────
def _unfiltered_count(label: str, base_count_sql: Optional[str],
                       dsn_env: str) -> int:
    """Cached COUNT(*) of the underlying table. -1 means we don't know."""
    if not base_count_sql:
        return -1
    cache_key = f"{label}|{dsn_env}"
    now = time.time()
    with _lock:
        cached = _base_cache.get(cache_key)
        if cached and now - cached[0] < BASE_COUNT_CACHE_SEC:
            return cached[1]
    dsn = os.environ.get(dsn_env, "")
    if not dsn:
        return -1
    try:
        import psycopg2  # lazy import — many callers don't need it
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(base_count_sql)
                row = cur.fetchone()
                n = int(row[0]) if row and row[0] is not None else 0
        with _lock:
            _base_cache[cache_key] = (now, n)
        return n
    except Exception as e:
        log.debug("base_count fail for %s: %s", label, e)
        return -1


# ── args signature for throttling ───────────────────────────────────────
def _args_sig(args: tuple, kwargs: dict) -> str:
    try:
        rep = repr((args, sorted(kwargs.items())))
    except Exception:
        rep = repr((args, kwargs))
    return hashlib.sha256(rep.encode("utf-8", errors="replace")).hexdigest()[:12]


# ── notify (lazy) ──────────────────────────────────────────────────────
def _notify(text: str) -> None:
    try:
        from admin_notify import admin_notify  # type: ignore
        admin_notify(text, parse_mode="HTML", priority="normal")
    except Exception:
        # second-chance: emit to stderr so log_sentinel can pick it up
        log.warning("[EMPTY_GUARD_ALERT] %s", text)


# ── core decorator ──────────────────────────────────────────────────────
def guarded_query(
    label: str,
    *,
    base_count_sql: Optional[str] = None,
    dsn_env: str = "LIVE_DATABASE_URL",
    base_min: int = DEFAULT_BASE_MIN,
    alert_threshold: int = DEFAULT_ALERT_MIN,
    bot: Optional[str] = None,
    is_empty: Optional[Callable[[Any], bool]] = None,
) -> Callable:
    """Decorator that audits + alerts on suspiciously empty filter results.

    Args:
      label:            stable label, e.g. "analytics.get_latest_deals"
      base_count_sql:   SQL to count the base table (unfiltered).
                        If None, we still audit empties but cannot judge
                        whether they're suspicious — so no alert fired.
      dsn_env:          env-var name with the Postgres DSN.
      base_min:         if base table has fewer rows than this, treat
                        the empty result as legitimate (table just empty).
      alert_threshold:  if base table has MORE rows than this, send
                        an admin alert (in addition to audit log).
      bot:              optional service name for filtering /empty_log
      is_empty:         custom predicate. Default treats `None`, `[]`,
                        `{}`, and falsy as empty.
    """
    if is_empty is None:
        def _default_empty(r):
            if r is None:
                return True
            try:
                return len(r) == 0
            except TypeError:
                return not bool(r)
        is_empty = _default_empty

    def wrap(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            t0 = time.time()
            rows = fn(*args, **kwargs)
            try:
                if not is_empty(rows):
                    return rows
                # ── empty branch ────────────────────────────────────────
                base = _unfiltered_count(label, base_count_sql, dsn_env)
                dt_ms = int((time.time() - t0) * 1000)
                if base < 0 or base < base_min:
                    # no signal — quietly return
                    return rows
                sig = _args_sig(args, kwargs)
                key = f"{label}|{sig}"
                # audit
                _audit({
                    "label": label,
                    "bot": bot,
                    "args": _safe_repr(args),
                    "kwargs": _safe_repr(kwargs),
                    "base_count": base,
                    "dt_ms": dt_ms,
                    "sig": sig,
                })
                # alert (throttled)
                if base >= alert_threshold:
                    state = _load_throttle()
                    last = float(state.get(key, 0))
                    if time.time() - last > THROTTLE_WINDOW_SEC:
                        state[key] = time.time()
                        # prune old entries (>24h)
                        now = time.time()
                        state = {k: v for k, v in state.items()
                                 if now - float(v) < 24 * 3600}
                        _save_throttle(state)
                        _notify(
                            f"⚠️ <b>Empty filter result</b>: <code>{label}</code>\n"
                            f"base table has <b>{base:,}</b> rows but filter returned 0\n"
                            f"args: <code>{_safe_repr(args)[:200]}</code>\n"
                            f"kwargs: <code>{_safe_repr(kwargs)[:200]}</code>\n"
                            f"<i>investigate fixture / filter</i>"
                        )
            except Exception as e:
                log.debug("guarded_query inner fail (label=%s): %s", label, e)
            return rows
        # tag for introspection
        inner.__empty_guard__ = label  # type: ignore[attr-defined]
        return inner
    return wrap


def _safe_repr(x: Any) -> str:
    try:
        s = repr(x)
        return s if len(s) < 500 else s[:500] + "..."
    except Exception:
        return "<unrepr>"


# ── self-test ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Smoke check the decorator without DB
    @guarded_query(label="smoke.test", base_count_sql=None)
    def _noop():
        return []
    r = _noop()
    print("smoke ok, empty=", r == [])
    print("audit path:", _AUDIT_PATH)
    print("throttle path:", _THROTTLE_PATH)
