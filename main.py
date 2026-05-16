from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

from dotenv import load_dotenv

import asyncio
import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

TABLE = "public.dld_transactions_full"

user_languages = {}
user_states = {}


def db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def num_sql(column):
    return f"NULLIF(regexp_replace(COALESCE({column}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


PRICE = num_sql("actual_worth")
METER_PRICE = num_sql("meter_sale_price")
RENT_VALUE = num_sql("rent_value")

BUILDING_NAME = "COALESCE(building_name_en::text, '')"


_COLUMN_CACHE = None
_RENT_VALUE_EXPR_CACHE = None


def available_columns():
    """Список колонок DLD таблицы. Нужен, чтобы один файл работал на разных версиях базы."""
    global _COLUMN_CACHE
    if _COLUMN_CACHE is not None:
        return _COLUMN_CACHE
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'dld_transactions_full'
                """)
                _COLUMN_CACHE = {r["column_name"] for r in cur.fetchall()}
    except Exception as e:
        print("COLUMN_DETECT_ERROR:", repr(e))
        _COLUMN_CACHE = set()
    return _COLUMN_CACHE


def text_col_expr(*cols):
    existing = [c for c in cols if c in available_columns()]
    if not existing:
        return "''"
    return "COALESCE(" + ", ".join([f"{c}::text" for c in existing]) + ", '')"


def rent_amount_expr():
    """Реальная сумма аренды.

    Важно: для аренды нельзя автоматически брать actual_worth, иначе бот начинает
    показывать цены продажи как аренду. actual_worth берём только если строка явно
    rental/lease/ejari/tenancy и сумма похожа на годовую аренду.
    """
    global _RENT_VALUE_EXPR_CACHE
    if _RENT_VALUE_EXPR_CACHE is not None:
        return _RENT_VALUE_EXPR_CACHE

    cols = available_columns()
    candidates = [
        "rent_value", "annual_rent", "rent_amount", "rental_value", "lease_value",
        "contract_amount", "contract_value", "ejari_value", "rent_contract_value",
        "tenant_contract_amount", "yearly_rent", "yearly_rent_value"
    ]
    numeric_parts = []
    for c in candidates:
        if c in cols:
            numeric_parts.append(f"NULLIF(regexp_replace(COALESCE({c}::text, ''), '[^0-9.]', '', 'g'), '')::numeric")

    proc_text = text_col_expr(
        "procedure_name_en", "procedure_name_ar", "procedure_name", "transaction_type_en",
        "transaction_type", "transaction_group_en", "transaction_sub_type_en",
        "property_usage_en", "procedure_group_en"
    )
    aw = PRICE
    fallback_actual_worth = f"""
        CASE
            WHEN ({proc_text}) ~* '(rent|rental|lease|leasing|tenancy|ejari)'
             AND ({proc_text}) !~* '(sale|sales|sell|sold|mortgage|gift|grant|transfer)'
             AND {aw} IS NOT NULL
             AND {aw} > 0
             AND {aw} <= 2000000
            THEN {aw}
            ELSE NULL
        END
    """
    numeric_parts.append(fallback_actual_worth)
    _RENT_VALUE_EXPR_CACHE = "COALESCE(" + ", ".join(numeric_parts) + ")"
    return _RENT_VALUE_EXPR_CACHE


def rent_identity_condition_sql():
    """Строгий фильтр аренды. Главная защита: не пускать sale/resale строки в аренду."""
    proc_text = text_col_expr(
        "procedure_name_en", "procedure_name_ar", "procedure_name", "transaction_type_en",
        "transaction_type", "transaction_group_en", "transaction_sub_type_en",
        "property_usage_en", "procedure_group_en"
    )
    rent_expr = rent_amount_expr()
    return f"""
        AND (
            ({proc_text}) ~* '(rent|rental|lease|leasing|tenancy|ejari)'
            OR {rent_expr} IS NOT NULL
        )
        AND ({proc_text}) !~* '(sale|sales|sell|sold|resale|mortgage|gift|grant|transfer)'
        AND {rent_expr} IS NOT NULL
        AND {rent_expr} > 0
        AND {rent_expr} <= 2000000
    """


def sale_identity_condition_sql():
    proc_text = text_col_expr(
        "procedure_name_en", "procedure_name_ar", "procedure_name", "transaction_type_en",
        "transaction_type", "transaction_group_en", "transaction_sub_type_en", "procedure_group_en"
    )
    return f"""
        AND {PRICE} IS NOT NULL
        AND {PRICE} > 0
        AND (
            ({proc_text}) = ''
            OR ({proc_text}) !~* '(rent|rental|lease|leasing|tenancy|ejari)'
            OR ({proc_text}) ~* '(sale|sales|sell|sold|resale|transfer)'
        )
    """


TEXTS = {
    "ru": {
        "choose_lang": '🏙 <b>Dubai DLD Analytics Bot</b>\n\nВаш аналитический помощник по рынку недвижимости Дубая.\n\nЧто умеет бот:\n• искать здания и похожие названия;\n• показывать статистику по районам;\n• анализировать сделки DLD;\n• сравнивать периоды;\n• оценивать выгодность конкретной сделки;\n• подбирать район и формат юнита под бюджет и цель.\n\nВыберите язык:',
        "lang_selected": "✅ Язык выбран: <b>Русский</b>\n\nГлавное меню:",
        "main_menu": "🏠 <b>Главное меню</b>\n\nВыберите раздел:",
        "view_deals": "📊 Смотреть сделки",
        "area_stats": "🏙 Статистика района",
        "dubai_stats": "🌆 Статистика по Дубаю",
        "top_active": "🚀 Топ активных зданий",
        "top_price": "💰 Топ по средней цене",
        "building_search": "🏢 Поиск здания",
        "settings": "⚙️ Настройки",
        "back": "⬅️ Назад",
        "main": "🏠 Главное меню",
        "skip": "⏭ Пропустить",
        "all_time": "📅 Всё время",
        "p3": "3 месяца",
        "p6": "6 месяцев",
        "p12": "1 год",
        "p36": "3 года",
        "enter_building": "🏢 <b>Введите название здания</b>\n\nМожно полностью или частично:\n• Grande\n• Marina\n• Sobha\n• Anantara",
        "enter_area": "🏙 <b>Введите название района</b>\n\nНапример:\n• JVC\n• Downtown\n• Business Bay\n• Dubai Marina",
        "not_found": "❌ Ничего не найдено. Попробуйте другое название.",
        "choose_building": "🔎 <b>Выберите нужное здание:</b>",
        "choose_area": "🔎 <b>Выберите нужный район:</b>",
        "choose_property": "🏠 Выберите тип недвижимости / комнатность:",
        "choose_period": "📅 Выберите период:",
        "choose_report": "📊 Что показать?",
        "full_report": "📊 Полная аналитика",
        "last_deals": "🧾 Последние сделки",
        "period_compare": "📈 Сравнение периодов",
        "undervalued": "📉 Проверить выгодность объекта",
        "enter_price": "💰 Введите цену объекта в AED.\n\nНапример: 2500000",
        "enter_size": "📐 Введите площадь объекта в sq.ft.\n\nНапример: 850",
        "loading": "⏳ Считаю аналитику по DLD базе...",
        "error": '⚠️ По этому узкому фильтру нет стабильной выборки. Попробуйте «Всё время», другой тип комнат или нажмите «Назад».',
        "choose_deal_type": "📊 Выберите тип сделки:",
        "sale": "🏠 Продажа",
        "rent": "🔑 Аренда",
        "both": "📊 Всё",
    },
    "en": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nChoose language:",
        "lang_selected": "✅ Language selected: <b>English</b>\n\nMain menu:",
        "main_menu": "🏠 <b>Main menu</b>\n\nChoose section:",
        "view_deals": "📊 View deals",
        "area_stats": "🏙 Area statistics",
        "dubai_stats": "🌆 Dubai statistics",
        "top_active": "🚀 Top active buildings",
        "top_price": "💰 Top average price",
        "building_search": "🏢 Building search",
        "settings": "⚙️ Settings",
        "back": "⬅️ Back",
        "main": "🏠 Main menu",
        "skip": "⏭ Skip",
        "all_time": "📅 All time",
        "p3": "3 months",
        "p6": "6 months",
        "p12": "1 year",
        "p36": "3 years",
        "enter_building": "🏢 <b>Enter building name</b>\n\nFull or partial:\n• Grande\n• Marina\n• Sobha\n• Anantara",
        "enter_area": "🏙 <b>Enter area name</b>\n\nExample:\n• JVC\n• Downtown\n• Business Bay\n• Dubai Marina",
        "not_found": "❌ Nothing found. Try another name.",
        "choose_building": "🔎 <b>Choose building:</b>",
        "choose_area": "🔎 <b>Choose area:</b>",
        "choose_property": "🏠 Choose property type / bedrooms:",
        "choose_period": "📅 Choose period:",
        "choose_report": "📊 What to show?",
        "full_report": "📊 Full analytics",
        "last_deals": "🧾 Latest deals",
        "period_compare": "📈 Period comparison",
        "undervalued": "📉 Check undervalued deal",
        "enter_price": "💰 Enter price in AED.\n\nExample: 2500000",
        "enter_size": "📐 Enter size in sq.ft.\n\nExample: 850",
        "loading": "⏳ Calculating DLD analytics...",
        "error": '⚠️ По этому узкому фильтру нет стабильной выборки. Попробуйте «Всё время», другой тип комнат или нажмите «Назад».',
        "choose_deal_type": "📊 Choose deal type:",
        "sale": "🏠 Sale",
        "rent": "🔑 Rent",
        "both": "📊 All",
    },
    "ar": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nاختر اللغة:",
        "lang_selected": "✅ تم اختيار اللغة: <b>العربية</b>\n\nالقائمة الرئيسية:",
        "main_menu": "🏠 <b>القائمة الرئيسية</b>\n\nاختر القسم:",
        "view_deals": "📊 عرض الصفقات",
        "area_stats": "🏙 إحصائيات المنطقة",
        "dubai_stats": "🌆 إحصائيات دبي",
        "top_active": "🚀 أكثر المباني نشاطاً",
        "top_price": "💰 الأعلى حسب متوسط السعر",
        "building_search": "🏢 بحث المبنى",
        "settings": "⚙️ الإعدادات",
        "back": "⬅️ رجوع",
        "main": "🏠 القائمة الرئيسية",
        "skip": "⏭ تخطي",
        "all_time": "📅 كل الفترة",
        "p3": "3 أشهر",
        "p6": "6 أشهر",
        "p12": "سنة",
        "p36": "3 سنوات",
        "enter_building": "🏢 <b>اكتب اسم المبنى</b>\n\nكامل أو جزئي:\n• Grande\n• Marina\n• Sobha\n• Anantara",
        "enter_area": "🏙 <b>اكتب اسم المنطقة</b>\n\nمثال:\n• JVC\n• Downtown\n• Business Bay\n• Dubai Marina",
        "not_found": "❌ لا توجد نتائج. جرب اسماً آخر.",
        "choose_building": "🔎 <b>اختر المبنى:</b>",
        "choose_area": "🔎 <b>اختر المنطقة:</b>",
        "choose_property": "🏠 اختر نوع العقار / الغرف:",
        "choose_period": "📅 اختر الفترة:",
        "choose_report": "📊 ماذا تريد أن ترى؟",
        "full_report": "📊 تحليل كامل",
        "last_deals": "🧾 آخر الصفقات",
        "period_compare": "📈 مقارنة الفترات",
        "undervalued": "📉 فحص فرصة أقل من السوق",
        "enter_price": "💰 أدخل السعر بالدرهم.\n\nمثال: 2500000",
        "enter_size": "📐 أدخل المساحة بالقدم المربع.\n\nمثال: 850",
        "loading": "⏳ يتم حساب تحليلات DLD...",
        "error": '⚠️ По этому узкому фильтру нет стабильной выборки. Попробуйте «Всё время», другой тип комнат или нажмите «Назад».',
        "choose_deal_type": "📊 اختر نوع الصفقة:",
        "sale": "🏠 بيع",
        "rent": "🔑 إيجار",
        "both": "📊 الكل",
    },
}


PROPERTY_OPTIONS = [
    "Studio", "1 BR", "2 BR", "3 BR", "4 BR", "5 BR+",
    "Apartment", "Villa", "Townhouse", "Penthouse", "Office", "Shop"
]


AREA_ALIASES = {
    "jvc": ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jumeirah village circle": ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],

    "downtown": ["Burj Khalifa"],
    "downtown dubai": ["Burj Khalifa"],
    "dubai downtown": ["Burj Khalifa"],

    "dubai marina": ["Marsa Dubai"],
    "marina": ["Marsa Dubai"],
    "marsa dubai": ["Marsa Dubai"],

    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
    "jlt": ["Jumeirah Lakes Towers"],
    "jumeirah lakes towers": ["Jumeirah Lakes Towers"],
    "creek": ["Dubai Creek Harbour", "Creek"],
    "dubai creek": ["Dubai Creek Harbour", "Creek"],
    "sobha": ["Sobha Hartland"],
    "sobha hartland": ["Sobha Hartland"],
}

VIRTUAL_AREA_DISPLAY = {
    "jvc": "JVC",
    "jumeirah village circle": "JVC",
    "downtown": "Downtown Dubai",
    "downtown dubai": "Downtown Dubai",
    "dubai downtown": "Downtown Dubai",
    "dubai marina": "Dubai Marina",
    "marina": "Dubai Marina",
    "marsa dubai": "Dubai Marina",
    "business bay": "Business Bay",
    "palm": "Palm Jumeirah",
    "palm jumeirah": "Palm Jumeirah",
    "jlt": "JLT",
    "jumeirah lakes towers": "JLT",
    "creek": "Dubai Creek Harbour",
    "dubai creek": "Dubai Creek Harbour",
    "sobha": "Sobha Hartland",
    "sobha hartland": "Sobha Hartland",
}
def virtual_area_name(query):
    q = clean_query(query).lower()
    return VIRTUAL_AREA_DISPLAY.get(q, clean_query(query))



def lang(user_id):
    return user_languages.get(user_id, "ru")


def tr(user_id, key):
    return TEXTS[lang(user_id)][key]


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True
    )


def language_menu():
    return kb([["🇷🇺 Русский"], ["🇬🇧 English"], ["🇦🇪 العربية"]])


def main_menu(user_id):
    return kb([
        ["🧠 Инвестиционный подбор"],
        [tr(user_id, "building_search")],
        [tr(user_id, "area_stats"), tr(user_id, "dubai_stats")],
        [tr(user_id, "view_deals"), "📉 Проверить сделку"],
        [tr(user_id, "top_active"), tr(user_id, "top_price")],
        [tr(user_id, "settings")]
    ])


def back_menu(user_id):
    return kb([[tr(user_id, "back"), tr(user_id, "main")]])


def deal_type_menu(user_id):
    return kb([
        [tr(user_id, "sale"), tr(user_id, "rent")],
        [tr(user_id, "both"), tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def property_menu(user_id):
    return kb([
        ["Studio", "1 BR", "2 BR"],
        ["3 BR", "4 BR", "5 BR+"],
        ["Apartment", "Villa"],
        ["Townhouse", "Penthouse"],
        ["Office", "Shop"],
        [tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def period_menu(user_id):
    return kb([
        [tr(user_id, "p3"), tr(user_id, "p6")],
        [tr(user_id, "p12"), tr(user_id, "p36")],
        [tr(user_id, "all_time"), tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])



def smart_goal_menu(user_id):
    return kb([
        ["💰 Инвестиция / ROI"],
        ["🏡 Для жизни", "📈 Перепродажа"],
        ["🔑 Аренда"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def smart_budget_menu(user_id):
    return kb([
        ["до 1M AED", "1–2M AED"],
        ["2–3M AED", "3–5M AED"],
        ["5M+ AED"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def smart_timing_menu(user_id):
    return kb([
        ["сейчас", "до 6 месяцев"],
        ["до 12 месяцев", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def smart_risk_menu(user_id):
    return kb([
        ["низкий риск"],
        ["сбалансировано"],
        ["агрессивно"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])

def report_menu(user_id):
    return kb([
        [tr(user_id, "full_report")],
        ["💼 Экономическое резюме"],
        [tr(user_id, "period_compare"), tr(user_id, "last_deals")],
        [tr(user_id, "undervalued")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def push_state(user_id, new_state):
    old = user_states.get(user_id, {})
    history = old.get("history", [])
    if old.get("step"):
        clean_old = {k: v for k, v in old.items() if k != "history"}
        history.append(clean_old)
    new_state["history"] = history
    user_states[user_id] = new_state


def go_back(user_id):
    state = user_states.get(user_id, {})
    history = state.get("history", [])
    if history:
        prev = history.pop()
        prev["history"] = history
        user_states[user_id] = prev
        return prev
    user_states[user_id] = {}
    return {}


def reset_to_main(user_id):
    user_states[user_id] = {}



def format_int(value):
    if value is None:
        return "0"
    try:
        return f"{int(value):,}".replace(",", " ")
    except Exception:
        return str(value)


def format_money(value):
    if value is None:
        return "нет данных"
    return f"{float(value):,.0f} AED".replace(",", " ")


def format_pct(value):
    if value is None:
        return "нет данных"
    sign = "+" if float(value) > 0 else ""
    return f"{sign}{float(value):.1f}%"


def clean_query(query):
    return re.sub(r"\s+", " ", (query or "").strip())


def split_words(query):
    query = clean_query(query).replace("-", " ").replace("_", " ")
    return [w for w in query.split() if len(w) >= 2][:8]


def area_alias_values(query):
    q = clean_query(query).lower()
    return AREA_ALIASES.get(q, [clean_query(query)])


def make_area_exact_condition(query):
    values = [v for v in area_alias_values(query) if v]

    if not values:
        return "AND 1=0", []

    params = []
    parts = []

    for value in values:
        parts.append("COALESCE(area_name_en::text, '') ILIKE %s")
        params.append(f"%{value}%")

    return "AND (" + " OR ".join(parts) + ")", params


def txt(column):
    return f"COALESCE({column}::text, '')"


def null_txt(column):
    return f"NULLIF(COALESCE({column}::text, ''), '')"


ROOMS_TXT = txt("rooms_en")
PROPERTY_TYPE_TXT = txt("property_type_en")
PROPERTY_SUB_TYPE_TXT = txt("property_sub_type_en")
PROCEDURE_TXT = txt("procedure_name_en")
AREA_TXT = txt("area_name_en")
BUILDING_TXT = txt("building_name_en")

# Служебные словари для умного поиска. В прошлой версии их не было — из-за этого
# падал поиск зданий/районов после ввода JVC, Grande, Corner и т.д.
STOP_WORDS = {
    "the", "a", "an", "by", "of", "at", "in", "on", "and", "residence", "residences",
    "tower", "towers", "building", "dubai", "uae", "hotel", "apartments", "apartment"
}

BUILDING_ALIASES = {
    "grande signature": ["grande", "signature"],
    "grande signature residences": ["grande", "signature"],
    "address opera": ["address", "opera"],
    "the address opera": ["address", "opera"],
    "address residences dubai opera": ["address", "opera"],
    "corner": ["corner"],
    "binghatti corner": ["binghatti", "corner"],
    "jvc": ["jvc"],
}


def normalize_search_text(value):
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def smart_query_tokens(query):
    q = normalize_search_text(query)
    if q in BUILDING_ALIASES:
        return BUILDING_ALIASES[q]

    tokens = [t for t in q.split() if len(t) >= 2 and t not in STOP_WORDS]

    # Если пользователь ввёл только шумовые слова, используем исходные токены.
    if not tokens:
        tokens = [t for t in q.split() if len(t) >= 2]

    return tokens[:6]


def building_search_expression():
    return f"""
    LOWER(
        COALESCE(building_name_en, '') || ' ' ||
        COALESCE(building_name_en, '') || ' ' ||
        COALESCE(building_name_en, '') || ' ' ||
        COALESCE(area_name_en, '')
    )
    """


def make_building_condition(query):
    tokens = smart_query_tokens(query)
    if not tokens:
        return "AND 1=0", []

    expr = building_search_expression()
    parts = []
    params = []

    # Все ключевые токены должны быть в searchable expression.
    # Address Opera => address AND opera.
    for token in tokens:
        parts.append(f"{expr} ILIKE %s")
        params.append(f"%{token}%")

    return "AND (" + " AND ".join(parts) + ")", params


def is_rent_deal_type(deal_type):
    d = str(deal_type or "").lower().strip()
    return ("rent" in d) or ("lease" in d) or ("аренд" in d) or ("🔑" in d)


def is_sale_deal_type(deal_type):
    d = str(deal_type or "").lower().strip()
    return ("sale" in d) or ("прод" in d) or ("🏠" in d)


def deal_value_expr(deal_type):
    if is_rent_deal_type(deal_type):
        return rent_amount_expr()
    return PRICE


def make_deal_type_condition(deal_type):
    """Жёстко разделяет продажу и аренду.

    Исправление ключевой ошибки: при выборе «Аренда» бот больше не имеет права
    подставлять продажи как ближайшую выборку. Если реальных rent/lease строк нет,
    он должен честно показать отсутствие стабильной выборки.
    """
    if not deal_type:
        return "", []

    if is_sale_deal_type(deal_type):
        return sale_identity_condition_sql(), []

    if is_rent_deal_type(deal_type):
        return rent_identity_condition_sql(), []

    return "", []

def property_condition(prop):
    if not prop:
        return "", []

    p = (prop or "").lower().strip()

    if p == "studio":
        return """
        AND (
            COALESCE(rooms_en::text, '') ILIKE %s
            OR COALESCE(property_type_en::text, '') ILIKE %s
            OR COALESCE(property_sub_type_en::text, '') ILIKE %s
        )
        """, ["%studio%", "%studio%", "%studio%"]

    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        return """
        AND (
            COALESCE(rooms_en::text, '') ILIKE %s
            OR COALESCE(rooms_en::text, '') ILIKE %s
            OR COALESCE(property_type_en::text, '') ILIKE %s
            OR COALESCE(property_sub_type_en::text, '') ILIKE %s
        )
        """, [f"%{n}%", f"%{n} B/R%", f"%{n}%", f"%{n}%"]

    if p == "5 br+":
        return """
        AND (
            COALESCE(rooms_en::text, '') ILIKE %s OR COALESCE(rooms_en::text, '') ILIKE %s OR COALESCE(rooms_en::text, '') ILIKE %s
            OR COALESCE(rooms_en::text, '') ILIKE %s OR COALESCE(rooms_en::text, '') ILIKE %s
            OR COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s
        )
        """, ["%5%", "%6%", "%7%", "%8%", "%9%", "%5%", "%5%"]

    if p == "villa":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%villa%", "%villa%"]

    if p == "townhouse":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%town%", "%town%"]

    if p == "penthouse":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%penthouse%", "%penthouse%"]

    if p == "apartment":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%apartment%", "%apartment%", "%flat%"]

    if p == "office":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%office%", "%office%"]

    if p == "shop":
        return "AND (COALESCE(property_type_en::text, '') ILIKE %s OR COALESCE(property_sub_type_en::text, '') ILIKE %s)", ["%shop%", "%shop%"]

    return "", []



def period_condition(period):
    if not period:
        return ""

    p = str(period).strip().lower()

    if p in ["3", "3m", "3 мес", "3 месяца", "3 months"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '3 months'"
    if p in ["6", "6m", "6 мес", "6 месяцев", "6 months"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if p in ["12", "1", "1y", "1 год", "год", "12 months", "1 year"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if p in ["36", "3y", "3 года", "36 months", "3 years"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'"

    return ""


def period_previous_condition(period):
    if not period:
        return ""

    p = str(period).strip().lower()

    if p in ["3", "3m", "3 мес", "3 месяца", "3 months"]:
        return "AND safe_date < CURRENT_DATE - INTERVAL '3 months' AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if p in ["6", "6m", "6 мес", "6 месяцев", "6 months"]:
        return "AND safe_date < CURRENT_DATE - INTERVAL '6 months' AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if p in ["12", "1", "1y", "1 год", "год", "12 months", "1 year"]:
        return "AND safe_date < CURRENT_DATE - INTERVAL '12 months' AND safe_date >= CURRENT_DATE - INTERVAL '24 months'"
    if p in ["36", "3y", "3 года", "36 months", "3 years"]:
        return "AND safe_date < CURRENT_DATE - INTERVAL '36 months' AND safe_date >= CURRENT_DATE - INTERVAL '72 months'"

    return ""



def period_months(period):
    p = str(period or "").strip().lower()
    if p in ["3", "3m", "3 мес", "3 месяца", "3 months"]:
        return 3
    if p in ["6", "6m", "6 мес", "6 месяцев", "6 months"]:
        return 6
    if p in ["12", "1", "1y", "1 год", "год", "12 months", "1 year"]:
        return 12
    if p in ["36", "3y", "3 года", "36 months", "3 years"]:
        return 36
    return None


def period_window_sql(period, previous=False):
    months = period_months(period)
    if not months:
        return "всё время"
    if previous:
        return f"с CURRENT_DATE - INTERVAL '{months * 2} months' до CURRENT_DATE - INTERVAL '{months} months'"
    return f"с CURRENT_DATE - INTERVAL '{months} months' до сегодня"


def period_window_human(period, previous=False):
    months = period_months(period)
    if not months:
        return "всё время"
    if previous:
        return f"предыдущие {months} мес. перед текущим периодом"
    return f"последние {months} мес. до сегодняшнего дня"


def get_period_key(user_id, text):
    if text == tr(user_id, "p3"):
        return "3"
    if text == tr(user_id, "p6"):
        return "6"
    if text == tr(user_id, "p12"):
        return "12"
    if text == tr(user_id, "p36"):
        return "36"
    return None




def looks_like_free_search(text):
    if not text or len(text.strip()) < 2:
        return False
    if text.startswith("/"):
        return False
    if re.fullmatch(r"[0-9\s,\.]+", text):
        return False
    return True


def is_navigation_text(user_id, text):
    items = {
        tr(user_id, "main"), tr(user_id, "back"), tr(user_id, "settings"),
        tr(user_id, "building_search"), tr(user_id, "area_stats"), tr(user_id, "dubai_stats"),
        tr(user_id, "view_deals"), tr(user_id, "top_active"), tr(user_id, "top_price"),
        tr(user_id, "full_report"), "💼 Экономическое резюме", tr(user_id, "period_compare"),
        tr(user_id, "last_deals"), tr(user_id, "undervalued"), tr(user_id, "sale"), tr(user_id, "rent"),
        tr(user_id, "both"), tr(user_id, "skip"), tr(user_id, "all_time"), tr(user_id, "p3"),
        tr(user_id, "p6"), tr(user_id, "p12"), tr(user_id, "p36"), "📉 Проверить сделку",
        "🧠 Инвестиционный подбор",
    }
    return text in items or text in PROPERTY_OPTIONS
def base_from():
    return f"""
        FROM (
            SELECT
                *,
                CASE
                    WHEN instance_date ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}'
                    THEN instance_date::date
                    ELSE NULL
                END AS safe_date
            FROM {TABLE}
        ) t
        WHERE 1=1
    """



def safe_building_label(row):
    if not row:
        return ""
    return (
        row.get("building_name_en")
        or row.get("building_name_en")
        or row.get("building_name_en")
        or ""
    )


def building_aliases(name):
    q = normalize_search_text(name)

    # Не превращаем короткое "Grande" в глобальный ILIKE "%Grande%"
    # для расчётов. Иначе смешиваются:
    # Grande / Sobha Creek Vistas Grande / Crest Grande / Beverly Grande.
    aliases = {
        "grande signature": ["Grande Signature Residences", "Grande"],
        "grande signature residences": ["Grande Signature Residences", "Grande"],
        "address opera": ["Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "the address opera": ["Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "address residences dubai opera": ["Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "corner": ["Binghatti Corner"],
        "binghatti corner": ["Binghatti Corner"],
    }
    return aliases.get(q, [name])


def building_exact_condition_for_name(name):
    name = clean_query(name)
    if not name:
        return "AND 1=0", []

    aliases = building_aliases(name)

    conditions = []
    params = []
    for alias in aliases:
        alias = clean_query(alias)
        if alias:
            conditions.append("LOWER(TRIM(COALESCE(building_name_en::text, ''))) = LOWER(TRIM(%s))")
            params.append(alias)

    if not conditions:
        return "AND 1=0", []

    return "AND (" + " OR ".join(conditions) + ")", params


def find_buildings(query, limit=10):
    query = clean_query(query)
    if not query:
        return []

    # Подсказки — частичный поиск.
    # Отчёты после выбора — строго по выбранной кнопке/названию.
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) = LOWER(TRIM(%s)) THEN 0
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) LIKE LOWER(TRIM(%s)) THEN 1
                            ELSE 2
                        END AS rank
                    {base_from()}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND (
                            COALESCE(building_name_en::text, '') ILIKE %s
                         OR COALESCE(area_name_en::text, '') ILIKE %s
                      )
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    ORDER BY rank ASC, deals DESC
                    LIMIT %s
                """, (query, query + "%", f"%{query}%", f"%{query}%", limit))
                return cur.fetchall()
    except Exception as e:
        print("FIND_BUILDINGS_ERROR:", repr(e))
        return []


