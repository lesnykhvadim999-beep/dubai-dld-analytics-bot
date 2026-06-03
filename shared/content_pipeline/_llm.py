"""Тонкий клиент LLM для сторителлинг-постов: Cerebras → Groq fallback."""
import os
import logging
from typing import Optional

import requests

log = logging.getLogger("content_pipeline.llm")

CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

SYSTEM_BRAND = (
    "You are content editor for Vadim Realty Telegram channel. "
    "Always sign as Vadim Realty (RERA BRN 65011). NEVER mention any brokerage company name. "
    "Tone: дружелюбный, экспертный, на русском, без рекламной воды."
)


def chat(prompt: str, *, system: str = SYSTEM_BRAND, max_tokens: int = 700,
         temperature: float = 0.6) -> Optional[str]:
    if CEREBRAS_KEY:
        out = _call(
            "https://api.cerebras.ai/v1/chat/completions",
            CEREBRAS_KEY, CEREBRAS_MODEL, system, prompt, max_tokens, temperature,
        )
        if out:
            return out
    if GROQ_KEY:
        out = _call(
            "https://api.groq.com/openai/v1/chat/completions",
            GROQ_KEY, GROQ_MODEL, system, prompt, max_tokens, temperature,
        )
        if out:
            return out
    log.warning("no LLM provider available")
    return None


def _call(url, key, model, system, user, max_tokens, temperature) -> Optional[str]:
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=20,
        )
        if r.status_code != 200:
            log.warning("%s http=%s body=%s", url, r.status_code, r.text[:200])
            return None
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        log.exception("LLM call failed: %s", e)
        return None
