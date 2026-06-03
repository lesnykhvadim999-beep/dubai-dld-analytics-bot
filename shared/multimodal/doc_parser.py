"""
Layer 18 — Document/Floor plan parser via Gemini Vision.

Поддержка: PDF (page 1 как картинка → Gemini), PNG/JPG планировок.
Для PDF используем PyMuPDF (fitz), если доступен; иначе fallback на raw байты.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from ._db import save_multimodal
from .photo_understanding import GEMINI_KEY, GEMINI_MODEL, GEMINI_URL

log = logging.getLogger("multimodal.doc")

FLOORPLAN_PROMPT = (
    "You are a Dubai floor-plan / property document analyzer (Vadim Realty, RERA BRN 65011). "
    "Extract STRICT JSON: "
    '{"doc_type":"floor_plan|brochure|contract|title_deed|noc|other",'
    '"bedrooms":int|null,"bathrooms":int|null,"total_sqft":int|null,'
    '"rooms":[{"name":str,"sqft":int|null}],'
    '"developer":str|null,"project":str|null,"unit_number":str|null,'
    '"key_terms":[str],"summary":str}. '
    "Return raw JSON only."
)


def _pdf_first_page_png(pdf_bytes: bytes) -> Optional[bytes]:
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count == 0:
            return None
        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=144)
        return pix.tobytes("png")
    except Exception as e:  # noqa: BLE001
        log.warning("pdf rasterize failed: %s", e)
        return None


def _safe_json(text: str) -> Dict[str, Any]:
    text = text.strip()
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


def parse_document(
    source: Union[bytes, str, Path],
    *,
    user_id: int = 0,
    bot: str = "unknown",
    file_id: str = "",
    file_unique_id: Optional[str] = None,
    mime_type: str = "application/pdf",
    persist: bool = True,
) -> Dict[str, Any]:
    if not GEMINI_KEY:
        return {"error": "no_gemini_key"}

    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
    else:
        raw = bytes(source)

    img_bytes: Optional[bytes] = None
    img_mime = "image/png"
    if mime_type == "application/pdf":
        img_bytes = _pdf_first_page_png(raw)
        if img_bytes is None:
            err = "pdf_rasterize_failed_install_pymupdf"
            if persist:
                save_multimodal(user_id, bot, "document", file_id, {},
                                file_unique_id=file_unique_id, mime_type=mime_type,
                                llm_provider=GEMINI_MODEL, error=err)
            return {"error": err}
    else:
        img_bytes = raw
        img_mime = mime_type if mime_type.startswith("image/") else "image/jpeg"

    try:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        payload = {
            "contents": [{
                "parts": [
                    {"text": FLOORPLAN_PROMPT},
                    {"inline_data": {"mime_type": img_mime, "data": b64}},
                ],
            }],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 900},
        }
        r = requests.post(f"{GEMINI_URL}?key={GEMINI_KEY}", json=payload, timeout=30)
        if r.status_code != 200:
            err = f"gemini_http_{r.status_code}: {r.text[:200]}"
            log.error(err)
            if persist:
                save_multimodal(user_id, bot, "document", file_id, {},
                                file_unique_id=file_unique_id, mime_type=mime_type,
                                llm_provider=GEMINI_MODEL, error=err)
            return {"error": err}
        text = (
            r.json().get("candidates", [{}])[0]
            .get("content", {}).get("parts", [{}])[0].get("text", "")
        )
        extracted = _safe_json(text)
        if persist:
            save_multimodal(user_id, bot, "document", file_id, extracted,
                            file_unique_id=file_unique_id, mime_type=mime_type,
                            llm_provider=GEMINI_MODEL)
        return extracted
    except Exception as e:  # noqa: BLE001
        log.exception("parse_document failed: %s", e)
        if persist:
            save_multimodal(user_id, bot, "document", file_id, {},
                            file_unique_id=file_unique_id, mime_type=mime_type,
                            llm_provider=GEMINI_MODEL, error=str(e)[:300])
        return {"error": str(e)[:300]}