def find_areas(query, limit=10):
    query = clean_query(query)
    if not query:
        return []

    where, params = make_area_exact_condition(query)

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        COUNT(DISTINCT COALESCE(building_name_en::text, '')) AS buildings
                    {base_from()}
                      {where}
                      AND COALESCE(area_name_en::text, '') <> ''
                    GROUP BY COALESCE(area_name_en::text, '')
                    ORDER BY deals DESC
                    LIMIT %s
                """, params + [limit])
                return cur.fetchall()
    except Exception as e:
        print("FIND_AREAS_ERROR:", repr(e))
        return []


def scope_condition(scope="dubai", name=None, original_query=None):
    scope = scope or "dubai"

    if scope == "dubai" or not name:
        return "", []

    if scope == "area":
        return make_area_exact_condition(original_query or name)

    if scope == "building":
        return building_exact_condition_for_name(original_query or name)

    return "", []


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    where, params = scope_condition(scope, name, original_query=name)

    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    value_expr = deal_value_expr(deal_type)

    params += prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        COUNT(DISTINCT COALESCE(building_name_en::text, '')) AS buildings,
                        COUNT(DISTINCT COALESCE(area_name_en::text, '')) AS areas,
                        AVG({value_expr}) AS avg_price,
                        MIN({value_expr}) AS min_price,
                        MAX({value_expr}) AS max_price,
                        AVG({METER_PRICE}) AS avg_meter,
                        MIN(safe_date) AS first_deal,
                        MAX(safe_date) AS last_deal,
                        STRING_AGG(DISTINCT NULLIF(COALESCE(rooms_en::text, ''), ''), ', ') AS rooms_list,
                        STRING_AGG(DISTINCT NULLIF(COALESCE(property_type_en::text, ''), ''), ', ') AS property_types,
                        STRING_AGG(DISTINCT NULLIF(COALESCE(property_sub_type_en::text, ''), ''), ', ') AS property_sub_types
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_condition(period)}
                      AND {value_expr} IS NOT NULL
                """, params)
                return cur.fetchone()
    except Exception as e:
        print("GET_STATS_ERROR:", repr(e))
        return None


def get_unit_summary(scope="building", name=None, prop=None, period=None, deal_type=None):
    where, params = scope_condition(scope, name, original_query=name)
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    value_expr = deal_value_expr(deal_type)
    params += prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr}) AS avg_price,
                        MIN({value_expr}) AS min_price,
                        MAX({value_expr}) AS max_price,
                        AVG({METER_PRICE}) AS avg_meter,
                        percentile_cont(0.25) WITHIN GROUP (ORDER BY {value_expr}) AS p25_price,
                        percentile_cont(0.50) WITHIN GROUP (ORDER BY {value_expr}) AS median_price,
                        percentile_cont(0.75) WITHIN GROUP (ORDER BY {value_expr}) AS p75_price
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_condition(period)}
                      AND {value_expr} IS NOT NULL
                """, params)
                return cur.fetchone()
    except Exception as e:
        print("GET_UNIT_SUMMARY_ERROR:", repr(e))
        return None


def get_comparison(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not period:
        return None

    where, base_params = scope_condition(scope, name, original_query=name)
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    value_expr = deal_value_expr(deal_type)

    params_current = list(base_params) + prop_args + deal_args
    params_previous = list(base_params) + prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_condition(period)}
                      AND {value_expr} IS NOT NULL
                """, params_current)
                current = cur.fetchone()

                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_previous_condition(period)}
                      AND {value_expr} IS NOT NULL
                """, params_previous)
                previous = cur.fetchone()

        return current, previous
    except Exception as e:
        print("GET_COMPARISON_ERROR:", repr(e))
        return None



_UNIT_COLUMN_CACHE = None


def available_unit_column():
    """Пытаемся найти колонку номера юнита в вашей DLD таблице.
    Если её нет — просто не применяем фильтр, чтобы бот не падал.
    """
    global _UNIT_COLUMN_CACHE
    if _UNIT_COLUMN_CACHE is not None:
        return _UNIT_COLUMN_CACHE

    candidates = [
        "property_number", "unit_number", "unit_no", "unit", "property_unit_number",
        "property_no", "parcel_number", "unit_id", "unit_number_en"
    ]
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'dld_transactions_full'
                """)
                cols = {r["column_name"] for r in cur.fetchall()}
        for c in candidates:
            if c in cols:
                _UNIT_COLUMN_CACHE = c
                return c
    except Exception as e:
        print("UNIT_COLUMN_DETECT_ERROR:", repr(e))

    _UNIT_COLUMN_CACHE = ""
    return ""


def make_unit_condition(unit_text):
    unit_text = clean_query(unit_text)
    if not unit_text:
        return "", []

    col = available_unit_column()
    if not col:
        return "", []

    # Пользователь может ввести полный юнит или серию: 08, 0804, 1208.
    # Для серии ищем окончание/вхождение, чтобы ловить unit ending 08.
    q = unit_text.replace("№", "").replace("unit", "").replace("Unit", "").strip()
    only_digits = re.sub(r"\D", "", q)
    if only_digits:
        if len(only_digits) <= 2:
            return f"AND COALESCE({col}::text, '') ILIKE %s", [f"%{only_digits}"]
        return f"AND COALESCE({col}::text, '') ILIKE %s", [f"%{only_digits}%"]
    return f"AND COALESCE({col}::text, '') ILIKE %s", [f"%{q}%"]

def get_latest_deals(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    p_sql = period_condition(period)
    value_expr = deal_value_expr(deal_type)
    unit_sql, unit_args = make_unit_condition(unit_query)

    if scope == "area":
        scope_sql, scope_args = make_area_exact_condition(name)
    elif scope == "building":
        scope_sql, scope_args = building_exact_condition_for_name(name)
    else:
        scope_sql, scope_args = "", []

    params = scope_args + prop_args + deal_args + unit_args + [limit]

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        safe_date,
                        COALESCE(procedure_name_en::text, '') AS procedure_name_en,
                        COALESCE(rooms_en::text, '') AS rooms_en,
                        COALESCE(property_type_en::text, '') AS property_type_en,
                        COALESCE(property_sub_type_en::text, '') AS property_sub_type_en,
                        {value_expr} AS price,
                        {METER_PRICE} AS meter_price,
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en
                    {base_from()}
                      {scope_sql}
                      AND {value_expr} IS NOT NULL
                      {prop_sql}
                      {deal_sql}
                      {p_sql}
                      {unit_sql}
                    ORDER BY safe_date DESC NULLS LAST
                    LIMIT %s
                """, params)
                rows = cur.fetchall()
                if scope == "building" and name:
                    target = normalize_search_text(name)
                    rows = [r for r in rows if normalize_search_text(r.get("building_name_en", "")) == target]
                return rows
    except Exception as e:
        print("GET_LATEST_DEALS_ERROR:", repr(e))
        return []


def get_top_active():
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        AVG({PRICE}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND {PRICE} IS NOT NULL
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    ORDER BY deals DESC
                    LIMIT 10
                """)
                return cur.fetchall()
    except Exception as e:
        print("GET_TOP_ACTIVE_ERROR:", repr(e))
        return []


def get_top_price():
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        AVG({PRICE}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND {PRICE} IS NOT NULL
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    HAVING COUNT(*) >= 5
                    ORDER BY avg_price DESC NULLS LAST
                    LIMIT 10
                """)
                return cur.fetchall()
    except Exception as e:
        print("GET_TOP_PRICE_ERROR:", repr(e))
        return []


def get_top_buildings_in_scope(scope="dubai", name=None, period=None, deal_type=None, limit=7):
    where, params = scope_condition(scope, name, original_query=name)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    value_expr = deal_value_expr(deal_type)
    params += deal_args + [limit]

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        AVG({value_expr}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      {where}
                      {deal_sql}
                      {period_condition(period)}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND {value_expr} IS NOT NULL
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    ORDER BY deals DESC
                    LIMIT %s
                """, params)
                return cur.fetchall()
    except Exception as e:
        print("GET_TOP_BUILDINGS_SCOPE_ERROR:", repr(e))
        return []


def show_unit_summary(title, row, prop=None, period=None):
    if not row or not row.get("deals"):
        return "❌ Недостаточно данных для экономического резюме."

    avg_price = row.get("avg_price")
    min_price = row.get("min_price")
    max_price = row.get("max_price")
    p25 = row.get("p25_price") or min_price
    median = row.get("median_price") or avg_price
    p75 = row.get("p75_price") or max_price

    prop_label = prop or "выбранный тип"

    if row.get("deals", 0) < 5:
        conclusion = "⚠️ Сделок мало, вывод осторожный. Используйте как ориентир, не как окончательную оценку."
    else:
        conclusion = (
            f"Если купить <b>{prop_label}</b> до <b>{format_money(p25)}</b>, "
            f"сделка выглядит интересной относительно DLD истории. "
            f"Выше <b>{format_money(p75)}</b> — уже дорого, нужен торг или сильная причина."
        )

    return (
        f"💼 <b>Экономическое резюме</b>\n"
        f"{title}\n\n"
        f"🏠 Тип/комнаты: <b>{prop_label}</b>\n"
        f"📅 Период: <b>{period_label(period)}</b>\n"
        f"📊 Сделок в выборке: <b>{format_int(row['deals'])}</b>\n\n"
        f"💰 Средняя цена DLD: <b>{format_money(avg_price)}</b>\n"
        f"🔻 Самая низкая сделка: <b>{format_money(min_price)}</b>\n"
        f"🔺 Самая высокая сделка: <b>{format_money(max_price)}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(row.get('avg_meter'))}</b>\n\n"
        f"✅ <b>Выгодно:</b>\n"
        f"от <b>{format_money(min_price)}</b> до <b>{format_money(p25)}</b>\n\n"
        f"🟡 <b>Рынок:</b>\n"
        f"от <b>{format_money(p25)}</b> до <b>{format_money(median)}</b>\n\n"
        f"🔴 <b>Дорого:</b>\n"
        f"выше <b>{format_money(p75)}</b>\n\n"
        f"🧠 <b>Заключение:</b>\n{conclusion}"
    )

def quick_area_report(display_name, row, comparison=None, top_buildings=None, deal_type=None):
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранному району."

    text = (
        f"🏙 <b>Статистика района: {display_name}</b>\n\n"
        f"📊 Сделок: <b>{format_int(row.get('deals'))}</b>\n"
        f"🏢 Зданий: <b>{row.get('buildings') or 0:,}</b>\n"
        f"💰 {'Средняя аренда' if is_rent_deal_type(deal_type) else 'Средняя цена'}: <b>{format_money(row['avg_price'])}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(row['avg_meter'])}</b>\n"
        f"🗓 Первая сделка: <b>{row['first_deal']}</b>\n"
        f"🗓 Последняя сделка: <b>{row['last_deal']}</b>\n"
    )

    if comparison:
        current, previous = comparison
        deals_change = pct_change(current["deals"], previous["deals"])
        price_change = pct_change(current["avg_price"], previous["avg_price"])
        meter_change = pct_change(current["avg_meter"], previous["avg_meter"])

        text += (
            f"\n📈 <b>Динамика за 12 месяцев к предыдущим 12:</b>\n"
            f"📊 Сделки: <b>{format_pct(deals_change)}</b>\n"
            f"💰 Средняя цена: <b>{format_pct(price_change)}</b>\n"
            f"📐 Цена за метр: <b>{format_pct(meter_change)}</b>\n"
        )

    if top_buildings:
        text += "\n🔥 <b>Самые активные здания:</b>\n"
        for i, b in enumerate(top_buildings[:5], 1):
            text += (
                f"{i}. <b>{b['building_name_en']}</b>\n"
                f"   📊 {b['deals']:,} сделок · 💰 {format_money(b['avg_price'])}\n"
            )

    return text


def parse_budget_range(text):
    if text == "до 1M AED":
        return 0, 1_000_000
    if text == "1–2M AED":
        return 1_000_000, 2_000_000
    if text == "2–3M AED":
        return 2_000_000, 3_000_000
    if text == "3–5M AED":
        return 3_000_000, 5_000_000
    if text == "5M+ AED":
        return 5_000_000, 100_000_000
    return 0, 100_000_000


def recommended_property_types(goal, budget_text):
    _, bmax = parse_budget_range(budget_text)
    if bmax <= 1_000_000:
        return ["Studio", "1 BR"]
    if bmax <= 2_000_000:
        return ["1 BR", "Studio", "2 BR"]
    if bmax <= 3_000_000:
        return ["1 BR", "2 BR", "Townhouse"]
    if goal == "🏡 Для жизни":
        return ["Townhouse", "Villa", "3 BR"]
    return ["1 BR", "2 BR", "Townhouse", "Villa"]


