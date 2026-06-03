"""investment_thesis — AI Investment Thesis (Cerebras → Groq → fallback).

Produces a 4-6 sentence Russian thesis suitable for placement in PDF after the
KPI block. If LLM chain fails, falls back to a deterministic rule-based
narrative built from the same numbers.

Cache: pdf_thesis_cache (building+area+deal_type+date, 24h TTL).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import date
from typing import Any, Dict, Optional

log = logging.getLogger("pdf_intelligence.thesis")

# llm_chain lives next to bots, not in shared/; tolerate import errors
_LLM_CALL = None
try:
    from llm_chain import llm_call as _LLM_CALL  # type: ignore
except Exception:
    try:  # last resort — add resale-bot dir
        _resale = r"C:\Projects\resale-bot"
        if _resale not in sys.path and os.path.isdir(_resale):
            sys.path.insert(0, _resale)
            from llm_chain import llm_call as _LLM_CALL  # type: ignore
    except Exception:
        _LLM_CALL = None


# ── prompt ────────────────────────────────────────────────────────────────
_PROMPT_TMPL = """Ты — институциональный аналитик рынка недвижимости Дубая. Пиши строго на русском языке. Выводи ТОЛЬКО финальный текст, без рассуждений, без английского, без markdown, без списков.

ОБЪЕКТ: {building}, район {area}, тип сделки {deal_type}

ДАННЫЕ DLD за 12 месяцев:
- Сделок: {deals_count}
- Медианная цена: {median_price:,.0f} AED
- Цена за sqft: {median_per_sqft:,.0f} AED
- Тренд цен YoY: {trend_pct:+.1f}%
- Ликвидность (средн. дней между сделками): {days_to_sell}

РЫНОЧНЫЙ КОНТЕКСТ:
- Area ROI: {area_roi:.1f}%
- Dubai median ROI: {dubai_roi:.1f}%
- Ликвидность района vs Dubai: {liquidity_rank}/10

ЗАДАЧА: Напиши инвестиционный тезис в 4-6 предложений на русском. Структура:
(1) Сигнал — "Сигнал: КУПИТЬ" или "Сигнал: ДЕРЖАТЬ" или "Сигнал: ИЗБЕГАТЬ" с уверенностью (высокая/средняя/низкая).
(2) Ключевая причина (ROI / ликвидность / позиция по цене относительно медианы района).
(3) Главный риск (handover / oversupply / illiquidity).
(4) Рекомендуемый горизонт удержания в месяцах.

Тон: институциональный инвестор. Используй реальные числа из данных.
Формат: один абзац, без markdown, без буллетов, без заголовков, без английского, без "мы должны", без размышлений.

ОТВЕТ (начни прямо со слова "Сигнал:"):"""


def _safe(v: Any, default: Any = 0) -> Any:
    if v is None:
        return default
    try:
        return float(v) if isinstance(v, (int, float, str)) and str(v).replace(".", "").replace("-", "").isdigit() else v
    except Exception:
        return default


def _clean_llm_output(raw: str) -> str:
    """Strip reasoning-style preamble; isolate the actual Russian thesis.

    Some open-weight models (gpt-oss-120b on Cerebras) leak chain-of-thought
    like "We need to write..." or "Let's craft 5 sentences." before the final
    answer. Strategy:
      1. Find the LAST occurrence of "Сигнал:" / "сигнал:" — answer starts there.
      2. Otherwise, take the last 2-3 paragraphs that look Russian.
    """
    if not raw:
        return ""
    s = raw.strip()
    # try to locate the answer anchor
    for anchor in ("Сигнал:", "сигнал:", "СИГНАЛ:"):
        idx = s.rfind(anchor)
        if idx >= 0:
            s = s[idx:]
            break
    # strip trailing reasoning if present
    for cut in ("\n\nWe need", "\n\nLet's", "\n\nNote:"):
        if cut in s:
            s = s.split(cut)[0]
    # remove leftover markdown fences
    s = s.replace("```", "").strip()
    return s


def _looks_russian(s: str) -> bool:
    """Heuristic: at least 30% of letters are Cyrillic."""
    if not s:
        return False
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return cyr / len(letters) >= 0.30


def _normalize(building: str, area: str, deal_type: str,
               dld_stats: Dict[str, Any],
               market_context: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce all numeric inputs to safe values for the prompt."""
    return {
        "building": building or "—",
        "area": area or "—",
        "deal_type": deal_type or "secondary",
        "deals_count": int(dld_stats.get("deals_count") or 0),
        "median_price": float(dld_stats.get("median_price") or 0),
        "median_per_sqft": float(dld_stats.get("median_per_sqft") or 0),
        "trend_pct": float(dld_stats.get("trend_pct") or 0),
        "days_to_sell": int(dld_stats.get("days_to_sell") or 0),
        "area_roi": float(market_context.get("area_roi") or 0),
        "dubai_roi": float(market_context.get("dubai_roi") or 7.0),
        "liquidity_rank": int(market_context.get("liquidity_rank") or 5),
    }


