"""
admin_notifier — отправляет Telegram-сообщение с предложением паттерна.

Использует ADMIN_NOTIFY_TOKEN/ADMIN_CHAT_ID. Если не настроено — silent skip.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Sequence

import requests

log = logging.getLogger(__name__)

ADMIN_TOKEN = (
    os.environ.get("ADMIN_BOT_TOKEN")
    or os.environ.get("VADIM_ADMIN_BOT_TOKEN")
    or os.environ.get("RESALE_BOT_TOKEN")  # fallback
)
ADMIN_CHAT_ID = (
    os.environ.get("ADMIN_CHAT_ID")
    or os.environ.get("VADIM_ADMIN_CHAT_ID")
)


def _esc(s: str) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _build_message(cluster_id: int, size: int, rule: dict, samples: Sequence[str],
                   validation: dict | None = None) -> str:
    sample = _esc((samples[0] if samples else "")[:300])
    sym = _esc(rule.get("symptom", "—"))
    field = _esc(rule.get("field", "—"))
    # v2: pattern, fallback на legacy regex
    regex = _esc(rule.get("pattern") or rule.get("regex") or "—")
    hint = _esc(rule.get("extractor_hint", "—"))
    conf = rule.get("confidence", 0)
    provider = _esc(rule.get("_provider", "?"))

    # ── Validation block (v2) ──
    v = validation or rule.get("_validation") or {}
    mr = (v.get("match_rate") or 0) * 100
    fpr = (v.get("fp_rate") or 0) * 100
    tested_random = v.get("tested_random", 0)
    val_block = (
        f"\n<b>Validation</b> ✅\n"
        f"  Match rate (cluster): <b>{mr:.0f}%</b>\n"
        f"  False-positive rate (random {tested_random}): <b>{fpr:.1f}%</b>\n"
    )

    # ── Test cases recap ──
    tc_lines = []
    for tc in (rule.get("test_cases") or [])[:5]:
        if not isinstance(tc, dict):
            continue
        inp = _esc(str(tc.get("input", ""))[:60])
        exp = tc.get("expected_value")
        if exp is None:
            tc_lines.append(f"  ✅ <i>{inp}</i> → <code>no match</code> (correctly excluded)")
        else:
            tc_lines.append(f"  ✅ <i>{inp}</i> → <code>{_esc(str(exp))}</code>")
    tc_block = ("\n<b>Test cases passed:</b>\n" + "\n".join(tc_lines)) if tc_lines else ""

    anti = rule.get("anti_patterns") or []
    anti_block = ""
    if anti:
        anti_block = "\n<b>Anti-patterns:</b> " + ", ".join(_esc(str(a)) for a in anti[:6])

    return (
        f"🧠 <b>New parser pattern detected (validated)</b>\n\n"
        f"<b>Cluster #{cluster_id}</b> · size={size} · llm={provider} · conf={conf}\n"
        f"<b>Field:</b> {field}\n"
        f"<b>Symptom:</b> {sym}"
        f"{val_block}"
        f"\n<b>Pattern:</b>\n<code>{regex}</code>\n\n"
        f"<b>Hint:</b> {hint}"
        f"{tc_block}"
        f"{anti_block}\n\n"
        f"<b>Sample text:</b>\n<i>{sample}</i>"
    )


def _build_keyboard(cluster_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": f"psi:approve:{cluster_id}"},
                {"text": "❌ Reject", "callback_data": f"psi:reject:{cluster_id}"},
            ],
            [
                {"text": "👀 10 more samples", "callback_data": f"psi:more:{cluster_id}:1"},
            ],
        ]
    }


def send_pattern_proposal(
    cluster_id: int,
    size: int,
    rule: dict,
    samples: Sequence[str],
    validation: dict | None = None,
) -> int | None:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        log.info("admin_notifier: ADMIN_BOT_TOKEN/ADMIN_CHAT_ID not set — skip")
        return None
    text = _build_message(cluster_id, size, rule, samples, validation=validation)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": _build_keyboard(cluster_id),
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        return data.get("result", {}).get("message_id")
    except Exception as exc:
        log.warning("admin_notifier send failed: %s", exc)
        return None


def send_summary(text: str) -> int | None:
    if not (ADMIN_TOKEN and ADMIN_CHAT_ID):
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        return r.json().get("result", {}).get("message_id")
    except Exception as exc:
        log.warning("admin_notifier summary failed: %s", exc)
        return None