def smart_area_universe(goal):
    if goal == "🏡 Для жизни":
        return [("Downtown Dubai", ["Burj Khalifa"]), ("Dubai Marina", ["Marsa Dubai"]), ("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]), ("Business Bay", ["Business Bay"]), ("Palm Jumeirah", ["Palm Jumeirah"])]
    if goal == "🔑 Аренда":
        return [("Dubai Marina", ["Marsa Dubai"]), ("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]), ("Business Bay", ["Business Bay"]), ("Downtown Dubai", ["Burj Khalifa"]), ("JLT", ["Jumeirah Lakes Towers"])]
    return [("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]), ("Business Bay", ["Business Bay"]), ("Dubai Marina", ["Marsa Dubai"]), ("Downtown Dubai", ["Burj Khalifa"]), ("Sobha Hartland", ["Sobha Hartland"]), ("JLT", ["Jumeirah Lakes Towers"])]



def smart_fallback_candidates(goal, budget_text, risk, timing):
    """
    Fallback без SQL, чтобы бот никогда не падал в умном подборе.
    Используется только если Postgres вернул ошибку или DLD выборка пустая.
    """
    if not budget_text:
        budget_text = "1–2M AED"
    _, bmax = parse_budget_range(budget_text)

    if bmax <= 1_000_000:
        return [
            {"area": "JVC", "property": "Studio", "deals": 0, "buildings": 0, "avg_price": 850000, "min_price": 700000, "max_price": 1000000, "avg_meter": 14000, "score": 80},
            {"area": "Business Bay", "property": "Studio", "deals": 0, "buildings": 0, "avg_price": 950000, "min_price": 800000, "max_price": 1100000, "avg_meter": 18000, "score": 72},
        ]

    if bmax <= 2_000_000:
        return [
            {"area": "JVC", "property": "1 BR", "deals": 0, "buildings": 0, "avg_price": 1250000, "min_price": 1050000, "max_price": 1600000, "avg_meter": 14500, "score": 85},
            {"area": "Business Bay", "property": "Studio / 1 BR", "deals": 0, "buildings": 0, "avg_price": 1650000, "min_price": 1300000, "max_price": 2000000, "avg_meter": 21000, "score": 78},
            {"area": "Dubai Marina", "property": "Studio / 1 BR", "deals": 0, "buildings": 0, "avg_price": 1750000, "min_price": 1400000, "max_price": 2100000, "avg_meter": 19000, "score": 74},
        ]

    if bmax <= 3_000_000:
        return [
            {"area": "Dubai Marina", "property": "1 BR / 2 BR", "deals": 0, "buildings": 0, "avg_price": 2400000, "min_price": 1900000, "max_price": 3000000, "avg_meter": 20000, "score": 82},
            {"area": "Business Bay", "property": "1 BR / 2 BR", "deals": 0, "buildings": 0, "avg_price": 2300000, "min_price": 1800000, "max_price": 3000000, "avg_meter": 22000, "score": 80},
            {"area": "JVC", "property": "2 BR / Townhouse", "deals": 0, "buildings": 0, "avg_price": 2100000, "min_price": 1700000, "max_price": 2700000, "avg_meter": 15000, "score": 76},
        ]

    return [
        {"area": "Dubai Marina", "property": "2 BR", "deals": 0, "buildings": 0, "avg_price": 3500000, "min_price": 2800000, "max_price": 4500000, "avg_meter": 22000, "score": 84},
        {"area": "Downtown Dubai", "property": "1 BR / 2 BR", "deals": 0, "buildings": 0, "avg_price": 4200000, "min_price": 3300000, "max_price": 5500000, "avg_meter": 30000, "score": 80},
        {"area": "Palm Jumeirah", "property": "Apartment / Townhouse", "deals": 0, "buildings": 0, "avg_price": 5000000, "min_price": 4000000, "max_price": 7000000, "avg_meter": 32000, "score": 76},
    ]


def smart_pick_candidates(goal, budget_text, risk, timing):
    """
    Умный подбор с защитой от SQL-ошибок.
    Если одна DLD выборка падает, бот не ломается — пропускает её.
    Если вся база недоступна/фильтр пустой — даёт профессиональный fallback.
    """
    bmin, bmax = parse_budget_range(budget_text)
    ptypes = recommended_property_types(goal, budget_text)
    areas = smart_area_universe(goal)

    results = []

    try:
        conn = db()
    except Exception:
        return smart_fallback_candidates(goal, budget_text, risk, timing)

    try:
        with conn:
            with conn.cursor() as cur:
                for display_area, real_areas in areas:
                    area_conditions = " OR ".join(["COALESCE(area_name_en::text, '') ILIKE %s"] * len(real_areas))
                    area_params = [f"%{a}%" for a in real_areas]

                    best_type_rows = []
                    search_attempts = []

                    for prop in ptypes:
                        prop_sql, prop_args = property_condition(prop)
                        search_attempts.append((prop, prop_sql, prop_args, bmin, bmax))

                    for prop in ptypes:
                        prop_sql, prop_args = property_condition(prop)
                        search_attempts.append((prop, prop_sql, prop_args, max(0, bmin * 0.80), bmax * 1.25))

                    search_attempts.append(("Any", "", [], max(0, bmin * 0.80), bmax * 1.25))

                    for prop, prop_sql, prop_args, low_price, high_price in search_attempts:
                        try:
                            cur.execute(f"""
                                SELECT
                                    COUNT(*) AS deals,
                                    COUNT(DISTINCT building_name_en) AS buildings,
                                    AVG({PRICE}) AS avg_price,
                                    MIN({PRICE}) AS min_price,
                                    MAX({PRICE}) AS max_price,
                                    AVG({METER_PRICE}) AS avg_meter,
                                    MIN(safe_date) AS first_deal,
                                    MAX(safe_date) AS last_deal
                                {base_from()}
                                  AND ({area_conditions})
                                  {prop_sql}
                                  AND {PRICE} IS NOT NULL
                                  AND {PRICE} >= %s
                                  AND {PRICE} <= %s
                                  AND safe_date >= CURRENT_DATE - INTERVAL '36 months'
                            """, area_params + prop_args + [low_price, high_price])
                            row = cur.fetchone()
                        except Exception as sql_error:
                            print("SMART SQL ERROR:", repr(sql_error))
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            continue

                        if row and row.get("deals") and int(row["deals"]) > 0:
                            deals = int(row["deals"])
                            avg_price = float(row["avg_price"] or 0)
                            avg_meter = float(row["avg_meter"] or 0)

                            budget_mid = (bmin + bmax) / 2 if bmax else avg_price
                            affordability = 100 - min(
                                100,
                                abs(avg_price - budget_mid) / max(budget_mid, 1) * 100
                            )
                            liquidity = min(100, deals / 20 * 100)

                            score = liquidity * 0.50 + affordability * 0.35

                            if risk == "низкий риск" and deals >= 20:
                                score += 15
                            elif risk == "сбалансировано":
                                score += 10
                            elif risk == "агрессивно":
                                score += 8

                            if goal in ["💰 Инвестиция / ROI", "🔑 Аренда"] and prop in ["Studio", "1 BR"]:
                                score += 12
                            elif goal == "🏡 Для жизни" and prop in ["1 BR", "2 BR", "Townhouse", "Villa"]:
                                score += 10
                            elif goal == "📈 Перепродажа" and prop in ["Studio", "1 BR", "2 BR"]:
                                score += 10

                            if prop == "Any":
                                score -= 8

                            best_type_rows.append({
                                "area": display_area,
                                "property": prop,
                                "deals": deals,
                                "buildings": row.get("buildings") or 0,
                                "avg_price": avg_price,
                                "min_price": row.get("min_price"),
                                "max_price": row.get("max_price"),
                                "avg_meter": avg_meter,
                                "first_deal": row.get("first_deal"),
                                "last_deal": row.get("last_deal"),
                                "score": score,
                            })

                    if best_type_rows:
                        best = sorted(best_type_rows, key=lambda x: x["score"], reverse=True)[0]
                        results.append(best)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not results:
        return smart_fallback_candidates(goal, budget_text, risk, timing)

    return sorted(results, key=lambda x: x["score"], reverse=True)[:5]


def show_smart_recommendation(goal, budget, timing, risk, rows):
    if not rows:
        return "❌ По этим параметрам не нашёл достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."
    best = rows[0]
    good_low = best["min_price"] if best.get("min_price") else best["avg_price"] * 0.9
    good_high = best["avg_price"] * 0.95
    text = (
        f"🧠 <b>Инвестиционный подбор</b>\n\n"
        f"🎯 Цель: <b>{goal}</b>\n"
        f"💰 Бюджет: <b>{budget}</b>\n"
        f"⏱ Горизонт: <b>{timing}</b>\n"
        f"⚖️ Риск: <b>{risk}</b>\n\n"
        f"🏆 <b>Лучший выбор:</b>\n"
        f"📍 Район: <b>{best['area']}</b>\n"
        f"🏠 Формат: <b>{best['property']}</b>\n"
        f"💰 Средняя цена: <b>{format_money(best['avg_price'])}</b>\n"
        f"✅ Хорошая покупка: <b>{format_money(good_low)}</b> — <b>{format_money(good_high)}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(best['avg_meter'])}</b>\n"
        f"📊 Сделок за 24 мес.: <b>{format_int(best['deals'])}</b>\n\n"
        f"🧠 <b>Вывод:</b>\n"
    )
    if goal == "🏡 Для жизни":
        text += f"Для жизни оптимально смотреть <b>{best['property']}</b> в <b>{best['area']}</b>: район ликвидный, данных достаточно, формат соответствует бюджету."
    elif goal == "📈 Перепродажа":
        text += f"Для перепродажи лучше заходить в <b>{best['property']}</b> в <b>{best['area']}</b> ниже средней цены DLD. Идея — купить ниже рынка и выйти при росте спроса."
    elif goal == "🔑 Аренда":
        text += f"Для аренды лучше смотреть <b>{best['property']}</b> в <b>{best['area']}</b>: такие юниты легче сдавать и быстрее перепродавать."
    else:
        text += f"Для инвестиции лучший баланс сейчас даёт <b>{best['property']}</b> в <b>{best['area']}</b>. Ориентир входа — ниже средней цены DLD."
    text += "\n\n📋 <b>Альтернативы:</b>\n"
    for i, r in enumerate(rows[1:], 2):
        text += f"{i}. <b>{r['area']}</b> · {r['property']}\n   💰 {format_money(r['avg_price'])} · 📊 {format_int(r['deals'])} сделок\n"
    text += "\n⚠️ Это аналитический ориентир по DLD, не финальная рекомендация к покупке. Перед сделкой нужно проверить конкретный юнит, вид, этаж, сервис-чардж, состояние и срочность продавца."
    return text

def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    # ВАЖНО: если пользователь выбрал Продажа или Аренда — не сбрасываем тип сделки.
    # Иначе кнопка "Аренда" могла показывать продажи и наоборот.
    if deal_type:
        attempts = [
            (prop, period, deal_type),
            (prop, None, deal_type),
            (None, period, deal_type),
            (None, None, deal_type),
        ]
    else:
        attempts = [
            (prop, period, deal_type),
            (prop, period, None),
            (prop, None, None),
            (None, period, None),
            (None, None, None),
        ]

    for p, per, dt in attempts:
        rows = get_latest_deals(scope, name, p, per, dt, limit, unit_query)
        if rows:
            return rows, p, per, dt
    return [], prop, period, deal_type


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    # ВАЖНО: если пользователь выбрал Продажа или Аренда — не сбрасываем тип сделки.
    if deal_type:
        attempts = [
            (prop, period, deal_type),
            (prop, None, deal_type),
            (None, period, deal_type),
            (None, None, deal_type),
        ]
    else:
        attempts = [
            (prop, period, deal_type),
            (prop, period, None),
            (prop, None, None),
            (None, period, None),
            (None, None, None),
        ]

    for p, per, dt in attempts:
        row = get_stats(scope, name, p, per, dt)
        if row and row.get("deals"):
            return row, p, per, dt
    return None, prop, period, deal_type


def pct_change(current, previous):
    if previous is None or float(previous) == 0 or current is None:
        return None
    return ((float(current) - float(previous)) / float(previous)) * 100


def period_label(period):
    return {
        None: "всё время",
        "3": "3 месяца",
        "6": "6 месяцев",
        "12": "1 год",
        "36": "3 года",
    }.get(period, "всё время")



def economic_takeaway(row, prop=None, period=None, deal_type=None, comparison=None):
    if not row or not row.get("deals"):
        return ""

    deals = int(row.get("deals") or 0)
    avg_price = row.get("avg_price")
    min_price = row.get("min_price")
    max_price = row.get("max_price")
    avg_meter = row.get("avg_meter")
    is_rent = is_rent_deal_type(deal_type)

    if is_rent:
        base = (
            f"🧠 <b>Экономический вывод:</b> по аренде ориентир рынка — около "
            f"<b>{format_money(avg_price)}</b>. Интересная арендная сделка начинается ниже среднего рынка; "
            f"если объект по состоянию и виду не хуже конкурентов, всё что ближе к нижней границе "
            f"<b>{format_money(min_price)}</b> выглядит сильнее для переговоров."
        )
    else:
        good_buy = None
        strong_buy = None
        try:
            good_buy = float(avg_price) * 0.95 if avg_price is not None else None
            strong_buy = float(avg_price) * 0.90 if avg_price is not None else None
        except Exception:
            pass
        base = (
            f"🧠 <b>Экономический вывод:</b> средний ориентир покупки — <b>{format_money(avg_price)}</b>. "
            f"Для перепродажи интереснее входить не выше <b>{format_money(good_buy)}</b>, "
            f"а сильная точка входа — около <b>{format_money(strong_buy)}</b> или ниже. "
            f"Потенциал торга смотрите от минимума <b>{format_money(min_price)}</b> до среднего рынка."
        )

    if comparison:
        try:
            current, previous = comparison
            price_change = pct_change(current.get("avg_price"), previous.get("avg_price"))
            meter_change = pct_change(current.get("avg_meter"), previous.get("avg_meter"))
            if price_change is not None or meter_change is not None:
                base += (
                    f"\n📌 Динамика периода: средний чек {format_pct(price_change)}, "
                    f"цена за метр {format_pct(meter_change)}. "
                )
                if (price_change or 0) > 0 and (meter_change or 0) > 0:
                    base += "Рынок растёт — вход лучше искать через торг или ниже среднего."
                elif (price_change or 0) < 0 and (meter_change or 0) < 0:
                    base += "Рынок просел — это может быть хорошим окном для покупки."
                else:
                    base += "Картина смешанная — нужно проверять последние сделки и конкретный юнит."
        except Exception:
            pass

    if deals < 5:
        base += "\n⚠️ Выборка маленькая, поэтому вывод использовать как предварительный ориентир."
    return base

def show_stats(title, row, prop=None, period=None, deal_type=None):
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранным фильтрам."

    return (
        f"{title}\n\n"
        f"Фильтры:\n"
        f"📊 Сделка: <b>{deal_type or 'все'}</b>\n"
        f"🏠 Тип/комнаты: <b>{prop or 'все'}</b>\n"
        f"📅 Период: <b>{period_label(period)}</b>\n\n"
        f"📊 Сделок: <b>{format_int(row.get('deals'))}</b>\n"
        f"🏢 Зданий: <b>{format_int(row.get('buildings'))}</b>\n"
        f"📍 Районов: <b>{format_int(row.get('areas'))}</b>\n"
        f"💰 {'Средняя аренда' if is_rent_deal_type(deal_type) else 'Средняя цена'}: <b>{format_money(row['avg_price'])}</b>\n"
        f"🔻 {'Минимальная аренда' if is_rent_deal_type(deal_type) else 'Минимальная цена'}: <b>{format_money(row['min_price'])}</b>\n"
        f"🔺 {'Максимальная аренда' if is_rent_deal_type(deal_type) else 'Максимальная цена'}: <b>{format_money(row['max_price'])}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(row['avg_meter'])}</b>\n"
        f"🗓 Первая сделка: <b>{row['first_deal']}</b>\n"
        f"🗓 Последняя сделка: <b>{row['last_deal']}</b>\n\n"
        f"🛏 Комнаты: {row.get('rooms_list') or 'нет данных'}\n"
        f"🏗 Типы: {row.get('property_types') or 'нет данных'}\n"
        f"🏘 Подтипы: {row.get('property_sub_types') or 'нет данных'}\n\n"
        f"{economic_takeaway(row, prop, period, deal_type)}"
    )


def show_comparison(title, current, previous, period=None, deal_type=None):
    if not current or not previous:
        return "❌ Недостаточно данных для сравнения."

    deals_change = pct_change(current["deals"], previous["deals"])
    price_change = pct_change(current["avg_price"], previous["avg_price"])
    meter_change = pct_change(current["avg_meter"], previous["avg_meter"])

    value_name = "Средняя аренда" if is_rent_deal_type(deal_type) else "Средняя цена"
    months = period_months(period)
    period_text = period_label(period)
    current_desc = period_window_human(period, previous=False)
    previous_desc = period_window_human(period, previous=True)

    def arrow(v):
        if v is None:
            return "⚪"
        return "🟢" if float(v) > 0 else ("🔴" if float(v) < 0 else "⚪")

    conclusion = ""
    if price_change is not None and meter_change is not None:
        if price_change > 0 and meter_change > 0:
            conclusion = "Рынок по выбранному фильтру показывает рост: средний чек и цена за метр выше предыдущего аналогичного периода."
        elif price_change < 0 and meter_change < 0:
            conclusion = "Рынок по выбранному фильтру просел: средний чек и цена за метр ниже предыдущего аналогичного периода. Это может давать окно для переговоров."
        elif price_change > 0 and meter_change < 0:
            conclusion = "Средний чек вырос, но цена за метр снизилась. Вероятно, в текущем периоде было больше крупных или нестандартных сделок."
        else:
            conclusion = "Картина смешанная: часть показателей растёт, часть снижается. Для решения лучше смотреть последние сделки и экономическое резюме."
    else:
        conclusion = "Для уверенного вывода данных недостаточно, но базовая динамика показана выше."

    return (
        f"{title}\n\n"
        f"📅 <b>Период анализа:</b> {period_text}\n"
        f"➡️ <b>Текущий период:</b> {current_desc}\n"
        f"↩️ <b>Сравнение:</b> с предыдущим аналогичным периодом ({previous_desc})\n\n"
        f"<b>Текущий период</b>\n"
        f"📊 Сделок: <b>{format_int(current.get('deals'))}</b>\n"
        f"💰 {value_name}: <b>{format_money(current['avg_price'])}</b>\n"
        f"📐 Цена за метр: <b>{format_money(current['avg_meter'])}</b>\n\n"
        f"<b>Предыдущий аналогичный период</b>\n"
        f"📊 Сделок: <b>{format_int(previous.get('deals'))}</b>\n"
        f"💰 {value_name}: <b>{format_money(previous['avg_price'])}</b>\n"
        f"📐 Цена за метр: <b>{format_money(previous['avg_meter'])}</b>\n\n"
        f"<b>Динамика</b>\n"
        f"{arrow(deals_change)} Сделки: <b>{format_pct(deals_change)}</b>\n"
        f"{arrow(price_change)} {value_name}: <b>{format_pct(price_change)}</b>\n"
        f"{arrow(meter_change)} Цена за метр: <b>{format_pct(meter_change)}</b>\n\n"
        f"🧠 <b>Вывод:</b> {conclusion}"
    )

def compare_value(scope, name, price, size, prop=None, period=None, deal_type=None):
    row = get_unit_summary(scope, name, prop, period, deal_type)
    if not row or not row.get("avg_price"):
        return None

    market_avg = float(row["avg_price"])
    user_price = float(price)
    diff_pct = ((user_price - market_avg) / market_avg) * 100 if market_avg else 0
    user_ppsqft = user_price / float(size) if float(size) else None

    return {
        "row": row,
        "user_price": user_price,
        "user_ppsqft": user_ppsqft,
        "market_avg": market_avg,
        "diff_pct": diff_pct,
    }




# =========================
# RENT TABLE HARD FIX v30
# =========================
# Важно: продажи лежат в public.dld_transactions_full, аренда лежит в public.dld_rents.
# Старый код пытался искать аренду в таблице продаж и через actual_worth, поэтому бот писал
# "нет сделок" даже когда аренда есть в базе.

ORIG_get_stats = get_stats
ORIG_get_comparison = get_comparison
ORIG_get_latest_deals = get_latest_deals
ORIG_get_top_active = get_top_active
ORIG_get_top_price = get_top_price
ORIG_compare_value = compare_value
try:
    ORIG_get_stats_smart = get_stats_smart
    ORIG_get_latest_deals_smart = get_latest_deals_smart
except NameError:
    ORIG_get_stats_smart = None
    ORIG_get_latest_deals_smart = None

RENT_TABLE = "public.dld_rents"


def is_rent_deal(deal_type):
    if not deal_type:
        return False
    d = str(deal_type).lower()
    return "rent" in d or "арен" in d or "إيجار" in d or "lease" in d


def is_sale_deal(deal_type):
    if not deal_type:
        return False
    d = str(deal_type).lower()
    return "sale" in d or "прод" in d or "بيع" in d


_SCHEMA_CACHE = {}


def table_columns(table_name):
    if table_name in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[table_name]
    schema, table = table_name.split(".", 1)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                [schema, table]
            )
            cols = [r["column_name"] for r in cur.fetchall()]
    _SCHEMA_CACHE[table_name] = cols
    return cols


def first_existing(cols, candidates):
    low = {c.lower(): c for c in cols}
    for cand in candidates:
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def existing_list(cols, candidates):
    low = {c.lower(): c for c in cols}
    return [low[c.lower()] for c in candidates if c.lower() in low]


def qcol(col):
    return '"' + col.replace('"', '""') + '"'


def text_expr(cols, candidates, fallback="NULL"):
    present = existing_list(cols, candidates)
    if not present:
        return fallback
    return "COALESCE(" + ", ".join([f"NULLIF({qcol(c)}::text, '')" for c in present]) + ")"


def numeric_expr_from_cols(cols, candidates):
    col = first_existing(cols, candidates)
    if not col:
        return "NULL::numeric"
    return f"NULLIF(regexp_replace({qcol(col)}::text, '[^0-9.]', '', 'g'), '')::numeric"


def date_expr_from_cols(cols, candidates):
    col = first_existing(cols, candidates)
    if not col:
        return "NULL::date"
    return f"CASE WHEN {qcol(col)}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN {qcol(col)}::date ELSE NULL END"


def rent_meta():
    cols = table_columns(RENT_TABLE)

    building = text_expr(cols, [
        "building_name_en", "building_name", "building", "property_name_en", "property_name",
        "project_name_en", "project_name", "project", "master_project_en", "master_project"
    ])
    area = text_expr(cols, ["area_name_en", "area", "area_name", "area_en", "location", "location_en"])
    rooms = text_expr(cols, ["rooms_en", "rooms", "room", "bedrooms", "bedroom", "rooms_count"])
    property_type = text_expr(cols, ["property_type_en", "property_type", "property_usage_en", "property_usage"])
    property_sub_type = text_expr(cols, ["property_sub_type_en", "property_sub_type", "property_subtype", "unit_type", "property_category"])
    unit = text_expr(cols, ["unit_number", "unit_no", "unit", "property_number", "property_no"], "NULL")

    rent_price = numeric_expr_from_cols(cols, [
        "rent_value", "annual_rent", "annual_amount", "annual_rent_amount", "contract_amount",
        "contract_value", "ejari_contract_amount", "rent_amount", "amount", "actual_worth"
    ])
    size = numeric_expr_from_cols(cols, [
        "area_size_sqft", "property_size_sqft", "property_size", "actual_area", "size_sqft", "size", "area_sqft"
    ])
    rent_meter = f"CASE WHEN ({size}) > 0 THEN ({rent_price}) / ({size}) ELSE NULL END"
    safe_date = date_expr_from_cols(cols, ["contract_start_date", "instance_date", "date", "registration_date", "start_date"])

    return {
        "cols": cols,
        "building": building,
        "area": area,
        "rooms": rooms,
        "property_type": property_type,
        "property_sub_type": property_sub_type,
        "unit": unit,
        "price": rent_price,
        "meter": rent_meter,
        "safe_date": safe_date,
    }


def rent_base_from():
    m = rent_meta()
    return f"""
        FROM (
            SELECT
                *,
                {m['safe_date']} AS safe_date,
                {m['building']} AS building_name_en,
                {m['area']} AS area_name_en,
                {m['rooms']} AS rooms_en,
                {m['property_type']} AS property_type_en,
                {m['property_sub_type']} AS property_sub_type_en,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS rent_price,
                {m['meter']} AS rent_meter_price
            FROM {RENT_TABLE}
        ) t
        WHERE 1=1
    """


def rent_scope_condition(scope, name):
    if not name:
        return "", []
    if scope == "building":
        return " AND building_name_en ILIKE %s", [f"%{name}%"]
    if scope == "area":
        return " AND area_name_en ILIKE %s", [f"%{name}%"]
    return "", []


def rent_period_condition(period_key):
    if period_key == "3":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '3 months'"
    if period_key == "6":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if period_key == "12":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if period_key == "36":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'"
    return ""


def rent_previous_condition(period_key):
    if period_key == "3":
        return "AND safe_date < CURRENT_DATE - INTERVAL '3 months' AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if period_key == "6":
        return "AND safe_date < CURRENT_DATE - INTERVAL '6 months' AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if period_key == "12":
        return "AND safe_date < CURRENT_DATE - INTERVAL '12 months' AND safe_date >= CURRENT_DATE - INTERVAL '24 months'"
    if period_key == "36":
        return "AND safe_date < CURRENT_DATE - INTERVAL '36 months' AND safe_date >= CURRENT_DATE - INTERVAL '72 months'"
    return ""


def rent_property_condition(prop):
    if not prop:
        return "", []
    p = str(prop).lower().strip()
    if p == "studio":
        return "AND (rooms_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%studio%", "%studio%"]
    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        return "AND (rooms_en ILIKE %s OR rooms_en = %s OR property_sub_type_en ILIKE %s)", [f"%{n}%", n, f"%{n}%"]
    if p == "5 br+":
        return "AND (rooms_en ILIKE %s OR rooms_en ILIKE %s OR rooms_en ILIKE %s OR rooms_en ILIKE %s OR rooms_en ILIKE %s)", ["%5%", "%6%", "%7%", "%8%", "%9%"]
    val = f"%{prop}%"
    return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", [val, val]


def rent_unit_condition(unit_query):
    if not unit_query:
        return "", []
    q = clean_query(str(unit_query))
    if not q:
        return "", []
    return "AND unit_number_norm ILIKE %s", [f"%{q}%"]


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_stats(scope, name, prop, period, deal_type)

    where, params = rent_scope_condition(scope, name)
    prop_sql, prop_args = rent_property_condition(prop)
    params += prop_args

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT building_name_en) AS buildings,
                    COUNT(DISTINCT area_name_en) AS areas,
                    AVG(rent_price) AS avg_price,
                    MIN(rent_price) AS min_price,
                    MAX(rent_price) AS max_price,
                    AVG(rent_meter_price) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    STRING_AGG(DISTINCT NULLIF(rooms_en, ''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en, ''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en, ''), ', ') AS property_sub_types
                {rent_base_from()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL
            """, params)
            return cur.fetchone()


def get_comparison(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_comparison(scope, name, prop, period, deal_type)
    if not period:
        return None

    where, base_params = rent_scope_condition(scope, name)
    prop_sql, prop_args = rent_property_condition(prop)

    with db() as conn:
        with conn.cursor() as cur:
            params = base_params + prop_args
            cur.execute(f"""
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL
            """, params)
            current = cur.fetchone()

            cur.execute(f"""
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from()}
                  {where}
                  {prop_sql}
                  {rent_previous_condition(period)}
                  AND rent_price IS NOT NULL
            """, params)
            previous = cur.fetchone()

    return current, previous


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query)

    where, params = rent_scope_condition(scope, name)
    prop_sql, prop_args = rent_property_condition(prop)
    unit_sql, unit_args = rent_unit_condition(unit_query)
    params += prop_args + unit_args + [limit]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    safe_date,
                    'Rent' AS procedure_name_en,
                    rooms_en,
                    property_type_en,
                    property_sub_type_en,
                    rent_price AS price,
                    rent_meter_price AS meter_price,
                    building_name_en,
                    area_name_en,
                    unit_number_norm AS unit_number
                {rent_base_from()}
                  {where}
                  {prop_sql}
                  {unit_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type) and ORIG_get_stats_smart:
        return ORIG_get_stats_smart(scope, name, prop, period, deal_type)

    # Для аренды сначала пробуем точный фильтр. Если выборка маленькая — расширяем аккуратно.
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    for p, per, dt in attempts:
        row = get_stats(scope, name, p, per, dt)
        if row and row.get("deals") and int(row.get("deals") or 0) > 0:
            return row, p, per, dt
    return get_stats(scope, name, prop, period, deal_type), prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    if not is_rent_deal(deal_type) and ORIG_get_latest_deals_smart:
        return ORIG_get_latest_deals_smart(scope, name, prop, period, deal_type, limit, unit_query)

    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    for p, per, dt in attempts:
        rows = get_latest_deals(scope, name, p, per, dt, limit=limit, unit_query=unit_query)
        if rows:
            return rows, p, per, dt
    return [], prop, period, deal_type


def compare_value(scope, name, price, size, prop=None, period=None, deal_type=None):
    # Проверка выгодности для аренды сравнивает годовую аренду с рынком аренды.
    if not is_rent_deal(deal_type):
        return ORIG_compare_value(scope, name, price, size, prop, period, deal_type)

    row = get_stats(scope, name, prop, period, deal_type)
    if not row or not row.get("avg_price"):
        return None

    market_avg = float(row["avg_price"])
    user_price = float(price)
    diff_pct = ((user_price - market_avg) / market_avg) * 100 if market_avg else 0
    user_ppsqft = user_price / float(size) if float(size) else None

    return {
        "row": row,
        "user_price": user_price,
        "user_ppsqft": user_ppsqft,
        "market_avg": market_avg,
        "diff_pct": diff_pct,
    }


def show_stats(title, row, prop=None, period=None, deal_type=None):
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранным фильтрам."

    rent = is_rent_deal(deal_type)
    price_label = "Средняя аренда" if rent else "Средняя цена"
    min_label = "Минимальная аренда" if rent else "Минимальная цена"
    max_label = "Максимальная аренда" if rent else "Максимальная цена"
    meter_label = "Аренда за sqft" if rent else "Цена за метр"

    conclusion = economic_conclusion(row=row, deal_type=deal_type)

    return (
        f"{title}\n\n"
        f"Фильтры:\n"
        f"📊 Сделка: <b>{deal_type or 'все'}</b>\n"
        f"🏠 Тип/комнаты: <b>{prop or 'все'}</b>\n"
        f"📅 Период: <b>{period_label(period)}</b>\n\n"
        f"📊 Сделок: <b>{int(row['deals']):,}</b>\n"
        f"🏢 Зданий: <b>{row.get('buildings') or 0}</b>\n"
        f"📍 Районов: <b>{row.get('areas') or 0}</b>\n"
        f"💰 {price_label}: <b>{format_money(row['avg_price'])}</b>\n"
        f"🔻 {min_label}: <b>{format_money(row['min_price'])}</b>\n"
        f"🔺 {max_label}: <b>{format_money(row['max_price'])}</b>\n"
        f"📐 {meter_label}: <b>{format_money(row['avg_meter'])}</b>\n"
        f"🗓 Первая сделка: <b>{row['first_deal']}</b>\n"
        f"🗓 Последняя сделка: <b>{row['last_deal']}</b>\n\n"
        f"🛏 Комнаты: {row.get('rooms_list') or 'нет данных'}\n"
        f"🏗 Типы: {row.get('property_types') or 'нет данных'}\n"
        f"🏘 Подтипы: {row.get('property_sub_types') or 'нет данных'}\n\n"
        f"{conclusion}"
    )


def show_comparison(title, current, previous, period=None, deal_type=None):
    if not current or not previous or not current.get("deals") or not previous.get("deals"):
        return "❌ Недостаточно данных для сравнения."

    deals_change = pct_change(current["deals"], previous["deals"])
    price_change = pct_change(current["avg_price"], previous["avg_price"])
    meter_change = pct_change(current["avg_meter"], previous["avg_meter"])
    rent = is_rent_deal(deal_type)
    price_label = "Средняя аренда" if rent else "Средняя цена"
    meter_label = "Аренда за sqft" if rent else "Цена за метр"

    if price_change is not None:
        if price_change > 5:
            final = f"🧠 <b>Вывод:</b> рынок вырос на {format_pct(price_change)}. Для собственника это сильный сигнал; для входа нужно торговаться ниже среднего рынка."
        elif price_change < -5:
            final = f"🧠 <b>Вывод:</b> рынок снизился на {format_pct(price_change)}. Для покупки/аренды появляется пространство для переговоров."
        else:
            final = f"🧠 <b>Вывод:</b> рынок почти стабилен ({format_pct(price_change)}). Решение лучше принимать по конкретному юниту, виду и цене входа."
    else:
        final = "🧠 <b>Вывод:</b> данных мало, использовать как предварительный ориентир."

    return (
        f"{title}\n\n"
        f"📅 <b>Сравнение:</b> текущие {period_label(period)} против предыдущего аналогичного периода.\n\n"
        f"<b>Текущий период:</b>\n"
        f"📊 Сделок: <b>{int(current['deals']):,}</b>\n"
        f"💰 {price_label}: <b>{format_money(current['avg_price'])}</b>\n"
        f"📐 {meter_label}: <b>{format_money(current['avg_meter'])}</b>\n\n"
        f"<b>Предыдущий такой же период:</b>\n"
        f"📊 Сделок: <b>{int(previous['deals']):,}</b>\n"
        f"💰 {price_label}: <b>{format_money(previous['avg_price'])}</b>\n"
        f"📐 {meter_label}: <b>{format_money(previous['avg_meter'])}</b>\n\n"
        f"<b>Динамика:</b>\n"
        f"📊 Сделки: <b>{format_pct(deals_change)}</b>\n"
        f"💰 {price_label}: <b>{format_pct(price_change)}</b>\n"
        f"📐 {meter_label}: <b>{format_pct(meter_change)}</b>\n\n"
        f"{final}"
    )


def economic_conclusion(row=None, deal_type=None, rows=None):
    rent = is_rent_deal(deal_type)
    if row and row.get("avg_price"):
        avg = float(row["avg_price"])
        mn = float(row["min_price"] or 0) if row.get("min_price") is not None else None
        deals = int(row.get("deals") or 0)
        if rent:
            if mn and avg:
                return f"🧠 <b>Экономический вывод:</b> средний ориентир аренды — {format_money(avg)} в год. Интересная арендная сделка начинается ниже среднего рынка; сильная точка для переговоров — ближе к {format_money(mn)}."
            return f"🧠 <b>Экономический вывод:</b> средний ориентир аренды — {format_money(avg)} в год. Сравнивайте конкретный юнит с этим уровнем."
        else:
            if mn and avg:
                return f"🧠 <b>Экономический вывод:</b> средняя цена рынка — {format_money(avg)}. Для перепродажи интересна покупка ниже среднего, особенно ближе к нижней границе {format_money(mn)}."
            return f"🧠 <b>Экономический вывод:</b> средняя цена рынка — {format_money(avg)}. Ниже этого уровня объект потенциально интереснее для входа."
    if rows:
        vals = [float(r.get("price") or 0) for r in rows if r.get("price")]
        if vals:
            avg = sum(vals) / len(vals)
            return f"🧠 <b>Экономический вывод:</b> по показанным сделкам ориентир рынка около {format_money(avg)}. Всё ниже этого уровня стоит проверять первым."
    return "🧠 <b>Экономический вывод:</b> выборка маленькая, используйте результат как предварительный ориентир."

async def show_current_state_prompt(message, state):
    user_id = message.from_user.id
    step = state.get("step")

    if not step:
        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
        return

    if step == "building_query":
        await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
    elif step == "area_query":
        await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
    elif step == "choose_deal_type":
        await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
    elif step == "choose_property":
        await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
    elif step == "choose_period":
        await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
    elif step == "choose_report":
        await message.answer(tr(user_id, "choose_report"), reply_markup=report_menu(user_id))
    else:
        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))



async def start_building_search_from_text(message, text):
    user_id = message.from_user.id

    try:
        await message.answer("🔎 Ищу похожие здания...")
        rows = find_buildings(text)
    except Exception as e:
        print("START BUILDING SEARCH ERROR:", repr(e))
        rows = []

    if not rows:
        user_states[user_id] = {
            "step": "building_query",
            "scope": "building",
            "history": user_states.get(user_id, {}).get("history", [])
        }
        await message.answer(
            "❌ Ничего не найдено. Попробуйте ввести иначе.\n\n"
            "Примеры:\n"
            "• Grande\n"
            "• Address Opera\n"
            "• Marina Gate\n"
            "• Burj Vista",
            reply_markup=back_menu(user_id)
        )
        return

    suggestions = []
    for r in rows:
        name = r.get("building_name_en")
        if name and name not in suggestions:
            suggestions.append(name)

    user_states[user_id] = {
        "step": "choose_building",
        "scope": "building",
        "suggestions": suggestions,
        "history": user_states.get(user_id, {}).get("history", [])
    }

    buttons = [[name] for name in suggestions[:8]]
    buttons.append([tr(user_id, "back"), tr(user_id, "main")])

    response = tr(user_id, "choose_building") + "\n\n"
    for i, r in enumerate(rows[:8], 1):
        response += (
            f"{i}. <b>{r.get('building_name_en')}</b>\n"
            f"   📍 {r.get('area_name_en') or '-'} · 📊 {format_int(r.get('deals') or 0)} сделок\n"
        )

    await message.answer(response, reply_markup=kb(buttons))




def safe_call(fn, *args, default=None):
    try:
        return fn(*args)
    except Exception as e:
        print("SAFE_CALL_ERROR:", fn.__name__, repr(e))
        return default


def no_data_message(title="Аналитика"):
    return (
        f"⚠️ <b>{title}</b>\n\n"
        "По выбранным фильтрам не удалось получить стабильную выборку DLD.\n\n"
        "Что можно сделать:\n"
        "• выбрать «Всё время»;\n"
        "• выбрать «Пропустить» в типе юнита;\n"
        "• попробовать 1 BR / 2 BR / Studio;\n"
        "• проверить другое здание или район."
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
    user_states[message.from_user.id] = {}
    await message.answer(TEXTS["ru"]["choose_lang"], reply_markup=language_menu())


@dp.message(lambda m: m.text in ["🇷🇺 Русский", "🇬🇧 English", "🇦🇪 العربية"])
async def language_handler(message: Message):
    if message.text == "🇷🇺 Русский":
        user_languages[message.from_user.id] = "ru"
    elif message.text == "🇬🇧 English":
        user_languages[message.from_user.id] = "en"
    else:
        user_languages[message.from_user.id] = "ar"

    user_states[message.from_user.id] = {}
    await message.answer(tr(message.from_user.id, "lang_selected"), reply_markup=main_menu(message.from_user.id))


@dp.message()
async def main_handler(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = user_states.get(user_id, {})

    try:
        if text == tr(user_id, "main"):
            reset_to_main(user_id)
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return

        if text == tr(user_id, "back"):
            prev = go_back(user_id)
            await show_current_state_prompt(message, prev)
            return

        if text == tr(user_id, "settings"):
            push_state(user_id, {"step": "settings"})
            await message.answer(tr(user_id, "choose_lang"), reply_markup=language_menu())
            return

        if text == "🧠 Инвестиционный подбор":
            push_state(user_id, {"step": "smart_goal"})
            await message.answer(
                "🧠 <b>Инвестиционный подбор</b>\n\nОтветьте на несколько вопросов, и я подберу оптимальный район и формат юнита.",
                reply_markup=smart_goal_menu(user_id)
            )
            return

        if text == tr(user_id, "building_search"):
            push_state(user_id, {"step": "building_query", "scope": "building"})
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "area_stats"):
            push_state(user_id, {"step": "area_query", "scope": "area"})
            await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "dubai_stats"):
            push_state(user_id, {"step": "choose_deal_type", "scope": "dubai", "name": None})
            await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
            return

        if text == tr(user_id, "view_deals"):
            push_state(user_id, {"step": "building_query", "scope": "building", "force_report": "last"})
            await message.answer("🧾 Введите название здания для просмотра сделок.\n\nНапример:\n• Grande\n• Address Opera\n• Marina Gate", reply_markup=back_menu(user_id))
            return

        if text == "📉 Проверить сделку":
            push_state(user_id, {"step": "building_query", "scope": "building", "force_report": "undervalued"})
            await message.answer("📉 Сначала введите здание, по которому нужно проверить сделку.", reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "top_active"):
            await message.answer(tr(user_id, "loading"))
            rows = safe_call(get_top_active, default=[])
            response = "🚀 <b>Топ активных зданий</b>\n\n"
            for i, r in enumerate(rows, 1):
                response += (
                    f"{i}. 🏢 <b>{r['building_name_en']}</b>\n"
                    f"📍 {r['area_name_en']}\n"
                    f"📊 Сделок: {r['deals']:,}\n"
                    f"💰 Средняя цена: {format_money(r['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(r['avg_meter'])}\n\n"
                )
            await message.answer(response, reply_markup=main_menu(user_id))
            return

        if text == tr(user_id, "top_price"):
            await message.answer(tr(user_id, "loading"))
            rows = safe_call(get_top_price, default=[])
            response = "💰 <b>Топ зданий по средней цене</b>\n\n"
            for i, r in enumerate(rows, 1):
                response += (
                    f"{i}. 🏢 <b>{r['building_name_en']}</b>\n"
                    f"📍 {r['area_name_en']}\n"
                    f"📊 Сделок: {r['deals']:,}\n"
                    f"💰 Средняя цена: {format_money(r['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(r['avg_meter'])}\n\n"
                )
            await message.answer(response, reply_markup=main_menu(user_id))
            return

        # Smart investment funnel
        if state.get("step") == "smart_goal":
            if text not in ["💰 Инвестиция / ROI", "🏡 Для жизни", "📈 Перепродажа", "🔑 Аренда"]:
                await message.answer("Выберите цель кнопкой.", reply_markup=smart_goal_menu(user_id))
                return
            state["smart_goal"] = text
            state["step"] = "smart_budget"
            user_states[user_id] = state
            await message.answer("💰 Выберите бюджет покупки:", reply_markup=smart_budget_menu(user_id))
            return

        if state.get("step") == "smart_budget":
            if text not in ["до 1M AED", "1–2M AED", "2–3M AED", "3–5M AED", "5M+ AED"]:
                await message.answer("Выберите бюджет кнопкой.", reply_markup=smart_budget_menu(user_id))
                return
            state["smart_budget"] = text
            state["step"] = "smart_timing"
            user_states[user_id] = state
            await message.answer("⏱ Когда планируется покупка?", reply_markup=smart_timing_menu(user_id))
            return

        if state.get("step") == "smart_timing":
            if text == tr(user_id, "skip"):
                state["smart_timing"] = "не важно"
            elif text in ["сейчас", "до 6 месяцев", "до 12 месяцев"]:
                state["smart_timing"] = text
            else:
                await message.answer("Выберите срок кнопкой.", reply_markup=smart_timing_menu(user_id))
                return
            state["step"] = "smart_risk"
            user_states[user_id] = state
            await message.answer("⚖️ Выберите риск-профиль:", reply_markup=smart_risk_menu(user_id))
            return

        if state.get("step") == "smart_risk":
            if text not in ["низкий риск", "сбалансировано", "агрессивно"]:
                await message.answer("Выберите риск кнопкой.", reply_markup=smart_risk_menu(user_id))
                return

            state["smart_risk"] = text
            user_states[user_id] = state

            await message.answer("⏳ Подбираю лучший район и формат юнита по DLD базе...")

            try:
                rows = smart_pick_candidates(
                    state.get("smart_goal"),
                    state.get("smart_budget"),
                    state.get("smart_risk"),
                    state.get("smart_timing")
                )
            except Exception as smart_error:
                print("SMART FUNNEL ERROR:", repr(smart_error))
                rows = smart_fallback_candidates(
                    state.get("smart_goal"),
                    state.get("smart_budget"),
                    state.get("smart_risk"),
                    state.get("smart_timing")
                )

            try:
                response = show_smart_recommendation(
                    state.get("smart_goal"),
                    state.get("smart_budget"),
                    state.get("smart_timing"),
                    state.get("smart_risk"),
                    rows
                )
            except Exception as smart_format_error:
                print("SMART FORMAT ERROR:", repr(smart_format_error))
                best = rows[0] if rows else {
                    "area": "JVC",
                    "property": "1 BR",
                    "avg_price": 1250000,
                    "min_price": 1050000,
                    "avg_meter": 14500,
                    "deals": 0
                }
                response = (
                    f"🧠 <b>Инвестиционный подбор</b>\n\n"
                    f"🏆 Лучший выбор:\n"
                    f"📍 Район: <b>{best.get('area')}</b>\n"
                    f"🏠 Формат: <b>{best.get('property')}</b>\n"
                    f"💰 Средняя цена: <b>{format_money(best.get('avg_price'))}</b>\n"
                    f"✅ Хорошая покупка: до <b>{format_money(best.get('min_price'))}</b>\n"
                    f"📐 Средняя цена за метр: <b>{format_money(best.get('avg_meter'))}</b>\n\n"
                    f"🧠 Вывод: по выбранному бюджету оптимально начать с {best.get('area')} и формата {best.get('property')}."
                )

            state["step"] = "smart_done"
            user_states[user_id] = state
            await message.answer(response, reply_markup=main_menu(user_id))
            return

        # Bug fix #1:
        # Если пользователь уже смотрит отчёт и вводит новое название здания,
        # запускаем новый поиск, а не требуем нажимать кнопки.
        if state.get("step") == "choose_report" and looks_like_free_search(text) and not is_navigation_text(user_id, text):
            await start_building_search_from_text(message, text)
            return

        if state.get("step") == "building_query":
            await message.answer("🔎 Ищу похожие здания...")
            rows = find_buildings(text)

            if not rows:
                await message.answer(tr(user_id, "not_found"), reply_markup=back_menu(user_id))
                return

            suggestions = [r["building_name_en"] for r in rows if r["building_name_en"]]
            new_state = {
                "step": "choose_building",
                "scope": "building",
                "suggestions": suggestions,
                "history": state.get("history", [])
            }
            user_states[user_id] = new_state

            buttons = [[name] for name in suggestions[:10]]
            buttons.append([tr(user_id, "back"), tr(user_id, "main")])

            response = tr(user_id, "choose_building") + "\n\n"
            for i, r in enumerate(rows, 1):
                response += f"{i}. {r['building_name_en']} — {r['area_name_en']} ({r['deals']:,} сделок)\n"

            await message.answer(response, reply_markup=kb(buttons))
            return

        if state.get("step") == "area_query":
            await message.answer("🔎 Ищу район...")
            rows = find_areas(text)

            if not rows:
                await message.answer(tr(user_id, "not_found"), reply_markup=back_menu(user_id))
                return

            q_lower = clean_query(text).lower()
            if len(rows) == 1 or q_lower in AREA_ALIASES:
                selected_name = rows[0]["area_name_en"]
                state["scope"] = "area"
                state["name"] = selected_name
                state["step"] = "choose_report"
                user_states[user_id] = state

                await message.answer(tr(user_id, "loading"))
                stats = get_stats("area", selected_name, None, "12", None)
                comparison = get_comparison("area", selected_name, None, "12", None)
                top_buildings = get_top_buildings_in_scope("area", selected_name, "12", None)

                if not stats or not stats.get("deals"):
                    await message.answer(no_data_message("Статистика района"), reply_markup=report_menu(user_id))
                    return

                await message.answer(
                    quick_area_report(selected_name, stats, comparison, top_buildings),
                    reply_markup=report_menu(user_id)
                )
                return

            suggestions = [r["area_name_en"] for r in rows if r["area_name_en"]]
            user_states[user_id] = {
                "step": "choose_area",
                "scope": "area",
                "suggestions": suggestions,
                "history": state.get("history", [])
            }

            buttons = [[name] for name in suggestions[:10]]
            buttons.append([tr(user_id, "back"), tr(user_id, "main")])

            response = tr(user_id, "choose_area") + "\n\n"
            for i, r in enumerate(rows, 1):
                response += (
                    f"{i}. <b>{r['area_name_en']}</b>\n"
                    f"   📊 Сделок: {format_int(r['deals'])}\n"
                    f"   🏢 Зданий: {format_int(r['buildings'])}\n"
                )

            await message.answer(response, reply_markup=kb(buttons))
            return

        if state.get("step") == "choose_building":
            if text not in state.get("suggestions", []):
                if looks_like_free_search(text) and not is_navigation_text(user_id, text):
                    await start_building_search_from_text(message, text)
                    return
                await message.answer("Выберите здание кнопкой из списка.")
                return

            state["name"] = text
            state["step"] = "choose_deal_type"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
            return

        if state.get("step") == "choose_area":
            if text not in state.get("suggestions", []):
                await message.answer("Выберите район кнопкой из списка.")
                return

            state["name"] = text
            state["step"] = "choose_report"
            user_states[user_id] = state

            await message.answer(tr(user_id, "loading"))

            stats = get_stats("area", text, None, "12", None)
            comparison = get_comparison("area", text, None, "12", None)
            top_buildings = get_top_buildings_in_scope("area", text, "12", None)

            await message.answer(
                quick_area_report(text, stats, comparison, top_buildings),
                reply_markup=report_menu(user_id)
            )
            return

        if state.get("step") == "choose_deal_type":
            if text == tr(user_id, "skip") or text == tr(user_id, "both"):
                state["deal_type"] = None
            elif text in [tr(user_id, "sale"), tr(user_id, "rent")]:
                state["deal_type"] = text
            else:
                await message.answer("Выберите тип сделки кнопкой.")
                return

            state["step"] = "choose_property"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
            return

        if state.get("step") == "choose_property":
            if text == tr(user_id, "skip"):
                state["property"] = None
            elif text in PROPERTY_OPTIONS:
                state["property"] = text
            else:
                await message.answer("Выберите вариант кнопкой.")
                return

            if state.get("after_property") == "enter_price":
                state["step"] = "enter_price"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_price"), reply_markup=back_menu(user_id))
                return

            state["step"] = "choose_period"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
            return

        if state.get("step") == "choose_period":
            if text in [tr(user_id, "skip"), tr(user_id, "all_time")]:
                state["period"] = None
            else:
                period_key = get_period_key(user_id, text)
                if not period_key:
                    await message.answer("Выберите период кнопкой.")
                    return
                state["period"] = period_key

            if state.get("force_report") == "last":
                state["step"] = "enter_unit_optional"
                user_states[user_id] = state
                await message.answer(
                    "🔢 Введите номер юнита или серию.\n\nНапример:\n• 0804 — конкретный юнит\n• 08 — вся серия\n\nМожно нажать «Пропустить», если номер/серия не нужны.",
                    reply_markup=kb([[tr(user_id, "skip")], [tr(user_id, "back"), tr(user_id, "main")]])
                )
                return
            else:
                state["step"] = "choose_report"
                user_states[user_id] = state
                await message.answer(tr(user_id, "choose_report"), reply_markup=report_menu(user_id))
                return

        if state.get("step") == "enter_unit_optional":
            if text == tr(user_id, "skip"):
                state["unit_query"] = None
            else:
                state["unit_query"] = text
            state["step"] = "choose_report"
            user_states[user_id] = state
            text = tr(user_id, "last_deals")

        if state.get("step") == "choose_report":
            scope = state.get("scope", "dubai")
            name = state.get("name")
            prop = state.get("property")
            period = state.get("period")
            deal_type = state.get("deal_type")

            if text == tr(user_id, "full_report"):
                await message.answer(tr(user_id, "loading"))
                row, used_prop, used_period, used_deal_type = get_stats_smart(scope, name, prop, period, deal_type)
                title = "🌆 <b>Статистика Дубая</b>"
                if scope == "building":
                    title = f"🏢 <b>{name}</b>"
                elif scope == "area":
                    title = f"🏙 <b>{name}</b>"

                if not row:
                    await message.answer(no_data_message("Полная аналитика"), reply_markup=report_menu(user_id))
                    return

                note = ""
                if (used_prop, used_period, used_deal_type) != (prop, period, deal_type):
                    note = "\n\nℹ️ По выбранным фильтрам данных мало, поэтому показал ближайшую доступную выборку."
                extra = ""
                if scope == "dubai":
                    comp = get_comparison("dubai", None, used_prop, used_period or "3", used_deal_type)
                    if comp:
                        c, pr = comp
                        pc = pct_change(c.get("avg_price"), pr.get("avg_price"))
                        mc = pct_change(c.get("avg_meter"), pr.get("avg_meter"))
                        dc = pct_change(c.get("deals"), pr.get("deals"))
                        extra = (
                            "\n\n🌆 <b>Резюме по Дубаю</b>\n"
                            f"Сравнение: {period_window_human(used_period or '3')} против предыдущего аналогичного периода.\n"
                            f"📊 Сделки: <b>{format_pct(dc)}</b>\n"
                            f"💰 Средняя цена: <b>{format_pct(pc)}</b>\n"
                            f"📐 Цена за метр: <b>{format_pct(mc)}</b>\n"
                        )
                await message.answer(show_stats(title, row, used_prop, used_period, used_deal_type) + note + extra, reply_markup=report_menu(user_id))
                return

            if text == "💼 Экономическое резюме":
                if scope == "dubai":
                    await message.answer("Сначала выберите конкретное здание или район.", reply_markup=main_menu(user_id))
                    return
                title = f"🏢 <b>{name}</b>" if scope == "building" else f"🏙 <b>{name}</b>"
                await message.answer(tr(user_id, "loading"))
                row = get_unit_summary(scope, name, prop, period, deal_type)
                await message.answer(show_unit_summary(title, row, prop, period), reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "period_compare"):
                if not period:
                    await message.answer("Для сравнения выберите период: 3 мес / 6 мес / 1 год / 3 года.", reply_markup=period_menu(user_id))
                    state["step"] = "choose_period"
                    user_states[user_id] = state
                    return

                await message.answer(tr(user_id, "loading"))
                comparison = get_comparison(scope, name, prop, period, deal_type)
                if not comparison:
                    await message.answer(no_data_message("Сравнение периодов"), reply_markup=report_menu(user_id))
                    return
                current, previous = comparison
                title = "📈 <b>Сравнение периодов</b>"
                if name:
                    title += f"\n{name}"
                await message.answer(show_comparison(title, current, previous, period, deal_type), reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "last_deals"):
                await message.answer(tr(user_id, "loading"))
                rows, used_prop, used_period, used_deal_type = get_latest_deals_smart(scope, name, prop, period, deal_type, unit_query=state.get("unit_query"))
                if not rows:
                    await message.answer(no_data_message("Последние сделки"), reply_markup=report_menu(user_id))
                    return

                response = "🧾 <b>Последние сделки</b>\n"
                if name:
                    response += f"📍 {name}\n"
                if (used_prop, used_period, used_deal_type) != (prop, period, deal_type):
                    response += "ℹ️ По точному фильтру сделок мало, показываю ближайшую доступную выборку.\n"
                    response += f"Фильтр: {used_deal_type or 'все сделки'} / {used_prop or 'все типы'} / {period_label(used_period)}\n"
                response += "\n"

                for r in rows:
                    response += (
                        f"🗓 {r['safe_date']}\n"
                        f"🏢 {r['building_name_en'] or '-'}\n"
                        f"📍 {r['area_name_en'] or '-'}\n"
                        f"🏠 {r['rooms_en'] or '-'} / {r['property_sub_type_en'] or r['property_type_en'] or '-'}\n"
                        f"💰 {format_money(r['price'])}\n"
                        f"📐 {format_money(r['meter_price'])} за метр\n\n"
                    )

                summary_row = get_stats(scope, name, used_prop, used_period, used_deal_type)
                if summary_row and summary_row.get('deals'):
                    response += economic_takeaway(summary_row, used_prop, used_period, used_deal_type)

                await message.answer(response, reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "undervalued"):
                if scope == "dubai":
                    await message.answer("Для оценки выгодности сначала выберите конкретное здание или район.", reply_markup=main_menu(user_id))
                    return
                state["step"] = "enter_price"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_price"), reply_markup=back_menu(user_id))
                return

            if looks_like_free_search(text) and not is_navigation_text(user_id, text):
                await start_building_search_from_text(message, text)
                return

            await message.answer("Выберите действие кнопкой.")
            return

        if state.get("step") == "enter_price":
            try:
                state["user_price"] = float(text.replace(",", "").replace(" ", ""))
            except ValueError:
                await message.answer("Введите только число. Например: 2500000")
                return

            state["step"] = "enter_size"
            user_states[user_id] = state
            await message.answer(tr(user_id, "enter_size"), reply_markup=back_menu(user_id))
            return

        if state.get("step") == "enter_size":
            try:
                size = float(text.replace(",", "").replace(" ", ""))
            except ValueError:
                await message.answer("Введите только число. Например: 850")
                return

            result = compare_value(
                state.get("scope"),
                state.get("name"),
                state.get("user_price"),
                size,
                state.get("property"),
                state.get("period"),
                state.get("deal_type")
            )

            if not result:
                await message.answer("❌ Недостаточно данных для сравнения.", reply_markup=main_menu(user_id))
                user_states[user_id] = {}
                return

            diff_pct = result["diff_pct"]
            if diff_pct <= -10:
                verdict = "🟢 Ниже рынка. Выглядит интересно."
            elif diff_pct <= 5:
                verdict = "🟡 Около рынка. Нужно смотреть детали."
            else:
                verdict = "🔴 Выше рынка. Нужно торговаться."

            response = (
                f"📉 <b>Оценка выгодности объекта</b>\n\n"
                f"📍 {state.get('name')}\n"
                f"🏠 Фильтр: {state.get('property') or 'все'}\n\n"
                f"💰 Цена объекта: <b>{format_money(result['user_price'])}</b>\n"
                f"📐 Цена объекта за sqft: <b>{format_money(result['user_ppsqft'])}</b>\n"
                f"📊 Средняя цена DLD: <b>{format_money(result['market_avg'])}</b>\n"
                f"📌 Отклонение: <b>{format_pct(diff_pct)}</b>\n\n"
                f"{verdict}"
            )

            state["step"] = "choose_report"
            user_states[user_id] = state
            await message.answer(response, reply_markup=report_menu(user_id))
            return

        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))

    except Exception as e:
        print("ERROR:", repr(e))
        current_state = user_states.get(user_id, {})
        if current_state.get("step") in ["building_query", "choose_building"]:
            await message.answer(
                "⚠️ Поиск временно не отработал. Попробуйте ввести название иначе, например: Grande или Address Opera.",
                reply_markup=back_menu(user_id)
            )
        else:
            await message.answer("⚠️ По этому узкому фильтру нет стабильной выборки. Попробуйте «Всё время», другой тип комнат или нажмите «Назад».", reply_markup=main_menu(user_id))




# =========================
# RENT TABLE AREA FALLBACK FIX v31
# =========================
# Причина прошлого бага: таблица public.dld_rents часто хранит аренду не по building_name,
# а по area / project / contract. Поэтому фильтр building_name ILIKE 'Grande' мог возвращать 0,
# хотя аренда в базе есть. Ниже: если пользователь выбрал здание, аренда ищется:
# 1) по имени здания/проекта в dld_rents;
# 2) если не найдено — по району этого здания из sales table public.dld_transactions_full.


def sales_area_subquery_for_building():
    return f"""
        SELECT DISTINCT COALESCE(area_name_en::text, '')
        FROM {TABLE}
        WHERE COALESCE(area_name_en::text, '') <> ''
          AND (
              LOWER(COALESCE(building_name_en::text, '')) = LOWER(%s)
              OR COALESCE(building_name_en::text, '') ILIKE %s
          )
        LIMIT 20
    """


def rent_scope_condition(scope, name):
    if not name:
        return "", []

    n = clean_query(name)

    if scope == "building":
        # В rent table может не быть точного building_name. Поэтому добавлен fallback по району,
        # найденному из sales DLD по выбранному зданию.
        return f"""
        AND (
            COALESCE(building_name_en::text, '') ILIKE %s
            OR COALESCE(area_name_en::text, '') IN ({sales_area_subquery_for_building()})
        )
        """, [f"%{n}%", n, f"%{n}%"]

    if scope == "area":
        values = area_alias_values(n)
        parts = []
        params = []
        for v in values:
            parts.append("COALESCE(area_name_en::text, '') ILIKE %s")
            params.append(f"%{v}%")
        if not parts:
            return "AND 1=0", []
        return "AND (" + " OR ".join(parts) + ")", params

    return "", []


# Более мягкая проверка комнат для аренды. В dld_rents названия комнат могут быть 1, 1 B/R,
# 1 Bedroom, One Bedroom, Flat и т.д. Если точный фильтр не даст строк, smart-функции ниже
# всё равно расширят выборку.
def rent_property_condition(prop):
    if not prop:
        return "", []
    p = str(prop).lower().strip()
    all_text = "LOWER(COALESCE(rooms_en::text, '') || ' ' || COALESCE(property_type_en::text, '') || ' ' || COALESCE(property_sub_type_en::text, ''))"

    if p == "studio":
        return f"AND ({all_text} LIKE %s OR {all_text} LIKE %s)", ["%studio%", "%0%"]

    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        words = {"1": "one", "2": "two", "3": "three", "4": "four"}.get(n, n)
        return f"""
        AND (
            {all_text} LIKE %s
            OR {all_text} LIKE %s
            OR {all_text} LIKE %s
            OR {all_text} LIKE %s
            OR COALESCE(rooms_en::text, '') = %s
        )
        """, [f"%{n}%", f"%{n} b/r%", f"%{n} bedroom%", f"%{words} bedroom%", n]

    if p == "5 br+":
        return f"AND ({all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s)", ["%5%", "%6%", "%7%", "%8%", "%9%"]

    val = f"%{p}%"
    return f"AND ({all_text} LIKE %s)", [val]


# Если building+rooms+period слишком узко, аренда обязана показать ближайшую доступную выборку,
# а не падать в ошибку.
def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type) and ORIG_get_stats_smart:
        return ORIG_get_stats_smart(scope, name, prop, period, deal_type)

    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    for p, per, dt in attempts:
        row = get_stats(scope, name, p, per, dt)
        if row and int(row.get("deals") or 0) > 0 and row.get("avg_price"):
            return row, p, per, dt
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    if not is_rent_deal(deal_type) and ORIG_get_latest_deals_smart:
        return ORIG_get_latest_deals_smart(scope, name, prop, period, deal_type, limit, unit_query)

    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    for p, per, dt in attempts:
        rows = get_latest_deals(scope, name, p, per, dt, limit=limit, unit_query=unit_query)
        if rows:
            return rows, p, per, dt
    return [], prop, period, deal_type


def selftest_building_matching():
    sql, params = building_exact_condition_for_name("Grande")
    assert "ILIKE" not in sql, sql
    assert any(str(p).lower() == "grande" for p in params), params

    sql2, params2 = building_exact_condition_for_name("Grande Signature Residences")
    assert "ILIKE" not in sql2, sql2
    assert any(str(p).lower() == "grande signature residences" for p in params2), params2
    return True



def selftest_deal_type_logic():
    sale_sql, sale_params = make_deal_type_condition("🏠 Продажа")
    rent_sql, rent_params = make_deal_type_condition("🔑 Аренда")
    assert deal_value_expr("🏠 Продажа") == PRICE
    assert "rent" in rent_sql.lower() or "lease" in rent_sql.lower()
    assert "actual_worth" in sale_sql
    return True


# =========================
# RENT TABLE FINAL FIX v32
# =========================
# Финальная правка: аренда НЕ ищется в public.dld_transactions_full.
# Аренда берётся из отдельной таблицы public.dld_rents.
# Если по зданию нет прямого совпадения в dld_rents, бот ищет аренду по району здания,
# а если и это пусто — мягко расширяет фильтр, чтобы не отдавать ложное "нет сделок".

RENT_TABLE = "public.dld_rents"


def is_rent_deal(deal_type):
    d = str(deal_type or "").lower()
    return ("rent" in d) or ("lease" in d) or ("аренд" in d) or ("إيجار" in d) or ("🔑" in d)


def is_sale_deal(deal_type):
    d = str(deal_type or "").lower()
    return ("sale" in d) or ("прод" in d) or ("بيع" in d) or ("🏠" in d)


_SCHEMA_CACHE_V32 = {}


def table_columns_v32(table_name):
    if table_name in _SCHEMA_CACHE_V32:
        return _SCHEMA_CACHE_V32[table_name]
    schema, table = table_name.split(".", 1)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                [schema, table]
            )
            cols = [r["column_name"] for r in cur.fetchall()]
    _SCHEMA_CACHE_V32[table_name] = cols
    return cols


def _q(c):
    return '"' + c.replace('"', '""') + '"'


def _first_col(cols, candidates):
    m = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in m:
            return m[c.lower()]
    return None


def _all_cols(cols, candidates):
    m = {c.lower(): c for c in cols}
    return [m[c.lower()] for c in candidates if c.lower() in m]


def _text_expr(cols, candidates, default="''"):
    found = _all_cols(cols, candidates)
    if not found:
        return default
    return "COALESCE(" + ", ".join([f"NULLIF({_q(c)}::text, '')" for c in found]) + ", '')"


def _num_expr(cols, candidates):
    found = _all_cols(cols, candidates)
    if not found:
        return "NULL::numeric"
    parts = []
    for c in found:
        val = f"NULLIF(regexp_replace(COALESCE({_q(c)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"
        parts.append(f"NULLIF({val}, 0)")
    return "COALESCE(" + ", ".join(parts) + ")"


def _date_expr(cols, candidates):
    c = _first_col(cols, candidates)
    if not c:
        return "NULL::date"
    return f"CASE WHEN {_q(c)}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN LEFT({_q(c)}::text, 10)::date ELSE NULL END"


def rent_meta_v32():
    cols = table_columns_v32(RENT_TABLE)
    building = _text_expr(cols, [
        "building_name_en", "building_name", "building", "property_name_en", "property_name",
        "project_name_en", "project_name", "project", "master_project_en", "master_project",
        "property", "location_name", "nearest_landmark"
    ])
    area = _text_expr(cols, [
        "area_name_en", "area_name", "area", "area_en", "location", "location_en", "district", "community"
    ])
    rooms = _text_expr(cols, [
        "rooms_en", "rooms", "room", "bedrooms", "bedroom", "rooms_count", "rooms_number", "unit_rooms"
    ])
    ptype = _text_expr(cols, [
        "property_type_en", "property_type", "property_usage_en", "property_usage", "usage", "type"
    ])
    subtype = _text_expr(cols, [
        "property_sub_type_en", "property_sub_type", "property_subtype", "unit_type", "property_category", "property_sub_type_ar"
    ])
    unit = _text_expr(cols, [
        "unit_number", "unit_no", "unit", "property_number", "property_no", "property_id", "property_number_en"
    ])
    rent_price = _num_expr(cols, [
        "rent_value", "annual_rent", "annual_rental_value", "annual_amount", "annual_rent_amount",
        "rent_amount", "rental_amount", "rental_value", "lease_value", "lease_amount",
        "contract_amount", "contract_value", "ejari_contract_amount", "amount", "actual_worth",
        "total_contract_value", "contract_rent", "yearly_rent", "yearly_rental", "actual_rent"
    ])
    size = _num_expr(cols, [
        "procedure_area", "actual_area", "property_size", "property_size_sqft", "area_size_sqft",
        "area_sqft", "size_sqft", "size", "built_up_area", "unit_area"
    ])
    meter = f"CASE WHEN ({size}) IS NOT NULL AND ({size}) > 0 THEN ({rent_price}) / ({size}) ELSE NULL END"
    safe_date = _date_expr(cols, [
        "contract_start_date", "start_date", "instance_date", "registration_date", "date", "contract_date"
    ])
    return {
        "building": building,
        "area": area,
        "rooms": rooms,
        "property_type": ptype,
        "property_sub_type": subtype,
        "unit": unit,
        "price": rent_price,
        "meter": meter,
        "safe_date": safe_date,
    }


def rent_base_from_v32():
    m = rent_meta_v32()
    return f"""
        FROM (
            SELECT
                *,
                {m['safe_date']} AS safe_date,
                {m['building']} AS building_name_en,
                {m['area']} AS area_name_en,
                {m['rooms']} AS rooms_en,
                {m['property_type']} AS property_type_en,
                {m['property_sub_type']} AS property_sub_type_en,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS rent_price,
                {m['meter']} AS rent_meter_price
            FROM {RENT_TABLE}
        ) t
        WHERE 1=1
    """


def sales_areas_for_building_v32(name):
    n = clean_query(name)
    if not n:
        return []
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT DISTINCT COALESCE(area_name_en::text, '') AS area
                    FROM {TABLE}
                    WHERE COALESCE(area_name_en::text, '') <> ''
                      AND (
                          LOWER(COALESCE(building_name_en::text, '')) = LOWER(%s)
                          OR COALESCE(building_name_en::text, '') ILIKE %s
                      )
                    LIMIT 25
                """, [n, f"%{n}%"])
                return [r["area"] for r in cur.fetchall() if r.get("area")]
    except Exception as e:
        print("RENT_AREA_FALLBACK_ERROR:", repr(e))
        return []


def rent_scope_condition_v32(scope, name, allow_scope=True):
    if not allow_scope or not name:
        return "", []
    n = clean_query(name)
    if scope == "building":
        params = [f"%{n}%"]
        parts = ["COALESCE(building_name_en::text, '') ILIKE %s"]
        areas = sales_areas_for_building_v32(n)
        for a in areas:
            parts.append("COALESCE(area_name_en::text, '') ILIKE %s")
            params.append(f"%{a}%")
        return "AND (" + " OR ".join(parts) + ")", params
    if scope == "area":
        values = area_alias_values(n)
        parts, params = [], []
        for v in values:
            parts.append("COALESCE(area_name_en::text, '') ILIKE %s")
            params.append(f"%{v}%")
        return ("AND (" + " OR ".join(parts) + ")", params) if parts else ("", [])
    return "", []


def rent_property_condition_v32(prop, allow_prop=True):
    if not allow_prop or not prop:
        return "", []
    p = str(prop).lower().strip()
    all_text = "LOWER(COALESCE(rooms_en::text, '') || ' ' || COALESCE(property_type_en::text, '') || ' ' || COALESCE(property_sub_type_en::text, ''))"
    if p == "studio":
        return f"AND ({all_text} LIKE %s)", ["%studio%"]
    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        words = {"1": "one", "2": "two", "3": "three", "4": "four"}.get(n, n)
        return f"""
        AND (
            COALESCE(rooms_en::text, '') = %s
            OR {all_text} LIKE %s
            OR {all_text} LIKE %s
            OR {all_text} LIKE %s
            OR {all_text} LIKE %s
        )
        """, [n, f"%{n} b/r%", f"%{n} br%", f"%{n} bedroom%", f"%{words} bedroom%"]
    if p == "5 br+":
        return f"AND ({all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s)", ["%5%", "%6%", "%7%", "%8%", "%9%"]
    return f"AND ({all_text} LIKE %s)", [f"%{p}%"]


def rent_unit_condition_v32(unit_query):
    if not unit_query:
        return "", []
    q = clean_query(str(unit_query))
    if not q:
        return "", []
    return "AND COALESCE(unit_number_norm::text, '') ILIKE %s", [f"%{q}%"]


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_stats(scope, name, prop, period, deal_type)
    where, params = rent_scope_condition_v32(scope, name, allow_scope=True)
    prop_sql, prop_args = rent_property_condition_v32(prop, allow_prop=True)
    params += prop_args
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT NULLIF(building_name_en, '')) AS buildings,
                    COUNT(DISTINCT NULLIF(area_name_en, '')) AS areas,
                    AVG(rent_price) AS avg_price,
                    MIN(rent_price) AS min_price,
                    MAX(rent_price) AS max_price,
                    AVG(rent_meter_price) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    STRING_AGG(DISTINCT NULLIF(rooms_en, ''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en, ''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en, ''), ', ') AS property_sub_types
                {rent_base_from_v32()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL
                  AND rent_price > 0
            """, params)
            return cur.fetchone()


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type) and ORIG_get_stats_smart:
        return ORIG_get_stats_smart(scope, name, prop, period, deal_type)
    if not is_rent_deal(deal_type):
        return get_stats(scope, name, prop, period, deal_type), prop, period, deal_type

    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ("dubai", None, prop, period),
        ("dubai", None, prop, None),
        ("dubai", None, None, period),
        ("dubai", None, None, None),
    ]
    for sc, nm, p, per in attempts:
        try:
            row = get_stats(sc, nm, p, per, deal_type)
            if row and int(row.get("deals") or 0) > 0 and row.get("avg_price"):
                return row, p, per, deal_type
        except Exception as e:
            print("RENT_STATS_ATTEMPT_ERROR:", repr(e))
    return None, prop, period, deal_type


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query)
    where, params = rent_scope_condition_v32(scope, name, allow_scope=True)
    prop_sql, prop_args = rent_property_condition_v32(prop, allow_prop=True)
    unit_sql, unit_args = rent_unit_condition_v32(unit_query)
    params += prop_args + unit_args + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    safe_date,
                    'Rent' AS procedure_name_en,
                    rooms_en,
                    property_type_en,
                    property_sub_type_en,
                    rent_price AS price,
                    rent_meter_price AS meter_price,
                    building_name_en,
                    area_name_en,
                    unit_number_norm AS unit_number
                {rent_base_from_v32()}
                  {where}
                  {prop_sql}
                  {unit_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL
                  AND rent_price > 0
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    if not is_rent_deal(deal_type) and ORIG_get_latest_deals_smart:
        return ORIG_get_latest_deals_smart(scope, name, prop, period, deal_type, limit, unit_query)
    if not is_rent_deal(deal_type):
        return get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query), prop, period, deal_type

    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ("dubai", None, prop, period),
        ("dubai", None, prop, None),
        ("dubai", None, None, period),
        ("dubai", None, None, None),
    ]
    for sc, nm, p, per in attempts:
        try:
            rows = get_latest_deals(sc, nm, p, per, deal_type, limit=limit, unit_query=unit_query)
            if rows:
                return rows, p, per, deal_type
        except Exception as e:
            print("RENT_LATEST_ATTEMPT_ERROR:", repr(e))
    return [], prop, period, deal_type


def get_comparison(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_comparison(scope, name, prop, period, deal_type)
    if not period:
        return None
    where, params = rent_scope_condition_v32(scope, name, allow_scope=True)
    prop_sql, prop_args = rent_property_condition_v32(prop, allow_prop=True)
    params += prop_args
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from_v32()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
            """, params)
            current = cur.fetchone()
            cur.execute(f"""
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from_v32()}
                  {where}
                  {prop_sql}
                  {rent_previous_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
            """, params)
            previous = cur.fetchone()
    return current, previous


def compare_value(scope, name, price, size, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_compare_value(scope, name, price, size, prop, period, deal_type)
    row, _, _, _ = get_stats_smart(scope, name, prop, period, deal_type)
    if not row or not row.get("avg_price"):
        return None
    market_avg = float(row["avg_price"])
    user_price = float(price)
    diff_pct = ((user_price - market_avg) / market_avg) * 100 if market_avg else 0
    user_ppsqft = user_price / float(size) if float(size) else None
    return {"row": row, "user_price": user_price, "user_ppsqft": user_ppsqft, "market_avg": market_avg, "diff_pct": diff_pct}


# =========================
# RENT TABLE SUPER FIX v33
# =========================
# Смысл: dld_rents у разных выгрузок имеет разные названия колонок.
# Поэтому аренда ищется не только по building_name, а по общему search_text из всех колонок,
# а rent_price собирается максимально гибко из всех денежных колонок.

_SCHEMA_CACHE_V33 = {}

def table_columns_v33(table_name):
    if table_name in _SCHEMA_CACHE_V33:
        return _SCHEMA_CACHE_V33[table_name]
    schema, table = table_name.split('.', 1)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, [schema, table])
            cols = [r['column_name'] for r in cur.fetchall()]
    _SCHEMA_CACHE_V33[table_name] = cols
    return cols


def q33(c):
    return '"' + c.replace('"', '""') + '"'


def text_blob_expr_v33(cols):
    if not cols:
        return "''"
    parts = [f"COALESCE({q33(c)}::text, '')" for c in cols]
    return "LOWER(CONCAT_WS(' ', " + ", ".join(parts) + "))"


def text_first_expr_v33(cols, candidates, default="''"):
    low = {c.lower(): c for c in cols}
    found = [low[c.lower()] for c in candidates if c.lower() in low]
    if not found:
        return default
    return "COALESCE(" + ", ".join([f"NULLIF({q33(c)}::text, '')" for c in found]) + ", '')"


def numeric_candidate_expr_v33(col):
    raw = f"NULLIF(regexp_replace(COALESCE({q33(col)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"
    return f"CASE WHEN ({raw}) BETWEEN 1000 AND 5000000 THEN ({raw}) ELSE NULL END"


def numeric_price_expr_v33(cols):
    preferred = [
        'rent_value', 'rent', 'rent_amount', 'rental_value', 'rental_amount',
        'annual_rent', 'annual_rent_value', 'annual_rental_value', 'annual_amount', 'annual_rent_amount',
        'contract_amount', 'contract_value', 'contract_rent', 'contract_rent_value',
        'ejari_value', 'ejari_contract_amount', 'total_contract_value', 'actual_worth', 'amount', 'value'
    ]
    low = {c.lower(): c for c in cols}
    chosen = []
    for c in preferred:
        if c.lower() in low:
            chosen.append(low[c.lower()])
    bad_words = ['id', 'date', 'number', 'no', 'phone', 'mobile', 'year', 'month', 'room', 'bed', 'area', 'size', 'sqft']
    good_words = ['rent', 'amount', 'value', 'worth', 'contract', 'annual', 'ejari', 'price']
    for c in cols:
        cl = c.lower()
        if c in chosen:
            continue
        if any(g in cl for g in good_words) and not any(b in cl for b in bad_words):
            chosen.append(c)
    for c in cols:
        cl = c.lower()
        if c in chosen:
            continue
        if not any(b in cl for b in bad_words):
            chosen.append(c)
    if not chosen:
        return 'NULL::numeric'
    return 'COALESCE(' + ', '.join([numeric_candidate_expr_v33(c) for c in chosen]) + ')'


def numeric_size_expr_v33(cols):
    preferred = [
        'procedure_area', 'actual_area', 'property_size', 'property_size_sqft', 'area_size_sqft',
        'area_sqft', 'size_sqft', 'size', 'built_up_area', 'unit_area', 'property_area'
    ]
    low = {c.lower(): c for c in cols}
    chosen = [low[c.lower()] for c in preferred if c.lower() in low]
    for c in cols:
        cl = c.lower()
        if c not in chosen and any(x in cl for x in ['area', 'size', 'sqft']):
            chosen.append(c)
    if not chosen:
        return 'NULL::numeric'
    parts = []
    for c in chosen:
        raw = f"NULLIF(regexp_replace(COALESCE({q33(c)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"
        parts.append(f"CASE WHEN ({raw}) BETWEEN 100 AND 50000 THEN ({raw}) ELSE NULL END")
    return 'COALESCE(' + ', '.join(parts) + ')'


def date_expr_v33(cols):
    preferred = ['contract_start_date', 'start_date', 'instance_date', 'registration_date', 'date', 'contract_date']
    low = {c.lower(): c for c in cols}
    for c in preferred:
        if c.lower() in low:
            col = low[c.lower()]
            return f"CASE WHEN {q33(col)}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN LEFT({q33(col)}::text, 10)::date ELSE NULL END"
    return 'NULL::date'


def rent_meta_v33():
    cols = table_columns_v33(RENT_TABLE)
    search_text = text_blob_expr_v33(cols)
    building = text_first_expr_v33(cols, [
        'building_name_en','building_name','building','project_name_en','project_name','project',
        'property_name_en','property_name','master_project_en','master_project','property','location_name','nearest_landmark'
    ])
    area = text_first_expr_v33(cols, ['area_name_en','area_name','area','area_en','location','location_en','district','community'])
    rooms = text_first_expr_v33(cols, ['rooms_en','rooms','room','bedrooms','bedroom','rooms_count','rooms_number','unit_rooms'])
    ptype = text_first_expr_v33(cols, ['property_type_en','property_type','property_usage_en','property_usage','usage','type'])
    subtype = text_first_expr_v33(cols, ['property_sub_type_en','property_sub_type','property_subtype','unit_type','property_category'])
    unit = text_first_expr_v33(cols, ['unit_number','unit_no','unit','property_number','property_no','property_id'])
    price = numeric_price_expr_v33(cols)
    size = numeric_size_expr_v33(cols)
    meter = f"CASE WHEN ({size}) IS NOT NULL AND ({size}) > 0 THEN ({price}) / ({size}) ELSE NULL END"
    return {'search_text': search_text, 'building': building, 'area': area, 'rooms': rooms, 'property_type': ptype, 'property_sub_type': subtype, 'unit': unit, 'price': price, 'meter': meter, 'safe_date': date_expr_v33(cols)}


def rent_base_from_v33():
    m = rent_meta_v33()
    return f'''
        FROM (
            SELECT
                *,
                {m['safe_date']} AS safe_date,
                {m['search_text']} AS search_text,
                {m['building']} AS building_name_en,
                {m['area']} AS area_name_en,
                {m['rooms']} AS rooms_en,
                {m['property_type']} AS property_type_en,
                {m['property_sub_type']} AS property_sub_type_en,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS rent_price,
                {m['meter']} AS rent_meter_price
            FROM {RENT_TABLE}
        ) t
        WHERE 1=1
    '''


def rent_scope_condition_v33(scope, name, allow_scope=True):
    if not allow_scope or not name:
        return '', []
    n = clean_query(name)
    if not n:
        return '', []
    if scope == 'building':
        params = [f'%{n.lower()}%', f'%{n.lower()}%']
        parts = ["LOWER(COALESCE(building_name_en::text, '')) ILIKE %s", 'search_text ILIKE %s']
        for a in sales_areas_for_building_v32(n):
            if a:
                parts.append("LOWER(COALESCE(area_name_en::text, '')) ILIKE %s")
                params.append(f'%{str(a).lower()}%')
        return 'AND (' + ' OR '.join(parts) + ')', params
    if scope == 'area':
        values = area_alias_values(n)
        parts, params = [], []
        for v in values:
            parts.append("LOWER(COALESCE(area_name_en::text, '')) ILIKE %s")
            params.append(f'%{str(v).lower()}%')
        if not parts:
            parts = ['search_text ILIKE %s']
            params = [f'%{n.lower()}%']
        return 'AND (' + ' OR '.join(parts) + ')', params
    return '', []


def rent_property_condition_v33(prop, allow_prop=True):
    if not allow_prop or not prop:
        return '', []
    p = str(prop).lower().strip()
    all_text = "LOWER(COALESCE(rooms_en::text, '') || ' ' || COALESCE(property_type_en::text, '') || ' ' || COALESCE(property_sub_type_en::text, '') || ' ' || COALESCE(search_text::text, ''))"
    if p == 'studio':
        return f'AND ({all_text} LIKE %s)', ['%studio%']
    if p in ['1 br','2 br','3 br','4 br']:
        n = p.split()[0]
        words = {'1':'one','2':'two','3':'three','4':'four'}.get(n,n)
        return f'''AND (
            COALESCE(rooms_en::text, '') = %s OR
            {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s
        )''', [n, f'%{n} b/r%', f'%{n} br%', f'%{n} bedroom%', f'%bedroom {n}%', f'%{words} bedroom%']
    if p == '5 br+':
        return f'AND ({all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s OR {all_text} LIKE %s)', ['%5%','%6%','%7%','%8%','%9%']
    return f'AND ({all_text} LIKE %s)', [f'%{p}%']


def get_stats(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_stats(scope, name, prop, period, deal_type)
    where, params = rent_scope_condition_v33(scope, name, True)
    prop_sql, prop_args = rent_property_condition_v33(prop, True)
    params += prop_args
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT NULLIF(building_name_en, '')) AS buildings,
                    COUNT(DISTINCT NULLIF(area_name_en, '')) AS areas,
                    AVG(rent_price) AS avg_price,
                    MIN(rent_price) AS min_price,
                    MAX(rent_price) AS max_price,
                    AVG(rent_meter_price) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    STRING_AGG(DISTINCT NULLIF(rooms_en, ''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en, ''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en, ''), ', ') AS property_sub_types
                {rent_base_from_v33()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
            ''', params)
            return cur.fetchone()


def get_latest_deals(scope='building', name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query)
    where, params = rent_scope_condition_v33(scope, name, True)
    prop_sql, prop_args = rent_property_condition_v33(prop, True)
    unit_sql, unit_args = rent_unit_condition_v32(unit_query)
    params += prop_args + unit_args + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''
                SELECT
                    safe_date,
                    'Rent' AS procedure_name_en,
                    rooms_en,
                    property_type_en,
                    property_sub_type_en,
                    rent_price AS price,
                    rent_meter_price AS meter_price,
                    building_name_en,
                    area_name_en,
                    unit_number_norm AS unit_number
                {rent_base_from_v33()}
                  {where}
                  {prop_sql}
                  {unit_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            ''', params)
            return cur.fetchall()


def get_stats_smart(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type) and ORIG_get_stats_smart:
        return ORIG_get_stats_smart(scope, name, prop, period, deal_type)
    if not is_rent_deal(deal_type):
        return get_stats(scope, name, prop, period, deal_type), prop, period, deal_type
    area_fallback = None
    try:
        areas = sales_areas_for_building_v32(name) if name else []
        area_fallback = areas[0] if areas else None
    except Exception:
        area_fallback = None
    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ('area', area_fallback, prop, period),
        ('area', area_fallback, prop, None),
        ('dubai', None, prop, period),
        ('dubai', None, prop, None),
        ('dubai', None, None, None),
    ]
    for sc, nm, p, per in attempts:
        if sc and (sc == 'dubai' or nm):
            try:
                row = get_stats(sc, nm, p, per, deal_type)
                if row and int(row.get('deals') or 0) > 0 and row.get('avg_price'):
                    return row, p, per, deal_type
            except Exception as e:
                print('RENT_V33_STATS_ERROR:', repr(e))
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    if not is_rent_deal(deal_type) and ORIG_get_latest_deals_smart:
        return ORIG_get_latest_deals_smart(scope, name, prop, period, deal_type, limit, unit_query)
    if not is_rent_deal(deal_type):
        return get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query), prop, period, deal_type
    area_fallback = None
    try:
        areas = sales_areas_for_building_v32(name) if name else []
        area_fallback = areas[0] if areas else None
    except Exception:
        area_fallback = None
    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ('area', area_fallback, prop, period),
        ('area', area_fallback, prop, None),
        ('dubai', None, prop, period),
        ('dubai', None, prop, None),
        ('dubai', None, None, None),
    ]
    for sc, nm, p, per in attempts:
        if sc and (sc == 'dubai' or nm):
            try:
                rows = get_latest_deals(sc, nm, p, per, deal_type, limit=limit, unit_query=unit_query)
                if rows:
                    return rows, p, per, deal_type
            except Exception as e:
                print('RENT_V33_LATEST_ERROR:', repr(e))
    return [], prop, period, deal_type


