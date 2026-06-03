"""
Layer 18 — Voice -> Intent (Groq Whisper-v3, бесплатный план).

voice_to_intent(audio_bytes, *, user_id, bot, file_id) -> {transcript, intent, confidence, ...}

Intent классификация делается через Cerebras (free LLaMa) после транскрипции.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import requests

from ._db import save_multimodal

log = logging.getLogger("multimodal.voice")

GROQ_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_WHISPER_MODEL = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

CEREBRAS_KEY = os.getenv("CEREBRAS_API_KEY", "")
CEREBRAS_MODEL = os.getenv("CEREBRAS_MODEL", "llama-3.3-70b")
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"

INTENTS = [
    "search_listing",   # ищу квартиру
    "sell_property",    # хочу продать
    "rent_property",    # сдать / снять
    "viewing_request",  # хочу посмотреть
    "price_inquiry",    # сколько стоит
    "callback_request", # перезвоните
    "complaint",        # жалоба
    "smalltalk",        # привет
    "unknown",
]

CLASSIFY_PROMPT = (
    "You are a Dubai real-estate lead-intent classifier for Vadim Realty (RERA BRN 65011).\n"
    "Given a short transcript, output STRICT JSON ONLY: "
    '{"intent":"<one of: ' + " | ".join(INTENTS) + '>",'
    '"confidence":0..1,"summary":"1 sentence","entities":{"area":string|null,"budget_aed":int|null,'
    '"bedrooms":int|null,"phone":string|null}}'
)


def _transcribe(audio: Union[bytes, str, Path], mime: str = "audio/ogg") -> Dict[str, Any]:
    if not GROQ_KEY:
        return {"error": "no_groq_key"}
    if isinstance(audio, (str, Path)):
        data = Path(audio).read_bytes()
    else:
        data = bytes(audio)
    try:
        files = {"file": ("voice.ogg", io.BytesIO(data), mime)}
        form = {"model": GROQ_WHISPER_MODEL, "response_format": "json"}
        r = requests.post(
            GROQ_WHISPER_URL,
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files=files,
            data=form,
            timeout=30,
        )
        if r.status_code != 200:
            return {"error": f"groq_http_{r.status_code}: {r.text[:150]}"}
        return {"text": r.json().get("text", "").strip()}
    except Exception as e:  # noqa: BLE001
        log.exception("whisper failed: %s", e)
        return {"error": str(e)[:200]}


def _classify(transcript: str) -> Dict[str, Any]:
    if not transcript:
        return {"intent": "unknown", "confidence": 0.0, "summary": "", "entities": {}}
    if not CEREBRAS_KEY:
        return {"intent": "unknown", "confidence": 0.0, "summary": transcript[:160], "entities": {}}
    try:
        r = requests.post(
            CEREBRAS_URL,
            headers={"Authorization": f"Bearer {CEREBRAS_KEY}"},
            json={
                "model": CEREBRAS_MODEL,
                "messages": [
                    {"role": "system", "content": CLASSIFY_PROMPT},
                    {"role": "user", "content": transcript[:2000]},
                ],
                "temperature": 0.1,
                "max_tokens": 250,
            },
            timeout=12,
        )
        if r.status_code != 200:
            return {"intent": "unknown", "confidence": 0.0,
                    "summary": transcript[:160], "entities": {},
                    "_err": f"cerebras_{r.status_code}"}
        content = r.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        try:
            return json.loads(content)
        except Exception:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            return {"intent": "unknown", "confidence": 0.0, "summary": content[:160], "entities": {}}
    except Exception as e:  # noqa: BLE001
        log.exception("classify failed: %s", e)
        return {"intent": "unknown", "confidence": 0.0, "summary": transcript[:160],
                "entities": {}, "_err": str(e)[:200]}


def voice_to_intent(
    audio: Union[bytes, str, Path],
    *,
    user_id: int = 0,
    bot: str = "unknown",
    file_id: str = "",
    file_unique_id: Optional[str] = None,
    mime: str = "audio/ogg",
    persist: bool = True,
) -> Dict[str, Any]:
    tr = _transcribe(audio, mime=mime)
    if "error" in tr:
        if persist:
            save_multimodal(user_id, bot, "voice", file_id, {},
                            file_unique_id=file_unique_id, mime_type=mime,
                            llm_provider=GROQ_WHISPER_MODEL, error=tr["error"])
        return {"error": tr["error"]}
    transcript = tr.get("text", "")
    cls = _classify(transcript)

    extracted = {
        "transcript": transcript,
        "intent": cls.get("intent", "unknown"),
        "confidence": cls.get("confidence", 0.0),
        "summary": cls.get("summary", ""),
        "entities": cls.get("entities", {}),
    }
    if persist:
        save_multimodal(
            user_id, bot, "voice", file_id, extracted,
            file_unique_id=file_unique_id, mime_type=mime,
            transcript=transcript,
            intent=extracted["intent"],
            confidence=float(extracted["confidence"] or 0.0),
            llm_provider=f"{GROQ_WHISPER_MODEL}+{CEREBRAS_MODEL}",
        )
    return extracted
