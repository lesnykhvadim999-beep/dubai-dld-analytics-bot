"""
llm_pattern_proposer — Cerebras (→ Groq → Gemini) предлагает regex/правило по cluster.

Free chain order: Cerebras → Groq → Gemini (см. CLAUDE.md hard-rule no_paid_services).

v2 (2026-06):
- Жёсткий field-aware prompt с anti-patterns
- Обязательные test_cases (positive + negative)
- Named capture group (?P<value>...)
- confidence + anti_patterns обязательны
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from typing import Sequence

import requests

log = logging.getLogger(__name__)


# ────────────── Field detection ──────────────
def _guess_field(texts: Sequence[str]) -> str:
    """Эвристика: какое поле скорее всего пропущено в кластере.

    Возвращает один из: price, area_sqft, bedrooms, building, deal_type,
    property_type, handover, unknown.
    """
    joined = " ".join(texts[:5]).lower()
    score = Counter()
    if re.search(r"\baed\b|price|د\.إ|aed\s*\d|\d[\d,]{5,}", joined):
        score["price"] += 2
    if re.search(r"sq\.?\s*ft|sqft|sq\s*m|m²", joined):
        score["area_sqft"] += 2
    if re.search(r"\bbr\b|bed(s|room)|studio|\b[1-6]\s*br\b", joined):
        score["bedrooms"] += 1
    if re.search(r"hand[\s-]?over|q[1-4]\s*20\d{2}|completion", joined):
        score["handover"] += 1
    if re.search(r"\bsale\b|\bbuy\b|\brent\b|for sale|for rent", joined):
        score["deal_type"] += 1
    if not score:
        return "unknown"
    return score.most_common(1)[0][0]


# ────────────── Field-aware anti-patterns ──────────────
ANTI_PATTERNS_BY_FIELD = {
    "price": [
        "phone numbers (+971-..., 050-..., 04-...)",
        "areas in sq.ft / sq.m / m² (number followed by 'sqft', 'sq.ft', 'sq ft', 'sqm')",
        "years 2020-2030 (Q1 2026, handover dates)",
        "rental amounts (followed by 'per year', '/year', 'yearly', 'annual', 'p.a.')",
        "building/floor numbers (e.g. 'floor 25', 'unit 1402')",
        "BRN/RERA numbers (5-7 digits, often after 'BRN' or 'RERA')",
    ],
    "area_sqft": [
        "prices (with AED/د.إ context)",
        "phone numbers",
        "years",
        "BRN/RERA numbers",
        "bedrooms count (1-7)",
    ],
    "bedrooms": [
        "bathroom counts",
        "floor numbers",
        "parking spots",
        "years (2020-2030)",
    ],
    "handover": [
        "prices that include year-like numbers",
        "phone numbers with year-like segments",
    ],
    "deal_type": [
        "feature words 'sale price', 'rental income' (these are price/rent context, not deal_type)",
    ],
    "building": [
        "developer names (Emaar, Damac, Nakheel — those are developers)",
        "area/community names (Downtown, Marina — those are communities)",
    ],
    "property_type": [
        "deal_type words (sale, rent)",
        "bedrooms (1BR, 2BR)",
    ],
    "unknown": [
        "phone numbers",
        "years 2020-2030",
        "BRN/RERA numbers",
    ],
}

FIELD_VALUE_HINTS = {
    "price": "accept numbers in range 100,000 to 100,000,000 (AED). MUST be anchored to AED/aed/د.إ/USD/$ OR preceded by 'Price:'/'selling price:'/'asking:'",
    "area_sqft": "accept numbers in range 200 to 50,000. MUST be anchored to 'sqft'/'sq.ft'/'sq ft' suffix (NOT sqm without conversion)",
    "bedrooms": "accept integers 0-9 OR 'studio'. MUST be anchored to 'BR'/'bed'/'bedroom' OR preceded by 'beds:'",
    "handover": "accept Q1-Q4 + year (2024-2030) OR YYYY-MM OR month name + year. MUST be anchored to 'handover'/'completion'/'ready by'",
    "deal_type": "accept 'sale'|'rent'|'buy'|'lease'. MUST be standalone or anchored to 'for'/'type:'",
    "building": "accept proper-noun building names. MUST be anchored to 'building:'/'tower:'/'project:' or 'at '",
    "property_type": "accept 'apartment'|'villa'|'townhouse'|'penthouse'|'studio'|'office'|'plot'. MUST be standalone match",
    "unknown": "be very conservative; require explicit context anchors",
}


PROMPT_TMPL = """You are extracting a SPECIFIC FIELD from Dubai real estate listings.