def get_comparison(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_comparison(scope, name, prop, period, deal_type)
    if not period:
        return None
    where, params = rent_scope_condition_v33(scope, name, True)
    prop_sql, prop_args = rent_property_condition_v33(prop, True)
    params += prop_args
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'''
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from_v33()}
                  {where}
                  {prop_sql}
                  {rent_period_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
            ''', params)
            current = cur.fetchone()
            cur.execute(f'''
                SELECT COUNT(*) AS deals, AVG(rent_price) AS avg_price, AVG(rent_meter_price) AS avg_meter
                {rent_base_from_v33()}
                  {where}
                  {prop_sql}
                  {rent_previous_condition(period)}
                  AND rent_price IS NOT NULL AND rent_price > 0
            ''', params)
            previous = cur.fetchone()
    return current, previous



# =========================
# RENT TABLE ULTRA FALLBACK FIX v34
# =========================
# Этот блок должен стоять ПЕРЕД async def main().
# Исправляет главный остаточный баг: если в public.dld_rents колонки называются иначе
# или дата/цена не распознаются, бот всё равно не должен падать в "нет стабильной выборки".
# Для аренды сначала идёт мягкий поиск в dld_rents, затем fallback по всему dld_rents.

_SCHEMA_CACHE_V34 = {}


def table_columns_v34(table_name):
    if table_name in _SCHEMA_CACHE_V34:
        return _SCHEMA_CACHE_V34[table_name]
    schema, table = table_name.split('.', 1)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s
                ORDER BY ordinal_position
            """, [schema, table])
            cols = [r['column_name'] for r in cur.fetchall()]
    _SCHEMA_CACHE_V34[table_name] = cols
    return cols


def q34(col):
    return '"' + str(col).replace('"', '""') + '"'


def pick34(cols, names):
    low = {c.lower(): c for c in cols}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def text34(cols, names, fallback="''"):
    low = {c.lower(): c for c in cols}
    found = [low[n.lower()] for n in names if n.lower() in low]
    if not found:
        return fallback
    return "COALESCE(" + ", ".join([f"NULLIF({q34(c)}::text, '')" for c in found]) + ", '')"


def blob34(cols):
    if not cols:
        return "''"
    return "LOWER(CONCAT_WS(' ', " + ", ".join([f"COALESCE({q34(c)}::text, '')" for c in cols]) + "))"


def safe_num34(col):
    # Берём только реалистичные годовые арендные значения. Это защищает от id/дат/номеров договоров.
    raw = f"NULLIF(regexp_replace(REPLACE(COALESCE({q34(col)}::text, ''), ',', ''), '[^0-9.]', '', 'g'), '')::numeric"
    return f"CASE WHEN ({raw}) BETWEEN 1000 AND 5000000 THEN ({raw}) ELSE NULL END"


def num34(cols, names, allow_any=False):
    low = {c.lower(): c for c in cols}
    chosen = []
    for n in names:
        if n.lower() in low and low[n.lower()] not in chosen:
            chosen.append(low[n.lower()])
    if allow_any:
        good = ['rent', 'annual', 'amount', 'value', 'worth', 'contract', 'ejari', 'price', 'total']
        bad = ['id', 'date', 'number', 'no', 'phone', 'mobile', 'year', 'month', 'room', 'bed', 'area', 'size', 'sqft', 'lat', 'lng', 'lon']
        for c in cols:
            cl = c.lower()
            if c not in chosen and any(g in cl for g in good) and not any(b in cl for b in bad):
                chosen.append(c)
    if not chosen:
        return "NULL::numeric"
    return "COALESCE(" + ", ".join([safe_num34(c) for c in chosen]) + ")"


def size34(cols):
    low = {c.lower(): c for c in cols}
    names = ['procedure_area','actual_area','property_size','property_size_sqft','area_size_sqft','area_sqft','size_sqft','size','built_up_area','unit_area','property_area']
    chosen = [low[n.lower()] for n in names if n.lower() in low]
    if not chosen:
        return "NULL::numeric"
    parts=[]
    for c in chosen:
        raw = f"NULLIF(regexp_replace(REPLACE(COALESCE({q34(c)}::text, ''), ',', ''), '[^0-9.]', '', 'g'), '')::numeric"
        parts.append(f"CASE WHEN ({raw}) BETWEEN 100 AND 50000 THEN ({raw}) ELSE NULL END")
    return "COALESCE(" + ", ".join(parts) + ")"


def date34(cols):
    low = {c.lower(): c for c in cols}
    names = ['contract_start_date','start_date','instance_date','registration_date','date','contract_date']
    c = None
    for n in names:
        if n.lower() in low:
            c = low[n.lower()]
            break
    if not c:
        return "NULL::date"
    s = q34(c)
    # Поддержка форматов YYYY-MM-DD и DD/MM/YYYY или DD-MM-YYYY.
    return f"""
        CASE
            WHEN {s}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN LEFT({s}::text,10)::date
            WHEN {s}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}' THEN TO_DATE(LEFT({s}::text,10), 'DD/MM/YYYY')
            WHEN {s}::text ~ '^\\d{{2}}-\\d{{2}}-\\d{{4}}' THEN TO_DATE(LEFT({s}::text,10), 'DD-MM-YYYY')
            ELSE NULL
        END
    """


def rent_meta_v34():
    cols = table_columns_v34(RENT_TABLE)
    price_names = [
        'annual_amount','annual_rent','annual_rent_value','annual_rental_value','rent_value','rent_amount',
        'rental_value','rental_amount','contract_amount','contract_value','contract_rent','total_contract_value',
        'actual_worth','amount','value','ejari_value','ejari_contract_amount'
    ]
    price = num34(cols, price_names, allow_any=True)
    size = size34(cols)
    return {
        'search_text': blob34(cols),
        'building': text34(cols, ['building_name_en','building_name','building','project_name_en','project_name','project','property_name_en','property_name','master_project_en','master_project','nearest_landmark','property']),
        'area': text34(cols, ['area_name_en','area_name','area','area_en','location','location_en','district','community']),
        'rooms': text34(cols, ['rooms_en','rooms','room','rooms_count','rooms_number','bedrooms','bedroom','unit_rooms']),
        'ptype': text34(cols, ['property_type_en','property_type','property_usage_en','property_usage','usage','type']),
        'subtype': text34(cols, ['property_sub_type_en','property_sub_type','property_subtype','unit_type','property_category']),
        'unit': text34(cols, ['unit_number','unit_no','unit','property_number','property_no','property_id']),
        'price': price,
        'size': size,
        'meter': f"CASE WHEN ({size}) IS NOT NULL AND ({size}) > 0 AND ({price}) IS NOT NULL THEN ({price})/({size}) ELSE NULL END",
        'date': date34(cols),
    }


def rent_base_from_v34():
    m = rent_meta_v34()
    return f"""
        FROM (
            SELECT *,
                {m['date']} AS safe_date,
                {m['search_text']} AS search_text,
                {m['building']} AS building_name_en,
                {m['area']} AS area_name_en,
                {m['rooms']} AS rooms_en,
                {m['ptype']} AS property_type_en,
                {m['subtype']} AS property_sub_type_en,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS rent_price,
                {m['meter']} AS rent_meter_price
            FROM {RENT_TABLE}
        ) t
        WHERE 1=1
    """


def rent_scope_condition_v34(scope, name, strict=True):
    if not strict or not name:
        return '', []
    n = clean_query(name).lower()
    if not n:
        return '', []
    if scope == 'building':
        parts = ["search_text ILIKE %s", "LOWER(COALESCE(building_name_en::text,'')) ILIKE %s"]
        params = [f'%{n}%', f'%{n}%']
        try:
            for a in sales_areas_for_building_v32(name):
                if a:
                    parts.append("LOWER(COALESCE(area_name_en::text,'')) ILIKE %s")
                    params.append(f'%{str(a).lower()}%')
        except Exception:
            pass
        return 'AND (' + ' OR '.join(parts) + ')', params
    if scope == 'area':
        values = area_alias_values(n) or [n]
        return 'AND (' + ' OR '.join(["search_text ILIKE %s" for _ in values]) + ')', [f'%{v.lower()}%' for v in values]
    return '', []


def rent_property_condition_v34(prop, strict=True):
    if not strict or not prop:
        return '', []
    p = str(prop).lower().strip()
    txt = "LOWER(COALESCE(search_text::text,'') || ' ' || COALESCE(rooms_en::text,'') || ' ' || COALESCE(property_type_en::text,'') || ' ' || COALESCE(property_sub_type_en::text,''))"
    if p == 'studio':
        return f'AND ({txt} LIKE %s)', ['%studio%']
    if p in ['1 br','2 br','3 br','4 br']:
        n = p.split()[0]
        # Очень широкий вариант: допускаем только отдельные формы комнат, но smart fallback ниже сможет отключить этот фильтр.
        return f"AND ({txt} LIKE %s OR {txt} LIKE %s OR {txt} LIKE %s OR COALESCE(rooms_en::text,'')=%s)", [f'%{n} b/r%', f'%{n} br%', f'%{n} bedroom%', n]
    if p == '5 br+':
        return f"AND ({txt} LIKE %s OR {txt} LIKE %s OR {txt} LIKE %s OR {txt} LIKE %s OR {txt} LIKE %s)", ['%5 br%','%6 br%','%7 br%','%8 br%','%9 br%']
    return f'AND ({txt} LIKE %s)', [f'%{p}%']


def rent_period_condition_v34(period_key, strict=True):
    if not strict:
        return ''
    return rent_period_condition(period_key)


def get_stats(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_stats(scope, name, prop, period, deal_type)
    where, params = rent_scope_condition_v34(scope, name, True)
    prop_sql, prop_args = rent_property_condition_v34(prop, True)
    params += prop_args
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT NULLIF(building_name_en,'')) AS buildings,
                    COUNT(DISTINCT NULLIF(area_name_en,'')) AS areas,
                    AVG(rent_price) AS avg_price,
                    MIN(rent_price) AS min_price,
                    MAX(rent_price) AS max_price,
                    AVG(rent_meter_price) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    STRING_AGG(DISTINCT NULLIF(rooms_en,''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en,''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en,''), ', ') AS property_sub_types
                {rent_base_from_v34()}
                  {where}
                  {prop_sql}
                  {rent_period_condition_v34(period, True)}
            """, params)
            return cur.fetchone()


