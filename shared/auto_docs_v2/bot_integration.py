"""PHASE BO O4 — bot-side /help integration helper.

Each bot's /help command should call:

    from shared.auto_docs_v2.bot_integration import render_help
    text = render_help(bot_name="resale-bot", language=user_lang(uid))
    bot.send_message(cid, text, parse_mode="Markdown")

The helper:
- reads from `bot_help_texts` table (cache-first),
- falls back to live generation via `help_generator`,
- never raises — returns a safe fallback string on any error.
"""
from __future__ import annotations

import sys
from typing import Optional


_FALLBACK = (
    "👋 /help is being prepared. Try again in a minute or contact @VadimRealty.\n"
    "_Vadim Realty (RERA BRN 65011)_"
)


def render_help(bot_name: str, language: str = "ru") -> str:
    lang = (language or "ru").lower()
    if lang not in ("ru", "en"):
        lang = "ru"
    try:
        from .schema import get_help
        cached = get_help(bot_name, lang)
        if cached:
            return cached
    except Exception as e:
        sys.stderr.write(f"[auto_docs_v2.bot_integration] cache read failed: {e}\n")
    # live generation as fallback (also persists)
    try:
        from .help_generator import generate_help
        fresh = generate_help(bot_name, lang, persist=True)
        if fresh:
            return fresh
    except Exception as e:
        sys.stderr.write(f"[auto_docs_v2.bot_integration] generate failed: {e}\n")
    return _FALLBACK
