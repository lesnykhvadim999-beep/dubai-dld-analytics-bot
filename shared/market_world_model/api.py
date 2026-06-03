"""api.py — single entry point for bots.

Example:
    from shared.market_world_model import api as market
    text = market.ask("Что будет с Marina через 6 месяцев?", lang="ru")
"""
from __future__ import annotations

import re
from typing import Any

from . import query as _q
from . import explainer as _ex


class DubaiMarketModel:
    """Stateless facade — every method opens its own short connection."""

    @staticmethod
    def forecast(entity: str | int, metric: str = "price_per_sqft",
                 horizon_months: int = 6) -> dict:
        return _q.forecast(entity, metric, horizon_months)

    @staticmethod
    def what_if(entity: str | int, scenario: str,
                metric: str = "price_per_sqft",
                horizon_months: int = 6) -> dict:
        return _q.what_if(entity, scenario, metric, horizon_months)

    @staticmethod
    def compare(a: str | int, b: str | int,
                metric: str = "price_per_sqft",
                horizon_months: int = 6) -> dict:
        return _q.compare(a, b, metric, horizon_months)

    @staticmethod
    def ask(question: str, lang: str = "ru") -> str:
        return ask(question, lang)


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def forecast(entity, metric: str = "price_per_sqft",
             horizon_months: int = 6) -> dict:
    return _q.forecast(entity, metric, horizon_months)


def what_if(entity, scenario, metric: str = "price_per_sqft",
            horizon_months: int = 6) -> dict:
    return _q.what_if(entity, scenario, metric, horizon_months)


def compare(a, b, metric: str = "price_per_sqft",
            horizon_months: int = 6) -> dict:
    return _q.compare(a, b, metric, horizon_months)


# ---------------------------------------------------------------------------
# Free-text question parser
# ---------------------------------------------------------------------------

_HORIZON_RE = re.compile(
    r"(\d{1,2})\s*(мес|месяц|month|m|год|year|y|лет)",
    re.IGNORECASE,
)

_COMPARE_RE = re.compile(
    r"(.+?)\s+(?:vs|против|или|or)\s+(.+)",
    re.IGNORECASE,
)


def _extract_horizon_months(text: str, default: int = 6) -> int:
    m = _HORIZON_RE.search(text)
    if not m:
        return default
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("год", "year", "y", "лет"):
        return n * 12
    return n


def _extract_entity(text: str) -> str | None:
    """Best-effort extraction of an area/building name from a question.

    Strategy: drop common stop phrases, return the remainder. Real resolution
    happens in resolve_entity() which does ILIKE matching.
    """
    s = text
    for stop in [
        "что будет с", "что будет", "прогноз для", "прогноз", "forecast for",
        "forecast", "через", "in", "next", "следующие", "месяц", "месяца", "месяцев",
        "month", "months", "год", "года", "лет", "year", "years",
        "?", ".", "!", "ru", "en",
    ]:
        s = re.sub(rf"\b{re.escape(stop)}\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\d+", " ", s)
    s = re.sub(r"[?!.,;:]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.;:-")
    return s or None


def ask(question: str, lang: str = "ru") -> str:
    """High-level free-text Q&A. Returns plain text response."""
    q = question.strip()
    horizon = _extract_horizon_months(q)

    # compare?
    m = _COMPARE_RE.search(q)
    if m:
        a = _extract_entity(m.group(1)) or m.group(1).strip()
        b = _extract_entity(m.group(2)) or m.group(2).strip()
        cmp = _q.compare(a, b, horizon_months=horizon)
        return _ex.explain_compare(cmp, lang=lang)

    # what-if?
    low = q.lower()
    if any(k in low for k in ("если ", "если, ", "what if", "scenario", "сценарий")):
        ent = _extract_entity(q) or q
        # crude scenario extraction: look for "+/-N%" pattern
        sc_match = re.search(
            r"(supply|demand|rates?|спрос|предложение|ставк[аи])\s*([+\-]?\d+)\s*%",
            q, re.IGNORECASE,
        )
        scenario = sc_match.group(0) if sc_match else "demand +0%"
        # translate ru -> en keywords for parser
        scenario = (scenario.lower()
                    .replace("спрос", "demand")
                    .replace("предложение", "supply")
                    .replace("ставка", "rates")
                    .replace("ставки", "rates"))
        wf = _q.what_if(ent, scenario, horizon_months=horizon)
        return _ex.explain_what_if(wf, lang=lang)

    # default: forecast
    ent = _extract_entity(q) or q
    fc = _q.forecast(ent, horizon_months=horizon)
    if not fc.get("ok"):
        fc["query"] = ent
    return _ex.explain_forecast(fc, lang=lang)
