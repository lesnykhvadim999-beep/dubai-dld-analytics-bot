"""Track every API call into api_usage_log.

Design:
* `track_api_call(...)` is async, non-blocking — failures never raise to caller.
* A sync helper `track_api_call_sync(...)` schedules the write onto the running
  loop (or a background thread when called from a sync context).
* `@track_quota("cerebras")` decorator: works for both sync and async funcs.
* Privacy: only counts (tokens_in / tokens_out) — NEVER prompt content.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from typing import Any, Callable, Optional

from ._db import async_connect, sync_connect
from .limits import _canonical

log = logging.getLogger("quota_tracker.tracker")

_INSERT_SQL = """
INSERT INTO api_usage_log
  (provider, tokens_in, tokens_out, latency_ms, success, bot_name, user_id)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_INSERT_SQL_PSY = _INSERT_SQL.replace("$1", "%s").replace("$2", "%s").replace(
    "$3", "%s").replace("$4", "%s").replace("$5", "%s").replace("$6", "%s").replace(
    "$7", "%s")


async def track_api_call(
    provider: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    *,
    bot_name: Optional[str] = None,
    user_id: Optional[int] = None,
) -> bool:
    """Async INSERT into api_usage_log. Never raises."""
    try:
        conn = await async_connect()
        if conn is None:
            # async path unavailable — try sync fallback in a thread
            return await asyncio.get_event_loop().run_in_executor(
                None,
                _sync_insert,
                provider, tokens_in, tokens_out, latency_ms,
                success, bot_name, user_id,
            )
        try:
            await conn.execute(
                _INSERT_SQL,
                _canonical(provider),
                int(tokens_in or 0),
                int(tokens_out or 0),
                int(latency_ms or 0),
                bool(success),
                bot_name,
                user_id,
            )
            return True
        finally:
            try:
                await conn.close()
            except Exception:
                pass
    except Exception as e:
        log.debug("quota_tracker.track_api_call swallow: %s", e)
        return False


def _sync_insert(provider, tokens_in, tokens_out, latency_ms,
                 success, bot_name, user_id) -> bool:
    conn = sync_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    _INSERT_SQL_PSY,
                    (
                        _canonical(provider),
                        int(tokens_in or 0),
                        int(tokens_out or 0),
                        int(latency_ms or 0),
                        bool(success),
                        bot_name,
                        user_id,
                    ),
                )
        return True
    except Exception as e:
        log.debug("quota_tracker._sync_insert swallow: %s", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def track_api_call_sync(
    provider: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    *,
    bot_name: Optional[str] = None,
    user_id: Optional[int] = None,
) -> bool:
    """Sync entry-point.

    * If a running event loop is available — schedule async insert (fire-and-forget).
    * Otherwise — run sync insert in a background thread so the caller never blocks.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(track_api_call(
                provider, tokens_in, tokens_out, latency_ms, success,
                bot_name=bot_name, user_id=user_id,
            ))
            return True
    except RuntimeError:
        pass

    def _worker():
        _sync_insert(provider, tokens_in, tokens_out, latency_ms,
                     success, bot_name, user_id)

    threading.Thread(target=_worker, daemon=True).start()
    return True


# ── decorator ────────────────────────────────────────────────────────────────
def track_quota(provider: str, *, bot_name: Optional[str] = None):
    """Decorator: wraps a function so each call lands in api_usage_log.

    Works for both sync and async callables. The wrapped function may return
    a dict containing `tokens_in`/`tokens_out` to record real usage; otherwise
    counts are zero.
    """

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def _aw(*args: Any, **kwargs: Any) -> Any:
                t0 = time.perf_counter()
                ok = True
                result = None
                try:
                    result = await fn(*args, **kwargs)
                    return result
                except Exception:
                    ok = False
                    raise
                finally:
                    latency = int((time.perf_counter() - t0) * 1000)
                    t_in, t_out = _extract_tokens(result)
                    await track_api_call(
                        provider, t_in, t_out, latency, ok,
                        bot_name=bot_name,
                    )

            return _aw

        @functools.wraps(fn)
        def _sw(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            ok = True
            result = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception:
                ok = False
                raise
            finally:
                latency = int((time.perf_counter() - t0) * 1000)
                t_in, t_out = _extract_tokens(result)
                track_api_call_sync(
                    provider, t_in, t_out, latency, ok,
                    bot_name=bot_name,
                )

        return _sw

    return _decorator


def _extract_tokens(result: Any) -> tuple[int, int]:
    """Best-effort extraction of {tokens_in, tokens_out} from common shapes."""
    if isinstance(result, dict):
        ti = result.get("tokens_in") or result.get("prompt_tokens") or 0
        to = result.get("tokens_out") or result.get("completion_tokens") or 0
        try:
            return int(ti or 0), int(to or 0)
        except Exception:
            return 0, 0
    return 0, 0
