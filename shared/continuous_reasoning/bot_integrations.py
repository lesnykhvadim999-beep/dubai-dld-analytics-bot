"""
Drop-in для любого бота:

    from shared.continuous_reasoning.bot_integrations import (
        attach_background_think, register_noreminders_command,
    )

    attach_background_think(dp, bot_name="resale-bot")
    register_noreminders_command(dp)
"""
from __future__ import annotations

import logging
from typing import Any

from .background_thinker import fire_background_think
from .rate_limiter import mark_optout

log = logging.getLogger("continuous_reasoning.integrations")


def attach_background_think(dp: Any, *, bot_name: str) -> None:
    """
    Универсальный middleware-style: после каждого text-сообщения юзера
    стреляет в background_thinker. Не блокирует UX.
    """
    try:
        from aiogram import F
        from aiogram.types import Message
    except Exception:
        log.warning("aiogram not available — attach_background_think skipped")
        return

    # high-priority router last — чтобы не перехватывать обычные команды
    @dp.message(F.text & ~F.text.startswith("/"))
    async def _after_text(message: "Message") -> None:
        try:
            fire_background_think(
                user_id=message.from_user.id if message.from_user else 0,
                bot=bot_name,
                user_msg=message.text or "",
                context="",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("attach_background_think error: %s", e)
        # ВАЖНО: не отвечаем — пусть основной обработчик отрабатывает.
        # aiogram продолжит цепочку, если этот хэндлер не вызвал .answer().


def register_noreminders_command(dp: Any) -> None:
    try:
        from aiogram.filters import Command
        from aiogram.types import Message
    except Exception:
        return

    @dp.message(Command("noreminders"))
    async def _opt(message: "Message") -> None:
        uid = message.from_user.id if message.from_user else 0
        if not uid:
            return
        mark_optout(uid)
        await message.answer(
            "✅ Готово. Я больше не буду писать с подсказками. "
            "Если передумаете — просто напишите мне и используйте /reminders_on.\n\n"
            "_Vadim Realty · RERA BRN 65011_",
            parse_mode="Markdown",
        )
