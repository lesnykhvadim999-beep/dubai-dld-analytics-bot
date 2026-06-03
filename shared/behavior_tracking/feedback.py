"""
behavior_tracking.feedback — inline feedback buttons + callback handler.

Callback data format:  fb:<rating>:<interaction_id>
  rating ∈ {+1, -1, 0}
"""
from __future__ import annotations

import os
import threading
from typing import Optional, Tuple

from .tracker import _get_conn, is_enabled, _redact


def is_feedback_enabled() -> bool:
    return os.environ.get("BEHAVIOR_FEEDBACK_ENABLED", "0") == "1"


_EMOJI = {1: "👍", -1: "👎", 0: "🤔"}
_LABELS = {
    "ru": {1: "Полезно", -1: "Плохо", 0: "Не то"},
    "en": {1: "Useful", -1: "Bad", 0: "Off-topic"},
}


def feedback_kb(interaction_id: Optional[int], lang: str = "ru"):
    """
    Build python-telegram-bot InlineKeyboardMarkup with 3 feedback buttons.
    Returns None if feedback disabled or interaction_id missing OR import fails.
    """
    if not is_feedback_enabled() or not interaction_id:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return None
    labels = _LABELS.get(lang, _LABELS["ru"])
    iid = int(interaction_id)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"👍 {labels[1]}",  callback_data=f"fb:+1:{iid}"),
        InlineKeyboardButton(f"👎 {labels[-1]}", callback_data=f"fb:-1:{iid}"),
        InlineKeyboardButton(f"🤔 {labels[0]}",  callback_data=f"fb:0:{iid}"),
    ]])


def record_feedback(*, interaction_id: int, user_id: Optional[int],
                    bot_name: str, rating: int,
                    feedback_text: Optional[str] = None) -> Optional[int]:
    """Sync insert. Returns feedback id."""
    if not is_enabled():
        return None
    conn = _get_conn()
    if conn is None:
        return None
    try:
        emoji = _EMOJI.get(int(rating), "")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_feedback
                  (interaction_id, user_id, bot_name, rating, rating_emoji, feedback_text, responded)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id;
                """,
                (interaction_id, user_id, bot_name, int(rating), emoji,
                 _redact(feedback_text, 1000), True),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        try:
            print(f"[behavior_tracking] record_feedback failed: {e}")
        except Exception:
            pass
        return None


def parse_callback(data: str) -> Optional[Tuple[int, int]]:
    """
    'fb:+1:123' -> (1, 123).
    Returns None if not a feedback callback.
    """
    if not data or not data.startswith("fb:"):
        return None
    try:
        _, r, iid = data.split(":", 2)
        rating = int(r)
        return rating, int(iid)
    except Exception:
        return None


async def handle_feedback_callback(update, context, *, bot_name: str) -> bool:
    """
    Generic python-telegram-bot CallbackQueryHandler.
    Returns True if the callback was a feedback one and was handled.
    """
    try:
        q = update.callback_query
        if q is None:
            return False
        parsed = parse_callback(q.data or "")
        if parsed is None:
            return False
        rating, iid = parsed
        user_id = q.from_user.id if q.from_user else None
        # spawn record in background — don't block the answer()
        threading.Thread(
            target=record_feedback,
            kwargs=dict(interaction_id=iid, user_id=user_id, bot_name=bot_name,
                        rating=rating),
            daemon=True,
        ).start()
        try:
            ack = {1: "Спасибо! 👍", -1: "Принято, улучшим 👎", 0: "Понял, доработаем 🤔"}.get(rating, "Спасибо!")
            await q.answer(ack, show_alert=False)
        except Exception:
            pass
        # if negative — prompt for free-text comment
        if rating == -1:
            try:
                await q.message.reply_text(
                    "Что именно не так? Напиши одной строкой — учту в улучшениях."
                )
                # mark in user context so next message can be captured
                context.user_data["awaiting_feedback_for"] = iid
            except Exception:
                pass
        return True
    except Exception:
        return False


async def maybe_capture_feedback_text(update, context, *, bot_name: str) -> bool:
    """
    If user previously clicked 👎 and was asked for a comment, capture next message.
    Returns True if handled (caller should skip default processing).
    """
    try:
        iid = (context.user_data or {}).get("awaiting_feedback_for") if context else None
        if not iid:
            return False
        msg = update.effective_message
        if not msg or not (msg.text or "").strip():
            return False
        user_id = update.effective_user.id if update.effective_user else None
        threading.Thread(
            target=record_feedback,
            kwargs=dict(interaction_id=int(iid), user_id=user_id, bot_name=bot_name,
                        rating=-1, feedback_text=msg.text),
            daemon=True,
        ).start()
        try:
            context.user_data.pop("awaiting_feedback_for", None)
        except Exception:
            pass
        try:
            await msg.reply_text("Спасибо, записал. ✍️")
        except Exception:
            pass
        return True
    except Exception:
        return False
