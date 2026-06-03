"""
Layer 22 — Background thinker.

После каждого user-запроса (через bot handler) запускает фоновую таску:
LLM генерирует "что было бы полезно сказать через час/день",
сохраняет в continuous_thoughts со status=queued.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import requests

from ._db import insert_thought
from .rate_limiter import can_send_followup

log = logging.getLogger("continuous_reasoning.thinker")

CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

SYSTEM = (
    "You are a thoughtful follow-up planner for Vadim Realty (RERA BRN 65011) "
    "Telegram bots. Given the latest user message and recent context, "
    "decide if a delayed follow-up (1h..48h) would be USEFUL (not spammy) "
    "and what to say. STRICT JSON ONLY: "
    '{"should_followup":true|false,'
    '"delay_hours":int,'
    '"thought":"why this is useful",'
    '"followup_msg":"final RU message <=400 chars, end with: \\"Если не нужно — /noreminders\\""}.'
    " If unsure → should_followup=false."
)


def _ask_llm(user_msg: str, context: str = "") -> Optional[Dict[str, Any]]:
    if not CEREBRAS_KEY:
        return None
    prompt = f"USER MESSAGE:\n{user_msg}\n\nCONTEXT:\n{context}\n\nDecide:"
    try:
        r = requests.post(
            CEREBRAS_URL,
            headers={"Authorization": f"Bearer {CEREBRAS_KEY}"},
            json={
                "model": CEREBRAS_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.4,
                "max_tokens": 400,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning("cerebras http=%s", r.status_code)
            return None
        text = r.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        try:
            return json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        log.exception("thinker LLM call failed: %s", e)
    return None


def _decide_and_save(user_id: int, bot: str, user_msg: str, context: str) -> Optional[int]:
    if not can_send_followup(user_id):
        log.debug("rate-limit blocks user=%s", user_id)
        return None
    plan = _ask_llm(user_msg, context)
    if not plan or not plan.get("should_followup"):
        return None
    delay = max(1, min(int(plan.get("delay_hours") or 24), 48))
    msg = (plan.get("followup_msg") or "").strip()
    if not msg:
        return None
    if "/noreminders" not in msg:
        msg = msg.rstrip() + "\n\nЕсли не нужно — /noreminders"
    fire_at = datetime.now(timezone.utc) + timedelta(hours=delay)
    return insert_thought(
        user_id=user_id, bot=bot, trigger_msg=user_msg,
        thought=str(plan.get("thought") or ""),
        followup_msg=msg, fire_at=fire_at,
    )


def fire_background_think(
    user_id: int, bot: str, user_msg: str, *, context: str = "",
) -> None:
    """
    Запускает фоновый thread. Никогда не блокирует основной handler.
    Без auto-send — handler только формирует план; отправка — через cron.
    """
    def _runner():
        try:
            tid = _decide_and_save(user_id, bot, user_msg, context)
            log.debug("background_think saved thought_id=%s", tid)
        except Exception as e:  # noqa: BLE001
            log.exception("background_think failed: %s", e)

    t = threading.Thread(target=_runner, daemon=True, name="cr-thinker")
    t.start()
