"""
i18n_contract.py — shared i18n propagation contract.

Goal: make it impossible to forget passing ``user_id`` into any function that
forms text shown to a user. Two main mistakes we want to catch:

1. Mixing languages on a single screen — e.g. one message in RU, the next in EN
   because a helper got a literal "en" default while another path used tr(uid).
   (See B055: analytics-bot no_data_message called without user_id.)
2. Silent fallback to "en" when ``user_id`` is None, hiding bugs.

Strategy
--------

* ``STRICT_I18N=0`` (default, prod) — missing user_id is logged + admin alert
  (throttled) and we fall back to ``default`` lang. Nothing crashes.
* ``STRICT_I18N=1`` (dev/CI/tests) — raise ``UserIdRequired`` so the call site
  is fixed before it ships.

Each bot owns its own ``user_lang`` dict and locale tables. To plug them into
this contract, call ``register_lang_resolver(callable)`` and
``register_locales(dict)`` at startup.

Public API
~~~~~~~~~~

* ``lang_of(user_id, *, allow_none=False, default="en") -> str``
* ``tr(user_id, key, **fmt) -> str``
* ``safe_text(user_id, text_dict, **fmt) -> str``
* ``register_lang_resolver(callable)``
* ``register_locales(dict)``
* ``UserIdRequired`` exception
* ``get_warning_stats() -> dict`` — for /lang_audit_runtime
"""

from __future__ import annotations

import os
import sys
import time
import threading
import traceback
from typing import Any, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# Exceptions & globals
# ---------------------------------------------------------------------------

class UserIdRequired(Exception):
    """Raised when a user-facing function is called without ``user_id``.

    Only raised when env ``STRICT_I18N=1``. In prod (=0) we log + throttled
    admin warn instead, so a tail of legacy callers cannot brick the bot.
    """


_DEFAULT_LANG = "en"
_SUPPORTED_LANGS = ("en", "ru", "ar")

# Pluggable resolver: bot calls register_lang_resolver(lambda uid: user_lang.get(uid))
_lang_resolver: Optional[Callable[[Any], Optional[str]]] = None

# Pluggable locales table (or one merged via register_locales).
_LOCALES: Dict[str, Dict[str, str]] = {}

# Warning throttling state (process-local).
_warn_lock = threading.Lock()
_warn_state: Dict[str, Any] = {
    "count_24h": 0,           # rolling, reset by /lang_audit_runtime caller
    "last_reset_ts": time.time(),
    "last_alert_ts": 0.0,
    "by_site": {},            # site_key -> count
}
_WARN_ALERT_INTERVAL_S = 3600  # at most 1 admin alert per hour


def strict_mode() -> bool:
    """Return True if STRICT_I18N=1 (raise on missing user_id)."""
    return os.getenv("STRICT_I18N", "0") == "1"


# ---------------------------------------------------------------------------
# Registration helpers (each bot calls these at startup)
# ---------------------------------------------------------------------------

def register_lang_resolver(fn: Callable[[Any], Optional[str]]) -> None:
    """Plug in the bot's ``user_id -> lang`` lookup.

    Example::

        from i18n_contract import register_lang_resolver
        register_lang_resolver(lambda uid: user_languages.get(uid))
    """
    global _lang_resolver
    _lang_resolver = fn


def register_locales(locales: Dict[str, Dict[str, str]]) -> None:
    """Plug in a ``{lang: {key: text}}`` table for ``tr()`` to use."""
    global _LOCALES
    _LOCALES = locales or {}


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def _record_warning(reason: str) -> None:
    """Throttled warning: log once, alert admin at most every hour."""
    with _warn_lock:
        # Roll the 24h window.
        if time.time() - _warn_state["last_reset_ts"] > 86400:
            _warn_state["count_24h"] = 0
            _warn_state["by_site"] = {}
            _warn_state["last_reset_ts"] = time.time()

        _warn_state["count_24h"] += 1

        # Find the first caller outside this module — that's the site to blame.
        frame = sys._getframe(2) if hasattr(sys, "_getframe") else None
        site_key = "unknown"
        if frame is not None:
            site_key = f"{frame.f_code.co_filename}:{frame.f_lineno}"
        _warn_state["by_site"][site_key] = _warn_state["by_site"].get(site_key, 0) + 1

        # Throttled admin alert.
        now = time.time()
        if now - _warn_state["last_alert_ts"] > _WARN_ALERT_INTERVAL_S:
            _warn_state["last_alert_ts"] = now
            try:
                from admin_notify import notify_admin  # type: ignore
                notify_admin(
                    f"[i18n] missing user_id — reason={reason} site={site_key} "
                    f"count_24h={_warn_state['count_24h']}"
                )
            except Exception:
                pass

        # Always log to stderr so Railway logs catch it.
        print(f"[i18n] WARN missing user_id reason={reason} site={site_key}", file=sys.stderr)  # safety: ignore — no secrets, just diagnostic


