"""photo_analyzer — Gemini Flash 2.0 Vision photo analysis (free tier).

Analyzes up to 5 property photos and returns aggregated JSON with style,
condition, view quality, light and luxury indicators. Result is cached
forever per photo-set hash.

Free tier: Gemini Flash 2.0 = 15 RPM, 1500 req/day. No paid services.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger("pdf_intelligence.photo")

try:
    import google.generativeai as genai  # type: ignore
    GENAI_OK = True
except Exception:
    GENAI_OK = False
    genai = None  # type: ignore


_PROMPT = """Analyze these Dubai real estate property photos.
Return STRICT JSON only — no markdown, no commentary:
{
  "style": "modern|classic|luxury|minimal|contemporary",
  "condition": "new|like_new|renovated|used|needs_renovation",
  "view_quality": "sea|burj_khalifa|city|garden|street|none",
  "view_rating": 1-10,
  "natural_light": "excellent|good|moderate|poor",
  "space_utilization": "open|conventional|cramped",
  "luxury_indicators": ["marble", "chandelier", "private_pool", "smart_home"],
  "summary_ru": "2-sentence Russian buyer-facing description",
  "summary_en": "2-sentence English buyer-facing description"
}
"""

# emoji icon maps for PDF rendering
STYLE_EMOJI = {
    "modern": "Современный",
    "classic": "Классический",
    "luxury": "Люкс",
    "minimal": "Минимализм",
    "contemporary": "Современный (contemporary)",
}
CONDITION_EMOJI = {
    "new": "Новое",
    "like_new": "Как новое",
    "renovated": "После ремонта",
    "used": "Используемое",
    "needs_renovation": "Требует ремонта",
}
VIEW_EMOJI = {
    "sea": "Море",
    "burj_khalifa": "Бурж Халифа",
    "city": "Город",
    "garden": "Сад",
    "street": "Улица",
    "none": "Без вида",
}
LIGHT_EMOJI = {
    "excellent": "Отличное",
    "good": "Хорошее",
    "moderate": "Среднее",
    "poor": "Слабое",
}


def _api_key() -> Optional[str]:
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def _photo_hash(paths: List[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except Exception:
            h.update(p.encode())
    return h.hexdigest()[:24]


def _db_url() -> Optional[str]:
    for k in ("DATABASE_URL", "READ_MODEL_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        v = os.environ.get(k)
        if v and "postgres" in v.lower():
            return v
    return None


def _ensure_cache() -> None:
    url = _db_url()
    if not url:
        return
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pdf_photo_analysis_cache (
                        photo_hash TEXT PRIMARY KEY,
                        analysis_json JSONB NOT NULL,
                        generated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                conn.commit()
    except Exception as e:
        log.debug(f"photo cache ensure failed: {e}")


def _cache_get(h: str) -> Optional[dict]:
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT analysis_json FROM pdf_photo_analysis_cache WHERE photo_hash=%s",
                    (h,))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_put(h: str, data: dict) -> None:
    url = _db_url()
    if not url:
        return
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pdf_photo_analysis_cache (photo_hash, analysis_json)
                    VALUES (%s, %s)
                    ON CONFLICT (photo_hash) DO UPDATE
                       SET analysis_json=EXCLUDED.analysis_json,
                           generated_at=NOW()
                """, (h, json.dumps(data, ensure_ascii=False)))
                conn.commit()
    except Exception as e:
        log.debug(f"photo cache put failed: {e}")


def _fallback_analysis() -> Dict[str, Any]:
    """Returned when Gemini is unavailable — flagged with provider=fallback."""
    return {
        "style": "contemporary",
        "condition": "used",
        "view_quality": "city",
        "view_rating": 6,
        "natural_light": "good",
        "space_utilization": "conventional",
        "luxury_indicators": [],
        "summary_ru": "Фото-анализ недоступен — Gemini Vision API не сконфигурирован.",
        "summary_en": "Photo analysis unavailable — Gemini Vision API not configured.",
        "_provider": "fallback",
    }


def analyze_property_photos(photo_paths: List[str]) -> Dict[str, Any]:
    """Analyze up to 5 photos. Returns aggregated JSON.

    Args:
        photo_paths: list of local file paths to JPEG/PNG images.
    """
    if not photo_paths:
        return _fallback_analysis()

    # cache check
    h = _photo_hash(photo_paths)
    _ensure_cache()
    cached = _cache_get(h)
    if cached:
        log.info(f"photo analysis cache hit: {h}")
        return cached

    if not GENAI_OK:
        log.warning("google-generativeai not installed — using fallback")
        return _fallback_analysis()

    api_key = _api_key()
    if not api_key:
        log.warning("GEMINI_API_KEY missing — using fallback")
        return _fallback_analysis()

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash-exp")
        parts: List[Any] = [_PROMPT]
        for p in photo_paths[:5]:
            try:
                with open(p, "rb") as f:
                    img_bytes = f.read()
                # detect mime by ext
                ext = os.path.splitext(p)[1].lower()
                mime = "image/png" if ext == ".png" else "image/jpeg"
                parts.append({"mime_type": mime, "data": img_bytes})
            except Exception as e:
                log.debug(f"skip photo {p}: {e}")

        resp = model.generate_content(parts, request_options={"timeout": 30})
        text = (resp.text or "").strip()
        # tolerate ```json fences
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text.lstrip("`")
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        data["_provider"] = "gemini-2.0-flash"
        _cache_put(h, data)
        return data
    except Exception as e:
        log.warning(f"Gemini vision call failed: {e}")
        return _fallback_analysis()


def format_photo_analysis(data: Dict[str, Any], lang: str = "ru") -> str:
    """Human-readable single block of text describing the photo analysis."""
    if not data:
        return ""
    style = STYLE_EMOJI.get(data.get("style", ""), data.get("style", "—"))
    cond = CONDITION_EMOJI.get(data.get("condition", ""), data.get("condition", "—"))
    view = VIEW_EMOJI.get(data.get("view_quality", ""), data.get("view_quality", "—"))
    light = LIGHT_EMOJI.get(data.get("natural_light", ""), data.get("natural_light", "—"))
    view_rating = data.get("view_rating", "—")
    lux = data.get("luxury_indicators") or []
    summary = data.get("summary_ru") if lang == "ru" else data.get("summary_en", "")

    lines = []
    if lang == "ru":
        lines.append(f"Стиль: {style}")
        lines.append(f"Состояние: {cond}")
        lines.append(f"Вид: {view} ({view_rating}/10)")
        lines.append(f"Освещение: {light}")
        if lux:
            lines.append(f"Премиум-элементы: {', '.join(lux[:5])}")
    else:
        lines.append(f"Style: {style}")
        lines.append(f"Condition: {cond}")
        lines.append(f"View: {view} ({view_rating}/10)")
        lines.append(f"Light: {light}")
        if lux:
            lines.append(f"Luxury features: {', '.join(lux[:5])}")
    if summary:
        lines.append("")
        lines.append(summary)
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Module loaded. genai_ok =", GENAI_OK, "key_set =", bool(_api_key()))
    print(format_photo_analysis(_fallback_analysis(), lang="ru"))
