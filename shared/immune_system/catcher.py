"""Universal exception catcher — install once per bot at boot.

Hooks `sys.excepthook` and the asyncio loop's exception_handler so every
uncaught exception lands in Postgres `immune_incidents` (dedup by
sha256(file:line:exception_class)). Best-effort — failure to record never
re-raises and never blocks the bot.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from . import _db

log = logging.getLogger("immune_system.catcher")

_INSTALLED = False
_LOCK = threading.Lock()
_CURRENT_BOT: str = "unknown"


def _signature(file_path: str, line_no: int, exc_class: str) -> str:
    raw = f"{file_path}:{line_no}:{exc_class}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def _tail_frame(tb) -> tuple[str, int]:
    """Return (file_path, line_no) of the *deepest* user frame in the traceback."""
    file_path = ""
    line_no = 0
    cur = tb
    while cur is not None:
        try:
            file_path = cur.tb_frame.f_code.co_filename or file_path
            line_no = cur.tb_lineno or line_no
        except Exception:
            pass
        cur = cur.tb_next
    return file_path, line_no


def _env_snapshot() -> dict[str, Any]:
    """Capture small, non-sensitive env context (host, service, python)."""
    return {
        "host": os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "",
        "service": os.environ.get("RAILWAY_SERVICE_NAME") or "",
        "python": sys.version.split()[0],
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def _record(
    bot_name: str,
    exc_type: type,
    exc_value: BaseException,
    tb,
    user_id: Optional[int] = None,
) -> None:
    """Insert / upsert into immune_incidents. Swallows all errors."""
    try:
        exc_class = getattr(exc_type, "__name__", "Exception")
        file_path, line_no = _tail_frame(tb)
        sig = _signature(file_path, line_no, exc_class)
        stack = "".join(traceback.format_exception(exc_type, exc_value, tb))[:8000]
        env_json = json.dumps(_env_snapshot())

        conn = _db.connect()
        if conn is None:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO immune_incidents
                        (signature, bot_name, exception_class, stack_trace,
                         file_path, line_no, user_id, env)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (signature) DO UPDATE
                        SET occurrence_count = immune_incidents.occurrence_count + 1,
                            last_seen        = NOW(),
                            bot_name         = COALESCE(immune_incidents.bot_name,
                                                        EXCLUDED.bot_name)
                    """,
                    (sig, bot_name, exc_class, stack, file_path, line_no,
                     user_id, env_json),
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # never let the catcher crash the bot
        log.debug("immune_system._record failed: %s", e)


def install_immune_catcher(bot_name: str) -> None:
    """Idempotent install. Hooks sys.excepthook and the running/default
    asyncio loop's exception_handler. Safe to call from boot of any bot."""
    global _INSTALLED, _CURRENT_BOT
    with _LOCK:
        _CURRENT_BOT = bot_name or "unknown"
        if _INSTALLED:
            return
        prev_hook = sys.excepthook

        def _hook(exc_type, exc_value, tb):
            _record(_CURRENT_BOT, exc_type, exc_value, tb)
            try:
                prev_hook(exc_type, exc_value, tb)
            except Exception:
                pass

        sys.excepthook = _hook

        # threading.excepthook (Py3.8+)
        try:
            prev_thread_hook = threading.excepthook  # type: ignore[attr-defined]

            def _thread_hook(args):  # type: ignore[no-untyped-def]
                _record(_CURRENT_BOT, args.exc_type, args.exc_value, args.exc_traceback)
                try:
                    prev_thread_hook(args)
                except Exception:
                    pass

            threading.excepthook = _thread_hook  # type: ignore[attr-defined]
        except Exception:
            pass

        # asyncio loop exception handler — best-effort attach to running loop
        def _asyncio_handler(loop, context):  # type: ignore[no-untyped-def]
            exc = context.get("exception")
            if exc is not None:
                _record(_CURRENT_BOT, type(exc), exc, exc.__traceback__)
            try:
                loop.default_exception_handler(context)
            except Exception:
                pass

        try:
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(_asyncio_handler)
        except Exception:
            pass

        # Patch get_event_loop so future loops also get the handler
        try:
            _orig_new_loop = asyncio.new_event_loop

            def _patched_new_loop():
                loop = _orig_new_loop()
                try:
                    loop.set_exception_handler(_asyncio_handler)
                except Exception:
                    pass
                return loop

            asyncio.new_event_loop = _patched_new_loop  # type: ignore[assignment]
        except Exception:
            pass

        _INSTALLED = True
        log.info("immune_system catcher installed for %s", _CURRENT_BOT)


def record_handled_exception(
    exc: BaseException,
    user_id: Optional[int] = None,
    bot_name: Optional[str] = None,
) -> None:
    """Manual recorder for caught-but-noteworthy exceptions (try/except blocks
    in handler code). Useful for incrementing occurrence_count of known bugs."""
    _record(bot_name or _CURRENT_BOT, type(exc), exc, exc.__traceback__, user_id)