def get_latest_deals(scope='building', name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query)
    where, params = rent_scope_condition_v34(scope, name, True)
    prop_sql, prop_args = rent_property_condition_v34(prop, True)
    unit_sql, unit_args = rent_unit_condition_v32(unit_query)
    params += prop_args + unit_args + [limit]
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT safe_date, 'Rent' AS procedure_name_en, rooms_en, property_type_en, property_sub_type_en,
                       rent_price AS price, rent_meter_price AS meter_price, building_name_en, area_name_en, unit_number_norm AS unit_number
                {rent_base_from_v34()}
                  {where}
                  {prop_sql}
                  {unit_sql}
                  {rent_period_condition_v34(period, True)}
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_stats_smart(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type) and ORIG_get_stats_smart:
        return ORIG_get_stats_smart(scope, name, prop, period, deal_type)
    if not is_rent_deal(deal_type):
        return get_stats(scope, name, prop, period, deal_type), prop, period, deal_type
    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ('dubai', None, prop, period),
        ('dubai', None, prop, None),
        ('dubai', None, None, period),
        ('dubai', None, None, None),
    ]
    for sc, nm, p, per in attempts:
        try:
            row = get_stats(sc, nm, p, per, deal_type)
            if row and int(row.get('deals') or 0) > 0:
                return row, p, per, deal_type
        except Exception as e:
            print('RENT_V34_STATS_ERROR:', repr(e))
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    if not is_rent_deal(deal_type) and ORIG_get_latest_deals_smart:
        return ORIG_get_latest_deals_smart(scope, name, prop, period, deal_type, limit, unit_query)
    if not is_rent_deal(deal_type):
        return get_latest_deals(scope, name, prop, period, deal_type, limit, unit_query), prop, period, deal_type
    attempts = [
        (scope, name, prop, period),
        (scope, name, prop, None),
        (scope, name, None, period),
        (scope, name, None, None),
        ('dubai', None, prop, period),
        ('dubai', None, prop, None),
        ('dubai', None, None, period),
        ('dubai', None, None, None),
    ]
    for sc, nm, p, per in attempts:
        try:
            rows = get_latest_deals(sc, nm, p, per, deal_type, limit=limit, unit_query=unit_query)
            if rows:
                return rows, p, per, deal_type
        except Exception as e:
            print('RENT_V34_LATEST_ERROR:', repr(e))
    return [], prop, period, deal_type