# ── fallback narrative (deterministic) ───────────────────────────────────
def fallback_thesis(building: str, area: str, deal_type: str,
                    dld_stats: Dict[str, Any],
                    market_context: Dict[str, Any]) -> str:
    n = _normalize(building, area, deal_type, dld_stats, market_context)

    # signal heuristic
    score = 0
    if n["trend_pct"] > 8: score += 2
    elif n["trend_pct"] > 3: score += 1
    elif n["trend_pct"] < -3: score -= 2
    if n["area_roi"] > n["dubai_roi"] + 1: score += 1
    elif n["area_roi"] < n["dubai_roi"] - 1: score -= 1
    if n["liquidity_rank"] >= 7: score += 1
    elif n["liquidity_rank"] <= 3: score -= 1

    if score >= 3:
        signal, conf = "КУПИТЬ", "высокая"
    elif score >= 1:
        signal, conf = "КУПИТЬ", "средняя"
    elif score <= -2:
        signal, conf = "ИЗБЕГАТЬ", "средняя"
    else:
        signal, conf = "ДЕРЖАТЬ", "средняя"

    # risk heuristic
    if n["deal_type"].lower() in ("offplan", "off-plan", "off_plan"):
        risk = "риск сроков handover и волатильность до сдачи"
    elif n["liquidity_rank"] <= 3:
        risk = "пониженная ликвидность — продажа может занять 6+ месяцев"
    elif n["trend_pct"] < 0:
        risk = "нисходящий тренд цен — возможна коррекция ещё 5-10%"
    else:
        risk = "избыток предложения в сегменте может ограничить рост"

    # hold horizon
    if signal == "КУПИТЬ":
        hold = "24-36 мес"
    elif signal == "ДЕРЖАТЬ":
        hold = "12-18 мес до пересмотра"
    else:
        hold = "не входить"

    return (
        f"Сигнал: {signal} (уверенность: {conf}). "
        f"{n['building']} в {n['area']} показывает {n['deals_count']} сделок за 12 мес "
        f"при медиане {n['median_price']:,.0f} AED и динамике {n['trend_pct']:+.1f}% YoY; "
        f"area ROI {n['area_roi']:.1f}% против медианы Dubai {n['dubai_roi']:.1f}% — "
        f"{'выше' if n['area_roi'] > n['dubai_roi'] else 'ниже'} рынка. "
        f"Ключевой риск: {risk}. "
        f"Рекомендуемый горизонт удержания — {hold}."
    )


# ── cache (24h TTL) ───────────────────────────────────────────────────────
def _cache_key(building: str, area: str, deal_type: str) -> str:
    raw = f"{building.lower().strip()}|{area.lower().strip()}|{deal_type.lower().strip()}|{date.today().isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _db_url() -> Optional[str]:
    for k in ("DATABASE_URL", "READ_MODEL_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        v = os.environ.get(k)
        if v and "postgres" in v.lower():
            return v
    return None


def _ensure_cache_table() -> None:
    url = _db_url()
    if not url:
        return
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pdf_thesis_cache (
                        cache_key TEXT PRIMARY KEY,
                        thesis_text TEXT NOT NULL,
                        provider TEXT,
                        generated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                conn.commit()
    except Exception as e:
        log.debug(f"pdf_thesis_cache ensure failed: {e}")


def _cache_get(key: str) -> Optional[str]:
    url = _db_url()
    if not url:
        return None
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT thesis_text FROM pdf_thesis_cache "
                    "WHERE cache_key=%s AND generated_at > NOW() - INTERVAL '24 hours'",
                    (key,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:
        return None


def _cache_put(key: str, text: str, provider: str = "llm") -> None:
    url = _db_url()
    if not url:
        return
    try:
        import psycopg  # type: ignore
        with psycopg.connect(url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pdf_thesis_cache (cache_key, thesis_text, provider)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE
                       SET thesis_text=EXCLUDED.thesis_text,
                           provider=EXCLUDED.provider,
                           generated_at=NOW()
                """, (key, text, provider))
                conn.commit()
    except Exception as e:
        log.debug(f"pdf_thesis_cache put failed: {e}")


# ── public API ────────────────────────────────────────────────────────────
def generate_thesis(building: str,
                    area: str,
                    deal_type: str,
                    dld_stats: Dict[str, Any],
                    market_context: Dict[str, Any]) -> str:
    """Generate Russian investment thesis. Always returns a string (LLM or fallback).

    Args:
        building: Building name (or "—" for area-level reports).
        area: Area name.
        deal_type: 'sale' / 'rent' / 'offplan' / 'secondary'.
        dld_stats: dict with deals_count, median_price, median_per_sqft, trend_pct, days_to_sell.
        market_context: dict with area_roi, dubai_roi, liquidity_rank.

    Returns:
        Russian thesis text (single paragraph).
    """
    key = _cache_key(building, area, deal_type)
    _ensure_cache_table()
    cached = _cache_get(key)
    if cached:
        log.info(f"thesis cache hit: {key}")
        return cached

    n = _normalize(building, area, deal_type, dld_stats, market_context)

    if _LLM_CALL is None:
        text = fallback_thesis(building, area, deal_type, dld_stats, market_context)
        _cache_put(key, text, provider="fallback")
        return text

    prompt = _PROMPT_TMPL.format(**n)
    try:
        out = _LLM_CALL(prompt, max_tokens=350, timeout=8)
        if out and isinstance(out, str):
            cleaned = _clean_llm_output(out)
            if cleaned and len(cleaned) > 60 and _looks_russian(cleaned):
                cleaned = (cleaned
                           .replace("First Place Realtor L.L.C.", "")
                           .replace("First Place Realty", "")
                           .replace("First Place", ""))
                _cache_put(key, cleaned, provider="llm")
                return cleaned
            log.info("thesis LLM output rejected (not russian / too short) — fallback")
    except Exception as e:
        log.warning(f"thesis LLM call failed: {e}")

    # fallback
    text = fallback_thesis(building, area, deal_type, dld_stats, market_context)
    _cache_put(key, text, provider="fallback")
    return text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    txt = generate_thesis(
        building="Marina Crown",
        area="Dubai Marina",
        deal_type="secondary",
        dld_stats={
            "deals_count": 42, "median_price": 2_400_000,
            "median_per_sqft": 1850, "trend_pct": 7.5,
            "days_to_sell": 65,
        },
        market_context={
            "area_roi": 7.8, "dubai_roi": 6.5, "liquidity_rank": 8,
        },
    )
    print(txt)
