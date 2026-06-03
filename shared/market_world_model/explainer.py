"""explainer.py — natural language explanations for forecasts.

Uses local templates first (zero LLM calls). If shared.causal_engine is
available, asks it for causal drivers (Layer 11 integration).
"""
from __future__ import annotations

from typing import Any


def _trend_word(pct: float | None, ru: bool = True) -> str:
    if pct is None:
        return "стабилен" if ru else "stable"
    if pct >= 5:
        return "сильный рост" if ru else "strong growth"
    if pct >= 1:
        return "умеренный рост" if ru else "moderate growth"
    if pct >= -1:
        return "стабильность" if ru else "stable"
    if pct >= -5:
        return "умеренное снижение" if ru else "moderate decline"
    return "сильное снижение" if ru else "sharp decline"


def explain_forecast(fc: dict, lang: str = "ru") -> str:
    """Render a forecast dict (from query.forecast) as plain text."""
    if not fc.get("ok"):
        return ("Не нашёл такого района/здания: " + str(fc.get("query", "?"))) \
            if lang == "ru" else \
            ("Entity not found: " + str(fc.get("query", "?")))

    ent = fc["entity"]["name"]
    metric_label = {
        "price_per_sqft": "цена за кв. фут" if lang == "ru" else "price per sqft",
        "supply": "предложение" if lang == "ru" else "supply",
        "demand": "спрос" if lang == "ru" else "demand",
    }.get(fc["metric"], fc["metric"])
    horizon = fc["horizon_months"]
    current = fc.get("current")
    predicted = fc.get("predicted")
    pct = fc.get("pct_change")
    trend = _trend_word(pct, ru=(lang == "ru"))

    if lang == "ru":
        out = [
            f"🔮 Прогноз для {ent} (горизонт {horizon} мес)",
            f"📊 Метрика: {metric_label}",
        ]
        if current is not None:
            out.append(f"Сейчас: {current:,.0f}".replace(",", " "))
        if predicted is not None:
            out.append(f"Прогноз: {predicted:,.0f}".replace(",", " "))
        if pct is not None:
            sign = "+" if pct >= 0 else ""
            out.append(f"Изменение: {sign}{pct:.1f}% — {trend}")
        meta = fc.get("model_meta") or {}
        if meta.get("rmse") is not None:
            out.append(f"RMSE модели: {meta['rmse']:.1f}")
        out.append("\n_Источник: Layer 13 Market World Model_")
        out.append("_Прогноз не является финансовым советом. Vadim Realty — RERA BRN 65011._")
    else:
        out = [
            f"🔮 Forecast for {ent} (horizon {horizon} months)",
            f"📊 Metric: {metric_label}",
        ]
        if current is not None:
            out.append(f"Current: {current:,.0f}")
        if predicted is not None:
            out.append(f"Predicted: {predicted:,.0f}")
        if pct is not None:
            sign = "+" if pct >= 0 else ""
            out.append(f"Change: {sign}{pct:.1f}% — {trend}")
        out.append("\n_Source: Layer 13 Market World Model_")
        out.append("_Not financial advice. Vadim Realty — RERA BRN 65011._")

    # try causal engine
    try:
        from shared.causal_engine import client as causal  # type: ignore
        if hasattr(causal, "explain"):
            try:
                drivers = causal.explain(entity=ent, metric=fc["metric"])
                if drivers:
                    out.append("\n📌 Драйверы:" if lang == "ru" else "\n📌 Drivers:")
                    for d in drivers[:3]:
                        out.append(f"• {d}")
            except Exception:
                pass
    except Exception:
        pass

    return "\n".join(out)


def explain_compare(cmp: dict, lang: str = "ru") -> str:
    if not cmp.get("ok"):
        return "Сравнение не удалось" if lang == "ru" else "Comparison failed"
    a, b = cmp["a"], cmp["b"]
    if lang == "ru":
        lines = [
            f"⚖️ Сравнение: {a['entity']} vs {b['entity']}",
            f"Метрика: {cmp['metric']} (горизонт {cmp['horizon_months']} мес)",
            "",
            f"{a['entity']}: {a['current']:.0f} → {a['predicted']:.0f}  ({a['pct_change']:+.1f}%)" if a.get('predicted') else f"{a['entity']}: нет модели",
            f"{b['entity']}: {b['current']:.0f} → {b['predicted']:.0f}  ({b['pct_change']:+.1f}%)" if b.get('predicted') else f"{b['entity']}: нет модели",
        ]
        if cmp.get("winner_by_growth"):
            lines.append(f"\n🏆 Лидер по росту: {cmp['winner_by_growth']}")
        return "\n".join(lines)
    lines = [
        f"⚖️ Compare: {a['entity']} vs {b['entity']}",
        f"Metric: {cmp['metric']} (horizon {cmp['horizon_months']} mo)",
        "",
        f"{a['entity']}: {a['current']:.0f} → {a['predicted']:.0f}  ({a['pct_change']:+.1f}%)" if a.get('predicted') else f"{a['entity']}: no model",
        f"{b['entity']}: {b['current']:.0f} → {b['predicted']:.0f}  ({b['pct_change']:+.1f}%)" if b.get('predicted') else f"{b['entity']}: no model",
    ]
    if cmp.get("winner_by_growth"):
        lines.append(f"\n🏆 Growth leader: {cmp['winner_by_growth']}")
    return "\n".join(lines)


def explain_what_if(wf: dict, lang: str = "ru") -> str:
    if not wf.get("ok"):
        return "Сценарий не построен" if lang == "ru" else "Scenario failed"
    ent = wf["entity"]["name"]
    base_p = wf.get("base_predicted")
    sc_p = wf.get("scenario_predicted")
    shift = wf.get("scenario_shift_pct") or 0.0
    if lang == "ru":
        lines = [
            f"🧪 Сценарий «{wf['scenario']}» для {ent}",
            f"Метрика: {wf['metric']} ({wf['horizon_months']} мес)",
            f"База: {base_p:.0f}" if base_p else "База: —",
            f"Сценарий: {sc_p:.0f}  (сдвиг {shift:+.1f}%)" if sc_p else "Сценарий: —",
            "\n_Эластичности заложены эмпирически. Это симуляция, не прогноз._",
        ]
    else:
        lines = [
            f"🧪 Scenario '{wf['scenario']}' for {ent}",
            f"Metric: {wf['metric']} ({wf['horizon_months']} mo)",
            f"Base: {base_p:.0f}" if base_p else "Base: —",
            f"Scenario: {sc_p:.0f}  (shift {shift:+.1f}%)" if sc_p else "Scenario: —",
        ]
    return "\n".join(lines)
