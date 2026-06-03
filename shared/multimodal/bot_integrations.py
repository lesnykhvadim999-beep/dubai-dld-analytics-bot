"""
Drop-in handlers для aiogram-ботов.

Использование (resale-bot main.py):

    from shared.multimodal.bot_integrations import register_multimodal_handlers
    register_multimodal_handlers(dp, bot_name="resale-bot")

Для lead-bot — то же с bot_name="lead-bot" (voice -> CRM intent).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import analyze_photo, voice_to_intent, parse_document
from .photo_understanding import format_for_user

log = logging.getLogger("multimodal.integrations")


def register_multimodal_handlers(dp: Any, *, bot_name: str = "unknown") -> None:
    """Регистрирует aiogram-хэндлеры для фото / голоса / документа."""
    try:
        from aiogram import F
        from aiogram.types import Message
    except Exception:  # pragma: no cover - aiogram отсутствует в тестах
        log.warning("aiogram not available — handlers not registered")
        return

    @dp.message(F.photo)
    async def _on_photo(message: "Message") -> None:
        try:
            ph = message.photo[-1]
            file = await message.bot.get_file(ph.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            extracted = analyze_photo(
                data, bot=bot_name,
                user_id=message.from_user.id if message.from_user else 0,
                file_id=ph.file_id, file_unique_id=ph.file_unique_id,
                mime_type="image/jpeg",
            )
            await message.answer(format_for_user(extracted, lang="ru"),
                                 parse_mode="Markdown")
        except Exception as e:  # noqa: BLE001
            log.exception("photo handler error: %s", e)
            try:
                await message.answer("🤖 Не смог обработать фото, попробуйте ещё раз.")
            except Exception:
                pass

    @dp.message(F.voice)
    async def _on_voice(message: "Message") -> None:
        try:
            v = message.voice
            file = await message.bot.get_file(v.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            result = voice_to_intent(
                data, bot=bot_name,
                user_id=message.from_user.id if message.from_user else 0,
                file_id=v.file_id, file_unique_id=v.file_unique_id,
                mime=v.mime_type or "audio/ogg",
            )
            if "error" in result:
                await message.answer("🤖 Не удалось распознать голосовое сообщение.")
                return
            intent = result.get("intent", "unknown")
            summary = result.get("summary", "")
            await message.answer(
                f"🎤 Понял: *{intent}*\n_{summary}_\n\n"
                "_Vadim Realty · RERA BRN 65011_",
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("voice handler error: %s", e)

    @dp.message(F.document)
    async def _on_document(message: "Message") -> None:
        try:
            d = message.document
            mt = d.mime_type or "application/octet-stream"
            if not (mt == "application/pdf" or mt.startswith("image/")):
                return
            file = await message.bot.get_file(d.file_id)
            buf = await message.bot.download_file(file.file_path)
            data = buf.read() if hasattr(buf, "read") else bytes(buf)
            extracted = parse_document(
                data, bot=bot_name,
                user_id=message.from_user.id if message.from_user else 0,
                file_id=d.file_id, file_unique_id=d.file_unique_id,
                mime_type=mt,
            )
            if "error" in extracted:
                await message.answer("📄 Не смог разобрать документ.")
                return
            summary = extracted.get("summary", "")
            await message.answer(
                f"📄 *Документ:* {extracted.get('doc_type','?')}\n"
                f"Спальни: {extracted.get('bedrooms','—')} · "
                f"Площадь: {extracted.get('total_sqft','—')} sqft\n\n"
                f"_{summary}_\n\n_Vadim Realty · RERA BRN 65011_",
                parse_mode="Markdown",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("document handler error: %s", e)

    log.info("multimodal handlers registered for bot=%s", bot_name)