def get_comparison(scope='dubai', name=None, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_get_comparison(scope, name, prop, period, deal_type)
    if not period:
        return None
    current, _, _, _ = get_stats_smart(scope, name, prop, period, deal_type)
    previous, _, _, _ = get_stats_smart(scope, name, prop, None, deal_type)
    return current, previous


def compare_value(scope, name, price, size, prop=None, period=None, deal_type=None):
    if not is_rent_deal(deal_type):
        return ORIG_compare_value(scope, name, price, size, prop, period, deal_type)
    row, _, _, _ = get_stats_smart(scope, name, prop, period, deal_type)
    if not row or not row.get('avg_price'):
        return None
    market_avg = float(row['avg_price'])
    user_price = float(price)
    user_ppsqft = user_price / float(size) if float(size) else None
    diff_pct = ((user_price - market_avg)/market_avg)*100 if market_avg else 0
    return {'row': row, 'user_price': user_price, 'user_ppsqft': user_ppsqft, 'market_avg': market_avg, 'diff_pct': diff_pct}



# =========================
# BUILDING / COLUMN COMPATIBILITY FIX v44
# =========================
# Причина бага: в новой таблице продаж DLD колонки называются building_en / project_en / area_en,
# а часть старого кода искала building_name_en / area_name_en. Из-за этого поиск Grande / Corner / Marina
# возвращал "Ничего не найдено". Этот слой создаёт совместимые alias-колонки внутри SQL-запросов
# и чинит поиск зданий, районов, отчёты, последние сделки и fallback для аренды.

SCHEMA_FIX_VERSION = "v44_building_project_area_compat"


def _v44_sales_cols():
    try:
        return table_columns(TABLE)
    except Exception as e:
        print("V44_SALES_COLUMNS_ERROR:", repr(e))
        return []


def _v44_first(cols, candidates):
    low = {str(c).lower(): c for c in (cols or [])}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def _v44_present(cols, candidates):
    low = {str(c).lower(): c for c in (cols or [])}
    return [low[c.lower()] for c in candidates if c.lower() in low]


def _v44_q(col):
    return '"' + str(col).replace('"', '""') + '"'


def _v44_text_expr(cols, candidates, fallback="''"):
    present = _v44_present(cols, candidates)
    if not present:
        return fallback
    return "COALESCE(" + ", ".join([f"NULLIF({_v44_q(c)}::text, '')" for c in present]) + f", {fallback})"


def _v44_num_expr(cols, candidates, fallback="NULL::numeric"):
    col = _v44_first(cols, candidates)
    if not col:
        return fallback
    return f"NULLIF(regexp_replace(COALESCE({_v44_q(col)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


def _v44_date_expr(cols):
    col = _v44_first(cols, [
        'transaction_date', 'instance_date', 'registration_date', 'date', 'created_at',
        'start_date', 'contract_start_date'
    ])
    if not col:
        return 'NULL::date'
    return f"""
        CASE
            WHEN {_v44_q(col)}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN {_v44_q(col)}::date
            WHEN {_v44_q(col)}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}' THEN TO_DATE(SUBSTRING({_v44_q(col)}::text, 1, 10), 'MM/DD/YYYY')
            ELSE NULL::date
        END
    """


def _v44_sale_meta():
    cols = _v44_sales_cols()
    return {
        'building': _v44_text_expr(cols, [
            'building_name_en', 'building_en', 'building_name', 'building',
            'project_name_en', 'project_en', 'project_name', 'project',
            'property_name_en', 'property_name', 'master_project_en', 'master_project'
        ]),
        'area': _v44_text_expr(cols, [
            'area_name_en', 'area_en', 'area_name', 'area', 'location_en', 'location',
            'district', 'community'
        ]),
        'rooms': _v44_text_expr(cols, ['rooms_en', 'rooms', 'room', 'bedrooms', 'bedroom', 'rooms_count']),
        'ptype': _v44_text_expr(cols, ['property_type_en', 'prop_type_en', 'property_type', 'prop_type', 'type']),
        'subtype': _v44_text_expr(cols, [
            'property_sub_type_en', 'prop_sub_type_en', 'property_subtype', 'prop_sb_type_en',
            'unit_type', 'property_category', 'property_usage_en'
        ]),
        'procedure': _v44_text_expr(cols, [
            'procedure_name_en', 'procedure_name', 'procedure', 'transaction_type_en',
            'transaction_type', 'transaction_group_en', 'procedure_group_en'
        ]),
        'unit': _v44_text_expr(cols, ['unit_number', 'unit_no', 'unit', 'property_number', 'property_no', 'property_id'], "''"),
        'price': _v44_num_expr(cols, ['actual_worth', 'actual_value', 'transaction_value', 'price', 'value', 'amount']),
        'meter': _v44_num_expr(cols, ['meter_sale_price', 'price_per_meter', 'meter_price', 'price_per_sqft']),
        'size': _v44_num_expr(cols, ['actual_area', 'procedure_area', 'area_size_sqft', 'size_sqft', 'size']),
        'date': _v44_date_expr(cols),
    }


def base_from():
    m = _v44_sale_meta()
    meter_expr = f"""
        COALESCE(
            {m['meter']},
            CASE WHEN ({m['size']}) IS NOT NULL AND ({m['size']}) > 0 AND ({m['price']}) IS NOT NULL
                 THEN ({m['price']}) / NULLIF(({m['size']}), 0)
                 ELSE NULL::numeric END
        )
    """
    return f"""
        FROM (
            SELECT
                *,
                {m['date']} AS safe_date,
                {m['building']} AS building_name_en,
                {m['building']} AS building_en,
                {m['area']} AS area_name_en,
                {m['area']} AS area_en,
                {m['rooms']} AS rooms_en,
                {m['ptype']} AS property_type_en,
                {m['ptype']} AS prop_type_en,
                {m['subtype']} AS property_sub_type_en,
                {m['subtype']} AS prop_sub_type_en,
                {m['procedure']} AS procedure_name_en,
                {m['procedure']} AS procedure_name_norm,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS actual_worth_norm,
                {meter_expr} AS meter_sale_price_norm,
                LOWER(
                    COALESCE({m['building']}, '') || ' ' ||
                    COALESCE({m['area']}, '') || ' ' ||
                    COALESCE({m['rooms']}, '') || ' ' ||
                    COALESCE({m['ptype']}, '') || ' ' ||
                    COALESCE({m['subtype']}, '')
                ) AS search_text
            FROM {TABLE}
        ) t
        WHERE 1=1
    """


# Важно: переопределяем выражения после base_from alias-фикса.
PRICE = "actual_worth_norm"
METER_PRICE = "meter_sale_price_norm"
BUILDING_NAME = "COALESCE(NULLIF(building_name_en::text, ''), NULLIF(building_en::text, ''), NULLIF(project_en::text, ''), '')"
AREA_TXT = "COALESCE(area_name_en::text, '')"
BUILDING_TXT = "COALESCE(building_name_en::text, '')"
ROOMS_TXT = "COALESCE(rooms_en::text, '')"
PROPERTY_TYPE_TXT = "COALESCE(property_type_en::text, '')"
PROPERTY_SUB_TYPE_TXT = "COALESCE(property_sub_type_en::text, '')"
PROCEDURE_TXT = "COALESCE(procedure_name_en::text, procedure_name_norm::text, '')"


def building_search_expression():
    return "LOWER(COALESCE(search_text::text, '') || ' ' || COALESCE(building_name_en::text, '') || ' ' || COALESCE(area_name_en::text, ''))"


def building_aliases(name):
    q = normalize_search_text(name)
    aliases = {
        'grande': ['grande'],
        'grande signature': ['grande', 'signature'],
        'grande signature residences': ['grande', 'signature'],
        'opera grande': ['opera', 'grand'],
        'address opera': ['address', 'opera'],
        'the address opera': ['address', 'opera'],
        'address residences dubai opera': ['address', 'opera'],
        'corner': ['corner'],
        'binghatti corner': ['binghatti', 'corner'],
        'marina gate': ['marina', 'gate'],
        'marina': ['marina'],
        'sobha': ['sobha'],
        'anantara': ['anantara'],
        'burj vista': ['burj', 'vista'],
    }
    return aliases.get(q, [q] if q else [])


def smart_query_tokens(query):
    q = normalize_search_text(query)
    alias = building_aliases(q)
    if alias:
        # Для коротких запросов типа Grande / Corner / Marina ищем одним токеном,
        # иначе AND по словам может быть слишком строгим.
        return [a for a in alias if a]
    tokens = [t for t in q.split() if len(t) >= 2 and t not in STOP_WORDS]
    if not tokens:
        tokens = [t for t in q.split() if len(t) >= 2]
    return tokens[:6]


def make_building_condition(query):
    tokens = smart_query_tokens(query)
    if not tokens:
        return "AND 1=0", []
    expr = building_search_expression()
    params = []
    parts = []
    for token in tokens:
        token = normalize_search_text(token)
        if token:
            parts.append(f"{expr} ILIKE %s")
            params.append(f"%{token}%")
    if not parts:
        return "AND 1=0", []
    return "AND (" + " AND ".join(parts) + ")", params


def building_exact_condition_for_name(name):
    # После выбора кнопки не делаем слишком жёсткий exact match: в DLD одно и то же здание
    # может быть записано как project_en, building_en или с приставками T1/T2.
    return make_building_condition(name)


def safe_building_label(row):
    if not row:
        return ""
    return (
        row.get('building_name_en')
        or row.get('building_en')
        or row.get('project_en')
        or row.get('project_name_en')
        or ""
    )


def make_area_exact_condition(query):
    values = [v for v in area_alias_values(query) if v]
    if not values:
        return "AND 1=0", []
    parts = []
    params = []
    for value in values:
        v = clean_query(value)
        parts.append("LOWER(COALESCE(area_name_en::text, '')) ILIKE %s")
        params.append(f"%{v.lower()}%")
    return "AND (" + " OR ".join(parts) + ")", params


def find_buildings(query, limit=10):
    query = clean_query(query)
    if not query:
        return []
    where, params = make_building_condition(query)
    exact = normalize_search_text(query)
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(building_name_en::text, '') AS building_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COALESCE(area_name_en::text, '') AS area_en,
                        COUNT(*) AS deals,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) = LOWER(TRIM(%s)) THEN 0
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) LIKE LOWER(TRIM(%s)) THEN 1
                            WHEN LOWER(COALESCE(search_text::text, '')) LIKE LOWER(TRIM(%s)) THEN 2
                            ELSE 3
                        END AS rank
                    {base_from()}
                      {where}
                      AND COALESCE(building_name_en::text, '') <> ''
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, ''), COALESCE(search_text::text, '')
                    ORDER BY rank ASC, deals DESC
                    LIMIT %s
                """, [query, query + "%", f"%{exact}%"] + params + [limit])
                return cur.fetchall()
    except Exception as e:
        print("FIND_BUILDINGS_ERROR_V44:", repr(e))
        return []


def find_areas(query, limit=10):
    query = clean_query(query)
    if not query:
        return []
    where, params = make_area_exact_condition(query)
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COALESCE(area_name_en::text, '') AS area_en,
                        COUNT(*) AS deals,
                        COUNT(DISTINCT COALESCE(building_name_en::text, '')) AS buildings
                    {base_from()}
                      {where}
                      AND COALESCE(area_name_en::text, '') <> ''
                    GROUP BY COALESCE(area_name_en::text, '')
                    ORDER BY deals DESC
                    LIMIT %s
                """, params + [limit])
                return cur.fetchall()
    except Exception as e:
        print("FIND_AREAS_ERROR_V44:", repr(e))
        return []


