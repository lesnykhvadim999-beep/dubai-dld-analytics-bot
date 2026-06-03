"""
Layer 21 — Admin approval flow.

request_approval(proposal_id, bot) — шлёт админу сообщение с inline-кнопками.
handle_decision(callback_data, admin_id) — обрабатывает '✅/❌', обновляет статус.
Никакого auto-apply.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from ._db import get_proposal, set_decision, list_by_status

log = logging.getLogger("self_modify.approval")

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)

CB_APPROVE_PREFIX = "sm_approve:"
CB_REJECT_PREFIX = "sm_reject:"


def _truncate(s: str, n: int = 1500) -> str:
    return s if len(s) <= n else s[:n] + "\n... (truncated)"


def _send_admin_message(text: str, kb: list[list[dict]]) -> bool:
    if not ADMIN_BOT_TOKEN or not ADMIN_CHAT_ID:
        log.warning("admin bot env missing — preview only")
        log.info("PROPOSAL PREVIEW:\n%s", text[:1200])
        return False
    import requests
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": {"inline_keyboard": kb},
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.exception("send_admin failed: %s", e)
        return False


def request_approval(proposal_id: int) -> bool:
    p = get_proposal(proposal_id)
    if not p:
        return False
    if not p.get("sandbox_passed"):
        log.warning("request_approval: sandbox not passed for #%s", proposal_id)
        return False

    text = (
        f"🤖 *Code proposal #{proposal_id}*\n"
        f"_repo:_ `{p['target_repo']}`\n"
        f"_files:_ `{p['target_files']}`\n"
        f"_bug:_ `{p.get('bug_id') or '—'}`\n\n"
        f"*{p['title']}*\n\n"
        f"_Why:_ {p.get('rationale','')[:400]}\n\n"
        f"```diff\n{_truncate(p['proposed_diff'], 1400)}\n```\n"
        f"Sandbox: ✅ passed in "
        f"{(p.get('sandbox_test_result') or {}).get('duration_s','?')}s"
    )
    kb = [[
        {"text": "✅ Применить", "callback_data": f"{CB_APPROVE_PREFIX}{proposal_id}"},
        {"text": "❌ Отклонить", "callback_data": f"{CB_REJECT_PREFIX}{proposal_id}"},
    ]]
    return _send_admin_message(text, kb)


def handle_decision(callback_data: str, admin_id: int) -> Dict[str, Any]:
    """Возвращает {action, proposal_id, ok}."""
    if callback_data.startswith(CB_APPROVE_PREFIX):
        pid = int(callback_data[len(CB_APPROVE_PREFIX):])
        ok = set_decision(pid, approved=True, admin_id=admin_id)
        return {"action": "approve", "proposal_id": pid, "ok": ok}
    if callback_data.startswith(CB_REJECT_PREFIX):
        pid = int(callback_data[len(CB_REJECT_PREFIX):])
        ok = set_decision(pid, approved=False, admin_id=admin_id)
        return {"action": "reject", "proposal_id": pid, "ok": ok}
    return {"action": "noop", "proposal_id": None, "ok": False}


def list_pending_proposals(limit: int = 10):
    return list_by_status("pending", limit)
