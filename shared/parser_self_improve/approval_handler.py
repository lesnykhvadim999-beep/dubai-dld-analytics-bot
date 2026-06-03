"""
approval_handler — обработка Telegram callback'ов psi:approve|reject|more.

Подключается в admin-bot-handler (или resale-bot для администратора).
Использование (aiogram-style):

    from parser_self_improve.approval_handler import register_handlers
    register_handlers(dp)

Если в проекте другой фреймворк — есть `handle_callback(data) -> str`
для прямого вызова из existing router.
"""
from __future__ import annotations

import json
import logging

from ._db import conn_cursor

log = logging.getLogger(__name__)


def _get_cluster(cluster_id: int) -> dict | None:
    with conn_cursor(dict_cursor=True) as (_, cur):
        cur.execute(
            "SELECT * FROM parser_pattern_clusters WHERE id=%s",
            (cluster_id,),
        )
        return cur.fetchone()


def _mark_status(cluster_id: int, status: str) -> None:
    field = {
        "approved": "approved_at",
        "rejected": "rejected_at",
        "applied": "applied_at",
    }.get(status)
    with conn_cursor() as (_, cur):
        if field:
            cur.execute(
                f"UPDATE parser_pattern_clusters SET status=%s, {field}=NOW() WHERE id=%s",
                (status, cluster_id),
            )
        else:
            cur.execute(
                "UPDATE parser_pattern_clusters SET status=%s WHERE id=%s",
                (status, cluster_id),
            )


def _samples_for_cluster(cluster_id: int, offset: int = 0, limit: int = 10) -> list[str]:
    with conn_cursor() as (_, cur):
        cur.execute(
            """
            SELECT original_text
            FROM parser_failed_log
            WHERE cluster_id=%s
            ORDER BY id
            LIMIT %s OFFSET %s
            """,
            (cluster_id, limit, offset),
        )
        return [r[0] for r in cur.fetchall()]


def handle_callback(data: str) -> str:
    """Универсальный entry-point. Возвращает текст ответа.

    data format: psi:<action>:<cluster_id>[:<page>]
    """
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "psi":
        return "Invalid callback"
    action = parts[1]
    try:
        cid = int(parts[2])
    except Exception:
        return "Bad cluster id"

    if action == "approve":
        # Reject применение pattern автоматически не делаем — только статус.
        # pattern_applier подхватит approved status при следующем cron-цикле,
        # или Vadim запустит вручную.
        _mark_status(cid, "approved")
        # Авто-применение (через git) — отдельным шагом
        try:
            from .pattern_applier import apply_approved_cluster
            sha = apply_approved_cluster(cid)
            if sha:
                return f"Approved cluster {cid}, applied commit {sha[:8]}"
            return f"Approved cluster {cid} (apply queued — manual review needed)"
        except Exception as exc:
            log.warning("apply_approved_cluster failed: %s", exc)
            return f"Approved cluster {cid} (apply error: {exc})"

    if action == "reject":
        _mark_status(cid, "rejected")
        # И отметим все failed_log этого cluster как resolved (не предлагать снова)
        with conn_cursor() as (_, cur):
            cur.execute(
                "UPDATE parser_failed_log SET resolved=true, resolved_at=NOW() WHERE cluster_id=%s",
                (cid,),
            )
        return f"Rejected cluster {cid}"

    if action == "more":
        page = int(parts[3]) if len(parts) > 3 else 1
        offset = page * 10
        samples = _samples_for_cluster(cid, offset=offset, limit=10)
        if not samples:
            return f"No more samples (cluster {cid})"
        body = "\n\n---\n\n".join(s[:300] for s in samples)
        return f"Cluster {cid} samples page {page}:\n\n{body}"

    return "Unknown action"


# ──────────────────────────── aiogram glue (optional) ──────────────────────
def register_handlers(dp) -> None:
    """Регистрация в aiogram Dispatcher. Без жёсткого import — try/except."""
    try:
        from aiogram import F  # type: ignore
        from aiogram.types import CallbackQuery  # type: ignore
    except Exception as exc:
        log.warning("aiogram not available: %s — skip register_handlers", exc)
        return

    @dp.callback_query(F.data.startswith("psi:"))
    async def _on_psi(cb: CallbackQuery):  # type: ignore
        try:
            reply = handle_callback(cb.data or "")
            await cb.answer(reply[:200], show_alert=False)
            # Для action=more / approve / reject — отправим полное сообщение
            if cb.message and len(reply) > 200:
                await cb.message.answer(reply[:4000])
        except Exception as exc:
            log.exception("psi callback error")
            await cb.answer(f"Error: {exc}"[:200], show_alert=True)