def lang_of(user_id: Any, *, allow_none: bool = False, default: str = _DEFAULT_LANG) -> str:
    """Get language for ``user_id``.

    Behaviour
    ---------
    * ``user_id is not None`` → resolver lookup, fall back to ``default``.
    * ``user_id is None`` and ``allow_none=True`` → silently return ``default``.
    * ``user_id is None`` and ``allow_none=False``:
        * ``STRICT_I18N=1`` → raise :class:`UserIdRequired`.
        * else → log + throttled admin alert, return ``default``.

    Always returns one of the supported language codes (``en``/``ru``/``ar``).
    """
    if user_id is None:
        if not allow_none:
            if strict_mode():
                raise UserIdRequired(
                    "lang_of() called with user_id=None; pass it through the call chain."
                )
            _record_warning("lang_of_none")
        return default if default in _SUPPORTED_LANGS else _DEFAULT_LANG

    if _lang_resolver is None:
        # No resolver wired up — quietly default. (Common in unit tests.)
        return default if default in _SUPPORTED_LANGS else _DEFAULT_LANG

    try:
        raw = _lang_resolver(user_id)
    except Exception as e:  # pragma: no cover — defensive
        print(f"[i18n] resolver error for {user_id}: {e!r}", file=sys.stderr)
        return default if default in _SUPPORTED_LANGS else _DEFAULT_LANG

    if raw in _SUPPORTED_LANGS:
        return raw
    return default if default in _SUPPORTED_LANGS else _DEFAULT_LANG


def tr(user_id: Any, key: str, **fmt: Any) -> str:
    """Translate ``key`` for ``user_id``, with optional ``str.format`` kwargs.

    Raises :class:`UserIdRequired` if ``user_id is None`` and STRICT_I18N=1.
    Falls back to English then to ``[!key]`` so missing strings are visible.
    """
    user_lang = lang_of(user_id)
    table = _LOCALES.get(user_lang) or _LOCALES.get("en") or {}
    text = table.get(key) or _LOCALES.get("en", {}).get(key) or f"[!{key}]"
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def safe_text(user_id: Any, text_dict: Dict[str, str], **fmt: Any) -> str:
    """Pick the correct language from an inline ``{lang: text}`` dict.

    Designed for the legacy ``L = _NO_DATA_BODY[lang]; L["key"]`` pattern —
    callers that already had a dict literal next to them shouldn't have to
    move strings into a global locale table just to be safe.
    """
    user_lang = lang_of(user_id)
    text = (
        text_dict.get(user_lang)
        or text_dict.get("en")
        or next(iter(text_dict.values()), "")
    )
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# ---------------------------------------------------------------------------
# Observability — used by /lang_audit_runtime
# ---------------------------------------------------------------------------

def get_warning_stats() -> Dict[str, Any]:
    """Snapshot of warnings for the admin command."""
    with _warn_lock:
        return {
            "count_24h": _warn_state["count_24h"],
            "since_ts": _warn_state["last_reset_ts"],
            "top_sites": sorted(
                _warn_state["by_site"].items(), key=lambda kv: kv[1], reverse=True
            )[:10],
            "strict_mode": strict_mode(),
        }


def reset_warning_stats() -> None:
    with _warn_lock:
        _warn_state["count_24h"] = 0
        _warn_state["by_site"] = {}
        _warn_state["last_reset_ts"] = time.time()


__all__ = [
    "UserIdRequired",
    "lang_of",
    "tr",
    "safe_text",
    "register_lang_resolver",
    "register_locales",
    "get_warning_stats",
    "reset_warning_stats",
    "strict_mode",
]
