"""
intelligence_router.py
Dubai DLD Analytics Bot — Universal Intelligence Router / Smart Data Conductor

Version: v2 enterprise conductor
Purpose:
    Central "brain" layer between Telegram UI (main.py), DLD database, analytics logic,
    PDF/report generation and economic_engine.py.

This module is intentionally independent. It does not start the bot, does not own aiogram
handlers, and does not execute SQL directly. It prepares correct instructions, canonical
filters, fallback strategy and clean semantic payloads for main.py.

What it solves:
    1) Understands what the user selected or typed.
    2) Converts chaotic values into canonical fields.
    3) Knows where the request must go: sale table, rent table, both, archive/live/unified.
    4) Knows what a value means: area, building, bedroom, property type, price, size, date.
    5) Protects against old bugs:
        - sale shown as rent;
        - rent fallback to sale;
        - townhouse/villa/plot not found because of aliases;
        - area written as building;
        - building written as area;
        - missing rooms;
        - missing area/sqft;
        - 0 AED or future-date DLD records;
        - old context contaminating new scenario;
        - too-narrow filter returns nothing instead of useful fallback;
        - PDF/report generated with wrong sections.
    6) Generates a ready analysis payload for:
        - analytics;
        - latest deals;
        - rankings;
        - smart pick;
        - best object;
        - format comparison;
        - economic report;
        - PDF report.

Recommended integration in main.py:
    from intelligence_router import prepare_request, route_after_db_result, clean_dld_rows

    payload = prepare_request(
        raw_text=text,
        intent="best_object",
        deal_type=state.get("deal_type"),
        property_format=state.get("property_format"),
        bedrooms=state.get("bedrooms"),
        budget=state.get("budget"),
        goal=state.get("goal"),
        area=state.get("area"),
        building=state.get("building"),
        previous_context=state,
    )

    # Use payload.routing and payload.sql_plan to call existing DB helpers.
    # Use payload.report_plan and payload.economic_context for rendering/economic_engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union
import re


# ============================================================
# 1. Canonical tables / views / modes
# ============================================================

SALE_UNIFIED_TABLE = "public.dld_sales_unified"
RENT_UNIFIED_TABLE = "public.dld_rents_unified"

SALE_ARCHIVE_TABLE = "public.dld_sale_archive"
SALE_LIVE_TABLE = "public.dld_transactions_full"

RENT_ARCHIVE_TABLE = "public.dld_rent_archive"
RENT_LIVE_TABLE = "public.dld_rents_full"
RENT_LEGACY_TABLE = "public.dld_rents"

SALE_TABLE_CANDIDATES = [SALE_UNIFIED_TABLE, SALE_ARCHIVE_TABLE, SALE_LIVE_TABLE]
RENT_TABLE_CANDIDATES = [RENT_UNIFIED_TABLE, RENT_ARCHIVE_TABLE, RENT_LIVE_TABLE, RENT_LEGACY_TABLE]

SUPPORTED_INTENTS = {
    "start",
    "language",
    "settings",
    "main_menu",
    "building_search",
    "area_search",
    "building_analytics",
    "area_analytics",
    "dubai_analytics",
    "latest_deals",
    "period_comparison",
    "ranking",
    "smart_pick",
    "format_comparison",
    "best_object",
    "undervalued_check",
    "pdf",
    "consultation",
    "admin",
}

SUPPORTED_DEAL_TYPES = {"sale", "rent", "both"}
SUPPORTED_SCOPES = {"dubai", "area", "building", "project"}
SUPPORTED_GOALS = {
    "life",
    "rent_income",
    "short_term_rent",
    "resale",
    "roi",
    "capital_growth",
    "balanced",
}
SUPPORTED_RISKS = {"low", "balanced", "aggressive"}

# Queries that need both sale and rent data even if the selected deal_type is sale.
INTENTS_REQUIRING_RENT_MODEL = {"smart_pick", "best_object", "format_comparison", "undervalued_check"}
INTENTS_REQUIRING_BOTH_MARKETS = {"smart_pick", "best_object", "format_comparison"}


# ============================================================
# 2. Aliases and semantic dictionaries
# ============================================================

DEAL_TYPE_ALIASES = {
    "sale": [
        "sale", "sales", "sell", "sold", "resale", "transfer", "buy", "purchase",
        "продажа", "продажи", "купить", "покупка", "продать", "перепродажа", "перепродажи", "сделка покупки",
        "بيع", "شراء",
        "🏠 продажа", "🏠 sale",
    ],
    "rent": [
        "rent", "rental", "lease", "leasing", "tenancy", "ejari", "let", "letting",
        "аренда", "аренды", "для аренды", "сдать", "снять", "арендовать", "сдача", "арендный доход",
        "إيجار", "استئجار",
        "🔑 аренда", "🔑 rent",
    ],
    "both": [
        "both", "all", "any", "всё", "все", "любой", "любая", "без разницы", "неважно",
        "📊 всё", "📊 all",
    ],
}

PROPERTY_FORMAT_ALIASES = {
    "apartment": [
        "apartment", "apartments", "apt", "flat", "unit", "residential unit", "residential",
        "апартамент", "апартаменты", "квартира", "квартиры", "юнит", "юни т", "апарт",
        "شقة",
    ],
    "villa": [
        "villa", "villas", "вилла", "виллы", "вила", "вилл", "فيلا",
    ],
    "townhouse": [
        "townhouse", "town house", "townhouses", "town houses", "th", "t/h",
        "таунхаус", "таунхаусы", "таун", "таун хаус", "тонхаус", "тон хаус",
        "تاون هاوس",
    ],
    "land": [
        "land", "plot", "plots", "land plot", "residential land", "земля", "плот",
        "плоты", "плоть", "плотно", "участок", "земельный участок", "ارض", "أرض",
    ],
    "plot": [
        "plot", "plots", "land plot", "земельный участок", "участок", "плот",
    ],
    "office": [
        "office", "offices", "офис", "офисы", "مكتب",
    ],
    "retail": [
        "retail", "shop", "shops", "магазин", "ритейл", "торговое", "محل",
    ],
    "shop": [
        "shop", "shops", "retail", "магазин", "محل",
    ],
    "commercial": [
        "commercial", "коммерция", "коммерческая", "commercial unit", "تجاري",
    ],
    "penthouse": [
        "penthouse", "пентхаус", "пент", "بنتهاوس",
    ],
    "duplex": [
        "duplex", "дуплекс", "دوبلكس",
    ],
    "full_building": [
        "full building", "whole building", "entire building", "building only",
        "здание целиком", "полное здание",
    ],
    "warehouse": [
        "warehouse", "склад", "مستودع",
    ],
}

BEDROOM_ALIASES = {
    "studio": ["studio", "студия", "0 br", "0br", "studio apartment", "студио"],
    "1 br": ["1 br", "1br", "1 b/r", "1 bedroom", "one bedroom", "1 bed", "1-bed", "одна спальня", "1 спальня", "1 комнат"],
    "2 br": ["2 br", "2br", "2 b/r", "2 bedroom", "two bedroom", "2 bed", "2-bed", "две спальни", "2 спальни", "2 комнат"],
    "3 br": ["3 br", "3br", "3 b/r", "3 bedroom", "three bedroom", "3 bed", "3-bed", "три спальни", "3 спальни", "3 комнат"],
    "4 br": ["4 br", "4br", "4 b/r", "4 bedroom", "four bedroom", "4 bed", "4-bed", "4 спальни", "4 комнат"],
    "5 br+": ["5 br", "5br", "5+ br", "5 b/r", "5 bedroom", "five bedroom", "6 br", "7 br", "8 br", "5 спальни", "5+"],
}

AREA_ALIASES = {
    "jvc": ["jvc", "jumeirah village circle", "al barsha south fourth", "al barsha south fifth", "al hebiah first"],
    "jlt": ["jlt", "jumeirah lakes towers"],
    "downtown dubai": ["downtown", "downtown dubai", "dubai downtown", "burj khalifa"],
    "dubai marina": ["dubai marina", "marina", "marsa dubai"],
    "business bay": ["business bay", "бизнес бей"],
    "palm jumeirah": ["palm", "palm jumeirah", "the palm"],
    "dubai creek harbour": ["creek", "dubai creek", "dubai creek harbour", "creek harbour"],
    "sobha hartland": ["sobha", "sobha hartland"],
    "motor city": ["motor city", "dubai motor city"],
    "dubai hills estate": ["dubai hills", "dubai hills estate"],
    "damac hills": ["damac hills"],
    "damac lagoons": ["damac lagoons", "lagoons"],
    "arabian ranches": ["arabian ranches", "ranches"],
    "emirates living": ["emirates living", "emirates hills", "springs", "meadows", "lakes"],
    "meydan": ["meydan", "nad al sheba"],
    "jumeirah beach residence": ["jbr", "jumeirah beach residence"],
    "bluewaters": ["bluewaters", "bluewaters island"],
}

BUILDING_ALIASES = {
    "grande signature residences": ["grande signature residences", "grande signature", "grande"],
    "address residences dubai opera": ["address residences dubai opera", "address opera", "the address opera"],
    "binghatti corner": ["binghatti corner", "corner"],
}

GOAL_ALIASES = {
    "life": ["life", "live", "for life", "для жизни", "жить", "семья", "проживание", "себе"],
    "rent_income": ["rent", "rental income", "аренда", "аренды", "для аренды", "сдавать", "доход от аренды", "cashflow", "кэшфлоу"],
    "short_term_rent": ["short term", "short-term", "airbnb", "holiday home", "посуточно", "короткая аренда"],
    "resale": ["resale", "flip", "перепродажа", "перепродажи", "для перепродажи", "продажа", "продажи", "для продажи", "флип", "быстро продать"],
    "roi": ["roi", "yield", "доходность", "инвестиция", "инвестиции", "максимальный доход"],
    "capital_growth": ["capital growth", "appreciation", "рост капитала", "капитализация", "рост цены"],
    "balanced": ["balanced", "balance", "сбалансировано", "универсально", "неважно", "без разницы"],
}

RISK_ALIASES = {
    "low": ["low", "низкий", "низкий риск", "safe", "conservative", "консервативно", "безопасно"],
    "balanced": ["balanced", "сбалансировано", "medium", "normal", "средний"],
    "aggressive": ["aggressive", "агрессивно", "high risk", "высокий риск", "максимум"],
}

INTENT_ALIASES = {
    "best_object": ["лучший объект", "best object", "что лучше купить", "лучшие варианты", "подбери лучший"],
    "smart_pick": ["подбор", "инвестиционный подбор", "smart pick", "подобрать"],
    "format_comparison": ["сравнение форматов", "сравнить", "apartment vs", "villa vs", "таунхаус vs"],
    "latest_deals": ["сделки", "последние сделки", "deals", "transactions"],
    "ranking": ["рейтинг", "топ", "top", "ranking"],
    "building_search": ["здание", "билдинг", "building"],
    "area_search": ["район", "area", "community", "ария"],
    "pdf": ["pdf", "пдф", "скачать", "отчет"],
    "consultation": ["заявка", "консультация", "lead", "оставить заявку"],
}

# The bot sometimes receives column names, DB rows, or user text. These roles help map fields correctly.
FIELD_ROLE_ALIASES = {
    "area": ["area", "area_name_en", "area_name", "district", "community", "location", "район", "ария", "локация"],
    "building": ["building", "building_name_en", "project", "project_name_en", "master_project", "property_name", "здание", "билдинг", "проект", "дом"],
    "bedrooms": ["rooms", "rooms_en", "bedrooms", "bedroom", "beds", "комнаты", "спальни"],
    "property_format": ["property_type_en", "property_sub_type_en", "property_usage_en", "unit_type", "тип", "формат"],
    "price": ["actual_worth", "price", "amount", "sale_price", "transaction_amount", "цена", "стоимость", "сумма"],
    "rent": ["rent_value", "annual_rent", "rent_amount", "contract_amount", "rental_value", "аренда", "годовая аренда"],
    "area_size": ["actual_area", "procedure_area", "size", "sqft", "area_sqft", "property_size", "площадь", "скверфит", "sq.ft", "м2", "м²"],
    "price_per_area": ["meter_sale_price", "price_per_sqft", "price_per_meter", "rent_meter_price", "psf", "ppm", "цена за метр", "цена за sqft"],
    "date": ["instance_date", "registration_date", "transaction_date", "contract_start_date", "date", "дата"],
}


# ============================================================
# 3. Data structures
# ============================================================

@dataclass
class RouterNote:
    level: str
    code: str
    message: str
    source: Optional[str] = None
    target: Optional[str] = None


@dataclass
class IntelligenceRequest:
    raw_text: str = ""
    intent: Optional[str] = None
    deal_type: str = "sale"
    scope: str = "dubai"
    area: Optional[str] = None
    building: Optional[str] = None
    project: Optional[str] = None
    property_format: Optional[str] = None
    bedrooms: Optional[str] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    period_months: Optional[int] = None
    goal: str = "balanced"
    risk: str = "balanced"
    language: str = "ru"
    allow_fallback: bool = True
    previous_context: Dict[str, Any] = field(default_factory=dict)
    notes: List[RouterNote] = field(default_factory=list)


@dataclass
class RoutingDecision:
    primary_table: str
    fallback_tables: List[str]
    secondary_tables: List[str]
    source_mode: str
    deal_type: str
    reason: str
    forbidden_fallbacks: List[str] = field(default_factory=list)


@dataclass
class FallbackStep:
    scope: str
    area: Optional[str] = None
    building: Optional[str] = None
    property_format: Optional[str] = None
    bedrooms: Optional[str] = None
    period_months: Optional[int] = None
    budget_min: Optional[float] = None
    budget_max: Optional[float] = None
    reason: str = ""


@dataclass
class SQLPlan:
    table: str
    semantic_filters: Dict[str, Any]
    column_roles: Dict[str, List[str]]
    fallback_steps: List[FallbackStep]
    clean_rules: Dict[str, Any]
    require_separate_rent_sale: bool = True


@dataclass
class ReportPlan:
    report_type: str
    title: str
    required_sections: List[str]
    optional_sections: List[str]
    pdf_type: str
    result_buttons: List[str]
    should_show_notes: bool = True


@dataclass
class AnalysisPayload:
    request: IntelligenceRequest
    routing: RoutingDecision
    sql_plan: SQLPlan
    economic_context: Dict[str, Any]
    report_plan: ReportPlan
    next_steps: List[str]
    confidence: Dict[str, Any]
    notes: List[RouterNote]


# ============================================================
# 4. Primitive helpers
# ============================================================

def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _norm(value: Any) -> str:
    text = _s(value).lower().replace("ё", "е")
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"[^a-zа-я0-9\u0600-\u06FF\s/+–]", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    raw = _s(value).replace(",", "").replace(" ", "")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if not raw or raw in {"-", "."}:
        return None
    try:
        return float(Decimal(raw))
    except (InvalidOperation, ValueError):
        return None


def _date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _s(value)
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y-%m-%d").date()
    except Exception:
        return None


def _contains_any(text: str, words: Sequence[str]) -> bool:
    n = _norm(text)
    return any(_norm(w) in n for w in words if _norm(w))


def _alias_lookup(value: Any, alias_map: Dict[str, List[str]]) -> Optional[str]:
    t = _norm(value)
    if not t:
        return None
    if t in alias_map:
        return t

    for canonical, aliases in alias_map.items():
        for alias in aliases:
            if t == _norm(alias):
                return canonical

    matches: List[Tuple[int, str]] = []
    for canonical, aliases in alias_map.items():
        for alias in aliases:
            a = _norm(alias)
            if len(a) >= 2 and (a in t or t in a):
                matches.append((len(a), canonical))
    if matches:
        return sorted(matches, reverse=True)[0][1]
    return None


def _note(req: IntelligenceRequest, level: str, code: str, message: str, source: Any = None, target: Any = None) -> None:
    req.notes.append(RouterNote(level=level, code=code, message=message, source=_s(source) or None, target=_s(target) or None))


# ============================================================
# 5. Normalization layer
# ============================================================

def normalize_deal_type(value: Any) -> Optional[str]:
    return _alias_lookup(value, DEAL_TYPE_ALIASES)


def normalize_property_format(value: Any) -> Optional[str]:
    return _alias_lookup(value, PROPERTY_FORMAT_ALIASES)


def normalize_bedrooms(value: Any) -> Optional[str]:
    return _alias_lookup(value, BEDROOM_ALIASES)


def normalize_area(value: Any) -> Optional[str]:
    val = _alias_lookup(value, AREA_ALIASES)
    return val or (_s(value) if value else None)


def normalize_building(value: Any) -> Optional[str]:
    val = _alias_lookup(value, BUILDING_ALIASES)
    return val or (_s(value) if value else None)


def normalize_goal(value: Any) -> Optional[str]:
    return _alias_lookup(value, GOAL_ALIASES)


def normalize_risk(value: Any) -> Optional[str]:
    return _alias_lookup(value, RISK_ALIASES)


def infer_intent(text: Any) -> Optional[str]:
    return _alias_lookup(text, INTENT_ALIASES)


def parse_budget(value: Any) -> Tuple[Optional[float], Optional[float]]:
    text = _norm(value)
    raw = _s(value).lower()
    if not text:
        return None, None

    if "до 1" in text or "under 1" in text or "below 1" in text:
        return 0, 1_000_000
    if "1–2" in raw or "1-2" in raw or "1 2" in text:
        return 1_000_000, 2_000_000
    if "2–3" in raw or "2-3" in raw or "2 3" in text:
        return 2_000_000, 3_000_000
    if "3–5" in raw or "3-5" in raw or "3 5" in text:
        return 3_000_000, 5_000_000
    if "5m+" in text or "5м+" in text or "5+" in text:
        return 5_000_000, None

    nums = re.findall(r"\d+(?:\.\d+)?", raw.replace(",", ""))
    if not nums:
        return None, None

    mult = 1
    if any(x in raw for x in ["m", "м", "million", "миллион"]):
        mult = 1_000_000
    elif any(x in raw for x in ["k", "тыс", "thousand"]):
        mult = 1_000

    vals = [float(x) * mult for x in nums]
    if len(vals) >= 2:
        return min(vals[0], vals[1]), max(vals[0], vals[1])
    if any(x in raw for x in ["до", "under", "below", "less"]):
        return 0, vals[0]
    if any(x in raw for x in ["от", "above", "over", "+"]):
        return vals[0], None

    return None, vals[0]


def parse_period_months(value: Any) -> Optional[int]:
    text = _norm(value)
    if not text:
        return None
    if any(x in text for x in ["3 месяца", "3 month", "3 мес", "3m"]):
        return 3
    if any(x in text for x in ["6 месяцев", "6 month", "6 мес", "6m"]):
        return 6
    if any(x in text for x in ["1 год", "12 month", "1 year", "год", "12m"]):
        return 12
    if any(x in text for x in ["3 года", "36 month", "3 year", "36m"]):
        return 36
    if any(x in text for x in ["all time", "все время", "всё время"]):
        return None
    n = _num(text)
    if n in {3, 6, 12, 36}:
        return int(n)
    return None


def merge_previous_context(req: IntelligenceRequest, previous_context: Optional[Dict[str, Any]]) -> IntelligenceRequest:
    """Preserve useful context, but do not allow stale incompatible filters."""
    ctx = previous_context or {}
    req.previous_context = dict(ctx)

    # Keep selected area/building if current flow did not override it.
    if not req.area and ctx.get("area"):
        req.area = normalize_area(ctx.get("area"))
        req.scope = "area"
        _note(req, "info", "context_area", "Area restored from previous context", ctx.get("area"), req.area)

    if not req.building and ctx.get("building"):
        req.building = normalize_building(ctx.get("building"))
        req.scope = "building"
        _note(req, "info", "context_building", "Building restored from previous context", ctx.get("building"), req.building)

    # But if user selected Dubai-wide scope, clear object-specific filters.
    if req.scope == "dubai":
        if req.area or req.building:
            _note(req, "info", "scope_reset", "Dubai-wide scope clears area/building filters", f"{req.area}/{req.building}", "dubai")
        req.area = None
        req.building = None

    # Land/plot should not keep bedrooms.
    if req.property_format in {"land", "plot"} and req.bedrooms:
        _note(req, "warning", "incompatible_bedrooms", "Bedrooms removed because land/plot has no bedroom count", req.bedrooms, None)
        req.bedrooms = None

    # Office/shop/commercial should not keep residential bedroom count unless explicitly typed.
    if req.property_format in {"office", "shop", "retail", "commercial", "warehouse"} and req.bedrooms:
        _note(req, "warning", "incompatible_bedrooms", "Bedrooms removed because commercial asset has no bedroom count", req.bedrooms, None)
        req.bedrooms = None

    return req


def normalize_request(
    raw_text: str = "",
    *,
    intent: Optional[str] = None,
    deal_type: Optional[Any] = None,
    scope: Optional[str] = None,
    area: Optional[Any] = None,
    building: Optional[Any] = None,
    project: Optional[Any] = None,
    property_format: Optional[Any] = None,
    bedrooms: Optional[Any] = None,
    budget: Optional[Any] = None,
    budget_min: Optional[Any] = None,
    budget_max: Optional[Any] = None,
    period: Optional[Any] = None,
    goal: Optional[Any] = None,
    risk: Optional[Any] = None,
    language: str = "ru",
    previous_context: Optional[Dict[str, Any]] = None,
    allow_fallback: bool = True,
    **extra: Any,
) -> IntelligenceRequest:
    combined = " ".join([
        _s(raw_text), _s(intent), _s(deal_type), _s(scope), _s(area), _s(building),
        _s(project), _s(property_format), _s(bedrooms), _s(budget), _s(goal), _s(risk)
    ])

    req = IntelligenceRequest(raw_text=_s(raw_text), language=language or "ru", allow_fallback=allow_fallback)

    req.intent = intent or infer_intent(combined) or "smart_pick"
    if req.intent not in SUPPORTED_INTENTS:
        _note(req, "warning", "unknown_intent", "Unknown intent normalized to smart_pick", req.intent, "smart_pick")
        req.intent = "smart_pick"

    explicit_deal = normalize_deal_type(deal_type) if deal_type is not None else None
    req.deal_type = explicit_deal or normalize_deal_type(combined) or "sale"
    req.goal = normalize_goal(goal or combined) or "balanced"
    # If user explicitly chooses a rental goal and did not explicitly choose sale, route as rent.
    if explicit_deal is None and req.goal in {"rent_income", "short_term_rent"}:
        req.deal_type = "rent"
    req.risk = normalize_risk(risk or combined) or "balanced"

    req.property_format = normalize_property_format(property_format or combined)
    req.bedrooms = normalize_bedrooms(bedrooms or combined)

    req.scope = scope if scope in SUPPORTED_SCOPES else "dubai"

    if area:
        req.area = normalize_area(area)
        req.scope = "area"
        _note(req, "info", "area_mapped", "Value mapped as area", area, req.area)

    if building:
        req.building = normalize_building(building)
        req.scope = "building"
        _note(req, "info", "building_mapped", "Value mapped as building", building, req.building)

    if project and not req.building:
        req.building = normalize_building(project)
        req.scope = "building"
        _note(req, "info", "project_to_building", "Project value mapped as building/project", project, req.building)

    # Free text entity recognition only when no explicit entity.
    if not req.area and not req.building:
        maybe_area = normalize_area(raw_text)
        if maybe_area and _norm(maybe_area) != _norm(raw_text):
            req.area = maybe_area
            req.scope = "area"
            _note(req, "info", "text_to_area", "Free text recognized as area", raw_text, maybe_area)

    if budget is not None:
        req.budget_min, req.budget_max = parse_budget(budget)
    else:
        req.budget_min, req.budget_max = _num(budget_min), _num(budget_max)

    req.period_months = parse_period_months(period)

    if req.property_format:
        _note(req, "info", "format_normalized", "Property format normalized", property_format or combined, req.property_format)
    if req.bedrooms:
        _note(req, "info", "bedrooms_normalized", "Bedrooms normalized", bedrooms or combined, req.bedrooms)
    if req.budget_min is not None or req.budget_max is not None:
        _note(req, "info", "budget_normalized", "Budget normalized", budget or f"{budget_min}-{budget_max}", f"{req.budget_min}-{req.budget_max}")
    if req.period_months is not None:
        _note(req, "info", "period_normalized", "Period normalized", period, str(req.period_months))

    req = merge_previous_context(req, previous_context)
    return req


# ============================================================
# 6. Dynamic table intelligence
# ============================================================

def resolve_best_data_source(req: IntelligenceRequest) -> RoutingDecision:
    """Decide which source group should be queried first."""
    intent = req.intent
    deal = req.deal_type

    if deal == "rent":
        return RoutingDecision(
            primary_table=RENT_UNIFIED_TABLE,
            fallback_tables=[RENT_ARCHIVE_TABLE, RENT_LIVE_TABLE, RENT_LEGACY_TABLE],
            secondary_tables=[],
            source_mode="rent_only",
            deal_type="rent",
            reason="Rent request must use rent tables only. Sale fallback is forbidden.",
            forbidden_fallbacks=[SALE_UNIFIED_TABLE, SALE_ARCHIVE_TABLE, SALE_LIVE_TABLE],
        )

    if intent in INTENTS_REQUIRING_BOTH_MARKETS or deal == "both":
        return RoutingDecision(
            primary_table=SALE_UNIFIED_TABLE,
            fallback_tables=[SALE_ARCHIVE_TABLE, SALE_LIVE_TABLE],
            secondary_tables=[RENT_UNIFIED_TABLE, RENT_ARCHIVE_TABLE, RENT_LIVE_TABLE, RENT_LEGACY_TABLE],
            source_mode="sale_plus_rent_model",
            deal_type=deal,
            reason="Investment/best-object scenarios need sale prices plus rent model, but metrics remain separated.",
            forbidden_fallbacks=[],
        )

    if intent == "latest_deals":
        return RoutingDecision(
            primary_table=SALE_UNIFIED_TABLE,
            fallback_tables=[SALE_LIVE_TABLE, SALE_ARCHIVE_TABLE],
            secondary_tables=[],
            source_mode="latest_sale_priority",
            deal_type="sale",
            reason="Latest deals should prioritize unified/live sale stream.",
            forbidden_fallbacks=[],
        )

    return RoutingDecision(
        primary_table=SALE_UNIFIED_TABLE,
        fallback_tables=[SALE_ARCHIVE_TABLE, SALE_LIVE_TABLE],
        secondary_tables=[],
        source_mode="sale_analytics",
        deal_type="sale",
        reason="Sale analytics should use unified sale table first.",
        forbidden_fallbacks=[],
    )


# ============================================================
# 7. Unified column intelligence
# ============================================================

def canonical_column_roles(deal_type: str = "sale") -> Dict[str, List[str]]:
    if deal_type == "rent":
        return {
            "price": ["rent_value", "annual_rent", "annual_amount", "rent_amount", "rental_value", "contract_amount", "contract_value", "ejari_contract_amount", "actual_rent"],
            "date": ["contract_start_date", "start_date", "instance_date", "registration_date", "contract_date", "date"],
            "area": ["area_name_en", "area_name", "area", "community", "district", "location", "location_en"],
            "building": ["building_name_en", "building_name", "building", "project_name_en", "project_name", "project", "property_name", "master_project_en"],
            "bedrooms": ["rooms_en", "rooms", "bedrooms", "bedroom", "rooms_count", "unit_rooms"],
            "property_format": ["property_type_en", "property_sub_type_en", "property_usage_en", "unit_type", "property_category"],
            "area_size": ["actual_area", "procedure_area", "property_size", "area_sqft", "size_sqft", "built_up_area", "unit_area"],
            "price_per_area": ["rent_meter_price", "rent_per_sqft", "price_per_sqft", "price_per_meter"],
        }
    return {
        "price": ["actual_worth", "price", "amount", "transaction_amount", "sale_price"],
        "date": ["instance_date", "registration_date", "transaction_date", "date"],
        "area": ["area_name_en", "area_name", "area", "community", "district", "location"],
        "building": ["building_name_en", "building_name", "building", "project_name_en", "project_name", "project", "master_project_en", "property_name"],
        "bedrooms": ["rooms_en", "rooms", "bedrooms", "bedroom", "rooms_count"],
        "property_format": ["property_type_en", "property_sub_type_en", "property_usage_en", "unit_type", "property_category"],
        "area_size": ["actual_area", "procedure_area", "property_size", "area_sqft", "size_sqft", "built_up_area", "unit_area"],
        "price_per_area": ["meter_sale_price", "price_per_sqft", "price_per_meter", "psf"],
    }


def detect_field_role(column_name: str) -> Optional[str]:
    c = _norm(column_name)
    for role, aliases in FIELD_ROLE_ALIASES.items():
        for alias in aliases:
            a = _norm(alias)
            if c == a or a in c:
                return role
    return None


def smart_pick_column(available_columns: Sequence[str], role: str, deal_type: str = "sale") -> Optional[str]:
    roles = canonical_column_roles(deal_type)
    candidates = roles.get(role, [])
    available_lower = {c.lower(): c for c in available_columns}

    for c in candidates:
        if c.lower() in available_lower:
            return available_lower[c.lower()]

    for c in available_columns:
        if detect_field_role(c) == role:
            return c

    return None


def first_value(row: Dict[str, Any], candidates: Sequence[str]) -> Any:
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c]
    lower = {str(k).lower(): k for k in row.keys()}
    for c in candidates:
        k = lower.get(c.lower())
        if k is not None and row[k] not in (None, ""):
            return row[k]
    return None


# ============================================================
# 8. Fallback cascade and scenario recovery
# ============================================================

def build_fallback_steps(req: IntelligenceRequest) -> List[FallbackStep]:
    """Build safe fallback cascade without mixing sale/rent."""
    steps: List[FallbackStep] = []

    # Exact
    steps.append(FallbackStep(
        scope=req.scope,
        area=req.area,
        building=req.building,
        property_format=req.property_format,
        bedrooms=req.bedrooms,
        period_months=req.period_months,
        budget_min=req.budget_min,
        budget_max=req.budget_max,
        reason="Exact user filter.",
    ))

    # Period expansion
    if req.period_months:
        steps.append(FallbackStep(
            scope=req.scope,
            area=req.area,
            building=req.building,
            property_format=req.property_format,
            bedrooms=req.bedrooms,
            period_months=None,
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            reason="Expand period to all time if recent sample is too small.",
        ))

    # Bedroom expansion
    if req.bedrooms:
        steps.append(FallbackStep(
            scope=req.scope,
            area=req.area,
            building=req.building,
            property_format=req.property_format,
            bedrooms=None,
            period_months=req.period_months,
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            reason="Remove bedrooms filter if room-level sample is too small.",
        ))

    # Budget soft expansion
    if req.budget_min is not None or req.budget_max is not None:
        bmin = req.budget_min * 0.85 if req.budget_min else None
        bmax = req.budget_max * 1.20 if req.budget_max else None
        steps.append(FallbackStep(
            scope=req.scope,
            area=req.area,
            building=req.building,
            property_format=req.property_format,
            bedrooms=req.bedrooms,
            period_months=req.period_months,
            budget_min=bmin,
            budget_max=bmax,
            reason="Expand budget band moderately if exact budget has no stable data.",
        ))

    # Building -> area
    if req.scope == "building":
        steps.append(FallbackStep(
            scope="area",
            area=req.area,
            building=None,
            property_format=req.property_format,
            bedrooms=req.bedrooms,
            period_months=req.period_months,
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            reason="Fallback from building to its area/community.",
        ))

    # Area -> Dubai
    if req.scope in {"area", "building"}:
        steps.append(FallbackStep(
            scope="dubai",
            area=None,
            building=None,
            property_format=req.property_format,
            bedrooms=req.bedrooms,
            period_months=req.period_months,
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            reason="Fallback to Dubai-wide market for benchmark.",
        ))

    # Property format expansion
    if req.property_format:
        steps.append(FallbackStep(
            scope=req.scope,
            area=req.area,
            building=req.building,
            property_format=None,
            bedrooms=req.bedrooms,
            period_months=req.period_months,
            budget_min=req.budget_min,
            budget_max=req.budget_max,
            reason="Remove property format only as final benchmark fallback.",
        ))

    # Universal
    steps.append(FallbackStep(
        scope="dubai",
        area=None,
        building=None,
        property_format=None,
        bedrooms=None,
        period_months=None,
        budget_min=None,
        budget_max=None,
        reason="Last-resort Dubai-wide benchmark.",
    ))

    # Deduplicate
    seen = set()
    unique: List[FallbackStep] = []
    for st in steps:
        key = (st.scope, st.area, st.building, st.property_format, st.bedrooms, st.period_months, st.budget_min, st.budget_max)
        if key not in seen:
            seen.add(key)
            unique.append(st)
    return unique


def clean_incompatible_filters(req: IntelligenceRequest) -> IntelligenceRequest:
    if req.property_format in {"land", "plot"}:
        if req.bedrooms:
            _note(req, "warning", "bedrooms_removed_for_land", "Bedrooms removed because land/plot has no room count.", req.bedrooms, None)
        req.bedrooms = None
    if req.property_format in {"office", "shop", "retail", "commercial", "warehouse", "full_building"}:
        if req.bedrooms:
            _note(req, "warning", "bedrooms_removed_for_commercial", "Bedrooms removed because selected format is commercial/non-residential.", req.bedrooms, None)
        req.bedrooms = None
    if req.deal_type == "rent" and req.goal == "resale":
        _note(req, "warning", "goal_adjusted", "Resale goal requires sale market; rent request changed to rent_income.", req.goal, "rent_income")
        req.goal = "rent_income"
    return req


# ============================================================
# 9. SQL plan and semantic filters
# ============================================================

def semantic_filters(req: IntelligenceRequest) -> Dict[str, Any]:
    filters = {
        "deal_type": req.deal_type,
        "scope": req.scope,
        "area": req.area,
        "building": req.building,
        "property_format": req.property_format,
        "property_format_aliases": PROPERTY_FORMAT_ALIASES.get(req.property_format, [req.property_format]) if req.property_format else [],
        "bedrooms": req.bedrooms,
        "bedroom_aliases": BEDROOM_ALIASES.get(req.bedrooms, [req.bedrooms]) if req.bedrooms else [],
        "budget_min": req.budget_min,
        "budget_max": req.budget_max,
        "period_months": req.period_months,
        "area_aliases": AREA_ALIASES.get(_norm(req.area), [req.area]) if req.area else [],
        "building_aliases": BUILDING_ALIASES.get(_norm(req.building), [req.building]) if req.building else [],
    }
    return filters


def clean_rules_for(req: IntelligenceRequest) -> Dict[str, Any]:
    if req.deal_type == "rent":
        return {
            "price_min": 1_000,
            "price_max": 5_000_000,
            "exclude_zero_price": True,
            "exclude_negative_price": True,
            "exclude_future_dates": True,
            "allow_missing_area_size": True,
            "forbidden_keywords": ["sale", "sold", "resale", "transfer", "mortgage", "gift", "grant"],
        }
    return {
        "price_min": 10_000,
        "price_max": 1_000_000_000,
        "exclude_zero_price": True,
        "exclude_negative_price": True,
        "exclude_future_dates": True,
        "allow_missing_area_size": True,
        "forbidden_keywords": ["rent", "rental", "lease", "ejari", "tenancy"],
    }


def build_sql_plan(req: IntelligenceRequest, routing: RoutingDecision) -> SQLPlan:
    return SQLPlan(
        table=routing.primary_table,
        semantic_filters=semantic_filters(req),
        column_roles=canonical_column_roles("rent" if routing.deal_type == "rent" else "sale"),
        fallback_steps=build_fallback_steps(req),
        clean_rules=clean_rules_for(req),
        require_separate_rent_sale=True,
    )


# ============================================================
# 10. Row cleaning and semantic mapping
# ============================================================

def normalize_row(row: Dict[str, Any], deal_type: str = "sale") -> Dict[str, Any]:
    roles = canonical_column_roles(deal_type)
    raw_format = first_value(row, roles["property_format"])
    raw_bedrooms = first_value(row, roles["bedrooms"])
    raw_area = first_value(row, roles["area"])
    raw_building = first_value(row, roles["building"])

    result = {
        "deal_type": deal_type,
        "date": _date(first_value(row, roles["date"])),
        "price": _num(first_value(row, roles["price"])),
        "area": normalize_area(raw_area),
        "building": normalize_building(raw_building),
        "bedrooms": normalize_bedrooms(raw_bedrooms),
        "property_format": normalize_property_format(raw_format),
        "area_size": _num(first_value(row, roles["area_size"])),
        "price_per_area": _num(first_value(row, roles["price_per_area"])),
        "raw_area": raw_area,
        "raw_building": raw_building,
        "raw_bedrooms": raw_bedrooms,
        "raw_property_format": raw_format,
        "raw": row,
    }

    if not result["building"]:
        project = first_value(row, ["project_name_en", "project_name", "project", "master_project_en", "property_name"])
        if project:
            result["building"] = _s(project)

    return result


def is_dirty_row(row: Dict[str, Any], deal_type: str = "sale", today: Optional[date] = None) -> Tuple[bool, List[str]]:
    today = today or date.today()
    n = normalize_row(row, deal_type)
    rules = clean_rules_for(IntelligenceRequest(deal_type=deal_type))

    reasons: List[str] = []
    price = n.get("price")
    dt = n.get("date")
    area_size = n.get("area_size")

    if price is None:
        reasons.append("missing_price")
    elif price <= 0:
        reasons.append("zero_or_negative_price")
    elif price < rules["price_min"]:
        reasons.append("suspiciously_low_price")
    elif price > rules["price_max"]:
        reasons.append("suspiciously_high_price")

    if dt and dt > today:
        reasons.append("future_date")

    if area_size is not None and area_size < 0:
        reasons.append("negative_area_size")

    raw_blob = _norm(" ".join(str(v) for v in row.values() if v is not None))
    for kw in rules["forbidden_keywords"]:
        if _norm(kw) in raw_blob:
            # Only mark as dirty if deal type identity is very likely wrong.
            reasons.append(f"forbidden_keyword_{kw}")
            break

    return bool(reasons), reasons


def clean_dld_rows(rows: Iterable[Dict[str, Any]], deal_type: str = "sale") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows_list = list(rows or [])
    clean: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for row in rows_list:
        dirty, reasons = is_dirty_row(row, deal_type)
        if dirty:
            rejected.append({"reasons": reasons, "row": row})
        else:
            clean.append(normalize_row(row, deal_type))

    return clean, {
        "input_count": len(rows_list),
        "clean_count": len(clean),
        "rejected_count": len(rejected),
        "rejected_sample": rejected[:10],
    }


# ============================================================
# 11. Confidence scoring
# ============================================================

def confidence_from_stats(stats: Optional[Dict[str, Any]], fallback_used: Optional[FallbackStep] = None) -> Dict[str, Any]:
    if not stats:
        return {"level": "low", "score": 0, "reason": "No stats available."}

    deals = int(_num(stats.get("deals")) or 0)
    score = 0
    if deals >= 500:
        score += 55
    elif deals >= 100:
        score += 45
    elif deals >= 30:
        score += 32
    elif deals >= 10:
        score += 20
    elif deals > 0:
        score += 10

    if stats.get("avg_price"):
        score += 15
    if stats.get("avg_meter") or stats.get("price_per_area"):
        score += 10
    if stats.get("first_deal") and stats.get("last_deal"):
        score += 10
    if fallback_used:
        score -= 10

    score = max(0, min(100, score))
    if score >= 70:
        level = "high"
    elif score >= 40:
        level = "medium"
    else:
        level = "low"

    reason = f"{deals} DLD records"
    if fallback_used:
        reason += f"; fallback used: {fallback_used.reason}"
    return {"level": level, "score": score, "reason": reason}


def default_confidence(req: IntelligenceRequest) -> Dict[str, Any]:
    score = 50
    if req.area or req.building:
        score += 10
    if req.property_format:
        score += 10
    if req.bedrooms:
        score += 5
    if req.budget_min is not None or req.budget_max is not None:
        score += 5
    if req.intent in {"best_object", "smart_pick", "format_comparison"}:
        score += 5
    score = max(0, min(100, score))
    level = "high" if score >= 70 else "medium" if score >= 40 else "low"
    return {"level": level, "score": score, "reason": "Pre-query confidence from request completeness."}


# ============================================================
# 12. Economic context and report plan
# ============================================================

def build_economic_context(req: IntelligenceRequest, routing: RoutingDecision) -> Dict[str, Any]:
    needs_rent = req.goal in {"rent_income", "short_term_rent", "roi", "balanced"} or req.intent in INTENTS_REQUIRING_RENT_MODEL
    needs_resale = req.goal in {"resale", "capital_growth", "roi", "balanced"} or req.intent in INTENTS_REQUIRING_BOTH_MARKETS

    return {
        "deal_type": req.deal_type,
        "source_mode": routing.source_mode,
        "area": req.area,
        "building": req.building,
        "property_format": req.property_format,
        "bedrooms": req.bedrooms,
        "budget_min": req.budget_min,
        "budget_max": req.budget_max,
        "period_months": req.period_months,
        "goal": req.goal,
        "risk": req.risk,
        "needs_rent_model": needs_rent,
        "needs_short_term_model": req.goal in {"short_term_rent", "roi", "balanced"},
        "needs_resale_model": needs_resale,
        "needs_hold_strategy": True,
        "needs_exit_strategy": True,
        "needs_format_comparison": req.intent in {"best_object", "format_comparison", "smart_pick"},
        "needs_bedroom_comparison": req.property_format == "apartment" or req.bedrooms is not None,
        "must_explain_why": True,
        "must_show_alternatives": req.intent in {"best_object", "format_comparison", "smart_pick"},
        "must_not_mix_rent_and_sale": True,
    }


def report_plan_for(req: IntelligenceRequest) -> ReportPlan:
    if req.intent == "latest_deals":
        return ReportPlan(
            report_type="latest_deals",
            title="🧾 Последние сделки DLD",
            required_sections=["filters", "deal_cards", "data_quality_note"],
            optional_sections=["market_benchmark", "pdf"],
            pdf_type="deals_report",
            result_buttons=["📄 PDF", "💼 Заявка", "🔁 Новый отчёт", "🏠 Главное меню"],
        )

    if req.intent == "ranking":
        return ReportPlan(
            report_type="ranking",
            title="📊 Рейтинг рынка",
            required_sections=["ranking_table", "liquidity_score", "price_metrics"],
            optional_sections=["trend", "pdf"],
            pdf_type="ranking_report",
            result_buttons=["📄 PDF", "💼 Заявка", "🔁 Новый отчёт", "🏠 Главное меню"],
        )

    if req.intent == "format_comparison":
        return ReportPlan(
            report_type="format_comparison",
            title="⚖️ Сравнение форматов",
            required_sections=["apartment_vs_villa_vs_townhouse", "winner_reason", "risk", "next_steps"],
            optional_sections=["best_areas", "best_buildings", "rental_strategy", "resale_strategy", "pdf"],
            pdf_type="format_comparison_report",
            result_buttons=["🏆 Лучший формат", "🏙 Лучшие районы", "🏢 Лучшие здания", "📄 PDF", "💼 Заявка"],
        )

    if req.intent == "best_object":
        return ReportPlan(
            report_type="best_object",
            title="🏆 Лучший объект",
            required_sections=[
                "summary",
                "top_3_areas",
                "top_3_assets_or_buildings",
                "why_winner",
                "rental_model",
                "resale_model",
                "hold_strategy",
                "risk",
                "action_plan",
            ],
            optional_sections=["format_comparison", "bedroom_comparison", "pdf"],
            pdf_type="investment_best_object_report",
            result_buttons=["📄 PDF", "💼 Заявка", "🔁 Новый подбор", "🏠 Главное меню"],
        )

    if req.intent == "smart_pick":
        return ReportPlan(
            report_type="smart_pick",
            title="🧠 Инвестиционный подбор",
            required_sections=["best_scenario", "why", "rental_model", "resale_model", "alternatives", "risk"],
            optional_sections=["hold_1_3_5_years", "pdf"],
            pdf_type="investment_report",
            result_buttons=["📄 PDF", "💼 Заявка", "🔁 Новый подбор", "🏠 Главное меню"],
        )

    return ReportPlan(
        report_type="analytics",
        title="📊 Аналитика DLD",
        required_sections=["filters", "core_metrics", "economic_conclusion"],
        optional_sections=["latest_deals", "period_comparison", "pdf"],
        pdf_type="analytics_report",
        result_buttons=["📄 PDF", "💼 Заявка", "🔁 Новый отчёт", "🏠 Главное меню"],
    )


# ============================================================
# 13. Next-step conductor
# ============================================================

def next_required_steps(req: IntelligenceRequest) -> List[str]:
    steps: List[str] = []
    if req.intent in {"best_object", "smart_pick", "format_comparison"}:
        if req.deal_type not in SUPPORTED_DEAL_TYPES:
            steps.append("ask_deal_type")
        if req.intent != "format_comparison" and not req.property_format:
            steps.append("ask_property_format_or_skip")
        if req.budget_min is None and req.budget_max is None:
            steps.append("ask_budget_or_skip")
        if not req.goal:
            steps.append("ask_goal")
        if not req.risk:
            steps.append("ask_risk")
    if req.scope == "area" and not req.area:
        steps.append("ask_area")
    if req.scope == "building" and not req.building:
        steps.append("ask_building")
    return steps


def route_after_db_result(payload: AnalysisPayload, rows_or_stats: Any) -> Dict[str, Any]:
    """Tell main.py what to do after DB returned data.

    This is the scenario recovery layer.
    """
    req = payload.request
    empty = False

    if rows_or_stats is None:
        empty = True
    elif isinstance(rows_or_stats, list):
        empty = len(rows_or_stats) == 0
    elif isinstance(rows_or_stats, dict):
        empty = int(_num(rows_or_stats.get("deals")) or 0) == 0 and not rows_or_stats.get("price")

    if not empty:
        return {
            "action": "render_report",
            "report_type": payload.report_plan.report_type,
            "confidence": confidence_from_stats(rows_or_stats if isinstance(rows_or_stats, dict) else None),
            "message": "Data is sufficient for rendering.",
        }

    if not req.allow_fallback or not payload.sql_plan.fallback_steps:
        return {
            "action": "show_no_data",
            "report_type": payload.report_plan.report_type,
            "confidence": {"level": "low", "score": 0, "reason": "No data and fallback disabled."},
            "message": "No stable DLD sample for selected filters.",
        }

    return {
        "action": "try_next_fallback",
        "fallback_steps": [asdict(x) for x in payload.sql_plan.fallback_steps[1:]],
        "message": "No exact data; try fallback cascade instead of ending scenario.",
    }


# ============================================================
# 14. Display helpers and response composition
# ============================================================

def format_money(value: Any) -> str:
    n = _num(value)
    if n is None:
        return "нет данных"
    return f"{n:,.0f} AED".replace(",", " ")


def safe_display(value: Any, fallback: str = "нет данных") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def label_row_for_display(row: Dict[str, Any], deal_type: str = "sale", language: str = "ru") -> Dict[str, str]:
    n = normalize_row(row, deal_type)
    labels = {
        "date": "Дата сделки",
        "area": "Район",
        "building": "Здание",
        "property_format": "Тип недвижимости",
        "bedrooms": "Комнаты",
        "area_size": "Площадь",
        "price": "Годовая аренда" if deal_type == "rent" else "Цена",
        "price_per_area": "Аренда за м²/ft²" if deal_type == "rent" else "Цена за м²/ft²",
    }
    return {
        labels["date"]: safe_display(n.get("date")),
        labels["area"]: safe_display(n.get("area")),
        labels["building"]: safe_display(n.get("building")),
        labels["property_format"]: safe_display(n.get("property_format")),
        labels["bedrooms"]: safe_display(n.get("bedrooms")),
        labels["area_size"]: safe_display(n.get("area_size")),
        labels["price"]: format_money(n.get("price")),
        labels["price_per_area"]: format_money(n.get("price_per_area")),
    }


def human_notes(notes: Sequence[RouterNote], language: str = "ru") -> str:
    if not notes:
        return ""
    title = "📌 <b>Система автоматически уточнила:</b>" if language == "ru" else "📌 <b>Automatic normalization:</b>"
    lines = [title]
    for n in notes:
        if n.source and n.target:
            lines.append(f"• {n.source} → <b>{n.target}</b>")
        else:
            lines.append(f"• {n.message}")
    return "\n".join(lines)


def explain_payload(payload: AnalysisPayload) -> str:
    req = payload.request
    return (
        "🧭 <b>Intelligence Router</b>\n\n"
        f"Intent: <b>{req.intent}</b>\n"
        f"Deal type: <b>{req.deal_type}</b>\n"
        f"Scope: <b>{req.scope}</b>\n"
        f"Area: <b>{req.area or '-'}</b>\n"
        f"Building: <b>{req.building or '-'}</b>\n"
        f"Format: <b>{req.property_format or '-'}</b>\n"
        f"Bedrooms: <b>{req.bedrooms or '-'}</b>\n"
        f"Budget: <b>{format_money(req.budget_min) if req.budget_min else '-'} — {format_money(req.budget_max) if req.budget_max else '-'}</b>\n"
        f"Goal: <b>{req.goal}</b>\n\n"
        f"Primary table: <b>{payload.routing.primary_table}</b>\n"
        f"Source mode: <b>{payload.routing.source_mode}</b>\n"
        f"Report: <b>{payload.report_plan.report_type}</b>"
    )


# ============================================================
# 15. Public API
# ============================================================

def prepare_request(raw_text: str = "", **kwargs: Any) -> AnalysisPayload:
    req = normalize_request(raw_text, **kwargs)
    req = clean_incompatible_filters(req)
    routing = resolve_best_data_source(req)
    sql_plan = build_sql_plan(req, routing)
    econ = build_economic_context(req, routing)
    report = report_plan_for(req)
    steps = next_required_steps(req)
    confidence = default_confidence(req)

    return AnalysisPayload(
        request=req,
        routing=routing,
        sql_plan=sql_plan,
        economic_context=econ,
        report_plan=report,
        next_steps=steps,
        confidence=confidence,
        notes=req.notes,
    )


def prepare_from_state(text: str, state: Optional[Dict[str, Any]] = None, **overrides: Any) -> AnalysisPayload:
    state = state or {}
    kwargs = {
        "intent": overrides.get("intent", state.get("intent") or state.get("step") or state.get("report_kind")),
        "deal_type": overrides.get("deal_type", state.get("deal_type")),
        "scope": overrides.get("scope", state.get("scope")),
        "area": overrides.get("area", state.get("area") or (state.get("name") if state.get("scope") == "area" else None)),
        "building": overrides.get("building", state.get("building") or (state.get("name") if state.get("scope") == "building" else None)),
        "property_format": overrides.get("property_format", state.get("property_format") or state.get("property")),
        "bedrooms": overrides.get("bedrooms", state.get("bedrooms") or state.get("property")),
        "budget": overrides.get("budget", state.get("budget")),
        "period": overrides.get("period", state.get("period")),
        "goal": overrides.get("goal", state.get("goal")),
        "risk": overrides.get("risk", state.get("risk")),
        "language": overrides.get("language", state.get("language", "ru")),
        "previous_context": state,
    }
    return prepare_request(text, **kwargs)


# ============================================================
# 16. Self-test matrix
# ============================================================

def selftest() -> Dict[str, Any]:
    scenarios = [
        ("таунхаус для аренды 2-3M", {"intent": "best_object", "budget": "2–3M AED"}),
        ("тонхаус перепродажа", {"intent": "best_object", "budget": "3–5M AED", "goal": "перепродажа"}),
        ("плот земля инвестиция 5M+", {"intent": "best_object", "property_format": "плот", "budget": "5M+ AED"}),
        ("JVC 1 bedroom resale", {"intent": "smart_pick"}),
        ("Dubai Marina apartment rent", {"intent": "latest_deals", "deal_type": "аренда", "area": "Marina"}),
        ("сравнить вилла таунхаус апартаменты", {"intent": "format_comparison", "budget": "3-5M"}),
        ("Grande сделки продажа 2BR", {"intent": "latest_deals", "building": "Grande", "deal_type": "sale"}),
        ("район Business Bay 1BR аренда", {"intent": "area_analytics", "area": "Business Bay", "deal_type": "rent"}),
    ]
    results = []
    for text, kwargs in scenarios:
        p = prepare_request(text, **kwargs)
        results.append({
            "text": text,
            "request": asdict(p.request),
            "routing": asdict(p.routing),
            "filters": p.sql_plan.semantic_filters,
            "fallback_count": len(p.sql_plan.fallback_steps),
            "report": asdict(p.report_plan),
            "confidence": p.confidence,
        })
    return {"ok": True, "count": len(results), "results": results}


if __name__ == "__main__":
    import json
    print(json.dumps(selftest(), ensure_ascii=False, indent=2))
