"""
Layer 18 — Photo Understanding (Gemini Flash 2.0 Vision, бесплатный план).

analyze_photo(bytes/path, *, bot, user_id, file_id, file_unique_id) -> dict
Возвращает извлечённые признаки + сохраняет в multimodal_inputs.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Union

import requests

from ._db import save_multimodal

log = logging.getLogger("multimodal.photo")

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.0-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# Vadim Realty + RERA BRN 65011: фокус — оценка объекта.
SYSTEM_PROMPT = (
    "You are a Dubai real estate vision analyst for Vadim Realty (RERA BRN 65011). "
    "Extract from the photo a STRICT JSON with keys: "
    '{"category":"apartment|villa|townhouse|office|land|other",'
    '"rooms_visible":int,"likely_bedrooms":int|null,'
    '"view":"sea|marina|skyline|park|street|inner|unknown",'
    '"condition":"new|fitted|renovated|old|shell_and_core|unknown",'
    '"furnishing":"furnished|semi|unfurnished|unknown",'
    '"size_estimate_sqft":int|null,'
    '"est_value_aed":int|null,'
    '"key_features":[string],'
    '"red_flags":[string],'
    '"summary":string}. '
    "Return ONLY raw JSON, no markdown, no commentary."
)


def _load_bytes(src: Union[bytes, str, Path]) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src)
    return Path(src).read_bytes()


def _safe_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    # Срываем markdown-обвязку если LLM её добавил.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"summary": text[:500], "_parse_error": True}


def analyze_photo(
    image: Union[bytes, str, Path],
    *,
    bot: str = "unknown",
    user_id: int = 0,
    file_id: str = "",
    file_unique_id: Optional[str] = None,
    mime_type: str = "image/jpeg",
    persist: bool = True,
) -> Dict[str, Any]:
    """
    Возвращает dict с извлечёнными фичами. Никогда не кидает наружу —
    в случае ошибки кладёт {"error": ...}.
    """
    if not GEMINI_KEY:
        log.warning("GEMINI_API_KEY not set, photo_understanding skipped")
        return {"error": "no_gemini_key"}

    try:
        b = _load_bytes(image)
        b64 = base64.b64encode(b).decode("ascii")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": SYSTEM_PROMPT},
                        {"inline_data": {"mime_type": mime_type, "data": b64}},
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        }
        r = requests.post(
            f"{GEMINI_URL}?key={GEMINI_KEY}",
            json=payload,
            timeout=25,
        )
        if r.status_code != 200:
            err = f"gemini_http_{r.status_code}: {r.text[:200]}"
            log.error(err)
            if persist:
                save_multimodal(
                    user_id, bot, "photo", file_id, {},
                    file_unique_id=file_unique_id, mime_type=mime_type,
                    llm_provider=GEMINI_MODEL, error=err,
                )
            return {"error": err}

        data = r.json()
        text = (
            data.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        extracted = _safe_json(text)

        if persist:
            save_multimodal(
                user_id, bot, "photo", file_id, extracted,
                file_unique_id=file_unique_id, mime_type=mime_type,
                llm_provider=GEMINI_MODEL,
            )
        return extracted

    except Exception as e:  # noqa: BLE001
        log.exception("analyze_photo failed: %s", e)
        if persist:
            save_multimodal(
                user_id, bot, "photo", file_id, {},
                file_unique_id=file_unique_id, mime_type=mime_type,
                llm_provider=GEMINI_MODEL, error=str(e)[:300],
            )
        return {"error": str(e)[:300]}


def format_for_user(extracted: Dict[str, Any], lang: str = "ru") -> str:
    """Человеко-читаемый ответ для бота."""
    if "error" in extracted:
        return "🤖 Не удалось распознать фото. Пришлите более чёткий снимок."
    cat = extracted.get("category", "—")
    view = extracted.get("view", "—")
    cond = extracted.get("condition", "—")
    summary = extracted.get("summary", "")
    est = extracted.get("est_value_aed")
    if lang == "ru":
        head = f"🏠 *Анализ фото*\nТип: `{cat}`  Вид: `{view}`  Состояние: `{cond}`\n"
        price = f"\n💰 Ориентир: ~{est:,} AED" if est else ""
        return head + (f"\n_{summary}_" if summary else "") + price + \
               "\n\n_Vadim Realty · RERA BRN 65011_"
    return f"🏠 {cat} · {view} · {cond}\n{summary}"
