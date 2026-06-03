"""Optional integration wrapper around resale-bot's llm_chain.llm_call.

Usage from any bot:

    from shared.quota_tracker.integration import tracked_llm_call
    text = tracked_llm_call("hello", max_tokens=100, bot_name="resale-bot")

Behaviour:
* If resale-bot's llm_chain is importable — wrap llm_call so every invocation
  is tracked (provider attribution falls back to "llm_chain" when the actual
  winning provider isn't exposed by the public API).
* Token counts are best-effort: when the call returns plain text we estimate
  tokens by len(prompt)/4 and len(text)/4 (industry rule-of-thumb).
* Tracker failures never raise.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .tracker import track_api_call_sync

log = logging.getLogger("quota_tracker.integration")

try:
    from llm_chain import llm_call as _llm_call  # type: ignore
    _HAS_LLM_CHAIN = True
except Exception:
    _llm_call = None
    _HAS_LLM_CHAIN = False


def _approx_tokens(s: Optional[str]) -> int:
    if not s:
        return 0
    return max(1, len(s) // 4)


def tracked_llm_call(
    prompt: str,
    *,
    max_tokens: int = 600,
    timeout: int = 15,
    use_cache: bool = True,
    provider_hint: str = "llm_chain",
    bot_name: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[str]:
    """Drop-in replacement for llm_chain.llm_call with quota tracking."""
    if not _HAS_LLM_CHAIN or _llm_call is None:
        log.debug("tracked_llm_call: llm_chain not importable")
        return None

    t0 = time.perf_counter()
    text: Optional[str] = None
    ok = False
    try:
        text = _llm_call(prompt, max_tokens=max_tokens,
                         timeout=timeout, use_cache=use_cache)
        ok = text is not None
        return text
    finally:
        latency = int((time.perf_counter() - t0) * 1000)
        try:
            track_api_call_sync(
                provider=provider_hint,
                tokens_in=_approx_tokens(prompt),
                tokens_out=_approx_tokens(text),
                latency_ms=latency,
                success=ok,
                bot_name=bot_name,
                user_id=user_id,
            )
        except Exception as e:
            log.debug("tracker hook swallow: %s", e)