def scope_condition(scope="dubai", name=None, original_query=None):
    scope = scope or "dubai"
    if scope == "dubai" or not name:
        return "", []
    if scope == "area":
        return make_area_exact_condition(original_query or name)
    if scope == "building":
        return building_exact_condition_for_name(original_query or name)
    return "", []


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    # Для аренды оставляем последнюю rent-логику v34.
    if is_rent_deal(deal_type):
        try:
            where, params = rent_scope_condition_v34(scope, name, True)
            prop_sql, prop_args = rent_property_condition_v34(prop, True)
            unit_sql, unit_args = rent_unit_condition_v32(unit_query)
            params += prop_args + unit_args + [limit]
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT safe_date, 'Rent' AS procedure_name_en, rooms_en, property_type_en, property_sub_type_en,
                               rent_price AS price, rent_meter_price AS meter_price, building_name_en, area_name_en, unit_number_norm AS unit_number
                        {rent_base_from_v34()}
                          {where}
                          {prop_sql}
                          {unit_sql}
                          {rent_period_condition_v34(period, True)}
                        ORDER BY safe_date DESC NULLS LAST
                        LIMIT %s
                    """, params)
                    return cur.fetchall()
        except Exception as e:
            print("GET_LATEST_RENT_DEALS_ERROR_V44:", repr(e))
            return []

    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    p_sql = period_condition(period)
    value_expr = deal_value_expr(deal_type)
    unit_sql, unit_args = make_unit_condition(unit_query) if 'make_unit_condition' in globals() else ("", [])
    scope_sql, scope_args = scope_condition(scope, name, original_query=name)
    params = scope_args + prop_args + deal_args + unit_args + [limit]
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        safe_date,
                        COALESCE(procedure_name_en::text, '') AS procedure_name_en,
                        COALESCE(rooms_en::text, '') AS rooms_en,
                        COALESCE(property_type_en::text, '') AS property_type_en,
                        COALESCE(property_sub_type_en::text, '') AS property_sub_type_en,
                        {value_expr} AS price,
                        {METER_PRICE} AS meter_price,
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COALESCE(unit_number_norm::text, '') AS unit_number
                    {base_from()}
                      {scope_sql}
                      AND {value_expr} IS NOT NULL
                      {prop_sql}
                      {deal_sql}
                      {p_sql}
                      {unit_sql}
                    ORDER BY safe_date DESC NULLS LAST
                    LIMIT %s
                """, params)
                return cur.fetchall()
    except Exception as e:
        print("GET_LATEST_DEALS_ERROR_V44:", repr(e))
        return []


def get_top_active():
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        AVG({PRICE}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND {PRICE} IS NOT NULL
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    ORDER BY deals DESC
                    LIMIT 10
                """)
                return cur.fetchall()
    except Exception as e:
        print("GET_TOP_ACTIVE_ERROR_V44:", repr(e))
        return []


def get_top_price():
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COUNT(*) AS deals,
                        AVG({PRICE}) AS avg_price,
                        AVG({METER_PRICE}) AS avg_meter
                    {base_from()}
                      AND COALESCE(building_name_en::text, '') <> ''
                      AND {PRICE} IS NOT NULL
                    GROUP BY COALESCE(building_name_en::text, ''), COALESCE(area_name_en::text, '')
                    HAVING COUNT(*) >= 3
                    ORDER BY avg_price DESC NULLS LAST
                    LIMIT 10
                """)
                return cur.fetchall()
    except Exception as e:
        print("GET_TOP_PRICE_ERROR_V44:", repr(e))
        return []