FIELD: {field}

REQUIREMENTS for the regex:
1. MUST anchor to context (the value MUST be preceded or followed by an unambiguous keyword).
2. MUST use a named capture group: (?P<value>...)
3. MUST exclude the following confounders (DO NOT match these):
{anti_patterns_block}
4. Field-specific constraint: {value_hint}
5. Use Python `re` syntax (no PCRE-only features). Pattern will be compiled with re.IGNORECASE.
6. Mentally test against the samples below AND against the negative examples in your test_cases before finalising.
7. Prefer a slightly NARROWER pattern that misses a few rather than a wide one that over-matches.

OUTPUT STRICT JSON ONLY (no markdown, no commentary, no triple backticks):
{{
  "field": "{field}",
  "symptom": "1-sentence description of what current parser misses",
  "pattern": "Python regex with (?P<value>...) named group",
  "match_group": "value",
  "extractor_hint": "1-2 sentences: how to post-process the captured value (strip commas, int(), etc.)",
  "test_cases": [
    {{"input": "POSITIVE EXAMPLE from a real listing", "expected_value": "what (?P<value>) should capture"}},
    {{"input": "ANOTHER POSITIVE EXAMPLE", "expected_value": "..."}},
    {{"input": "NEGATIVE EXAMPLE — a confounder (phone/area/year) that MUST NOT match", "expected_value": null}},
    {{"input": "ANOTHER NEGATIVE EXAMPLE", "expected_value": null}}
  ],
  "anti_patterns": ["short tokens that, if present in context window, indicate a false-positive (e.g. 'sq.ft', '/year', '+971', 'BRN')"],
  "confidence": 0.0
}}

Provide AT LEAST 2 positive and 2 negative test_cases. Negative cases MUST exercise the anti_patterns above.

SAMPLES (from the cluster — all share the same parsing failure):
{samples}
"""


def _build_prompt(texts: Sequence[str], field: str) -> str:
    snippets = []
    for i, t in enumerate(texts[:10], 1):
        snippets.append(f"--- Sample {i} ---\n{t[:600]}")
    anti = ANTI_PATTERNS_BY_FIELD.get(field, ANTI_PATTERNS_BY_FIELD["unknown"])
    anti_block = "\n".join(f"   - {a}" for a in anti)
    value_hint = FIELD_VALUE_HINTS.get(field, FIELD_VALUE_HINTS["unknown"])
    return PROMPT_TMPL.format(
        field=field,
        anti_patterns_block=anti_block,
        value_hint=value_hint,
        samples="\n\n".join(snippets),
    )


# ────────────── Providers ──────────────
def _cerebras(prompt: str) -> str | None:
    key = os.environ.get("CEREBRAS_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.cerebras.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1200,
            },
            timeout=45,
        )
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        return msg.get("content") or msg.get("reasoning_content")
    except Exception as exc:
        log.warning("Cerebras failed: %s", exc)
        return None


def _groq(prompt: str) -> str | None:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 1200,
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("Groq failed: %s", exc)
        return None


def _gemini(prompt: str) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1200},
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        log.warning("Gemini failed: %s", exc)
        return None


def _strip_json(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        return s[a : b + 1]
    return s


def _normalize_rule(parsed: dict, field: str) -> dict:
    """Back-compat: rule может прийти с ключом 'regex' вместо 'pattern'."""
    if "pattern" not in parsed and "regex" in parsed:
        parsed["pattern"] = parsed["regex"]
    parsed.setdefault("field", field)
    parsed.setdefault("match_group", "value")
    parsed.setdefault("test_cases", [])
    parsed.setdefault("anti_patterns", [])
    parsed.setdefault("confidence", 0.0)
    return parsed


def propose_pattern(texts: Sequence[str], field: str | None = None) -> dict | None:
    """Возвращает dict с rule или None при тотальном фейле всех провайдеров.

    Если field не передан — пытаемся угадать по содержимому.
    """
    if not texts:
        return None
    if not field:
        field = _guess_field(texts)
    prompt = _build_prompt(texts, field)
    for provider in (_cerebras, _groq, _gemini):
        raw = provider(prompt)
        if not raw:
            continue
        try:
            parsed = json.loads(_strip_json(raw))
            if not isinstance(parsed, dict):
                continue
            parsed = _normalize_rule(parsed, field)
            if not parsed.get("pattern"):
                continue
            parsed["_provider"] = provider.__name__.lstrip("_")
            return parsed
        except Exception as exc:
            log.warning("Failed to parse %s output: %s", provider.__name__, exc)
            continue
    return None
