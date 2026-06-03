"""
shared.session_continuity — PHASE BM Layer 14.

Cross-bot conversation continuity. User context shared across all 6 bots.

Public API:
    get_user_context(telegram_id) -> dict
    update_user_context(telegram_id, key, value, bot=None, intent=None)
    handoff_to_bot(telegram_id, target_bot, message=None) -> dict
    classify_intent(text) -> {"bot": str, "intent": str, "confidence": float}
"""
from .context import get_user_context, update_user_context, touch_session
from .handoff import handoff_to_bot, build_handoff_link
from .intent_classifier import classify_intent, classify

__all__ = [
    "get_user_context",
    "update_user_context",
    "touch_session",
    "handoff_to_bot",
    "build_handoff_link",
    "classify_intent",
    "classify",
]
