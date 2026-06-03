"""
Drop-in для resale-bot: кнопка "🎥 Виртуальный тур" под карточкой объекта.

Использование:

    from shared.virtual_tours.bot_integrations import (
        tour_inline_button, register_tour_handler,
    )

    # в карточке листинга:
    kb.row(tour_inline_button(listing_id))

    # один раз при старте:
    register_tour_handler(dp, bot_name="resale-bot",
                          fetch_listing=lambda lid: my_db.get_listing(lid))
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from .tour_generator import generate_tour

log = logging.getLogger("virtual_tours.integrations")

CB_PREFIX = "vtour:"


def tour_inline_button(listing_id: str) -> Dict[str, Any]:
    """aiogram-совместимый dict для InlineKeyboardButton."""
    return {"text": "🎥 Виртуальный тур", "callback_data": f"{CB_PREFIX}{listing_id}"}


def register_tour_handler(
    dp: Any, *, bot_name: str,
    fetch_listing: Callable[[str], Optional[Dict[str, Any]]],
) -> None:
    try:
        from aiogram import F
        from aiogram.types import CallbackQuery, FSInputFile
    except Exception:
        log.warning("aiogram not available — tour handler not registered")
        return

    @dp.callback_query(F.data.startswith(CB_PREFIX))
    async def _on_tour(cb: "CallbackQuery") -> None:
        listing_id = cb.data[len(CB_PREFIX):]
        await cb.answer("Готовлю виртуальный тур…")
        try:
            listing = fetch_listing(listing_id) or {"listing_id": listing_id}
            listing.setdefault("listing_id", listing_id)
            tour = generate_tour(listing, bot=bot_name)
            if "error" in tour:
                await cb.message.answer("🤖 Не удалось собрать тур, попробуйте позже.")
                return
            # 1) текст-сценарий
            await cb.message.answer(tour.get("script_md", "🎥 Виртуальный тур"),
                                    parse_mode="Markdown")
            # 2) видео если есть, иначе картинки
            video = tour.get("video_path")
            if video:
                await cb.message.answer_video(FSInputFile(video))
            else:
                for r in tour.get("rooms", []):
                    if r.get("image_path"):
                        await cb.message.answer_photo(
                            FSInputFile(r["image_path"]),
                            caption=r.get("name", ""),
                        )
                audio = tour.get("audio_path")
                if audio:
                    await cb.message.answer_audio(FSInputFile(audio))
        except Exception as e:  # noqa: BLE001
            log.exception("tour handler error: %s", e)
            try:
                await cb.message.answer("🤖 Ошибка при сборке тура.")
            except Exception:
                pass

    log.info("virtual tour handler registered for bot=%s", bot_name)