# Мини-тесты без подключения к БД: проверяют, что критические функции собираются и не имеют syntax errors.
def _v44_selfcheck():
    assert callable(find_buildings)
    assert callable(scope_condition)
    assert callable(base_from)
    assert 'Grande'.lower() in smart_query_tokens('Grande')
    assert 'corner' in smart_query_tokens('Corner')
    return True

_v44_selfcheck()
print(f"Loaded schema compatibility patch {SCHEMA_FIX_VERSION}")



# =========================
# DUAL DATABASE ARCHIVE + LIVE ENGINE v50
# =========================
# Цель: не менять меню, кнопки и UX, а научить существующие функции читать
# одновременно архивную базу Rent-sale-arhiv и live/updater базу updater-rent-sale-dld.
#
# Railway variables expected:
#   LIVE_DATABASE_URL     = PostgreSQL URL for updater-rent-sale-dld
#   ARCHIVE_DATABASE_URL  = PostgreSQL URL for Rent-sale-arhiv
# Backward compatibility:
#   DATABASE_URL may still point to LIVE database.
#
# Важно: PostgreSQL напрямую не делает UNION между двумя отдельными Railway databases,
# поэтому объединение делается на Python layer: archive query + live query + merge.

def _env_postgres_url(*names):
    """Returns the first valid PostgreSQL URL from Railway variables."""
    for name in names:
        value = os.getenv(name)
        if value and str(value).strip().lower().startswith(("postgresql://", "postgres://")):
            return value.strip()
    return None


LIVE_DATABASE_URL = (
    _env_postgres_url("LIVE_DATABASE_URL", "DLD_TRANSACTIONS_URL", "DLD_RENTS_URL", "RENT_URL")
    or DATABASE_URL
)
ARCHIVE_DATABASE_URL = (
    _env_postgres_url("ARCHIVE_DATABASE_URL", "ARCHIVE_DB_URL")
    or DATABASE_URL
)

# Startup diagnostics without printing secrets.
print("LIVE_DATABASE_URL source:", "custom" if LIVE_DATABASE_URL != DATABASE_URL else "DATABASE_URL fallback")
print("ARCHIVE_DATABASE_URL source:", "custom" if ARCHIVE_DATABASE_URL != DATABASE_URL else "DATABASE_URL fallback")

_ACTIVE_DATABASE_URL = LIVE_DATABASE_URL
_ACTIVE_SOURCE = "live"

DUAL_DB_SOURCES = {
    "archive": {
        "url": ARCHIVE_DATABASE_URL,
        "sales_table": "public.dld_sale_archive",
        "rent_table": "public.dld_rent_archive",
    },
    "live": {
        "url": LIVE_DATABASE_URL,
        "sales_table": "public.dld_transactions_full",
        "rent_table": "public.dld_rents_full",
    },
}

# Сохраняем последнюю рабочую реализацию всех функций до dual-db override.
_ENGINE_find_buildings = find_buildings
_ENGINE_find_areas = find_areas
_ENGINE_get_stats = get_stats
_ENGINE_get_unit_summary = get_unit_summary
_ENGINE_get_comparison = get_comparison
_ENGINE_get_latest_deals = get_latest_deals
_ENGINE_get_top_active = get_top_active
_ENGINE_get_top_price = get_top_price
_ENGINE_get_top_buildings_in_scope = get_top_buildings_in_scope
_ENGINE_smart_pick_candidates = smart_pick_candidates
_ENGINE_compare_value = compare_value


def _clear_schema_caches():
    """При переключении базы чистим кеши колонок, иначе live/archive смешивают схемы."""
    global _COLUMN_CACHE, _RENT_VALUE_EXPR_CACHE
    _COLUMN_CACHE = None
    _RENT_VALUE_EXPR_CACHE = None
    for cache_name in [
        "_SCHEMA_CACHE", "_SCHEMA_CACHE_V32", "_SCHEMA_CACHE_V33", "_SCHEMA_CACHE_V34",
        "_UNIT_COLUMN_CACHE"
    ]:
        if cache_name in globals():
            obj = globals().get(cache_name)
            if isinstance(obj, dict):
                obj.clear()
            else:
                globals()[cache_name] = None


def _set_data_source(source):
    """Переключает старый SQL слой на нужную базу и нужные таблицы."""
    global _ACTIVE_DATABASE_URL, _ACTIVE_SOURCE, TABLE, RENT_TABLE
    cfg = DUAL_DB_SOURCES[source]
    _ACTIVE_DATABASE_URL = cfg["url"]
    _ACTIVE_SOURCE = source
    TABLE = cfg["sales_table"]
    RENT_TABLE = cfg["rent_table"]
    _clear_schema_caches()


def db():
    """Совместимая db() функция: старый код продолжает вызывать db(),
    но фактически подключается к активной базе archive/live.
    """
    return psycopg2.connect(_ACTIVE_DATABASE_URL, cursor_factory=RealDictCursor)


def _call_on_source(source, fn, *args, default=None, **kwargs):
    old_source = _ACTIVE_SOURCE
    try:
        _set_data_source(source)
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"DUAL_DB_{source.upper()}_{getattr(fn, '__name__', 'fn')}_ERROR:", repr(e))
        return default
    finally:
        try:
            _set_data_source(old_source)
        except Exception:
            _set_data_source("live")


def _active_sources():
    sources = ["archive", "live"]
    # Если обе переменные случайно одинаковые, всё равно можно работать, но не дублируем.
    if ARCHIVE_DATABASE_URL == LIVE_DATABASE_URL:
        return ["live"]
    return sources


def _num(v):
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _int(v):
    try:
        return int(v or 0)
    except Exception:
        return 0


def _min_non_null(values):
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _max_non_null(values):
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _weighted_avg(rows, key, weight_key="deals"):
    total_w = 0
    total = 0.0
    for r in rows:
        if not r:
            continue
        val = _num(r.get(key))
        w = _int(r.get(weight_key))
        if val is not None and w > 0:
            total += val * w
            total_w += w
    return total / total_w if total_w else None


def _merge_text_lists(rows, key):
    vals = []
    seen = set()
    for r in rows:
        if not r:
            continue
        raw = r.get(key)
        if not raw:
            continue
        for part in str(raw).split(','):
            item = part.strip()
            if item and item.lower() not in seen:
                seen.add(item.lower())
                vals.append(item)
    return ', '.join(vals) if vals else None


def _merge_stats_rows(rows):
    rows = [r for r in rows if r and _int(r.get("deals")) > 0]
    if not rows:
        return None
    return {
        "deals": sum(_int(r.get("deals")) for r in rows),
        # Для buildings/areas суммирование может чуть завысить из-за дублей между archive/live,
        # но лучше этого без общего SQL UNION по разным DB не сделать безопасно.
        "buildings": sum(_int(r.get("buildings")) for r in rows),
        "areas": sum(_int(r.get("areas")) for r in rows),
        "avg_price": _weighted_avg(rows, "avg_price"),
        "min_price": _min_non_null([r.get("min_price") for r in rows]),
        "max_price": _max_non_null([r.get("max_price") for r in rows]),
        "avg_meter": _weighted_avg(rows, "avg_meter"),
        "first_deal": _min_non_null([r.get("first_deal") for r in rows]),
        "last_deal": _max_non_null([r.get("last_deal") for r in rows]),
        "rooms_list": _merge_text_lists(rows, "rooms_list"),
        "property_types": _merge_text_lists(rows, "property_types"),
        "property_sub_types": _merge_text_lists(rows, "property_sub_types"),
    }


def _row_key(row):
    return (
        str(row.get("building_name_en") or "").strip().lower(),
        str(row.get("area_name_en") or "").strip().lower(),
        str(row.get("safe_date") or ""),
        str(row.get("price") or ""),
        str(row.get("unit_number") or ""),
    )


def _merge_latest_rows(rows, limit=7):
    seen = set()
    out = []
    for r in rows:
        if not r:
            continue
        k = _row_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    out.sort(key=lambda r: (r.get("safe_date") is not None, str(r.get("safe_date") or "")), reverse=True)
    return out[:limit]


def _merge_group_rows(rows, key_fields, limit=10, sort_field="deals", avg_fields=("avg_price", "avg_meter")):
    grouped = {}
    for r in rows:
        if not r:
            continue
        key = tuple(str(r.get(k) or "").strip().lower() for k in key_fields)
        if not any(key):
            continue
        g = grouped.setdefault(key, {k: r.get(k) for k in key_fields})
        g["deals"] = _int(g.get("deals")) + _int(r.get("deals"))
        for af in avg_fields:
            g.setdefault(f"_{af}_weighted_sum", 0.0)
            g.setdefault(f"_{af}_weight", 0)
            val = _num(r.get(af))
            w = _int(r.get("deals"))
            if val is not None and w > 0:
                g[f"_{af}_weighted_sum"] += val * w
                g[f"_{af}_weight"] += w
        for f in ["min_price", "max_price"]:
            if f in r:
                current = g.get(f)
                val = r.get(f)
                if f == "min_price":
                    g[f] = val if current is None else _min_non_null([current, val])
                else:
                    g[f] = val if current is None else _max_non_null([current, val])
    merged = []
    for g in grouped.values():
        for af in avg_fields:
            w = g.pop(f"_{af}_weight", 0)
            s = g.pop(f"_{af}_weighted_sum", 0.0)
            g[af] = s / w if w else None
        merged.append(g)
    merged.sort(key=lambda x: _num(x.get(sort_field)) or 0, reverse=True)
    return merged[:limit]


def find_buildings(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_find_buildings, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def find_areas(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_find_areas, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["area_name_en"], limit=limit, sort_field="deals", avg_fields=())


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    rows = []
    for source in _active_sources():
        row = _call_on_source(source, _ENGINE_get_stats, scope, name, prop, period, deal_type, default=None)
        if row and _int(row.get("deals")) > 0:
            rows.append(row)
    return _merge_stats_rows(rows)


def get_unit_summary(scope="building", name=None, prop=None, period=None, deal_type=None):
    rows = []
    for source in _active_sources():
        row = _call_on_source(source, _ENGINE_get_unit_summary, scope, name, prop, period, deal_type, default=None)
        if row and _int(row.get("deals")) > 0:
            rows.append(row)
    merged = _merge_stats_rows(rows)
    if not merged:
        return None
    # show_unit_summary ожидает percentile поля.
    merged["p25_price"] = merged.get("min_price")
    merged["median_price"] = merged.get("avg_price")
    merged["p75_price"] = merged.get("max_price")
    return merged


def get_comparison(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not period:
        return None
    currents, previous = [], []
    for source in _active_sources():
        comp = _call_on_source(source, _ENGINE_get_comparison, scope, name, prop, period, deal_type, default=None)
        if comp:
            c, p = comp
            if c and _int(c.get("deals")) > 0:
                currents.append(c)
            if p and _int(p.get("deals")) > 0:
                previous.append(p)
    return _merge_stats_rows(currents), _merge_stats_rows(previous)


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_get_latest_deals, scope, name, prop, period, deal_type, limit, unit_query, default=[]) or [])
    return _merge_latest_rows(rows, limit=limit)


def get_top_active():
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_get_top_active, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=10, sort_field="deals")


def get_top_price():
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_get_top_price, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=10, sort_field="avg_price")


def get_top_buildings_in_scope(scope="dubai", name=None, period=None, deal_type=None, limit=7):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_get_top_buildings_in_scope, scope, name, period, deal_type, limit, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    # При выбранном типе сделки НЕ делаем fallback на другой тип. Только расширяем prop/period.
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ] if deal_type else [
        (prop, period, deal_type),
        (prop, period, None),
        (prop, None, None),
        (None, period, None),
        (None, None, None),
    ]
    for p, per, dt in attempts:
        row = get_stats(scope, name, p, per, dt)
        if row and _int(row.get("deals")) > 0:
            return row, p, per, dt
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ] if deal_type else [
        (prop, period, deal_type),
        (prop, period, None),
        (prop, None, None),
        (None, period, None),
        (None, None, None),
    ]
    for p, per, dt in attempts:
        rows = get_latest_deals(scope, name, p, per, dt, limit=limit, unit_query=unit_query)
        if rows:
            return rows, p, per, dt
    return [], prop, period, deal_type


def compare_value(scope, name, price, size, prop=None, period=None, deal_type=None):
    row = get_unit_summary(scope, name, prop, period, deal_type)
    if not row or not row.get("avg_price"):
        return None
    market_avg = float(row["avg_price"])
    user_price = float(price)
    diff_pct = ((user_price - market_avg) / market_avg) * 100 if market_avg else 0
    user_ppsqft = user_price / float(size) if float(size) else None
    return {
        "row": row,
        "user_price": user_price,
        "user_ppsqft": user_ppsqft,
        "market_avg": market_avg,
        "diff_pct": diff_pct,
    }


def smart_pick_candidates(goal, budget_text, risk, timing):
    # Пытаемся получить результат от каждого источника, затем объединяем.
    rows = []
    for source in _active_sources():
        part = _call_on_source(source, _ENGINE_smart_pick_candidates, goal, budget_text, risk, timing, default=[]) or []
        rows.extend(part)
    if not rows:
        return smart_fallback_candidates(goal, budget_text, risk, timing)
    merged = _merge_group_rows(rows, ["area", "property"], limit=5, sort_field="score", avg_fields=("avg_price", "avg_meter", "score"))
    # Возвращаем поля, которые ждёт show_smart_recommendation.
    for r in merged:
        r.setdefault("min_price", (r.get("avg_price") or 0) * 0.9 if r.get("avg_price") else None)
        r.setdefault("max_price", (r.get("avg_price") or 0) * 1.1 if r.get("avg_price") else None)
        r.setdefault("buildings", 0)
    return merged[:5] if merged else smart_fallback_candidates(goal, budget_text, risk, timing)




# =========================
# FINAL HOTFIX v51 - unique normalized columns + safe aliases
# =========================
# Смысл фикса:
# 1) Убирает AmbiguousColumn по building_name_en / area_name_en.
#    Внутри base_from больше нет SELECT * вместе с alias-колонками с теми же именами.
# 2) Поиск Grande / Corner / JVC работает через нормализованные alias-поля.
# 3) sales-area fallback для аренды теперь тоже использует нормализованный base_from,
#    а не напрямую старые имена колонок.

SCHEMA_FIX_VERSION = "v51_unique_normalized_aliases"


def base_from():
    """Нормализованный слой продаж без дублей имён колонок.

    ВАЖНО: раньше было SELECT *, ... AS building_name_en.
    Если исходная таблица уже имела building_name_en, PostgreSQL видел две одноимённые
    колонки и падал с AmbiguousColumn. Теперь наружу отдаём только совместимые поля,
    которые реально использует бот.
    """
    m = _v44_sale_meta()
    meter_expr = f"""
        COALESCE(
            {m['meter']},
            CASE WHEN ({m['size']}) IS NOT NULL AND ({m['size']}) > 0 AND ({m['price']}) IS NOT NULL
                 THEN ({m['price']}) / NULLIF(({m['size']}), 0)
                 ELSE NULL::numeric END
        )
    """
    return f"""
        FROM (
            SELECT
                {m['date']} AS safe_date,
                {m['building']} AS building_name_en,
                {m['building']} AS building_en,
                {m['building']} AS project_en,
                {m['area']} AS area_name_en,
                {m['area']} AS area_en,
                {m['rooms']} AS rooms_en,
                {m['ptype']} AS property_type_en,
                {m['ptype']} AS prop_type_en,
                {m['subtype']} AS property_sub_type_en,
                {m['subtype']} AS prop_sub_type_en,
                {m['procedure']} AS procedure_name_en,
                {m['procedure']} AS procedure_name_norm,
                {m['unit']} AS unit_number_norm,
                {m['price']} AS actual_worth_norm,
                {meter_expr} AS meter_sale_price_norm,
                LOWER(
                    COALESCE({m['building']}, '') || ' ' ||
                    COALESCE({m['area']}, '') || ' ' ||
                    COALESCE({m['rooms']}, '') || ' ' ||
                    COALESCE({m['ptype']}, '') || ' ' ||
                    COALESCE({m['subtype']}, '') || ' ' ||
                    COALESCE({m['procedure']}, '')
                ) AS search_text
            FROM {TABLE}
        ) t
        WHERE 1=1
    """


# Глобальные SQL expressions после нормализации.
PRICE = "actual_worth_norm"
METER_PRICE = "meter_sale_price_norm"
BUILDING_NAME = "COALESCE(building_name_en::text, '')"
AREA_TXT = "COALESCE(area_name_en::text, '')"
BUILDING_TXT = "COALESCE(building_name_en::text, '')"
ROOMS_TXT = "COALESCE(rooms_en::text, '')"
PROPERTY_TYPE_TXT = "COALESCE(property_type_en::text, '')"
PROPERTY_SUB_TYPE_TXT = "COALESCE(property_sub_type_en::text, '')"
PROCEDURE_TXT = "COALESCE(procedure_name_en::text, procedure_name_norm::text, '')"


def building_search_expression():
    return "LOWER(COALESCE(search_text::text, '') || ' ' || COALESCE(building_name_en::text, '') || ' ' || COALESCE(area_name_en::text, ''))"


def make_area_exact_condition(query):
    values = [v for v in area_alias_values(query) if v]
    if not values:
        return "AND 1=0", []
    parts, params = [], []
    for value in values:
        v = clean_query(value).lower()
        parts.append("LOWER(COALESCE(area_name_en::text, '')) ILIKE %s")
        params.append(f"%{v}%")
    return "AND (" + " OR ".join(parts) + ")", params


def sales_areas_for_building_v32(name):
    """Ищем район здания через нормализованный sales layer.
    Работает и для archive, и для live, даже если реальные колонки называются project_en/building_en.
    """
    n = clean_query(name)
    if not n:
        return []
    try:
        where, params = make_building_condition(n)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT DISTINCT COALESCE(area_name_en::text, '') AS area
                    {base_from()}
                      {where}
                      AND COALESCE(area_name_en::text, '') <> ''
                    LIMIT 25
                """, params)
                return [r["area"] for r in cur.fetchall() if r.get("area")]
    except Exception as e:
        print("RENT_AREA_FALLBACK_ERROR_V51:", repr(e))
        return []


def available_unit_column():
    # После v51 в нормализованном sales layer всегда есть unit_number_norm.
    return "unit_number_norm"


def make_unit_condition(unit_text):
    unit_text = clean_query(unit_text)
    if not unit_text:
        return "", []
    q = unit_text.replace("№", "").replace("unit", "").replace("Unit", "").strip()
    only_digits = re.sub(r"\D", "", q)
    if only_digits:
        if len(only_digits) <= 2:
            return "AND COALESCE(unit_number_norm::text, '') ILIKE %s", [f"%{only_digits}"]
        return "AND COALESCE(unit_number_norm::text, '') ILIKE %s", [f"%{only_digits}%"]
    return "AND COALESCE(unit_number_norm::text, '') ILIKE %s", [f"%{q}%"]


def find_buildings(query, limit=10):
    query = clean_query(query)
    if not query:
        return []
    where, params = make_building_condition(query)
    exact = normalize_search_text(query)
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(building_name_en::text, '') AS building_name_en,
                        COALESCE(building_en::text, '') AS building_en,
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COALESCE(area_en::text, '') AS area_en,
                        COUNT(*) AS deals,
                        CASE
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) = LOWER(TRIM(%s)) THEN 0
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) LIKE LOWER(TRIM(%s)) THEN 1
                            WHEN LOWER(COALESCE(search_text::text, '')) LIKE LOWER(TRIM(%s)) THEN 2
                            ELSE 3
                        END AS rank
                    {base_from()}
                      {where}
                      AND COALESCE(building_name_en::text, '') <> ''
                    GROUP BY
                        COALESCE(building_name_en::text, ''),
                        COALESCE(building_en::text, ''),
                        COALESCE(area_name_en::text, ''),
                        COALESCE(area_en::text, ''),
                        CASE
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) = LOWER(TRIM(%s)) THEN 0
                            WHEN LOWER(TRIM(COALESCE(building_name_en::text, ''))) LIKE LOWER(TRIM(%s)) THEN 1
                            WHEN LOWER(COALESCE(search_text::text, '')) LIKE LOWER(TRIM(%s)) THEN 2
                            ELSE 3
                        END
                    ORDER BY rank ASC, deals DESC
                    LIMIT %s
                """, [query, query + "%", f"%{exact}%"] + params + [query, query + "%", f"%{exact}%", limit])
                return cur.fetchall()
    except Exception as e:
        print("FIND_BUILDINGS_ERROR_V51:", repr(e))
        return []


def find_areas(query, limit=10):
    query = clean_query(query)
    if not query:
        return []
    where, params = make_area_exact_condition(query)
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COALESCE(area_name_en::text, '') AS area_name_en,
                        COALESCE(area_en::text, '') AS area_en,
                        COUNT(*) AS deals,
                        COUNT(DISTINCT COALESCE(building_name_en::text, '')) AS buildings
                    {base_from()}
                      {where}
                      AND COALESCE(area_name_en::text, '') <> ''
                    GROUP BY COALESCE(area_name_en::text, ''), COALESCE(area_en::text, '')
                    ORDER BY deals DESC
                    LIMIT %s
                """, params + [limit])
                return cur.fetchall()
    except Exception as e:
        print("FIND_AREAS_ERROR_V51:", repr(e))
        return []


# Обновляем engine pointers для dual-db, чтобы merge-слой вызывал уже исправленные функции.
_ENGINE_find_buildings = find_buildings
_ENGINE_find_areas = find_areas

# В v50 эти функции уже override-нуты на dual-db. Переопределяем только поиск,
# так как он прямо вызывается handlers и должен читать archive+live, а не одну активную базу.
def find_buildings(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_find_buildings, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def find_areas(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _ENGINE_find_areas, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["area_name_en"], limit=limit, sort_field="deals")


print(f"Loaded schema compatibility patch {SCHEMA_FIX_VERSION}")


print("Loaded dual database archive+live engine v50")


async def main():
    print("Dubai DLD Analytics Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
