"""Format a CausalChain into human-readable Telegram-friendly text."""
from __future__ import annotations

from typing import Union, Dict, Any
from .reasoner import CausalChain


_RU_HEADERS = {
    "why_roi":      "🧠 Почему такой ROI",
    "why_price":    "🧠 Почему цена именно такая",
    "why_rent":     "🧠 Почему аренда на этом уровне",
    "why_demand":   "🧠 Что движет спросом",
    "explain_trend":"🧠 Объяснение тренда",
    "generic":      "🧠 Причинный анализ",
}

_EN_HEADERS = {
    "why_roi":      "🧠 Why this ROI",
    "why_price":    "🧠 Why this price",
    "why_rent":     "🧠 Why this rent level",
    "why_demand":   "🧠 What drives demand",
    "explain_trend":"🧠 Trend explanation",
    "generic":      "🧠 Causal analysis",
}


def _conf_badge(c: float) -> str:
    if c >= 0.8: return "🟢"
    if c >= 0.6: return "🟡"
    return "🟠"


def format_chain(chain: Union[CausalChain, Dict[str, Any]], lang: str = "ru") -> str:
    if isinstance(chain, dict):
        steps = chain.get("steps", [])
        final = chain.get("final_explanation", "")
        pattern = chain.get("pattern", "generic")
    else:
        steps = chain.steps
        final = chain.final_explanation
        pattern = chain.pattern

    head_key = pattern.split(":", 1)[0]
    headers  = _RU_HEADERS if lang == "ru" else _EN_HEADERS
    title    = headers.get(head_key, headers["generic"])

    if not steps:
        empty = "Недостаточно данных для уверенного причинного объяснения." if lang == "ru" \
                else "Not enough data for a confident causal explanation."
        return f"{title}\n\n{empty}"

    lines = [title, ""]
    for i, s in enumerate(steps, 1):
        badge = _conf_badge(float(s.get("conf", 0)))
        cause = s.get("cause", "")
        effect = s.get("effect", "")
        src   = s.get("source", "")
        arrow = "→"
        lines.append(f"{i}. {badge} {cause}\n   {arrow} {effect}  _(src: {src})_")
        lines.append("")

    if final:
        verdict_label = "*Вывод:* " if lang == "ru" else "*Verdict:* "
        lines.append(verdict_label + final)

    disclaimer = (
        "\n\n_Vadim Realty · RERA BRN 65011 · оценка вероятностная, не финансовая рекомендация._"
        if lang == "ru" else
        "\n\n_Vadim Realty · RERA BRN 65011 · probabilistic estimate, not financial advice._"
    )
    return "\n".join(lines) + disclaimer
