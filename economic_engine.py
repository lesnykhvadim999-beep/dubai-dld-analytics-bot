# economic_engine.py
# Premium DLD economic conclusion engine.
# Standalone module: receives already-calculated DLD metrics and returns a full HTML report.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
import math


def _num(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _int(x: Any, default: int = 0) -> int:
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except Exception:
        return default


def money(x: Any) -> str:
    v = _num(x)
    if v is None or v <= 0:
        return "нет данных"
    return f"{v:,.0f}".replace(",", " ") + " AED"


def pct(x: Any) -> str:
    v = _num(x)
    if v is None:
        return "нет данных"
    return f"{v:.1f}%"


def fmt_int(x: Any) -> str:
    return f"{_int(x):,}".replace(",", " ")


def _format_ru(fmt: Any) -> str:
    p = str(fmt or "").lower()
    if any(s in p for s in ["town", "таун"]):
        return "таунхаус"
    if any(s in p for s in ["villa", "вилл"]):
        return "вилла"
    if any(s in p for s in ["land", "plot", "зем"]):
        return "земля / plot"
    if any(s in p for s in ["office", "shop", "commercial", "коммер"]):
        return "коммерческая недвижимость"
    return "апартаменты"


def _liquidity_label(deals: int) -> Tuple[str, float, str]:
    if deals >= 5000:
        return "очень высокая", 0.080, "низкий"
    if deals >= 1500:
        return "высокая", 0.070, "низкий / умеренный"
    if deals >= 500:
        return "средняя", 0.055, "умеренный"
    if deals >= 100:
        return "ограниченная", 0.040, "повышенный"
    return "слабая", 0.025, "высокий из-за маленькой выборки"


def _format_strategy(fmt: str) -> str:
    f = _format_ru(fmt)
    if f == "таунхаус":
        return (
            "Таунхаус — промежуточный формат между апартаментом и виллой. Его сила — семейный спрос, "
            "более понятный lifestyle-продукт и потенциально более высокая защита от конкуренции внутри одного здания. "
            "Минус — аудитория уже, чем у апартаментов, поэтому критичны community, планировка, сервисные платежи и реальная ликвидность района."
        )
    if f == "вилла":
        return (
            "Вилла — капиталоёмкий формат с более длинным горизонтом. Она выигрывает при дефиците семейного продукта, "
            "но требует большего бюджета, тщательной проверки community, участка, состояния и будущего спроса. "
            "Это не самый быстрый актив для выхода, зато при правильном входе может дать сильный прирост капитала."
        )
    if f == "земля / plot":
        return (
            "Земля / plot оценивается иначе: основной доход строится не на текущей аренде, а на потенциале застройки, "
            "назначении участка, инфраструктуре и будущем спросе. Здесь обязательны отдельная юридическая и градостроительная проверки."
        )
    return (
        "Апартаменты — наиболее массовый и ликвидный формат. Их проще сравнивать по DLD, проще сдавать, проще перепродавать, "
        "и у них самая широкая база покупателей. Главный риск — высокая конкуренция в одном здании или районе, поэтому важна цена входа ниже или около DLD-средней."
    )


def _score(row: Dict[str, Any]) -> float:
    explicit = _num(row.get("score"))
    if explicit is not None:
        return max(0.0, min(100.0, explicit))
    deals = _int(row.get("deals"))
    avg_price = _num(row.get("avg_price"), 0) or 0
    y = _num(row.get("yield_pct"), 0) or 0
    liq = min(40, math.log10(max(deals, 1)) * 10)
    affordability = 30 if avg_price <= 1500000 else 20 if avg_price <= 3000000 else 12
    yield_score = min(30, y * 4)
    return max(0.0, min(100.0, round(liq + affordability + yield_score, 1)))


def rank_formats(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared = []
    for r in rows or []:
        d = dict(r)
        if _int(d.get("deals")) <= 0:
            continue
        d["format_ru"] = _format_ru(d.get("format") or d.get("property") or d.get("property_sub_type_en"))
        d["score_calc"] = _score(d)
        prepared.append(d)
    prepared.sort(key=lambda x: x.get("score_calc", 0), reverse=True)
    return prepared


def build_economic_report(
    *,
    selected: Optional[Dict[str, Any]] = None,
    comparisons: Optional[List[Dict[str, Any]]] = None,
    goal: Optional[str] = None,
    budget: Optional[str] = None,
    period: Optional[str] = None,
    area: Optional[str] = None,
    deal_type: Optional[str] = None,
    language: str = "ru",
) -> str:
    """Return a full premium economic conclusion in Telegram HTML.

    selected: main winning scenario row.
    comparisons: apartment/villa/townhouse rows, already calculated by main.py.
    """
    selected = dict(selected or {})
    comparisons = rank_formats(comparisons or [])

    if not comparisons and selected:
        comparisons = rank_formats([selected])

    best = comparisons[0] if comparisons else selected
    chosen = selected or best or {}

    fmt = _format_ru(chosen.get("format") or chosen.get("property") or best.get("format") if best else None)
    location = area or chosen.get("area") or chosen.get("location") or "Dubai"
    deals = _int(chosen.get("deals") or (best or {}).get("deals"))
    avg_price = _num(chosen.get("avg_price") or (best or {}).get("avg_price"))
    avg_meter = _num(chosen.get("avg_meter") or (best or {}).get("avg_meter"))
    min_price = _num(chosen.get("min_price") or (best or {}).get("min_price"))
    max_price = _num(chosen.get("max_price") or (best or {}).get("max_price"))
    avg_rent = _num(chosen.get("avg_rent") or chosen.get("annual_rent") or (best or {}).get("avg_rent"))
    yield_pct = _num(chosen.get("yield_pct") or (best or {}).get("yield_pct"))
    liquidity, growth_rate, exit_risk = _liquidity_label(deals)

    entry_low = min_price if min_price and min_price > 0 else (avg_price * 0.90 if avg_price else None)
    entry_target = avg_price * 0.95 if avg_price else None
    resale_1 = avg_price * ((1 + growth_rate) ** 1) if avg_price else None
    resale_3 = avg_price * ((1 + growth_rate) ** 3) if avg_price else None
    resale_5 = avg_price * ((1 + growth_rate) ** 5) if avg_price else None

    title_goal = goal or "сбалансированный инвестиционный сценарий"
    title_budget = budget or "бюджет не указан"
    title_period = period or "период не указан"

    text = (
        "\n\n🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"<b>Профиль запроса:</b> {title_goal}. Бюджет: <b>{title_budget}</b>. Горизонт анализа: <b>{title_period}</b>.\n"
        f"<b>Рынок:</b> {location}. Предварительно выбранный формат: <b>{fmt}</b>.\n\n"
        "<b>1) Рыночная база DLD</b>\n"
        f"В выборке учтено <b>{fmt_int(deals)}</b> сделок. Средний ориентир цены — <b>{money(avg_price)}</b>, "
        f"средняя цена за м² — <b>{money(avg_meter)}</b>. "
        f"Рабочий диапазон рынка: от <b>{money(min_price)}</b> до <b>{money(max_price)}</b>.\n\n"
        "<b>2) Ликвидность и риск</b>\n"
        f"Ликвидность по DLD: <b>{liquidity}</b>. Риск выхода: <b>{exit_risk}</b>. "
        "Чем выше число сделок, тем легче проверить рыночность цены, быстрее выйти из позиции и меньше риск зависнуть с активом.\n\n"
        "<b>3) Логика выбранного формата</b>\n"
        f"{_format_strategy(fmt)}\n\n"
    )

    if comparisons:
        text += "<b>4) Сравнение форматов: апартаменты / виллы / таунхаусы</b>\n"
        best_score = _score(comparisons[0]) if comparisons else None
        for i, r in enumerate(comparisons, 1):
            score = _score(r)
            delta = ""
            if best_score is not None and i > 1:
                delta = f"; отставание от лидера: {max(0, best_score - score):.1f} пункта"
            text += (
                f"{i}. <b>{r.get('format_ru')}</b>: "
                f"{fmt_int(r.get('deals'))} сделок, средняя цена <b>{money(r.get('avg_price'))}</b>, "
                f"цена за м² <b>{money(r.get('avg_meter'))}</b>, "
                f"доходность <b>{pct(r.get('yield_pct'))}</b>, индекс <b>{score:.1f}/100</b>{delta}.\n"
            )
        winner = comparisons[0]
        text += (
            f"\n<b>Вывод по сравнению:</b> на текущих фильтрах сильнее выглядит <b>{winner.get('format_ru')}</b>. "
            "Причина — лучший баланс ликвидности, цены входа, потенциала аренды и вероятности перепродажи. "
            "Если один из форматов не попал в сравнение, значит по нему не было устойчивой выборки в выбранном бюджете/периоде, и его нельзя честно рекомендовать без расширения фильтра.\n\n"
        )
    else:
        text += (
            "<b>4) Сравнение форматов</b>\n"
            "По текущим фильтрам недостаточно данных для корректного сравнения апартаментов, вилл и таунхаусов. "
            "Рекомендация: расширить период, район или бюджетный коридор.\n\n"
        )

    text += (
        "<b>5) Цена входа</b>\n"
        f"Комфортная зона входа: <b>{money(entry_low)}</b> — <b>{money(entry_target)}</b>. "
        "Покупка выше DLD-средней оправдана только при объективной премии: вид, этаж, планировка, редкость, состояние, готовая аренда или сильная мотивация рынка.\n\n"
    )

    if avg_rent or yield_pct:
        text += (
            "<b>6) Арендная модель</b>\n"
            f"Среднегодовая аренда: <b>{money(avg_rent)}</b>. Ориентир доходности: <b>{pct(yield_pct)}</b>. "
            "Для посуточной аренды нужна отдельная проверка occupancy, fees, management и сезонности; без внешних STR-данных модель считается консервативно.\n\n"
        )
    else:
        text += (
            "<b>6) Арендная модель</b>\n"
            "Для выбранного сценария недостаточно прямой арендной выборки. Это не значит, что объект плохой; это значит, что перед покупкой нужно отдельно проверить долгосрочную аренду, short-term потенциал, occupancy и сервисные платежи.\n\n"
        )

    text += (
        "<b>7) Сценарий перепродажи</b>\n"
        f"Ориентир перепродажи при осторожной модели роста {growth_rate * 100:.1f}% в год: "
        f"через 1 год — <b>{money(resale_1)}</b>, через 3 года — <b>{money(resale_3)}</b>, через 5 лет — <b>{money(resale_5)}</b>. "
        "Это не гарантия, а инвестиционный ориентир, основанный на ликвидности и качестве выборки.\n\n"
        "<b>8) Финальная рекомендация</b>\n"
        "Покупать стоит только при совпадении трёх условий: цена не выше справедливого DLD-ориентира, объект имеет ликвидный формат, а район показывает достаточную глубину сделок. "
        "Перед внесением депозита обязательно сравнить конкретный unit с последними сделками, площадью, этажом, видом, состоянием, сервисными платежами, арендным контрактом и юридической чистотой."
    )
    return text
