"""
shared.user_memory.integration — turnkey helpers for bots.

Public API for bot handlers:
    from shared.user_memory.integration import (
        enrich_context, record_turn,
    )

    # On every incoming message:
    record_turn(user_id, bot_name, language="ru")

    # When you need memory injected into your prompt:
    facts_block = enrich_context(user_id, current_query, bot_name="resale-bot")
    if facts_block:
        prompt = f"{facts_block}\n\nUser: {current_query}"
"""
from __future__ import annotations
import asyncio
import threading
from typing import Optional, List, Dict, Any

from .retriever import semantic_search_facts, get_user_profile
from .extractor import extract_facts_for_user, upsert_user_profile


def enrich_context(user_id: int, current_query: str,
                   bot_name: Optional[str] = None,
                   top_k: int = 5,
                   language: Optional[str] = None) -> str:
    """Return a short context block ready to prepend to an LLM prompt.

    Empty string if nothing useful. Safe to call always.
    """
    try:
        facts = semantic_search_facts(user_id, current_query,
                                      bot_name=bot_name, top_k=top_k)
        if not facts:
            return ""
        header_ru = "Что мы знаем о пользователе:"
        header_en = "What we know about the user:"
        header = header_ru if (language or "").startswith("ru") else header_en
        lines = [header]
        for f in facts:
            tag = f.get("fact_type", "other")
            txt = f.get("fact_text", "").strip()
            if not txt:
                continue
            lines.append(f"- [{tag}] {txt}")
        return "\n".join(lines)
    except Exception:
        return ""


def get_facts_list(user_id: int, current_query: str,
                   bot_name: Optional[str] = None,
                   top_k: int = 5) -> List[Dict[str, Any]]:
    """Raw list, when bots want to format their own context."""
    try:
        return semantic_search_facts(user_id, current_query,
                                     bot_name=bot_name, top_k=top_k)
    except Exception:
        return []


# ───────────────────────── background extractor trigger ─────────────────────
_pending_users: Dict[str, int] = {}      # key=f"{bot}:{uid}" → turn count
_pending_lock = threading.Lock()
_EXTRACT_EVERY = 5  # run extraction every Nth turn per user


def record_turn(user_id: int, bot_name: str,
                language: Optional[str] = None,
                last_user_text: Optional[str] = None,
                force_extract: bool = False) -> None:
    """Lightweight bookkeeping: bump counters, maybe schedule extraction."""
    try:
        upsert_user_profile(user_id, bot_name, language=language,
                            recent_text=last_user_text)
    except Exception:
        pass

    key = f"{bot_name}:{user_id}"
    schedule = False
    with _pending_lock:
        _pending_users[key] = _pending_users.get(key, 0) + 1
        if force_extract or _pending_users[key] >= _EXTRACT_EVERY:
            _pending_users[key] = 0
            schedule = True
    if not schedule:
        return

    def _run():
        try:
            extract_facts_for_user(user_id, bot_name=bot_name,
                                   lookback_n=20)
        except Exception:
            pass

    # Try asyncio task first; fall back to a daemon thread.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(asyncio.to_thread(_run))
        return
    except RuntimeError:
        pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def get_user_summary(user_id: int, bot_name: str) -> Dict[str, Any]:
    """High-level snapshot for admin UIs / debugging."""
    profile = get_user_profile(user_id, bot_name) or {}
    facts = semantic_search_facts(user_id, "", bot_name=bot_name, top_k=20)
    return {"profile": profile, "facts": facts}
