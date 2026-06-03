"""
Drop-in для admin-bot: команды /queue, /approve, /reject + публикатор.

    from shared.content_pipeline.bot_integrations import (
        register_admin_handlers, publish_approved_loop,
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from ._queue import list_pending, approve, reject, mark_published

log = logging.getLogger("content_pipeline.integrations")

CB_APPROVE = "cpq_approve:"
CB_REJECT = "cpq_reject:"


def register_admin_handlers(dp: Any) -> None:
    try:
        from aiogram import F
        from aiogram.filters import Command
        from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
    except Exception:
        log.warning("aiogram not available — admin handlers not registered")
        return

    @dp.message(Command("queue"))
    async def _q(message: "Message") -> None:
        items = list_pending(10)
        if not items:
            await message.answer("Очередь пуста.")
            return
        for it in items:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅", callback_data=f"{CB_APPROVE}{it['id']}"),
                InlineKeyboardButton(text="❌", callback_data=f"{CB_REJECT}{it['id']}"),
            ]])
            text = (f"#{it['id']} `{it['kind']}` → {it['target_chat']}\n\n"
                    f"{(it['body_md'] or '')[:800]}")
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")

    @dp.callback_query(F.data.startswith(CB_APPROVE))
    async def _ap(cb: "CallbackQuery") -> None:
        qid = int(cb.data[len(CB_APPROVE):])
        admin_id = cb.from_user.id if cb.from_user else 0
        ok = approve(qid, admin_id, "via admin-bot")
        await cb.answer("Approved ✅" if ok else "Не получилось")

    @dp.callback_query(F.data.startswith(CB_REJECT))
    async def _rj(cb: "CallbackQuery") -> None:
        qid = int(cb.data[len(CB_REJECT):])
        admin_id = cb.from_user.id if cb.from_user else 0
        ok = reject(qid, admin_id, "via admin-bot")
        await cb.answer("Rejected ❌" if ok else "Не получилось")


async def publish_approved_loop(bot: Any, *, poll_sec: int = 60) -> None:
    """
    Фоновая корутина в channel-bot-new: каждые 60с публикует approved-посты.
    """
    import psycopg2
    import psycopg2.extras
    DSN = os.getenv(
        "RAILWAY_PG_DSN",
        os.environ.get("LIVE_DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("INTELLIGENCE_DATABASE_URL") or "",
    )
    while True:
        try:
            with psycopg2.connect(DSN, connect_timeout=8) as c:
                with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        "SELECT * FROM content_pipeline_queue "
                        "WHERE status='approved' ORDER BY created_at ASC LIMIT 5"
                    )
                    rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                try:
                    media = r.get("media_paths") or []
                    body = r["body_md"]
                    chat = r["target_chat"]
                    # Простая стратегия: первый media (если картинка) как photo+caption,
                    # остальные — отдельными сообщениями.
                    sent = False
                    if media:
                        first = media[0]
                        if isinstance(first, str) and first.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                            from aiogram.types import FSInputFile
                            if Path(first).exists():
                                await bot.send_photo(chat, FSInputFile(first),
                                                     caption=body[:1024], parse_mode="Markdown")
                                sent = True
                    if not sent:
                        await bot.send_message(chat, body, parse_mode="Markdown",
                                               disable_web_page_preview=True)
                    # доп-файлы (PDF)
                    for extra in media[1:]:
                        if isinstance(extra, str) and Path(extra).exists():
                            from aiogram.types import FSInputFile
                            await bot.send_document(chat, FSInputFile(extra))
                    mark_published(r["id"])
                except Exception as e:  # noqa: BLE001
                    log.exception("publish item %s failed: %s", r.get("id"), e)
        except Exception as e:  # noqa: BLE001
            log.exception("publish loop error: %s", e)
        await asyncio.sleep(poll_sec)
