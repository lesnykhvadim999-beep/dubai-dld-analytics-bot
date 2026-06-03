"""Classify external signals: relevant to Dubai real estate? + score 0..1.

Primary: Gemini Flash 2.0 via shared LLM chain.
Fallback: keyword heuristic if LLM unavailable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Reuse the shared LLM chain that resale/staging-processor use.
try:
    from shared.auto_docs.llm_client import llm_call as _shared_llm_call  # type: ignore
except Exception:  # pragma: no cover
    _shared_llm_call = None  # type: ignore

_DUBAI_KEYWORDS = re.compile(
    r"\b(dubai|uae|emirates|sharjah|abu\s*dhabi|jvc|jbr|marina|"
    r"downtown|business\s*bay|dld|rera|property|real\s*estate|"
    r"off[\s\-]?plan|villa|apartment|aed|propertyfinder|bayut|"
    r"dubizzle|emaar|damac|nakheel|sobha|meraas)\b",
    re.IGNORECASE,
)


def _heuristic_score(text: str) -> float:
    if not text:
        return 0.0
    matches = len(_DUBAI_KEYWORDS.findall(text))
    if matches == 0:
        return 0.0
    return min(1.0, 0.2 + matches * 0.15)


def _llm_classify(title: str, body: str) -> dict[str, Any] | None:
    if _shared_llm_call is None:
        return None
    snippet = (body or "")[:1500]
    prompt = (
        "You are a Dubai real estate news classifier. Given a news item, "
        "decide if it is relevant for Dubai property investors / brokers.\n\n"
        f"TITLE: {title}\n"
        f"BODY: {snippet}\n\n"
        "Return STRICT JSON only, no prose:\n"
        '{"relevant": true|false, "score": 0.0..1.0, '
        '"signal_type": "news|trend|market_update|listing|other", '
        '"areas": ["..."], "summary": "1 short sentence"}'
    )
    try:
        raw = _shared_llm_call(prompt, max_tokens=300, timeout=15)
    except Exception as e:
        log.debug("relevance llm_call failed: %s", e)
        return None
    if not raw:
        return None
    raw = raw.strip()
    # strip fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
    # extract first {...}
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def classify(title: str, body: str) -> dict[str, Any]:
    """Return {relevant, score, signal_type, areas, summary} — never raises."""
    out = _llm_classify(title or "", body or "")
    if out and isinstance(out, dict):
        score = float(out.get("score") or 0.0)
        return {
            "relevant": bool(out.get("relevant", score >= 0.5)),
            "score": max(0.0, min(1.0, score)),
            "signal_type": out.get("signal_type") or "news",
            "areas": out.get("areas") or [],
            "summary": (out.get("summary") or "")[:300],
        }

    # heuristic fallback
    combined = f"{title}\n{body}"
    score = _heuristic_score(combined)
    return {
        "relevant": score >= 0.35,
        "score": score,
        "signal_type": "news",
        "areas": [],
        "summary": (title or "")[:200],
    }
