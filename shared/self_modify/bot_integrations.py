"""
Drop-in для admin-bot: callback handler для approve/reject code_proposals
+ команда /proposals.

    from shared.self_modify.bot_integrations import register_self_modify_handlers
    register_self_modify_handlers(dp)
"""
from __future__ import annotations

import logging
from typing import Any

from .approval import handle_decision, list_pending_proposals, CB_APPROVE_PREFIX, CB_REJECT_PREFIX
from .applier import apply_approved

log = logging.getLogger("self_modify.integrations")


def register_self_modify_handlers(dp: Any) -> None:
    try:
        from aiogram import F
        from aiogram.filters import Command
        from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
    except Exception:
        log.warning("aiogram not available — handlers not registered")
        return

    @dp.message(Command("proposals"))
    async def _list(message: "Message") -> None:
        items = list_pending_proposals(10)
        if not items:
            await message.answer("Нет pending-патчей.")
            return
        for p in items:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Применить",
                                     callback_data=f"{CB_APPROVE_PREFIX}{p['id']}"),
                InlineKeyboardButton(text="❌ Отклонить",
                                     callback_data=f"{CB_REJECT_PREFIX}{p['id']}"),
            ]])
            text = (
                f"🤖 *Proposal #{p['id']}* `{p['target_repo']}`\n"
                f"*{p['title']}*\n\n"
                f"_Why:_ {p.get('rationale','')[:300]}\n\n"
                f"```\n{(p['proposed_diff'] or '')[:900]}\n```"
            )
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")

    @dp.callback_query(F.data.startswith(CB_APPROVE_PREFIX) | F.data.startswith(CB_REJECT_PREFIX))
    async def _decide(cb: "CallbackQuery") -> None:
        admin_id = cb.from_user.id if cb.from_user else 0
        result = handle_decision(cb.data, admin_id)
        if not result["ok"]:
            await cb.answer("Не получилось обновить статус")
            return
        if result["action"] == "approve":
            await cb.answer("Approved ✅ — запускаю applier")
            # apply в фоне, чтобы не блокировать callback
            import threading
            threading.Thread(
                target=lambda: apply_approved(result["proposal_id"]),
                daemon=True, name="sm-applier",
            ).start()
        else:
            await cb.answer("Rejected ❌")
