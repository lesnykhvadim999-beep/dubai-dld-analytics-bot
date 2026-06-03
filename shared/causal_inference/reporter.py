"""reporter.py — natural-language formatting of causal effects.

Two layers:
    - format_effect_ru(): pure-template fallback (no LLM required), used
      everywhere there is no internet or no Cerebras key. Safe inside hot
      paths (<500 ms requirement, see feedback_bots_must_be_fast.md).
    - llm_explain(): Cerebras → Groq → Gemini chain, used only by
      cron/refresh code to populate the `natural_language` column once.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests

log = logging.getLogger(__name__)


# ───────────────────────── fast RU template ─────────────────────────
_STUDY_TEMPLATES_RU = {
    "Effect_of_metro_extension_on_prices": (
        "Открытие станции метро повышает цену района в среднем на "
        "{eff_pct:+.1f}% (95% CI: {lo_pct:+.1f}…{hi_pct:+.1f}%, "
        "n={n})."
    ),
    "Effect_of_handover_wave_on_rental_yield": (
        "Волна сдачи новостроек меняет арендную доходность на "
        "{eff_abs:+.2f} п.п. (95% CI: {lo_abs:+.2f}…{hi_abs:+.2f} п.п., "
        "n={n})."
    ),
    "Effect_of_developer_brand_premium": (
        "Премиум-застройщик (Emaar/Damac/Sobha/Meraas/Nakheel) добавляет "
        "{eff_aed:+,.0f} AED/sqft к цене (95% CI: "
        "{lo_aed:+,.0f}…{hi_aed:+,.0f}, n={n})."
    ),
    "Effect_of_view_premium": (
        "Морской/мариновый вид прибавляет {eff_aed:+,.0f} AED/sqft "
        "(95% CI: {lo_aed:+,.0f}…{hi_aed:+,.0f}, n={n})."
    ),
    "Effect_of_floor_height_on_price": (
        "Каждый дополнительный этаж даёт {eff_aed:+,.1f} AED/sqft "
        "(95% CI: {lo_aed:+,.1f}…{hi_aed:+,.1f}, n={n})."
    ),
}


def _pct_of_baseline(value: float, baseline: float) -> float:
    if not baseline:
        return 0.0
    return 100.0 * value / baseline


def format_effect_ru(study_row: Dict[str, Any], baseline: Optional[float] = None) -> str:
    """Render a saved study row into a single Russian sentence."""
    name = study_row.get("study_name", "")
    ate = float(study_row.get("ate") or 0.0)
    lo = float(study_row.get("ate_lower_ci") or ate)
    hi = float(study_row.get("ate_upper_ci") or ate)
    n = int(study_row.get("sample_size") or 0)
    tpl = _STUDY_TEMPLATES_RU.get(name)

    ctx = {"eff_aed": ate, "lo_aed": lo, "hi_aed": hi,
           "eff_abs": ate, "lo_abs": lo, "hi_abs": hi,
           "n": n}

    if baseline:
        ctx["eff_pct"] = _pct_of_baseline(ate, baseline)
        ctx["lo_pct"] = _pct_of_baseline(lo, baseline)
        ctx["hi_pct"] = _pct_of_baseline(hi, baseline)
    else:
        # For % studies (e.g. metro→price) we treat the ATE as already
        # a fraction of price; multiply by 100.
        ctx["eff_pct"] = ate * 100
        ctx["lo_pct"] = lo * 100
        ctx["hi_pct"] = hi * 100

    if tpl is None:
        unit = study_row.get("effect_unit") or ""
        return (
            f"Эффект {study_row.get('treatment_var')} → "
            f"{study_row.get('outcome_var')}: {ate:+.3f} {unit} "
            f"(95% CI: {lo:+.3f}…{hi:+.3f}, n={n})."
        )
    return tpl.format(**ctx)


# ───────────────────────── slow LLM explain ─────────────────────────
def _cerebras(prompt: str) -> Optional[str]:
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
                "temperature": 0.2,
                "max_tokens": 350,
            },
            timeout=45,
        )
        r.raise_for_status()
        m = (r.json().get("choices") or [{}])[0].get("message") or {}
        return m.get("content") or m.get("reasoning_content")
    except Exception as exc:
        log.warning("Cerebras explain failed: %s", exc)
        return None


def _groq(prompt: str) -> Optional[str]:
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
                "temperature": 0.2,
                "max_tokens": 350,
            },
            timeout=45,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("Groq explain failed: %s", exc)
        return None


def llm_explain(study_row: Dict[str, Any]) -> Optional[str]:
    """Try Cerebras → Groq; fall back to None (caller uses template)."""
    prompt = (
        "Ты эксперт по рынку недвижимости Дубая. Сформулируй коротко "
        "(2–3 предложения, по-русски) причинно-следственный вывод по "
        "следующему статистическому исследованию. Не упоминай DoWhy / "
        "EconML / математику — только смысл для инвестора.\n\n"
        f"Название: {study_row.get('study_name')}\n"
        f"Treatment: {study_row.get('treatment_var')}\n"
        f"Outcome:   {study_row.get('outcome_var')}\n"
        f"ATE:       {study_row.get('ate'):.4f} {study_row.get('effect_unit') or ''}\n"
        f"95% CI:    [{study_row.get('ate_lower_ci'):.4f}; "
        f"{study_row.get('ate_upper_ci'):.4f}]\n"
        f"Sample:    {study_row.get('sample_size')}\n"
        f"Метод:     {study_row.get('method')}\n"
        f"Refutation: {study_row.get('refutation_passed')}\n"
    )
    for provider in (_cerebras, _groq):
        out = provider(prompt)
        if out:
            return out.strip()
    return None
