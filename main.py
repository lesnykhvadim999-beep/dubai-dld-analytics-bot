from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

from dotenv import load_dotenv

import asyncio
import os
import re
import sys
import subprocess
import tempfile
import time
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

# v96: optional central intelligence router integration.
# If intelligence_router.py is not present, the bot keeps working with old logic.
try:
    from intelligence_router import (
        prepare_request as IR_prepare_request,
        prepare_from_state as IR_prepare_from_state,
        route_after_db_result as IR_route_after_db_result,
        clean_dld_rows as IR_clean_dld_rows,
        explain_payload as IR_explain_payload,
    )
    INTELLIGENCE_ROUTER_AVAILABLE = True
except Exception as _ir_import_error:
    print("INTELLIGENCE_ROUTER_IMPORT_ERROR:", repr(_ir_import_error))
    IR_prepare_request = None
    IR_prepare_from_state = None
    IR_route_after_db_result = None
    IR_clean_dld_rows = None
    IR_explain_payload = None
    INTELLIGENCE_ROUTER_AVAILABLE = False

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
AREA_SIZE = num_sql("actual_area")
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
        "view_deals": "🧾 Сделки",
        "area_stats": "🏙 Район",
        "dubai_stats": "🌆 Дубай",
        "top_active": "🚀 Активные",
        "top_price": "💰 Цены",
        "building_search": "🏢 Здание",
        "settings": "⚙️ Язык",
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
        "full_report": "📊 Отчёт",
        "last_deals": "🧾 Сделки",
        "period_compare": "📈 Периоды",
        "undervalued": "📉 Цена",
        "enter_price": "💰 Введите цену объекта в AED.\n\nНапример: 2500000",
        "enter_size": "📐 Введите площадь объекта в sq.ft.\n\nНапример: 850",
        "loading": "⌛️ <b>Идёт обработка DLD-данных</b>\n\n◇ Подключаю архив, live-базу и intelligence-слой.\n◇ Считаю сделки, средние цены, динамику и доходность.\n◇ Формирую профессиональное резюме.\n\nПожалуйста, подождите — аналитика готовится.",
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
    user_lang = lang(user_id)
    if user_lang not in TEXTS:
        user_lang = "ru"
    return TEXTS.get(user_lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True
    )


def language_menu():
    return kb([["🇷🇺 Русский"], ["🇬🇧 English"], ["🇦🇪 العربية"]])


def main_menu(user_id):
    """Главное меню v64: только основные пользовательские сценарии.
    Служебные функции вынесены в команды: /pdf, /admin, /language, /help.
    """
    return kb([
        ["🧠 Подбор", "🏢 Здание"],
        ["🏆 Лучший объект"],
        ["🏙 Район", "📊 Рейтинги"],
        ["⚖️ Сравнение форматов"],
        ["🧾 Сделки", "🌆 Дубай"],
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


def format_compare_scope_menu(user_id):
    return kb([
        ["🏙 По району", "🌆 По Дубаю"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])

def format_compare_budget_menu(user_id):
    return kb([
        ["до 1M AED", "1–2M AED"],
        ["2–3M AED", "3–5M AED"],
        ["5M+ AED", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])

def format_compare_goal_menu(user_id):
    return kb([
        ["📈 Перепродажа", "🔑 Аренда"],
        ["💰 ROI", "⚖️ Сбалансировано"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])

def format_compare_period_menu(user_id):
    return kb([
        [tr(user_id, "p6"), tr(user_id, "p12")],
        [tr(user_id, "p36"), tr(user_id, "all_time")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])

def format_compare_after_menu(user_id):
    return kb([
        ["🏆 Лучший формат"],
        ["🏙 Лучшие районы", "🏢 Лучшие здания"],
        ["📄 PDF", "💼 Заявка"],
        ["🔁 Новый отчёт", tr(user_id, "main")]
    ])


def result_menu(user_id, scope=None):
    """Адаптивное меню после готового результата: только релевантные действия."""
    rows = [
        ["📄 PDF", "💼 Заявка"],
        ["🔁 Изменить", tr(user_id, "main")],
    ]
    if scope == "building":
        rows.insert(0, ["📊 Аналитика", "🧾 Сделки"])
        rows.insert(1, ["📈 Периоды", "🏙 Район"])
    elif scope == "area":
        rows.insert(0, ["📊 Аналитика", "🏢 Здания"])
        rows.insert(1, ["🧾 Сделки", "📈 Периоды"])
    return kb(rows)

def report_menu(user_id):
    # Меню действий внутри выбранного здания/района.
    return kb([
        ["📊 Аналитика", "💼 Резюме"],
        ["📈 Периоды", "🧾 Сделки"],
        ["📄 PDF", "💼 Заявка"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])

def ranking_menu(user_id):
    return kb([
        ["🏢 Здания", "🏙 Районы"],
        ["💰 По цене", "📊 По сделкам"],
        ["📈 По росту", "💧 Ликвидность"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])

def process_menu(user_id):
    return kb([[tr(user_id, "main")]])

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




def format_area_dual(value):
    """Format total DLD area for deal cards. Most DLD area fields are in m²;
    show both m² and ft² so the card does not display only price per meter.
    """
    if value is None:
        return "нет данных"
    try:
        v = float(value)
    except Exception:
        return "нет данных"
    if v <= 0:
        return "нет данных"
    sqft = v * 10.7639
    m2_text = (f"{v:,.0f}" if v >= 100 else f"{v:,.1f}").replace(",", " ")
    sqft_text = f"{sqft:,.0f}".replace(",", " ")
    return f"{m2_text} м² / {sqft_text} ft²"

def _rooms_label_from_prop(prop):
    """Safe fallback for deal cards when DLD row has empty rooms_en."""
    if not prop:
        return None
    s = str(prop).strip()
    if not s or s.lower() in {"none", "null", "all", "any", "пропустить", "⏭ пропустить"}:
        return None
    low = s.lower()
    if low in {"studio", "1 br", "2 br", "3 br", "4 br", "5 br+"}:
        return s
    return None


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
        tr(user_id, "p6"), tr(user_id, "p12"), tr(user_id, "p36"), "📉 Проверить сделку", "🧠 Подбор", "📄 PDF", "💼 Консультация", "👑 Админ", "💼 Резюме",
        "🧠 Инвестиционный подбор", "📊 Рейтинги", "🏆 Лучший объект", "📊 Аналитика", "📈 Периоды", "📄 PDF", "💼 Заявка", "🔁 Изменить", "🏢 Здания", "🏙 Районы", "💰 По цене", "📊 По сделкам", "📈 По росту", "💧 Ликвидность",
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
                        {AREA_SIZE} AS area_size,
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
    avg_meter = row.get("avg_meter")
    deals = row.get("deals")
    prop_label = prop or "выбранный тип недвижимости"

    if row.get("deals", 0) < 5:
        conclusion = (
            "Выборка небольшая, поэтому вывод нужно использовать как предварительный ориентир. "
            "Для финального решения желательно дополнительно проверить этаж, вид, состояние объекта, сервисные платежи и срочность продавца."
        )
    else:
        conclusion = (
            f"Если объект удаётся купить до уровня <b>{format_money(p25)}</b>, цена выглядит интересной относительно истории сделок DLD. "
            f"Диапазон около медианы <b>{format_money(median)}</b> можно считать рыночным. "
            f"Выше <b>{format_money(p75)}</b> объект уже требует сильного аргумента: лучший вид, этаж, планировка, срочная аренда или высокий потенциал перепродажи."
        )

    return (
        "💼 <b>Экономическое резюме</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"{title}\n\n"
        "<b>1. Исходные данные</b>\n\n"
        f"🏠 Тип недвижимости: <b>{prop_label}</b>\n"
        f"📅 Период анализа: <b>{period_label(period)}</b>\n"
        f"📊 Количество сделок в DLD: <b>{format_int(deals)}</b>\n\n"
        "<b>2. Рыночные ориентиры</b>\n\n"
        f"💰 Средняя цена сделки: <b>{format_money(avg_price)}</b>\n"
        f"📌 Медианная цена: <b>{format_money(median)}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(avg_meter)}</b>\n\n"
        "<b>3. Диапазоны цены входа</b>\n\n"
        f"🟢 <b>Выгодная зона:</b>\nот <b>{format_money(min_price)}</b> до <b>{format_money(p25)}</b>\n\n"
        f"🟡 <b>Рыночная зона:</b>\nот <b>{format_money(p25)}</b> до <b>{format_money(median)}</b>\n\n"
        f"🔴 <b>Дорогая зона:</b>\nвыше <b>{format_money(p75)}</b>\n\n"
        "<b>4. Вывод профессионального аналитика</b>\n\n"
        f"{conclusion}\n\n"
        "<i>DLD — Dubai Land Department, официальный источник зарегистрированных сделок. "
        "ROI — Return on Investment, то есть годовая доходность на вложенный капитал.</i>"
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


# FORMAT COMPARISON ADDON v87
# Отдельный сценарий: Apartment vs Villa vs Townhouse. Существующие функции не меняем.
# =========================

def _budget_bounds(label):
    if not label or "Пропустить" in str(label) or "Skip" in str(label):
        return None, None
    t = str(label).replace(" ", "").lower()
    if "до1" in t or "under1" in t:
        return 0, 1_000_000
    if "1–2" in t or "1-2" in t:
        return 1_000_000, 2_000_000
    if "2–3" in t or "2-3" in t:
        return 2_000_000, 3_000_000
    if "3–5" in t or "3-5" in t:
        return 3_000_000, 5_000_000
    if "5m+" in t or "5м+" in t or "5+" in t:
        return 5_000_000, None
    return None, None


def _format_labels():
    # v88: canonical formats only. Aliases are handled by property_condition(),
    # so the comparison module does not multiply empty queries for flat/unit/etc.
    return [
        ("Apartment", ["apartment"]),
        ("Villa", ["villa"]),
        ("Townhouse", ["townhouse"]),
    ]


def _budget_label_from_row(row):
    avg = _num(row.get("avg_price"))
    if not avg:
        return "ориентир цены не определён"
    if avg < 1_000_000:
        return "до 1M AED"
    if avg < 2_000_000:
        return "1–2M AED"
    if avg < 3_000_000:
        return "2–3M AED"
    if avg < 5_000_000:
        return "3–5M AED"
    return "5M+ AED"


def _row_matches_budget(row, budget):
    bmin, bmax = _budget_bounds(budget)
    if bmin is None and bmax is None:
        return True

    avg_price = _num(row.get("avg_price"))
    min_price = _num(row.get("min_price"))
    max_price = _num(row.get("max_price"))

    # Prefer real deal range. If DLD range intersects requested budget, keep it.
    if min_price and max_price:
        low = bmin or 0
        high = bmax or 10**15
        return max_price >= low and min_price <= high

    # Fallback by average price.
    if avg_price:
        if bmin is not None and avg_price < bmin * 0.75:
            return False
        if bmax is not None and avg_price > bmax * 1.35:
            return False
        return True

    return False


def _nearest_budget_rows(rows, budget):
    bmin, bmax = _budget_bounds(budget)
    if bmin is None and bmax is None:
        return rows

    target = None
    if bmin is not None and bmax is not None:
        target = (bmin + bmax) / 2
    elif bmax is not None:
        target = bmax
    elif bmin is not None:
        target = bmin

    def distance(row):
        avg = _num(row.get("avg_price"))
        if not avg or not target:
            return 10**15
        return abs(avg - target)

    return sorted(rows, key=distance)


def _collect_format_rows(scope="dubai", area=None, period=None):
    rows = []
    missing = []
    for label, aliases in _format_labels():
        merged = []
        for alias in aliases:
            try:
                row = get_stats(scope, area, alias, period, "sale")
                if row and _int(row.get("deals")) > 0:
                    merged.append(row)
            except Exception as e:
                print("FORMAT_COMPARE_ALIAS_ERROR:", label, alias, repr(e))
        stat = _merge_stats_rows(merged) if merged else None
        if stat and _int(stat.get("deals")) > 0:
            stat["format"] = label
            rows.append(stat)
        else:
            missing.append(label)
    return rows, missing


def _format_stats_adaptive(scope="dubai", area=None, period=None, budget=None):
    """Adaptive comparison for Apartment/Villa/Townhouse.

    It never fails the whole scenario only because one format or budget segment is empty.
    If a period/budget is too narrow, it returns the nearest useful market alternative
    and explains what was expanded.
    """
    notes = []

    # 1) Try requested period first.
    base_rows, missing = _collect_format_rows(scope, area, period)
    used_period = period

    # 2) If requested period is too narrow, expand to all time.
    if not base_rows and period:
        base_rows, missing = _collect_format_rows(scope, area, None)
        used_period = None
        if base_rows:
            notes.append("За выбранный период стабильной выборки нет — анализ расширен до всего доступного DLD-периода.")

    if not base_rows:
        return [], notes

    if missing:
        notes.append("По части форматов данных меньше: " + ", ".join(missing) + ". Они не блокируют сравнение.")

    # 3) Budget filter. If no exact match, propose nearest available segment instead of stopping.
    if budget:
        in_budget = [r for r in base_rows if _row_matches_budget(r, budget)]
        if in_budget:
            rows = in_budget
            excluded = [r.get("format") for r in base_rows if r not in in_budget]
            if excluded:
                notes.append("Некоторые форматы вне выбранного бюджета: " + ", ".join(excluded) + ".")
        else:
            rows = _nearest_budget_rows(base_rows, budget)
            alt_parts = []
            for r in rows[:3]:
                alt_parts.append(f"{r.get('format')} — примерно {format_money(r.get('avg_price'))} ({_budget_label_from_row(r)})")
            notes.append(
                "В выбранном бюджете стабильной DLD-выборки нет. "
                "Вместо остановки я показываю ближайшие рабочие варианты: " + "; ".join(alt_parts) + "."
            )
    else:
        rows = base_rows

    # Save actual period used for display.
    for r in rows:
        r["used_period"] = used_period

    return rows, notes


def _format_stats(scope="dubai", area=None, period=None, budget=None):
    rows, _notes = _format_stats_adaptive(scope, area, period, budget)
    return rows


def _score_format_row(row, goal):
    deals = max(_num(row.get("deals")), 0)
    avg_price = max(_num(row.get("avg_price")), 0)
    avg_meter = max(_num(row.get("avg_meter")), 0)
    min_price = max(_num(row.get("min_price")), 0)
    max_price = max(_num(row.get("max_price")), 0)
    liquidity = min(deals / 250.0, 1.0) * 45
    entry = 0
    if avg_price:
        entry = max(0, 30 - (avg_price / 250000.0))
    spread = 0
    if max_price and min_price and avg_price:
        spread = min(((max_price - min_price) / avg_price) * 10, 15)
    price_eff = 0
    if avg_meter:
        price_eff = max(0, 20 - (avg_meter / 2500.0))
    score = liquidity + entry + spread + price_eff
    goal_text = str(goal or "").lower()
    if "аренд" in goal_text or "rent" in goal_text or "roi" in goal_text:
        score += liquidity * 0.25
    if "перепрод" in goal_text or "resale" in goal_text:
        score += spread * 0.35
    return round(score, 1)


def _fmt_line(row, score):
    return (
        f"🏠 <b>{row.get('format')}</b>\n"
        f"📊 Сделок: <b>{format_int(row.get('deals'))}</b>\n"
        f"💰 Средняя цена: <b>{format_money(row.get('avg_price'))}</b>\n"
        f"📐 Средняя цена за м²: <b>{format_money(row.get('avg_meter'))}</b>\n"
        f"🎯 Индекс выгоды: <b>{score}/100</b>\n"
    )


def build_format_comparison_report(scope="dubai", area=None, budget=None, goal=None, period=None):
    rows, notes = _format_stats_adaptive(scope=scope, area=area, period=period, budget=budget)
    if not rows:
        return None, []

    scored = []
    for r in rows:
        scored.append((_score_format_row(r, goal), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    scope_title = "Dubai" if scope == "dubai" else (area or "выбранный район")
    used_period = best.get("used_period", period)

    response = (
        f"⚖️ <b>Сравнение форматов</b>\n"
        f"📍 Рынок: <b>{scope_title}</b>\n"
        f"💰 Бюджет: <b>{budget or 'не указан'}</b>\n"
        f"🎯 Цель: <b>{goal or 'сбалансировано'}</b>\n"
        f"📅 Период: <b>{period_label(used_period)}</b>\n\n"
    )

    if notes:
        response += "📌 <b>Адаптивный анализ</b>\n"
        for n in notes:
            response += f"• {n}\n"
        response += "\n"

    for score, row in scored:
        response += _fmt_line(row, score) + "\n"

    best_format = best.get("format")
    response += f"🏆 <b>Лучший формат:</b> {best_format}\n\n"

    # Tree-style recommendation: format → district → asset focus.
    if best_format == "Apartment":
        next_logic = (
            "После выбора формата лучше идти по цепочке: <b>район с высокой ликвидностью → здание с частыми сделками → вход ниже средней цены за м²</b>. "
            "Для апартаментов ключевые параметры — ликвидность, арендная база и скорость перепродажи."
        )
    elif best_format == "Villa":
        next_logic = (
            "Для вилл важнее не только средняя цена, но и дефицит предложения, размер участка, community premium и горизонт выхода. "
            "Следующий шаг — сравнить районы вилл и выбрать локацию с понятным спросом семейных покупателей."
        )
    else:
        next_logic = (
            "Для таунхаусов важен баланс между ценой входа виллы и ликвидностью апартаментов. "
            "Следующий шаг — проверить районы с семейным спросом и достаточным количеством DLD-сделок."
        )

    response += (
        f"🧠 <b>Экономическое заключение 360°</b>\n"
        f"По выбранному профилю самый сильный баланс сейчас даёт <b>{best_format}</b>. "
        f"Причина — сочетание DLD-активности, понятного рыночного ориентира и относительной прогнозируемости выхода.\n\n"
        f"{next_logic}\n\n"
        f"📍 Нажмите <b>Лучшие районы</b>, чтобы я сузил выбор внутри формата. Затем можно перейти к зданиям/проектам и уже там принимать решение по конкретной точке входа."
    )
    return response, [r for _, r in scored]


def format_compare_best_areas(prop, period=None, budget=None, limit=7):
    candidates = ["JVC", "Dubai Marina", "Downtown Dubai", "Business Bay", "Palm Jumeirah", "JLT", "Dubai Creek Harbour", "Sobha Hartland"]
    rows = []
    for area in candidates:
        try:
            row = get_stats("area", area, prop, period, "sale")
            if row and _int(row.get("deals")) > 0:
                row["area"] = area
                row["score"] = _score_format_row(row, "сбалансировано")
                rows.append(row)
        except Exception as e:
            print("FORMAT_BEST_AREA_ERROR:", area, repr(e))
    rows.sort(key=lambda r: (_num(r.get("score")), _num(r.get("deals"))), reverse=True)
    return rows[:limit]


def show_format_best_areas(prop, period=None, budget=None):
    rows = format_compare_best_areas(prop, period, budget)
    if not rows:
        return no_data_message("Лучшие районы")
    text = f"🏙 <b>Лучшие районы для формата {prop}</b>\n\n"
    for i, r in enumerate(rows, 1):
        text += (
            f"{i}. <b>{r.get('area')}</b>\n"
            f"📊 Сделок: <b>{format_int(r.get('deals'))}</b>\n"
            f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n"
            f"📐 Цена за м²: <b>{format_money(r.get('avg_meter'))}</b>\n"
            f"🎯 Индекс: <b>{r.get('score')}</b>\n\n"
        )
    text += "🧠 Выберите район с высокой ликвидностью и средней ценой ниже соседних премиальных локаций."
    return text


def show_format_best_buildings(prop, area=None, period=None):
    scope = "area" if area else "dubai"
    name = area if area else None
    try:
        rows = get_top_buildings_in_scope(scope, name, period, "sale", limit=8)
    except Exception as e:
        print("FORMAT_BEST_BUILDINGS_ERROR:", repr(e))
        rows = []
    if not rows:
        return no_data_message("Лучшие здания")
    text = f"🏢 <b>Лучшие здания / проекты</b>\n"
    if area:
        text += f"📍 Район: <b>{area}</b>\n"
    text += f"🏠 Формат: <b>{prop}</b>\n\n"
    for i, r in enumerate(rows, 1):
        text += (
            f"{i}. <b>{r.get('building_name_en') or '-'}</b>\n"
            f"📍 {r.get('area_name_en') or '-'}\n"
            f"📊 Сделок: <b>{format_int(r.get('deals'))}</b>\n"
            f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n\n"
        )
    return text

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



# =========================
# ADAPTIVE PRODUCT MENU v72
# Простая иерархия: объект -> тип отчёта -> фильтры -> результат.
# Главное правило: на каждом экране только те кнопки, которые имеют смысл в текущем шаге.
# =========================

def main_menu(user_id):
    return kb([
        ["🧠 Подбор", "🏢 Здание"],
        ["🏆 Лучший объект"],
        ["🏙 Район", "🧾 Сделки"],
        ["📊 Рейтинги", "⚖️ Сравнение форматов"],
        ["🌆 Дубай"],
    ])


def building_action_menu(user_id):
    return kb([
        ["📊 Обзор 360", "🧾 Сделки"],
        ["📈 Динамика", "💰 Цены"],
        ["🔁 Другое здание", tr(user_id, "main")],
    ])


def area_action_menu(user_id):
    return kb([
        ["📊 Обзор 360", "🧾 Сделки"],
        ["📈 Динамика", "🏢 Топ зданий"],
        ["🔁 Другой район", tr(user_id, "main")],
    ])


def dubai_action_menu(user_id):
    return kb([
        ["📊 Обзор рынка", "🧾 Сделки"],
        ["📈 Динамика", "📊 Рейтинги"],
        [tr(user_id, "main")],
    ])


def deals_scope_menu(user_id):
    return kb([
        ["🏢 По зданию", "🏙 По району"],
        ["🌆 По Дубаю"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def ranking_menu(user_id):
    return kb([
        ["🏢 Здания", "🏙 Районы"],
        ["📊 По сделкам", "💰 По цене"],
        ["📈 По росту", "💧 Ликвидность"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def post_result_menu(user_id, scope=None):
    return kb([
        ["📄 PDF", "💼 Заявка"],
        ["🔁 Новый отчёт", "🔁 Изменить"],
        [tr(user_id, "main")],
    ])


def _action_title_v72(state):
    scope = state.get("scope")
    name = _display_scope_name_v71(state.get("name")) if "_display_scope_name_v71" in globals() else state.get("name")
    if scope == "building":
        return f"🏢 <b>{name}</b>\n\nВыберите, что показать по зданию."
    if scope == "area":
        return f"🏙 <b>{name}</b>\n\nВыберите, что показать по району."
    return "🌆 <b>Рынок Дубая</b>\n\nВыберите аналитический сценарий."


def _report_kind_label_v72(kind):
    return {
        "full": "Обзор 360",
        "deals": "Сделки DLD",
        "period": "Динамика периодов",
        "price": "Ценовая аналитика",
        "top_buildings": "Топ зданий",
    }.get(kind, "Аналитика")


async def _ask_action_menu_v72(message, state):
    user_id = message.from_user.id
    scope = state.get("scope")
    user_states[user_id] = state
    if scope == "building":
        await message.answer(_action_title_v72(state), reply_markup=building_action_menu(user_id))
    elif scope == "area":
        await message.answer(_action_title_v72(state), reply_markup=area_action_menu(user_id))
    else:
        await message.answer(_action_title_v72(state), reply_markup=dubai_action_menu(user_id))


async def _start_filters_for_report_v72(message, state, kind):
    user_id = message.from_user.id
    state["report_kind"] = kind
    state["step"] = "choose_deal_type"
    user_states[user_id] = state
    await message.answer(
        f"📊 <b>{_report_kind_label_v72(kind)}</b>\n\nШаг 1 из 3 — выберите тип сделки.",
        reply_markup=deal_type_menu(user_id),
    )


async def _execute_selected_report_v72(message, state):
    kind = state.get("report_kind") or "full"
    scope = state.get("scope", "dubai")
    name = state.get("name")
    prop = _skip_to_none_v86(state.get("property"))
    period = _skip_to_none_v86(state.get("period"))
    deal_type = _skip_to_none_v86(state.get("deal_type"))

    # Store normalized values back into state so downstream PDF/Заявка and report buttons
    # do not receive raw ⏭ Пропустить / empty filters.
    state["property"] = prop
    state["period"] = period
    state["deal_type"] = deal_type

    # Зафиксировать результатное состояние до отправки отчёта, чтобы PDF/Заявка работали с последним отчётом.
    user_states[message.from_user.id] = {
        **state,
        "step": "result",
        "history": state.get("history", []),
    }

    if kind == "deals":
        await send_deals_report(message, scope, name, prop, period, deal_type)
    elif kind == "period":
        await send_period_report(message, scope, name, prop, period, deal_type)
    elif kind == "top_buildings":
        await send_ranking_report(message, "active")
    else:
        await send_full_report(message, scope, name, prop, period, deal_type, _report_kind_label_v72(kind))


def _state_for_selected_building_v72(user_id, chosen, old_state):
    stored_name = chosen.get("name") or chosen.get("building_name_en") or ""
    if chosen.get("area"):
        stored_name = f"{stored_name}|||{chosen.get('area')}"
    return {
        "step": "building_action",
        "scope": "building",
        "name": stored_name,
        "history": old_state.get("history", []),
    }

# =========================
# MAIN ROUTER v65 — adaptive scenario engine
# =========================

SERVICE_BUTTONS = {
    "📄 PDF", "💼 Заявка", "🔁 Изменить", "📊 Аналитика", "🧾 Сделки", "📈 Периоды",
    "🏢 Здания", "🏙 Районы", "💰 По цене", "📊 По сделкам", "📈 По росту", "💧 Ликвидность",
    "🏠 Главное меню", "⬅️ Назад"
}


def set_last_report(user_id, title, html, scope=None):
    st = user_states.get(user_id, {}) or {}
    st["last_report_title"] = title or "Dubai DLD Analytics Report"
    st["last_report_html"] = html or ""
    if scope:
        st["scope"] = scope
    user_states[user_id] = st


async def send_processing(message, text=None):
    user_id = message.from_user.id
    return await message.answer(
        text or tr(user_id, "loading"),
        reply_markup=process_menu(user_id),
    )


def _is_main_button(user_id, text):
    return text in ["🧠 Подбор", "🏢 Здание", "🏆 Лучший объект", "🏙 Район", "📊 Рейтинги", "⚖️ Сравнение форматов", "🧾 Сделки", "🌆 Дубай"]


def _normalize_period_from_text(user_id, text):
    mapping = {
        tr(user_id, "p3"): "3",
        tr(user_id, "p6"): "6",
        tr(user_id, "p12"): "12",
        tr(user_id, "p36"): "36",
        tr(user_id, "all_time"): None,
        tr(user_id, "skip"): None,
        "3 месяца": "3",
        "6 месяцев": "6",
        "1 год": "12",
        "3 года": "36",
        "📅 Всё время": None,
    }
    return mapping.get(text)


def _normalize_deal_type_from_text(user_id, text):
    if text == tr(user_id, "sale") or text == "🏠 Продажа":
        return "sale"
    if text == tr(user_id, "rent") or text == "🔑 Аренда":
        return "rent"
    return None


def _normalize_property_from_text(user_id, text):
    if text == tr(user_id, "skip"):
        return None
    return text


def _skip_to_none_v86(value):
    """Treat Skip/All/empty values as no filter.
    Targeted fix: prevents report flows from crashing when user presses ⏭ Пропустить
    for property type and/or period.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    low = s.lower()
    skip_values = {
        "skip", "any", "all", "none", "null",
        "⏭ пропустить", "пропустить", "все", "всё", "любой", "любая",
        "📅 всё время", "всё время", "все время",
        "⏭ skip", "skip", "all time",
        "تخطي", "الكل"
    }
    if low in skip_values:
        return None
    return value


def _final_actions_menu(user_id, scope=None):
    return post_result_menu(user_id, scope) if "post_result_menu" in globals() else result_menu(user_id, scope)


def _selection_context_text(state):
    bits = []
    if state.get("name"):
        bits.append(str(state.get("name")))
    if state.get("deal_type"):
        bits.append("продажа" if state.get("deal_type") == "sale" else "аренда")
    if state.get("property"):
        bits.append(str(state.get("property")))
    if state.get("period"):
        bits.append(period_label(state.get("period")))
    return " / ".join(bits)


def _split_scope_name_v71(name):
    raw = str(name or "")
    if "|||" in raw:
        b, a = raw.split("|||", 1)
        return b.strip(), a.strip()
    return raw.strip(), None

def _display_scope_name_v71(name):
    b, a = _split_scope_name_v71(name)
    return b

def _human_report_title(scope, name, report_type="Аналитика"):
    clean_name = _display_scope_name_v71(name) if '_display_scope_name_v71' in globals() else name
    if scope == "building":
        return f"🏢 {report_type}: {clean_name}"
    if scope == "area":
        return f"🏙 {report_type}: {clean_name}"
    return f"🌆 {report_type}: Дубай"


def _build_360_conclusion(row, scope=None, name=None, report_kind=None):
    deals = _int(row.get("deals") if row else 0) or 0
    avg_price = row.get("avg_price") if row else None
    avg_meter = row.get("avg_meter") if row else None
    if deals >= 500:
        liquidity = "очень высокая"
        risk = "низкий риск ликвидности"
    elif deals >= 100:
        liquidity = "здоровая"
        risk = "умеренный риск ликвидности"
    elif deals > 0:
        liquidity = "ограниченная"
        risk = "нужна ручная проверка спроса"
    else:
        liquidity = "недостаточная"
        risk = "недостаточно данных для профессионального вывода"

    return (
        "\n\n🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"Ликвидность по DLD-сделкам: <b>{liquidity}</b>.\n"
        f"Рыночный ориентир: средняя цена <b>{format_money(avg_price)}</b>, "
        f"средняя цена за метр <b>{format_money(avg_meter)}</b>.\n\n"
        f"Инвестиционный вывод: <b>{risk}</b>. "
        "Перед покупкой стоит сравнить конкретный юнит с последними сделками, этажом, видом, состоянием, сервисными сборами и реальной арендной ставкой."
    )


async def send_full_report(message, scope, name=None, prop=None, period=None, deal_type=None, title_prefix="Полная аналитика"):
    user_id = message.from_user.id
    await send_processing(message)
    row, used_prop, used_period, used_deal_type = get_stats_smart(scope, name, prop, period, deal_type)
    if not row or not _int(row.get("deals")):
        await message.answer(no_data_message(title_prefix), reply_markup=report_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
        return

    title = _human_report_title(scope, name, title_prefix)
    html = show_stats(f"<b>{title}</b>", row, used_prop, used_period, used_deal_type)
    html += _build_360_conclusion(row, scope, name, title_prefix)
    if (used_prop, used_period, used_deal_type) != (prop, period, deal_type):
        html += "\n\nℹ️ По точному фильтру выборка была узкой, поэтому показана ближайшая стабильная DLD-выборка."
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))


async def send_period_report(message, scope, name=None, prop=None, period=None, deal_type=None):
    user_id = message.from_user.id
    await send_processing(message)
    period = period or "12"
    comparison = get_comparison(scope, name, prop, period, deal_type)
    if not comparison:
        await message.answer(no_data_message("Сравнение периодов"), reply_markup=report_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
        return
    current, previous = comparison
    title = _human_report_title(scope, name, "Сравнение периодов")
    html = show_comparison(f"<b>{title}</b>", current, previous, period, deal_type)
    html += _build_360_conclusion(current, scope, name, "period")
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))


async def send_deals_report(message, scope, name=None, prop=None, period=None, deal_type=None):
    user_id = message.from_user.id
    await send_processing(message)
    rows, used_prop, used_period, used_deal_type = get_latest_deals_smart(scope, name, prop, period, deal_type)
    if not rows:
        await message.answer(no_data_message("Последние сделки"), reply_markup=report_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
        return
    title = _human_report_title(scope, name, "Последние сделки")
    html = f"🧾 <b>{title}</b>\n"
    ctx = []
    if used_deal_type:
        ctx.append("продажа" if used_deal_type == "sale" else "аренда")
    if used_prop:
        ctx.append(str(used_prop))
    if used_period:
        ctx.append(period_label(used_period))
    if ctx:
        html += "Фильтр: " + " / ".join(ctx) + "\n"
    html += "\n"
    for r in rows[:10]:
        rooms_label = r.get('rooms_en') or _rooms_label_from_prop(prop) or _rooms_label_from_prop(used_prop) or '-'
        html += (
            f"🗓 <b>{r.get('safe_date') or '-'}</b>\n"
            f"🏢 {r.get('building_name_en') or '-'}\n"
            f"📍 {r.get('area_name_en') or '-'}\n"
            f"🏠 {rooms_label} / {r.get('property_sub_type_en') or r.get('property_type_en') or '-'}\n"
            f"📏 Площадь: {format_area_dual(r.get('area_size'))}\n"
            f"💰 {format_money(r.get('price'))}\n"
            f"📐 {format_money(r.get('meter_price'))} за метр\n\n"
        )
    row = get_stats(scope, name, used_prop, used_period, used_deal_type)
    if row:
        html += _build_360_conclusion(row, scope, name, "deals")
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))


async def send_ranking_report(message, ranking_type="active"):
    user_id = message.from_user.id
    await send_processing(message)
    rows = []
    title = "📊 Рейтинг рынка"
    if ranking_type in ["price", "💰 По цене"]:
        rows = safe_call(get_top_price, default=[]) or []
        title = "💰 Рейтинг зданий по средней цене"
    else:
        rows = safe_call(get_top_active, default=[]) or []
        title = "📊 Рейтинг зданий по активности и ликвидности"
    if not rows:
        await message.answer(no_data_message("Рейтинг"), reply_markup=ranking_menu(user_id))
        return
    html = f"<b>{title}</b>\n\n"
    for i, r in enumerate(rows[:10], 1):
        html += (
            f"{i}. 🏢 <b>{r.get('building_name_en') or '-'}</b>\n"
            f"📍 {r.get('area_name_en') or '-'}\n"
            f"📊 Сделки: <b>{format_int(r.get('deals'))}</b>\n"
            f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n"
            f"📐 Цена за метр: <b>{format_money(r.get('avg_meter'))}</b>\n\n"
        )
    html += _build_360_conclusion(rows[0], "dubai", None, "rating")
    set_last_report(user_id, title, html, "dubai")
    await message.answer(html, reply_markup=_final_actions_menu(user_id, "dubai"))


async def start_building_search_from_text(message, text):
    user_id = message.from_user.id
    await message.answer("🔎 Ищу похожие здания в archive + live базе...", reply_markup=process_menu(user_id))
    rows = safe_call(find_buildings, text, 10, default=[]) or []
    if not rows:
        user_states[user_id] = {"step": "building_query", "scope": "building", "history": user_states.get(user_id, {}).get("history", [])}
        await message.answer(
            "❌ <b>Здание не найдено.</b>\n\n"
            "Попробуйте другое написание или часть названия:\n"
            "• Grande\n• Address Opera\n• Marina Gate\n• Binghatti",
            reply_markup=back_menu(user_id),
        )
        return
    options = []
    used = set()
    for r in rows:
        n = (r.get("building_name_en") or "").strip()
        a = (r.get("area_name_en") or "").strip()
        if not n:
            continue
        label = f"{n} — {a}" if a else n
        key = label.lower()
        if key in used:
            continue
        used.add(key)
        options.append({"label": label, "name": n, "area": a, "deals": r.get("deals")})
    user_states[user_id] = {"step": "choose_building", "scope": "building", "building_options": options, "history": user_states.get(user_id, {}).get("history", [])}
    buttons = [[opt["label"]] for opt in options[:8]] + [[tr(user_id, "back"), tr(user_id, "main")]]
    html = tr(user_id, "choose_building") + "\n\n"
    for i, opt in enumerate(options[:8], 1):
        html += f"{i}. <b>{opt['name']}</b> — {opt.get('area') or '-'} ({format_int(opt.get('deals'))} сделок)\n"
    await message.answer(html, reply_markup=kb(buttons))


async def start_area_search_from_text(message, text):
    user_id = message.from_user.id
    await message.answer("🔎 Ищу район в archive + live базе...", reply_markup=process_menu(user_id))
    rows = safe_call(find_areas, text, 10, default=[]) or []
    if not rows:
        user_states[user_id] = {"step": "area_query", "scope": "area", "history": user_states.get(user_id, {}).get("history", [])}
        await message.answer(
            "❌ <b>Район не найден.</b>\n\n"
            "Попробуйте полное или другое написание:\n"
            "• Jumeirah Village Circle\n• Downtown Dubai\n• Business Bay\n• Dubai Marina",
            reply_markup=back_menu(user_id),
        )
        return
    suggestions = []
    for r in rows:
        n = r.get("area_name_en")
        if n and n not in suggestions:
            suggestions.append(n)
    user_states[user_id] = {"step": "choose_area", "scope": "area", "suggestions": suggestions, "history": user_states.get(user_id, {}).get("history", [])}
    buttons = [[name] for name in suggestions[:8]] + [[tr(user_id, "back"), tr(user_id, "main")]]
    html = tr(user_id, "choose_area") + "\n\n"
    for i, r in enumerate(rows[:8], 1):
        html += f"{i}. <b>{r.get('area_name_en')}</b> ({format_int(r.get('deals'))} сделок)\n"
    await message.answer(html, reply_markup=kb(buttons))


@dp.message()
async def main_handler(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    state = user_states.get(user_id, {}) or {}

    try:
        # Служебные команды — не засоряют главное меню.
        if text in ["/language", "/settings", "⚙️ Настройки", "⚙️ Язык"]:
            user_states[user_id] = {"step": "settings", "history": []}
            await message.answer("⚙️ <b>Язык интерфейса</b>\n\nВыберите язык.", reply_markup=language_menu())
            return
        if text == "/pdf" or text == "📄 PDF":
            await handle_pdf_request(message)
            return
        if text in ["/admin", "👑 Админ", "👑 Админ-панель"]:
            await handle_admin_dashboard(message)
            return
        if text == "/help":
            await message.answer(
                "🏛 <b>Dubai DLD Intelligence</b>\n\n"
                "Простая логика работы:\n"
                "1) выбираете сценарий;\n"
                "2) выбираете объект или рынок;\n"
                "3) задаёте 3 фильтра;\n"
                "4) получаете отчёт, PDF или заявку.",
                reply_markup=main_menu(user_id),
            )
            return

        # Навигация.
        if text == tr(user_id, "main") or text == "🏠 Главное меню":
            reset_to_main(user_id)
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return
        if text == tr(user_id, "back") or text == "⬅️ Назад":
            prev = go_back(user_id)
            await show_current_state_prompt(message, prev)
            return
        if text in ["💼 Заявка", "💼 Консультация"]:
            await handle_consultation_request(message)
            return

        # После готового отчёта показываем только действия результата.
        if state.get("step") == "result":
            if text in ["🔁 Новый отчёт"]:
                st = dict(state)
                st["step"] = "building_action" if st.get("scope") == "building" else "area_action" if st.get("scope") == "area" else "dubai_action"
                await _ask_action_menu_v72(message, st)
                return
            if text == "🔁 Изменить":
                reset_to_main(user_id)
                await message.answer("🔁 Выберите новый сценарий.", reply_markup=main_menu(user_id))
                return
            # Если человек нажал старую кнопку отчёта из результата — обработаем мягко.
            if text in ["📊 Аналитика", "💼 Резюме", "📊 Обзор 360"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "full"})
                return
            if text in ["🧾 Сделки"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "deals"})
                return
            if text in ["📈 Периоды", "📈 Динамика"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "period"})
                return

        # Главное меню: 6 понятных сценариев.
        if text == "🧠 Подбор":
            user_states[user_id] = {"step": "smart_goal", "history": []}
            await message.answer("🧠 <b>Инвестиционный подбор</b>\n\nВыберите цель покупки.", reply_markup=smart_goal_menu(user_id))
            return
        if text == "⚖️ Сравнение форматов":
            user_states[user_id] = {"step": "format_compare_scope", "history": []}
            await message.answer(
                "⚖️ <b>Сравнение форматов</b>\n\nСравню апартаменты, виллы и таунхаусы по цене входа, ликвидности, приросту, выгодности и инвестиционной логике.\n\nВыберите рынок анализа:",
                reply_markup=format_compare_scope_menu(user_id)
            )
            return
        if text == "🏆 Лучший объект":
            user_states[user_id] = {"step": "best_object_deal_type", "history": []}
            await message.answer(
                "🏆 <b>Лучший объект</b>\n\nЯ проведу по дереву выбора и подберу топ-3 района и топ-3 объекта/здания под цель, бюджет и формат.\n\nШаг 1 из 5 — выберите тип сделки:",
                reply_markup=best_object_deal_type_menu(user_id)
            )
            return
        if text == "🏢 Здание":
            user_states[user_id] = {"step": "building_query", "scope": "building", "history": []}
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
            return
        if text == "🏙 Район":
            user_states[user_id] = {"step": "area_query", "scope": "area", "history": []}
            await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
            return
        if text == "🧾 Сделки" and state.get("step") not in ["building_action", "area_action", "dubai_action", "result"]:
            user_states[user_id] = {"step": "deals_scope", "history": []}
            await message.answer("🧾 <b>Сделки DLD</b>\n\nГде показать сделки?", reply_markup=deals_scope_menu(user_id))
            return
        if text == "📊 Рейтинги":
            user_states[user_id] = {"step": "ranking_menu", "history": []}
            await message.answer(
                "📊 <b>Рейтинги рынка</b>\n\nВыберите, какой рейтинг построить.",
                reply_markup=ranking_menu(user_id),
            )
            return
        if text == "🌆 Дубай":
            st = {"step": "dubai_action", "scope": "dubai", "name": None, "history": []}
            await _ask_action_menu_v72(message, st)
            return

        # Рейтинги.
        if state.get("step") == "ranking_menu":
            if text == "🏢 Здания":
                await send_ranking_report(message, "building_deals")
                return
            if text == "🏙 Районы":
                await send_ranking_report(message, "area_deals")
                return
            if text == "📊 По сделкам":
                await send_ranking_report(message, "building_deals")
                return
            if text == "💰 По цене":
                await send_ranking_report(message, "building_price")
                return
            if text == "📈 По росту":
                await send_ranking_report(message, "area_growth")
                return
            if text == "💧 Ликвидность":
                await send_ranking_report(message, "building_liquidity")
                return
            await message.answer("Выберите рейтинг кнопкой.", reply_markup=ranking_menu(user_id))
            return

        # Сделки: выбор области.
        if state.get("step") == "deals_scope":
            if text == "🏢 По зданию":
                user_states[user_id] = {"step": "building_query", "scope": "building", "force_kind": "deals", "history": state.get("history", [])}
                await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
                return
            if text == "🏙 По району":
                user_states[user_id] = {"step": "area_query", "scope": "area", "force_kind": "deals", "history": state.get("history", [])}
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
                return
            if text == "🌆 По Дубаю":
                await _start_filters_for_report_v72(message, {"scope": "dubai", "name": None, "history": state.get("history", [])}, "deals")
                return
            await message.answer("Выберите область сделок кнопкой.", reply_markup=deals_scope_menu(user_id))
            return

        # Поиск здания/района.
        if state.get("step") == "building_query":
            await start_building_search_from_text(message, text)
            return
        if state.get("step") == "area_query":
            await start_area_search_from_text(message, text)
            return

        # Выбор найденного здания.
        if state.get("step") == "choose_building":
            options = state.get("building_options") or []
            chosen = None
            if text.isdigit() and 1 <= int(text) <= len(options):
                chosen = options[int(text) - 1]
            else:
                for opt in options:
                    if text == opt.get("label") or text == opt.get("name"):
                        chosen = opt
                        break
            if not chosen:
                await start_building_search_from_text(message, text)
                return
            st = _state_for_selected_building_v72(user_id, chosen, state)
            if state.get("force_kind"):
                await _start_filters_for_report_v72(message, st, state.get("force_kind"))
            else:
                await _ask_action_menu_v72(message, st)
            return

        # Выбор найденного района.
        if state.get("step") == "choose_area":
            suggestions = state.get("suggestions", [])
            if text.isdigit() and 1 <= int(text) <= len(suggestions):
                text = suggestions[int(text) - 1]
            if text not in suggestions:
                await start_area_search_from_text(message, text)
                return
            st = {"step": "area_action", "scope": "area", "name": text, "history": state.get("history", [])}
            if state.get("force_kind"):
                await _start_filters_for_report_v72(message, st, state.get("force_kind"))
            else:
                await _ask_action_menu_v72(message, st)
            return

        # Меню действий по зданию/району/Дубаю.
        if state.get("step") in ["building_action", "area_action", "dubai_action"]:
            if text in ["📊 Обзор 360", "📊 Обзор рынка"]:
                await _start_filters_for_report_v72(message, state, "full")
                return
            if text == "🧾 Сделки":
                await _start_filters_for_report_v72(message, state, "deals")
                return
            if text == "📈 Динамика":
                await _start_filters_for_report_v72(message, state, "period")
                return
            if text == "💰 Цены":
                await _start_filters_for_report_v72(message, state, "price")
                return
            if text == "🏢 Топ зданий":
                await send_ranking_report(message, "active")
                return
            if text == "📊 Рейтинги":
                user_states[user_id] = {"step": "ranking_menu", "history": state.get("history", [])}
                await message.answer("📊 <b>Рейтинги рынка</b>\n\nВыберите рейтинг.", reply_markup=ranking_menu(user_id))
                return
            if text == "🔁 Другое здание":
                user_states[user_id] = {"step": "building_query", "scope": "building", "history": []}
                await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
                return
            if text == "🔁 Другой район":
                user_states[user_id] = {"step": "area_query", "scope": "area", "history": []}
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
                return
            await _ask_action_menu_v72(message, state)
            return

        # Универсальная воронка фильтров — после неё сразу отчёт, без лишнего вопроса.
        if state.get("step") == "choose_deal_type":
            state["deal_type"] = _skip_to_none_v86(_normalize_deal_type_from_text(user_id, text))
            state["step"] = "choose_property"
            user_states[user_id] = state
            await message.answer("🏠 <b>Шаг 2 из 3</b>\n\nВыберите тип недвижимости или комнатность.", reply_markup=property_menu(user_id))
            return

        if state.get("step") == "choose_property":
            state["property"] = _skip_to_none_v86(_normalize_property_from_text(user_id, text))
            state["step"] = "choose_period"
            user_states[user_id] = state
            await message.answer("📅 <b>Шаг 3 из 3</b>\n\nВыберите период анализа.", reply_markup=period_menu(user_id))
            return

        if state.get("step") == "choose_period":
            state["period"] = _skip_to_none_v86(_normalize_period_from_text(user_id, text))
            await _execute_selected_report_v72(message, state)
            return

        # Best object funnel — отдельный сценарий, не меняет существующие меню и отчёты.
        if state.get("step") == "best_object_deal_type":
            allowed = ["🏠 Продажа", "🔑 Аренда", "📊 Неважно", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите тип сделки кнопкой.", reply_markup=best_object_deal_type_menu(user_id))
                return
            state["deal_type"] = None if text in ["📊 Неважно", tr(user_id, "skip")] else text
            state["step"] = "best_object_format"
            user_states[user_id] = state
            await message.answer("🏠 <b>Шаг 2 из 5</b>\n\nВыберите формат недвижимости.", reply_markup=best_object_format_menu(user_id))
            return

        if state.get("step") == "best_object_format":
            allowed = ["🏢 Апартаменты", "🏘 Таунхаус", "🏡 Вилла", "🌍 Plot / Land", "📊 Неважно", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите формат кнопкой.", reply_markup=best_object_format_menu(user_id))
                return
            state["object_format"] = None if text in ["📊 Неважно", tr(user_id, "skip")] else text
            state["step"] = "best_object_budget"
            user_states[user_id] = state
            await message.answer("💰 <b>Шаг 3 из 5</b>\n\nВыберите бюджет.", reply_markup=best_object_budget_menu(user_id))
            return

        if state.get("step") == "best_object_budget":
            allowed = ["до 1M AED", "1–2M AED", "2–3M AED", "3–5M AED", "5M+ AED", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите бюджет кнопкой.", reply_markup=best_object_budget_menu(user_id))
                return
            state["budget"] = None if text == tr(user_id, "skip") else text
            state["step"] = "best_object_rooms"
            user_states[user_id] = state
            await message.answer("🛏 <b>Шаг 4 из 5</b>\n\nВыберите комнатность / наименование юнита.", reply_markup=best_object_rooms_menu(user_id))
            return

        if state.get("step") == "best_object_rooms":
            allowed = ["Studio", "1 BR", "2 BR", "3 BR", "4 BR", "5 BR+", "📊 Неважно", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите комнатность кнопкой.", reply_markup=best_object_rooms_menu(user_id))
                return
            state["rooms"] = None if text in ["📊 Неважно", tr(user_id, "skip")] else text
            state["step"] = "best_object_goal"
            user_states[user_id] = state
            await message.answer("🎯 <b>Шаг 5 из 5</b>\n\nВыберите цель.", reply_markup=best_object_goal_menu(user_id))
            return

        if state.get("step") == "best_object_goal":
            allowed = ["🏡 Для жизни", "🔑 Для аренды", "📈 Для перепродажи", "💰 Максимальный ROI", "⚖️ Сбалансировано", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите цель кнопкой.", reply_markup=best_object_goal_menu(user_id))
                return
            state["goal"] = "⚖️ Сбалансировано" if text == tr(user_id, "skip") else text
            user_states[user_id] = state
            await send_processing(message, "⌛️ <b>Ищу лучший объект</b>\n\n◇ Проверяю DLD-сделки по выбранным фильтрам.\n◇ Сравниваю районы, здания/проекты, ликвидность и цену входа.\n◇ Формирую топ-3 вариантов и вывод 360°.")
            try:
                html = build_best_object_report_v95(state)
            except Exception as e:
                print("BEST_OBJECT_REPORT_ERROR:", repr(e))
                html = no_data_message("Лучший объект")
            user_states[user_id] = {"step": "result", "scope": "dubai", "last_report_title": "Лучший объект", "last_report_html": html, "history": []}
            await message.answer(html, reply_markup=post_result_menu(user_id, "dubai"))
            return

        # Format comparison funnel
        if state.get("step") == "format_compare_scope":
            if text == "🌆 По Дубаю":
                state.update({"scope": "dubai", "name": None, "step": "format_compare_budget"})
                user_states[user_id] = state
                await message.answer("💰 Выберите ориентир бюджета.", reply_markup=format_compare_budget_menu(user_id))
                return
            if text == "🏙 По району":
                state.update({"step": "format_compare_area_query"})
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
                return
            await message.answer("Выберите вариант кнопкой.", reply_markup=format_compare_scope_menu(user_id))
            return

        if state.get("step") == "format_compare_area_query":
            rows = safe_call(find_areas, text, 8, default=[])
            if not rows:
                # принимаем введённый район как есть, чтобы не ломать сценарий на псевдонимах
                state.update({"scope": "area", "name": virtual_area_name(text), "step": "format_compare_budget"})
                user_states[user_id] = state
                await message.answer("💰 Выберите ориентир бюджета.", reply_markup=format_compare_budget_menu(user_id))
                return
            suggestions = []
            for r in rows[:8]:
                area = r.get("area_name_en")
                if area and area not in suggestions:
                    suggestions.append(area)
            state.update({"area_suggestions": suggestions, "step": "format_compare_choose_area"})
            user_states[user_id] = state
            buttons = [[x] for x in suggestions[:8]]
            buttons.append([tr(user_id, "back"), tr(user_id, "main")])
            msg = tr(user_id, "choose_area") + "\n\n"
            for i, area in enumerate(suggestions[:8], 1):
                msg += f"{i}. <b>{area}</b>\n"
            await message.answer(msg, reply_markup=kb(buttons))
            return

        if state.get("step") == "format_compare_choose_area":
            suggestions = state.get("area_suggestions", [])
            chosen = None
            if text in suggestions:
                chosen = text
            else:
                try:
                    idx = int(text.strip()) - 1
                    if 0 <= idx < len(suggestions):
                        chosen = suggestions[idx]
                except Exception:
                    chosen = None
            if not chosen:
                await message.answer("Выберите район из списка.", reply_markup=back_menu(user_id))
                return
            state.update({"scope": "area", "name": chosen, "step": "format_compare_budget"})
            user_states[user_id] = state
            await message.answer("💰 Выберите ориентир бюджета.", reply_markup=format_compare_budget_menu(user_id))
            return

        if state.get("step") == "format_compare_budget":
            allowed = ["до 1M AED", "1–2M AED", "2–3M AED", "3–5M AED", "5M+ AED", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer("Выберите бюджет кнопкой.", reply_markup=format_compare_budget_menu(user_id))
                return
            state["budget"] = None if text == tr(user_id, "skip") else text
            state["step"] = "format_compare_goal"
            user_states[user_id] = state
            await message.answer("🎯 Выберите инвестиционную цель.", reply_markup=format_compare_goal_menu(user_id))
            return

        if state.get("step") == "format_compare_goal":
            if text not in ["📈 Перепродажа", "🔑 Аренда", "💰 ROI", "⚖️ Сбалансировано"]:
                await message.answer("Выберите цель кнопкой.", reply_markup=format_compare_goal_menu(user_id))
                return
            state["goal"] = text
            state["step"] = "format_compare_period"
            user_states[user_id] = state
            await message.answer("📅 Выберите период анализа.", reply_markup=format_compare_period_menu(user_id))
            return

        if state.get("step") == "format_compare_period":
            period_map = {tr(user_id, "p6"): "6", tr(user_id, "p12"): "12", tr(user_id, "p36"): "36", tr(user_id, "all_time"): None}
            if text not in period_map:
                await message.answer("Выберите период кнопкой.", reply_markup=format_compare_period_menu(user_id))
                return
            state["period"] = period_map[text]
            user_states[user_id] = state
            await message.answer(
                "⏳ <b>Сравниваю форматы</b>\n\n◇ Подключаю DLD-архив, live-базу и intelligence-слой.\n◇ Сравниваю апартаменты, виллы и таунхаусы.\n◇ Формирую инвестиционное заключение 360°.",
            )
            report, rows = build_format_comparison_report(
                scope=state.get("scope", "dubai"),
                area=state.get("name"),
                budget=state.get("budget"),
                goal=state.get("goal"),
                period=state.get("period"),
            )
            if not report:
                await message.answer(no_data_message("Сравнение форматов"), reply_markup=format_compare_after_menu(user_id))
                state["step"] = "format_compare_result"
                user_states[user_id] = state
                return
            best_format = rows[0].get("format") if rows else "Apartment"
            state.update({"step": "format_compare_result", "best_format": best_format, "format_rows": rows})
            user_states[user_id] = state
            await message.answer(report, reply_markup=format_compare_after_menu(user_id))
            return

        if state.get("step") == "format_compare_result":
            best = state.get("best_format") or "Apartment"
            if text == "🏆 Лучший формат":
                await message.answer(
                    f"🏆 <b>Лучший формат:</b> {best}\n\nСледующий логичный шаг — посмотреть лучшие районы и здания внутри этого формата.",
                    reply_markup=format_compare_after_menu(user_id)
                )
                return
            if text == "🏙 Лучшие районы":
                await message.answer(show_format_best_areas(best, state.get("period"), state.get("budget")), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "🏢 Лучшие здания":
                await message.answer(show_format_best_buildings(best, state.get("name"), state.get("period")), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "📄 PDF":
                await message.answer("📄 PDF можно сформировать после финального выбора района или здания.", reply_markup=format_compare_after_menu(user_id))
                return
            if text == "💼 Заявка":
                await message.answer("💼 Для консультации: https://t.me/dubai_fpr_lead_bot", reply_markup=format_compare_after_menu(user_id))
                return
            if text == "🔁 Новый отчёт":
                state.clear()
                state.update({"step": "format_compare_scope"})
                user_states[user_id] = state
                await message.answer("⚖️ <b>Сравнение форматов</b>\n\nВыберите рынок анализа:", reply_markup=format_compare_scope_menu(user_id))
                return

        # Smart investment flow — оставлен, но после результата только PDF/Заявка/Изменить.
        if state.get("step") == "smart_goal":
            state.update({"goal": text, "step": "smart_budget"})
            user_states[user_id] = state
            await message.answer("💰 <b>Бюджет</b>\n\nВыберите ориентир бюджета.", reply_markup=smart_budget_menu(user_id))
            return
        if state.get("step") == "smart_budget":
            state.update({"budget": text, "step": "smart_timing"})
            user_states[user_id] = state
            await message.answer("📅 <b>Горизонт покупки</b>\n\nКогда планируется сделка?", reply_markup=smart_timing_menu(user_id))
            return
        if state.get("step") == "smart_timing":
            state.update({"timing": text, "step": "smart_risk"})
            user_states[user_id] = state
            await message.answer("🛡 <b>Профиль риска</b>\n\nВыберите подходящий стиль.", reply_markup=smart_risk_menu(user_id))
            return
        if state.get("step") == "smart_risk":
            state["risk"] = text
            await send_processing(message, "⌛️ <b>Подбираю инвестиционный сценарий</b>\n\n◇ Сопоставляю бюджет, цель и риск.\n◇ Проверяю DLD-активность и ликвидность.\n◇ Формирую заключение 360°.")
            candidates = safe_call(smart_pick_candidates, state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"), default=[]) or []
            if not candidates:
                candidates = smart_fallback_candidates(state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"))

            title = "🧠 Инвестиционный подбор"

            # v93 targeted fix: the smart-pick flow previously built a hardcoded short
            # conclusion here, so the external economic_engine.py was never used for
            # this scenario. Do not touch menus/DB/UI; only route the final smart
            # recommendation through show_smart_recommendation(), which is overridden
            # later by v92 to call the external economic engine.
            try:
                html = show_smart_recommendation(
                    state.get("goal"),
                    state.get("budget"),
                    state.get("timing"),
                    state.get("risk"),
                    candidates,
                )
            except Exception as e:
                print("SMART_RECOMMENDATION_ENGINE_ERROR:", repr(e))
                best = candidates[0] if candidates else {}
                html = (
                    "🧠 <b>Инвестиционный подбор</b>\n\n"
                    "🏆 <b>Лучший сценарий</b>\n"
                    f"📍 Район: <b>{best.get('area') or 'JVC'}</b>\n"
                    f"🏠 Формат: <b>{best.get('property') or best.get('unit_segment') or '1 BR'}</b>\n"
                    f"📊 Сделки: <b>{format_int(best.get('deals'))}</b>\n"
                    f"💰 Средняя цена: <b>{format_money(best.get('avg_price'))}</b>\n"
                    f"📐 Средняя цена за метр: <b>{format_money(best.get('avg_meter'))}</b>\n\n"
                    "🧠 <b>Экономическое заключение 360°</b>\n\n"
                    "Недостаточно данных для полного экономического отчёта. Расширьте период или фильтр."
                )

            user_states[user_id] = {"step": "result", "scope": "dubai", "last_report_title": title, "last_report_html": html, "history": []}
            await message.answer(html, reply_markup=post_result_menu(user_id, "dubai"))
            return

        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))

    except Exception as e:
        print("MAIN_ROUTER_V72_ERROR:", repr(e))
        await message.answer("⚠️ Произошла техническая ошибка в сценарии. Нажмите «Главное меню» и повторите запрос.", reply_markup=main_menu(user_id))


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
# LUXURY UX + PDF + ADMIN + LEAD OVERLAY v63
# =========================
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
LEAD_BOT_URL = os.getenv("LEAD_BOT_URL", "https://t.me/dubai_fpr_lead_bot")
LAST_LEAD_TS = {}


def _ensure_reportlab():
    try:
        import reportlab  # noqa
        return True
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "reportlab"], timeout=120)
            import reportlab  # noqa
            return True
        except Exception as e:
            print("REPORTLAB_INSTALL_ERROR:", repr(e))
            return False


def _html_to_plain(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def build_pdf_bytes(title, content):
    if not _ensure_reportlab():
        return None
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import html as _html

    buffer = tempfile.SpooledTemporaryFile(max_size=5_000_000)
    font_name = "Helvetica"
    for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf"]:
        try:
            if os.path.exists(fp):
                pdfmetrics.registerFont(TTFont("DejaVuSans", fp))
                font_name = "DejaVuSans"
                break
        except Exception:
            pass

    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.35*cm, rightMargin=1.35*cm, topMargin=1.3*cm, bottomMargin=1.3*cm)
    styles = getSampleStyleSheet()
    h = ParagraphStyle("LuxuryHeading", parent=styles["Heading1"], fontName=font_name, fontSize=15, leading=19, spaceAfter=14)
    n = ParagraphStyle("LuxuryNormal", parent=styles["Normal"], fontName=font_name, fontSize=9.5, leading=14)
    story = [Paragraph(_html.escape(title), h), Paragraph("Dubai DLD Intelligence Report · " + datetime.now().strftime("%Y-%m-%d %H:%M"), n), Spacer(1, 0.3*cm)]
    for line in _html_to_plain(content).splitlines():
        if line.strip():
            story.append(Paragraph(_html.escape(line), n))
        else:
            story.append(Spacer(1, 0.14*cm))
    doc.build(story)
    buffer.seek(0)
    return buffer.read()


async def handle_pdf_request(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {}) or {}

    # PDF v65: сначала используем последний реально показанный отчёт.
    # Так PDF не пересчитывает слишком узкий фильтр и не падает после рейтингов/подбора.
    title = state.get("last_report_title") or "Dubai DLD Analytics Report"
    content = state.get("last_report_html")

    await message.answer(
        "📄 <b>Формирую PDF-отчёт</b>\n\n"
        "◇ Беру последний сформированный отчёт.\n"
        "◇ Готовлю финансовое резюме.\n"
        "◇ Упаковываю данные в документ.",
        reply_markup=process_menu(user_id),
    )

    if not content:
        scope = state.get("scope", "dubai")
        name = state.get("name")
        prop = state.get("property")
        period = state.get("period") or "12"
        deal_type = state.get("deal_type")
        row, used_prop, used_period, used_deal_type = get_stats_smart(scope, name, prop, period, deal_type)
        if not row or not _int(row.get("deals")):
            await message.answer(
                "⚠️ <b>PDF-отчёт</b>\n\n"
                "Сначала сформируйте аналитику, сделки, рейтинг или инвестиционный подбор. "
                "После готового результата нажмите PDF ещё раз.",
                reply_markup=main_menu(user_id),
            )
            return
        title = _human_report_title(scope, name, "PDF-отчёт")
        content = show_stats(f"<b>{title}</b>", row, used_prop, used_period, used_deal_type)
        content += _build_360_conclusion(row, scope, name, "pdf")

    pdf = build_pdf_bytes(title, content)
    if not pdf:
        await message.answer(
            "⚠️ <b>PDF-модуль временно недоступен.</b>\n\n"
            "Добавьте в requirements.txt строку:\n"
            "<code>reportlab</code>",
            reply_markup=result_menu(user_id, state.get("scope")),
        )
        return
    from aiogram.types import BufferedInputFile
    await message.answer_document(
        BufferedInputFile(pdf, filename="dubai_dld_analytics_report.pdf"),
        caption="📄 PDF-отчёт готов.",
        reply_markup=result_menu(user_id, state.get("scope")),
    )

async def handle_consultation_request(message):
    user_id = message.from_user.id
    now = time.time()
    if now - LAST_LEAD_TS.get(user_id, 0) < 600:
        await message.answer("⌛️ Заявку можно отправить один раз в 10 минут. Попробуйте немного позже.", reply_markup=result_menu(user_id))
        return
    LAST_LEAD_TS[user_id] = now
    await message.answer(f"💼 <b>Консультация</b>\n\nПерейдите в бот для заявки:\n{LEAD_BOT_URL}", reply_markup=result_menu(user_id))


async def handle_admin_dashboard(message):
    user_id = message.from_user.id
    if not ADMIN_IDS:
        await message.answer(
            f"👑 <b>Админ-панель</b>\n\n"
            f"ADMIN_IDS пока не указан в Railway Variables.\n\n"
            f"Ваш Telegram ID:\n<code>{user_id}</code>\n\n"
            f"Добавьте переменную:\n<code>ADMIN_IDS={user_id}</code>\n\n"
            f"После этого сделайте Redeploy.",
            reply_markup=main_menu(user_id)
        )
        return
    if user_id not in ADMIN_IDS:
        await message.answer("⛔️ Админ-панель доступна только владельцу.", reply_markup=main_menu(user_id))
        return
    try:
        total_sales = 0
        total_rents = 0
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS c FROM {TABLE}")
                total_sales = cur.fetchone()["c"]
        # Archive/live merged counts if helper available.
        await message.answer(
            f"👑 <b>Админ-панель</b>\n\n"
            f"🤖 Статус бота: активен\n"
            f"🏦 Live sale rows: <b>{format_int(total_sales)}</b>\n"
            f"📦 Архив + live engine: <b>включён</b>\n"
            f"🧠 Intelligence overlay: <b>подключён</b>\n"
            f"📄 PDF: <b>включён</b>\n\n"
            f"Для расширенной статистики пользователей можно добавить таблицы bot_users / bot_actions в intelligence DB.",
            reply_markup=main_menu(user_id)
        )
    except Exception as e:
        await message.answer(f"⚠️ Админ-панель временно недоступна.\n\n<code>{str(e)[:500]}</code>", reply_markup=main_menu(user_id))



# =========================
# PRODUCT UX OVERLAY v64
# =========================
# Простая структура: главное меню -> пошаговый сценарий -> адаптивные действия результата.

SEARCH_TABLES_V64 = [
    ("archive", "public.dld_sale_archive"),
    ("archive", "public.dld_rent_archive"),
    ("live", "public.dld_transactions_full"),
    ("live", "public.dld_rents_full"),
]


def _table_parts(full_name):
    if "." in full_name:
        schema, table = full_name.split(".", 1)
    else:
        schema, table = "public", full_name
    return schema, table


def _columns_for_table(table_full):
    schema, table = _table_parts(table_full)
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                """, [schema, table])
                return {r["column_name"] for r in cur.fetchall()}
    except Exception:
        return set()


def _coalesce_for(cols, candidates):
    existing = [c for c in candidates if c in cols]
    if not existing:
        return "''"
    return "COALESCE(" + ", ".join([f"{c}::text" for c in existing]) + ", '')"


def _numeric_for(cols, candidates):
    for c in candidates:
        if c in cols:
            return f"NULLIF(regexp_replace(COALESCE({c}::text,''), '[^0-9.]', '', 'g'), '')::numeric"
    return "NULL::numeric"


def _date_for(cols):
    for c in ["transaction_date", "instance_date", "contract_start_date", "contract_end_date", "load_timestamp", "created_at"]:
        if c in cols:
            return f"NULLIF({c}::text,'')::date"
    return "NULL::date"


def find_buildings(query, limit=10):
    """Robust search across archive + live sale/rent tables.
    Searches building/project/master_project/area fields, so Grande/Corner/Binghatti works even if one table has no building_name_en.
    """
    q = clean_query(query)
    if not q:
        return []
    tokens = smart_query_tokens(q) or [q.lower()]
    merged = {}
    old_source = globals().get("_ACTIVE_SOURCE", "live")
    for source, table in SEARCH_TABLES_V64:
        try:
            if source not in _active_sources():
                continue
            _set_data_source(source)
            cols = _columns_for_table(table)
            if not cols:
                continue
            building_expr = _coalesce_for(cols, ["building_name_en", "building_en", "building_name", "project_name_en", "project_en", "master_project_en", "project_name"])
            area_expr = _coalesce_for(cols, ["area_name_en", "area_en", "area_name", "area"])
            search_expr = "LOWER(" + " || ' ' || ".join([
                building_expr,
                _coalesce_for(cols, ["project_name_en", "project_en", "master_project_en"]),
                area_expr,
            ]) + ")"
            where = []
            params = []
            for t in tokens:
                where.append(f"{search_expr} ILIKE %s")
                params.append(f"%{t}%")
            sql = f"""
                SELECT
                    NULLIF({building_expr}, '') AS building_name_en,
                    NULLIF({area_expr}, '') AS area_name_en,
                    COUNT(*) AS deals
                FROM {table}
                WHERE {' AND '.join(where)}
                  AND NULLIF({building_expr}, '') IS NOT NULL
                GROUP BY 1,2
                ORDER BY deals DESC
                LIMIT %s
            """
            params.append(limit)
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    for r in cur.fetchall():
                        b = r.get("building_name_en")
                        a = r.get("area_name_en")
                        if not b:
                            continue
                        key = (str(b).strip().lower(), str(a or '').strip().lower())
                        item = merged.setdefault(key, {"building_name_en": b, "area_name_en": a, "deals": 0})
                        item["deals"] += int(r.get("deals") or 0)
        except Exception as e:
            print("FIND_BUILDINGS_V64_SOURCE_ERROR:", source, table, repr(e))
        finally:
            try:
                _set_data_source(old_source)
            except Exception:
                pass
    rows = list(merged.values())
    rows.sort(key=lambda x: int(x.get("deals") or 0), reverse=True)
    return rows[:limit]


def show_smart_recommendation(goal, budget, timing, risk, rows):
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."
    best = rows[0]
    good_low = best.get("min_price") or ((best.get("avg_price") or 0) * 0.90 if best.get("avg_price") else None)
    good_high = (best.get("avg_price") or 0) * 0.95 if best.get("avg_price") else None
    area = best.get("area") or "—"
    prop = best.get("property") or "—"
    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        f"🎯 <b>Цель:</b> {goal}\n"
        f"💰 <b>Бюджет:</b> {budget}\n"
        f"⏱ <b>Горизонт:</b> {timing}\n"
        f"⚖️ <b>Риск-профиль:</b> {risk}\n\n"
        "🏆 <b>Лучший выбор</b>\n\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"💰 <b>Средняя цена покупки:</b> {format_money(best.get('avg_price'))}\n"
        f"✅ <b>Комфортная цена входа:</b> {format_money(good_low)} — {format_money(good_high)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(best.get('avg_meter'))}\n"
        f"📊 <b>Количество сделок в выборке:</b> {format_int(best.get('deals'))}\n\n"
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
    )
    if goal == "🏡 Для жизни":
        text += (
            f"Для личного проживания оптимально рассматривать <b>{prop}</b> в районе <b>{area}</b>. "
            "Главный плюс такого выбора — ликвидность, понятная рыночная цена и достаточная глубина сделок. "
            "Это снижает риск переплаты и упрощает будущую перепродажу."
        )
    elif goal == "📈 Перепродажа":
        text += (
            f"Для перепродажи ключевая стратегия — входить в <b>{prop}</b> в районе <b>{area}</b> ниже средней цены DLD. "
            "Чем ниже вход относительно рынка и выше количество сделок, тем сильнее потенциал выхода с прибылью."
        )
    elif goal == "🔑 Аренда":
        text += (
            f"Для арендной стратегии <b>{prop}</b> в районе <b>{area}</b> выглядит логично: такие форматы обычно проще сдавать, "
            "а большое количество сделок помогает точнее оценить реальную рыночную аренду."
        )
    else:
        text += (
            f"Для инвестиции лучший баланс сейчас показывает <b>{prop}</b> в районе <b>{area}</b>. "
            "Ориентир — покупать ниже средней цены DLD, проверять ликвидность здания и избегать объектов с завышенной ценой входа."
        )
    text += "\n\n📋 <b>Альтернативы</b>\n\n"
    for i, r in enumerate(rows[1:], 2):
        text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {format_money(r.get('avg_price'))} · 📊 {format_int(r.get('deals'))} сделок\n\n"
    text += (
        "⚠️ <b>Важно:</b> это аналитический ориентир по DLD. Перед покупкой нужно отдельно проверить конкретный объект: "
        "этаж, вид, состояние, сервисные платежи, срочность продавца и юридическую чистоту сделки."
    )
    return text


def show_unit_summary(title, row, prop=None, period=None):
    if not row:
        return "⚠️ Недостаточно данных для экономического резюме."
    avg_price = row.get("avg_price")
    avg_meter = row.get("avg_meter")
    deals = row.get("deals")
    rent = row.get("avg_rent") or row.get("rent_avg")
    roi = None
    try:
        if avg_price and rent:
            roi = float(rent) / float(avg_price) * 100
    except Exception:
        roi = None
    return (
        f"{title}\n\n"
        "💼 <b>Экономическое резюме 360°</b>\n\n"
        f"📊 <b>Количество сделок в DLD:</b> {format_int(deals)}\n\n"
        f"💰 <b>Средняя цена покупки:</b> {format_money(avg_price)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(avg_meter)}\n"
        f"🏦 <b>Ориентир годовой аренды:</b> {format_money(rent)}\n"
        f"📈 <b>Ориентировочная доходность:</b> {format_pct(roi)}\n\n"
        "🧠 <b>Вывод аналитика</b>\n\n"
        "Если количество сделок высокое, объект или район можно считать более ликвидным: покупателю проще определить справедливую цену, "
        "а инвестору — быстрее выйти из позиции при продаже. Средняя цена показывает ориентир рынка, но финальное решение нужно принимать "
        "только после проверки конкретного юнита, этажа, вида, состояния и сервисных платежей.\n\n"
        "Для сильной покупки целевая цена входа должна быть ниже средней DLD-цены либо компенсироваться высоким качеством объекта."
    )


# =========================
# ROBUST SEARCH + PDF STATE PATCH v66
# =========================
# Усиление поиска после адаптивного меню:
# - район JVC ищется также как Jumeirah Village Circle;
# - building search смотрит building/project/master_project/nearest landmark;
# - поиск идёт по активному источнику archive/live и объединяется на Python layer.

_AREA_ALIASES_V66 = {
    # DLD often stores well-known marketing districts under official area names.
    # Without these aliases smart selection can show "0 deals / no data" for JVC, Marina, Downtown, etc.
    "jvc": [
        "JVC", "Jumeirah Village Circle", "Jumeirah Village", "Jumeirah Village Circle (JVC)",
        "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"
    ],
    "jumeirah village circle": [
        "JVC", "Jumeirah Village Circle", "Jumeirah Village",
        "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"
    ],
    "jlt": ["JLT", "Jumeirah Lakes Towers", "Jumeirah Lake Towers"],
    "downtown": ["Downtown", "Downtown Dubai", "Burj Khalifa"],
    "downtown dubai": ["Downtown", "Downtown Dubai", "Burj Khalifa"],
    "dubai marina": ["Dubai Marina", "Marina", "Marsa Dubai"],
    "marina": ["Dubai Marina", "Marina", "Marsa Dubai"],
    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
}

_SCHEMA_COLS_V66 = {}

def _cols_v66(table):
    key = (_ACTIVE_DATABASE_URL, table)
    if key in _SCHEMA_COLS_V66:
        return _SCHEMA_COLS_V66[key]
    try:
        schema, name = table.split(".", 1) if "." in table else ("public", table)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    """,
                    (schema, name),
                )
                cols = {r["column_name"] for r in cur.fetchall()}
                _SCHEMA_COLS_V66[key] = cols
                return cols
    except Exception as e:
        print("COLS_V66_ERROR:", table, repr(e))
        return set()


def _txt_expr_v66(cols, names):
    found = [n for n in names if n in cols]
    if not found:
        return "''"
    return "COALESCE(" + ", ".join([f"{n}::text" for n in found]) + ", '')"


def _query_aliases_v66(q):
    q = clean_query(q)
    if not q:
        return []
    low = q.lower().strip()
    aliases = [q]
    for k, vals in _AREA_ALIASES_V66.items():
        if low == k or low in [v.lower() for v in vals]:
            aliases.extend(vals)
    # also split very short common user input
    if low == "jvc":
        aliases.extend(["Jumeirah Village Circle"])
    out = []
    for a in aliases:
        if a and a not in out:
            out.append(a)
    return out


def _source_find_buildings_v66(query, limit=10):
    rows = []
    aliases = _query_aliases_v66(query)
    if not aliases:
        return []
    for table in [TABLE, RENT_TABLE]:
        cols = _cols_v66(table)
        if not cols:
            continue
        building = _txt_expr_v66(cols, [
            "building_name_en", "building_name", "building", "project_name_en", "project_name", "project_en", "project",
            "master_project_en", "master_project", "property_name_en", "property_name", "nearest_landmark_en", "nearest_landmark"
        ])
        area = _txt_expr_v66(cols, ["area_name_en", "area_en", "area_name", "area", "procedure_area"])
        search = " || ' ' || ".join([building, area])
        where = " OR ".join([f"({search}) ILIKE %s" for _ in aliases])
        params = [f"%{a}%" for a in aliases]
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            NULLIF({building}, '') AS building_name_en,
                            NULLIF({area}, '') AS area_name_en,
                            COUNT(*)::bigint AS deals
                        FROM {table}
                        WHERE ({where})
                          AND NULLIF({building}, '') IS NOT NULL
                        GROUP BY NULLIF({building}, ''), NULLIF({area}, '')
                        ORDER BY deals DESC
                        LIMIT %s
                    """, params + [limit])
                    rows.extend(cur.fetchall())
        except Exception as e:
            print("SOURCE_FIND_BUILDINGS_V66_ERROR:", table, repr(e))
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def _source_find_areas_v66(query, limit=10):
    rows = []
    aliases = _query_aliases_v66(query)
    if not aliases:
        return []
    for table in [TABLE, RENT_TABLE]:
        cols = _cols_v66(table)
        if not cols:
            continue
        area = _txt_expr_v66(cols, ["area_name_en", "area_en", "area_name", "area", "procedure_area"])
        building = _txt_expr_v66(cols, ["building_name_en", "building_name", "project_name_en", "project_name", "master_project_en", "master_project"])
        search = f"{area} || ' ' || {building}"
        where = " OR ".join([f"({search}) ILIKE %s" for _ in aliases])
        params = [f"%{a}%" for a in aliases]
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"""
                        SELECT
                            NULLIF({area}, '') AS area_name_en,
                            COUNT(*)::bigint AS deals,
                            COUNT(DISTINCT NULLIF({building}, ''))::bigint AS buildings
                        FROM {table}
                        WHERE ({where})
                          AND NULLIF({area}, '') IS NOT NULL
                        GROUP BY NULLIF({area}, '')
                        ORDER BY deals DESC
                        LIMIT %s
                    """, params + [limit])
                    rows.extend(cur.fetchall())
        except Exception as e:
            print("SOURCE_FIND_AREAS_V66_ERROR:", table, repr(e))
    return _merge_group_rows(rows, ["area_name_en"], limit=limit, sort_field="deals", avg_fields=())


def find_buildings(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _source_find_buildings_v66, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def find_areas(query, limit=10):
    rows = []
    for source in _active_sources():
        rows.extend(_call_on_source(source, _source_find_areas_v66, query, limit, default=[]) or [])
    return _merge_group_rows(rows, ["area_name_en"], limit=limit, sort_field="deals", avg_fields=())



# =========================
# FLOW + DATA + PDF HARD PATCH v67
# =========================
# Fixes:
# 1) report buttons no longer restart global scenarios when user is already inside a report flow;
# 2) deals/stats use direct schema-aware archive+live queries with fuzzy building/area matching;
# 3) PDF uses a Cyrillic-capable font and strips emoji glyphs to avoid black squares.

LAST_REPORTS = globals().setdefault("LAST_REPORTS", {})


def set_last_report(user_id, title, html, scope=None):
    LAST_REPORTS[user_id] = {"title": title, "html": html, "scope": scope, "ts": time.time()}
    st = user_states.get(user_id, {}) or {}
    st.update({"step": "result", "last_report_title": title, "last_report_html": html, "scope": scope or st.get("scope")})
    user_states[user_id] = st


def _strip_emoji_for_pdf(text):
    if not text:
        return ""
    # Keep letters/numbers/punctuation; remove Telegram emoji which often render as black boxes in PDFs.
    return re.sub(r"[\U00010000-\U0010ffff]", "", text)


def _pdf_font_path_v67():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            return fp
    # Railway slim images may not have system fonts. Matplotlib bundles DejaVuSans.
    try:
        import matplotlib
        fp = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", "DejaVuSans.ttf")
        if os.path.exists(fp):
            return fp
    except Exception:
        pass
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "matplotlib"], timeout=240)
        import matplotlib
        fp = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", "DejaVuSans.ttf")
        if os.path.exists(fp):
            return fp
    except Exception as e:
        print("PDF_FONT_INSTALL_ERROR:", repr(e))
    return None


def build_pdf_bytes(title, content):
    if not _ensure_reportlab():
        return None
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import html as _html

    buffer = tempfile.SpooledTemporaryFile(max_size=8_000_000)
    font_name = "Helvetica"
    font_path = _pdf_font_path_v67()
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("DLDUnicode", font_path))
            font_name = "DLDUnicode"
        except Exception as e:
            print("PDF_FONT_REGISTER_ERROR:", repr(e))

    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.35*cm, rightMargin=1.35*cm, topMargin=1.2*cm, bottomMargin=1.2*cm)
    styles = getSampleStyleSheet()
    h = ParagraphStyle("LuxuryHeadingV67", parent=styles["Heading1"], fontName=font_name, fontSize=14, leading=18, spaceAfter=12)
    n = ParagraphStyle("LuxuryNormalV67", parent=styles["Normal"], fontName=font_name, fontSize=9.8, leading=14.5, spaceAfter=3)

    plain_title = _strip_emoji_for_pdf(_html_to_plain(title)).strip() or "Dubai DLD Analytics Report"
    plain_content = _strip_emoji_for_pdf(_html_to_plain(content))
    story = [Paragraph(_html.escape(plain_title), h), Paragraph("Dubai DLD Intelligence Report · " + datetime.now().strftime("%Y-%m-%d %H:%M"), n), Spacer(1, 0.25*cm)]
    for raw in plain_content.splitlines():
        line = raw.strip()
        if line:
            story.append(Paragraph(_html.escape(line), n))
        else:
            story.append(Spacer(1, 0.13*cm))
    doc.build(story)
    buffer.seek(0)
    return buffer.read()


def _v67_table_plan(deal_type=None):
    if is_sale_deal(deal_type):
        return [("archive", "public.dld_sale_archive"), ("live", "public.dld_transactions_full")]
    if is_rent_deal(deal_type):
        return [("archive", "public.dld_rent_archive"), ("live", "public.dld_rents_full")]
    return [
        ("archive", "public.dld_sale_archive"),
        ("archive", "public.dld_rent_archive"),
        ("live", "public.dld_transactions_full"),
        ("live", "public.dld_rents_full"),
    ]


def _date_expr_v67(cols):
    for c in ["transaction_date", "instance_date", "contract_start_date", "contract_end_date", "load_timestamp", "created_at", "date"]:
        if c in cols:
            qc = qcol(c) if 'qcol' in globals() else '"' + c + '"'
            return f"CASE WHEN {qc}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN ({qc}::text)::date ELSE NULL END"
    return "NULL::date"


def _num_expr_v67(cols, names):
    for c in names:
        if c in cols:
            qc = qcol(c) if 'qcol' in globals() else '"' + c + '"'
            return f"NULLIF(regexp_replace(COALESCE({qc}::text,''), '[^0-9.]', '', 'g'), '')::numeric"
    return "NULL::numeric"


def _txt_expr2_v67(cols, names):
    found = [n for n in names if n in cols]
    if not found:
        return "''"
    parts = []
    for c in found:
        qc = qcol(c) if 'qcol' in globals() else '"' + c + '"'
        parts.append(f"NULLIF({qc}::text,'')")
    return "COALESCE(" + ", ".join(parts) + ", '')"


def _meta_v67(table):
    cols = _cols_v66(table) if '_cols_v66' in globals() else set(table_columns(table))
    # Building/project name must NOT fall back to area/landmark fields.
    # DLD rent/sale archives often contain nearest_landmark_en / nearest_mall_en / nearest_metro_en,
    # and those values can be districts like Motor City. If we use them as building names,
    # latest deals show an area instead of the actual building/project.
    building = _txt_expr2_v67(cols, [
        "building_name_en", "building_name", "building_en", "building",
        "project_name_en", "project_name", "project_en", "project",
        "master_project_en", "master_project",
        "property_name_en", "property_name"
    ])
    area = _txt_expr2_v67(cols, ["area_name_en", "area_en", "area_name", "area", "procedure_area", "location_en", "location"])
    rooms = _txt_expr2_v67(cols, ["rooms_en", "rooms", "bedrooms", "bedroom", "room", "rooms_count", "rooms_number", "unit_rooms", "bedrooms_count", "bedroom_count"])
    ptype = _txt_expr2_v67(cols, ["property_type_en", "property_type", "prop_type_en", "property_usage_en", "property_usage"])
    subtype = _txt_expr2_v67(cols, ["property_sub_type_en", "property_sub_type", "prop_sub_type_en", "unit_type", "property_category"])
    is_rent_table = "rent" in table.lower()
    price = _num_expr_v67(cols, [
        "annual_amount", "contract_amount", "contract_value", "rent_value", "rent_amount", "actual_worth", "amount"
    ] if is_rent_table else ["actual_worth", "procedure_value", "transaction_value", "sale_price", "price", "amount"])
    size = _num_expr_v67(cols, ["actual_area", "area", "procedure_area", "size_sqft", "property_size_sqft", "property_size", "area_size_sqft"])
    meter = _num_expr_v67(cols, ["meter_sale_price", "meter_price", "price_per_meter", "price_per_sqft"])
    meter = f"COALESCE({meter}, CASE WHEN ({size}) > 0 THEN ({price}) / ({size}) ELSE NULL END)"
    return {"cols": cols, "building": building, "area": area, "rooms": rooms, "ptype": ptype, "subtype": subtype, "price": price, "size": size, "meter": meter, "date": _date_expr_v67(cols)}


def _scope_where_v67(scope, name, meta):
    if not name or scope == "dubai":
        return "", []
    raw_name = str(name)
    if scope == "building" and "|||" in raw_name:
        building_name, area_name = raw_name.split("|||", 1)
        return (
            f"AND LOWER(NULLIF({meta['building']}, '')) = LOWER(%s) AND LOWER(NULLIF({meta['area']}, '')) = LOWER(%s)",
            [building_name.strip(), area_name.strip()]
        )
    aliases = _query_aliases_v66(raw_name) if '_query_aliases_v66' in globals() else [raw_name]
    if scope == "area":
        search = meta["area"]
    else:
        # For buildings, prefer exact building match, but keep fuzzy fallback for typed free text.
        parts, params = [], []
        for a in aliases:
            parts.append(f"LOWER(NULLIF({meta['building']}, '')) = LOWER(%s)")
            params.append(a)
            parts.append(f"({meta['building']}) ILIKE %s")
            params.append(f"%{a}%")
        return "AND (" + " OR ".join(parts) + ")", params
    parts, params = [], []
    for a in aliases:
        parts.append(f"({search}) ILIKE %s")
        params.append(f"%{a}%")
    return "AND (" + " OR ".join(parts) + ")", params


def _prop_where_v67(prop, meta):
    if not prop:
        return "", []
    p = str(prop).lower().strip()
    if p in ["⏭ пропустить", "skip", "any", "all", "все"]:
        return "", []
    search = "LOWER(" + " || ' ' || ".join([meta["rooms"], meta["ptype"], meta["subtype"]]) + ")"
    if "studio" in p:
        return f"AND ({search} LIKE %s OR {search} LIKE %s)", ["%studio%", "%студ%"]
    m = re.search(r"(\d+)\s*br", p)
    if m:
        n = m.group(1)
        words = {"1":"one", "2":"two", "3":"three", "4":"four", "5":"five"}.get(n, n)
        return f"AND ({search} LIKE %s OR {search} LIKE %s OR {search} LIKE %s OR {search} LIKE %s)", [f"%{n} br%", f"%{n} b/r%", f"%{n} bedroom%", f"%{words} bedroom%"]
    if "villa" in p.lower() or "вилл" in p.lower():
        return f"AND {search} LIKE %s", ["%villa%"]
    if "town" in p.lower() or "таун" in p.lower():
        return f"AND {search} LIKE %s", ["%town%"]
    return f"AND {search} LIKE %s", [f"%{p}%"]


def _period_where_v67(period, meta):
    months = period_months(period) if 'period_months' in globals() else None
    if not months:
        return "", []
    return f"AND {meta['date']} >= CURRENT_DATE - INTERVAL '{int(months)} months'", []


def _run_source_sql_v67(source, table, sql, params):
    old = globals().get("_ACTIVE_SOURCE", "live")
    try:
        _set_data_source(source)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as e:
        print("RUN_SOURCE_SQL_V67_ERROR:", source, table, repr(e))
        return []
    finally:
        try:
            _set_data_source(old)
        except Exception:
            pass


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    rows = []
    for source, table in _v67_table_plan(deal_type):
        if source not in _active_sources():
            continue
        old_src = globals().get("_ACTIVE_SOURCE", "live")
        try:
            _set_data_source(source)
            meta = _meta_v67(table)
        finally:
            try:
                _set_data_source(old_src)
            except Exception:
                pass
        if not meta["cols"]:
            continue
        sw, sp = _scope_where_v67(scope, name, meta)
        pw, pp = _prop_where_v67(prop, meta)
        tw, tp = _period_where_v67(period, meta)
        sql = f"""
            SELECT
                {meta['date']} AS safe_date,
                NULLIF({meta['building']}, '') AS building_name_en,
                NULLIF({meta['area']}, '') AS area_name_en,
                NULLIF({meta['rooms']}, '') AS rooms_en,
                NULLIF({meta['ptype']}, '') AS property_type_en,
                NULLIF({meta['subtype']}, '') AS property_sub_type_en,
                {meta['price']} AS price,
                {meta['size']} AS area_size,
                {meta['meter']} AS meter_price
            FROM {table}
            WHERE {meta['price']} IS NOT NULL AND {meta['price']} > 0
              {sw} {pw} {tw}
            ORDER BY safe_date DESC NULLS LAST
            LIMIT %s
        """
        rows.extend(_run_source_sql_v67(source, table, sql, sp + pp + tp + [limit]))
    return _merge_latest_rows(rows, limit=limit) if '_merge_latest_rows' in globals() else rows[:limit]


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    parts = []
    for source, table in _v67_table_plan(deal_type):
        if source not in _active_sources():
            continue
        old_src = globals().get("_ACTIVE_SOURCE", "live")
        try:
            _set_data_source(source)
            meta = _meta_v67(table)
        finally:
            try:
                _set_data_source(old_src)
            except Exception:
                pass
        if not meta["cols"]:
            continue
        sw, sp = _scope_where_v67(scope, name, meta)
        pw, pp = _prop_where_v67(prop, meta)
        tw, tp = _period_where_v67(period, meta)
        sql = f"""
            SELECT
                COUNT(*)::bigint AS deals,
                COUNT(DISTINCT NULLIF({meta['building']}, ''))::bigint AS buildings,
                COUNT(DISTINCT NULLIF({meta['area']}, ''))::bigint AS areas,
                AVG({meta['price']})::numeric AS avg_price,
                MIN({meta['price']})::numeric AS min_price,
                MAX({meta['price']})::numeric AS max_price,
                AVG({meta['meter']})::numeric AS avg_meter,
                MIN({meta['date']}) AS first_deal,
                MAX({meta['date']}) AS last_deal,
                STRING_AGG(DISTINCT NULLIF({meta['rooms']}, ''), ', ') AS rooms_list,
                STRING_AGG(DISTINCT NULLIF({meta['ptype']}, ''), ', ') AS property_types,
                STRING_AGG(DISTINCT NULLIF({meta['subtype']}, ''), ', ') AS property_sub_types
            FROM {table}
            WHERE {meta['price']} IS NOT NULL AND {meta['price']} > 0
              {sw} {pw} {tw}
        """
        got = _run_source_sql_v67(source, table, sql, sp + pp + tp)
        if got and _int(got[0].get("deals")) > 0:
            parts.append(got[0])
    return _merge_stats_rows(parts) if parts and '_merge_stats_rows' in globals() else (parts[0] if parts else None)


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    # If strict sale/rent has no data, we still DO NOT mix sale/rent; only broaden property/period.
    for p, per, dt in attempts:
        row = get_stats(scope, name, p, per, dt)
        if row and _int(row.get("deals")) > 0:
            return row, p, per, dt
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
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


async def _handle_current_report_button_v67(message, text, state):
    user_id = message.from_user.id
    scope = state.get("scope", "dubai")
    name = state.get("name")
    prop = state.get("property")
    period = state.get("period")
    deal_type = state.get("deal_type")
    if text in ["📊 Аналитика", tr(user_id, "full_report"), "💼 Резюме"]:
        await send_full_report(message, scope, name, prop, period, deal_type, "Полная аналитика")
        return True
    if text in ["🧾 Сделки", tr(user_id, "last_deals"), "📊 По сделкам"]:
        await send_deals_report(message, scope, name, prop, period, deal_type)
        return True
    if text in ["📈 Периоды", tr(user_id, "period_compare")]:
        await send_period_report(message, scope, name, prop, period, deal_type)
        return True
    return False



# =========================
# SMART PICK DATA FIX v73
# =========================
# Fixes the investment-selection card showing "0 deals / no data" even when archive/live
# contain DLD rows. The previous smart selector could fall back too early. This override uses
# the current schema-aware archive+live stats layer and the corrected official-area aliases.

def smart_pick_candidates(goal, budget_text, risk, timing):
    bmin, bmax = parse_budget_range(budget_text)
    ptypes = recommended_property_types(goal, budget_text)
    areas = smart_area_universe(goal)
    results = []

    # Use a broad period for investment selection, because the exact user timing is a buying
    # horizon, not a DLD data window. We need enough liquidity sample for a sane recommendation.
    data_period = "3 года"

    for display_area, _real_areas in areas:
        for prop in ptypes:
            try:
                row, used_prop, _used_period, _used_deal_type = get_stats_smart(
                    scope="area",
                    name=display_area,
                    prop=prop,
                    period=data_period,
                    deal_type="sale",
                )
            except Exception as e:
                print("SMART_PICK_V73_STATS_ERROR:", repr(e))
                row = None
                used_prop = prop

            if not row or _int(row.get("deals")) <= 0:
                continue

            deals = _int(row.get("deals"))
            avg_price = _num(row.get("avg_price")) or 0
            avg_meter = _num(row.get("avg_meter")) or 0
            min_price = _num(row.get("min_price"))
            max_price = _num(row.get("max_price"))

            budget_mid = ((bmin or 0) + (bmax or avg_price or 0)) / 2 if (bmin or bmax) else avg_price
            if budget_mid and avg_price:
                affordability = 100 - min(100, abs(avg_price - budget_mid) / max(budget_mid, 1) * 100)
            else:
                affordability = 45

            liquidity = min(100, deals / 50 * 100)
            score = liquidity * 0.55 + affordability * 0.35

            risk_text = str(risk or "").lower()
            goal_text = str(goal or "").lower()
            prop_text = str(used_prop or prop or "").lower()

            if "низ" in risk_text and deals >= 50:
                score += 14
            elif "сбал" in risk_text:
                score += 10
            elif "агр" in risk_text:
                score += 8

            if any(x in goal_text for x in ["roi", "аренд", "инвест"]):
                if "studio" in prop_text or "1 br" in prop_text:
                    score += 12
            if "жизн" in goal_text and any(x in prop_text for x in ["1 br", "2 br", "villa", "town"]):
                score += 10
            if "перепрод" in goal_text and any(x in prop_text for x in ["studio", "1 br", "2 br"]):
                score += 10

            results.append({
                "area": display_area,
                "property": used_prop or prop,
                "deals": deals,
                "buildings": _int(row.get("buildings")),
                "avg_price": avg_price,
                "min_price": min_price,
                "max_price": max_price,
                "avg_meter": avg_meter,
                "first_deal": row.get("first_deal"),
                "last_deal": row.get("last_deal"),
                "score": score,
            })

    if results:
        return sorted(results, key=lambda x: (x.get("score") or 0, x.get("deals") or 0), reverse=True)[:5]

    # Last safe fallback: never crash the user flow, but only after real archive/live attempts.
    return smart_fallback_candidates(goal, budget_text, risk, timing)

print("Loaded smart pick data fix v73")

print("Loaded adaptive exact archive/live menu patch v71")

print("Loaded robust search patch v66")

print("Loaded dual database archive+live engine v50")


# =========================
# v75 RATING FIX ONLY
# Исправляет только раздел рейтингов. Остальная логика файла не меняется.
# Причина прошлой ошибки: старый рейтинг опирался на один активный TABLE/base_from()
# и падал/возвращал пусто при несовпадении схем archive/live. Этот слой читает archive+live
# напрямую и сам адаптируется к доступным колонкам.
# =========================

_V75_SCHEMA_CACHE = {}


def _v75_conn(url):
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def _v75_cols(conn, table):
    key = (id(conn), table)
    # id(conn) кэшировать бессмысленно между подключениями, но безопасно внутри запроса.
    try:
        schema, name = table.split('.', 1) if '.' in table else ('public', table)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
            """, (schema, name))
            return {r['column_name'] for r in cur.fetchall()}
    except Exception:
        return set()


def _v75_q(col):
    return '"' + str(col).replace('"', '""') + '"'


def _v75_first(cols, candidates):
    for c in candidates:
        if c in cols:
            return c
    return None


def _v75_text_expr(cols, candidates):
    present = [c for c in candidates if c in cols]
    if not present:
        return "''"
    return "COALESCE(" + ", ".join([f"NULLIF({_v75_q(c)}::text, '')" for c in present]) + ", '')"


def _v75_num_expr(cols, candidates):
    c = _v75_first(cols, candidates)
    if not c:
        return "NULL::numeric"
    return f"NULLIF(regexp_replace(COALESCE({_v75_q(c)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


def _v75_date_expr(cols):
    c = _v75_first(cols, ['transaction_date', 'instance_date', 'registration_date', 'date', 'created_at', 'contract_start_date', 'start_date'])
    if not c:
        return "NULL::date"
    qc = _v75_q(c)
    return f"""
        CASE
            WHEN {qc}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN ({qc}::text)::date
            WHEN {qc}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}' THEN TO_DATE(SUBSTRING({qc}::text, 1, 10), 'MM/DD/YYYY')
            ELSE NULL::date
        END
    """


def _v75_source_tables():
    out = []
    for source in _active_sources():
        cfg = DUAL_DB_SOURCES[source]
        out.append((source, cfg['url'], cfg['sales_table'], 'sale'))
        out.append((source, cfg['url'], cfg['rent_table'], 'rent'))
    return out


def _v75_fetch_rating_from_table(url, table, table_kind, dimension='building', metric='deals', limit=25):
    try:
        with _v75_conn(url) as conn:
            cols = _v75_cols(conn, table)
            if not cols:
                return []

            building = _v75_text_expr(cols, [
                'building_name_en', 'building_en', 'building_name', 'building',
                'project_name_en', 'project_en', 'project_name', 'project',
                'master_project_en', 'master_project'
            ])
            area = _v75_text_expr(cols, ['area_name_en', 'area_en', 'area_name', 'area', 'location_en', 'location', 'district', 'community'])
            price_candidates = ['actual_worth', 'actual_value', 'transaction_value', 'price', 'value', 'amount']
            if table_kind == 'rent':
                price_candidates = ['annual_amount', 'contract_amount', 'rent_value', 'actual_worth', 'amount', 'price', 'value']
            price = _v75_num_expr(cols, price_candidates)
            meter = _v75_num_expr(cols, ['meter_sale_price', 'meter_price', 'price_per_meter', 'price_per_sqft'])
            dt = _v75_date_expr(cols)

            if dimension == 'area':
                key_select = f"{area} AS area_name_en, ''::text AS building_name_en"
                group_by = f"{area}"
                not_blank = f"NULLIF(TRIM({area}), '') IS NOT NULL"
            else:
                key_select = f"{building} AS building_name_en, {area} AS area_name_en"
                group_by = f"{building}, {area}"
                not_blank = f"NULLIF(TRIM({building}), '') IS NOT NULL"

            with conn.cursor() as cur:
                if metric == 'growth':
                    # Рост считаем по продажам: текущие 365 дней против предыдущих 365 дней.
                    # Если дат нет — таблица просто не участвует.
                    if table_kind != 'sale':
                        return []
                    cur.execute(f"""
                        WITH x AS (
                            SELECT {key_select}, {price} AS price, {dt} AS d
                            FROM {table}
                            WHERE {not_blank} AND {price} IS NOT NULL AND {dt} IS NOT NULL
                        ), g AS (
                            SELECT
                                building_name_en,
                                area_name_en,
                                COUNT(*) FILTER (WHERE d >= CURRENT_DATE - INTERVAL '365 days') AS deals,
                                AVG(price) FILTER (WHERE d >= CURRENT_DATE - INTERVAL '365 days') AS avg_price,
                                AVG(price) FILTER (WHERE d < CURRENT_DATE - INTERVAL '365 days' AND d >= CURRENT_DATE - INTERVAL '730 days') AS prev_price
                            FROM x
                            GROUP BY building_name_en, area_name_en
                        )
                        SELECT *,
                               CASE WHEN prev_price IS NOT NULL AND prev_price > 0
                                    THEN ((avg_price - prev_price) / prev_price) * 100
                                    ELSE NULL END AS growth_pct,
                               NULL::numeric AS avg_meter
                        FROM g
                        WHERE deals > 0 AND avg_price IS NOT NULL AND prev_price IS NOT NULL AND prev_price > 0
                        ORDER BY growth_pct DESC NULLS LAST, deals DESC
                        LIMIT %s
                    """, (limit,))
                    return cur.fetchall()

                # Для активности и ликвидности цену не требуем: иначе DLD-таблицы с неполной ценой дают пустой рейтинг.
                price_filter = ""
                having = ""
                order_by = "deals DESC"
                if metric == 'price':
                    price_filter = f"AND {price} IS NOT NULL"
                    having = "HAVING COUNT(*) >= 3 AND AVG(price) IS NOT NULL"
                    order_by = "avg_price DESC NULLS LAST, deals DESC"
                elif metric == 'liquidity':
                    order_by = "deals DESC, avg_price DESC NULLS LAST"

                cur.execute(f"""
                    WITH x AS (
                        SELECT {key_select}, {price} AS price, {meter} AS meter
                        FROM {table}
                        WHERE {not_blank} {price_filter}
                    )
                    SELECT
                        building_name_en,
                        area_name_en,
                        COUNT(*) AS deals,
                        AVG(price) AS avg_price,
                        AVG(meter) AS avg_meter
                    FROM x
                    GROUP BY building_name_en, area_name_en
                    {having}
                    ORDER BY {order_by}
                    LIMIT %s
                """, (limit,))
                return cur.fetchall()
    except Exception as e:
        print(f"RATING_TABLE_ERROR {table} {dimension} {metric}:", repr(e))
        return []


def _v75_merge_rating_rows(rows, dimension='building', metric='deals', limit=10):
    grouped = {}
    for r in rows:
        if not r:
            continue
        b = str(r.get('building_name_en') or '').strip()
        a = str(r.get('area_name_en') or '').strip()
        key = (a.lower(), '') if dimension == 'area' else (b.lower(), a.lower())
        if not key[0] and not key[1]:
            continue
        g = grouped.setdefault(key, {
            'building_name_en': b,
            'area_name_en': a,
            'deals': 0,
            '_price_sum': 0.0,
            '_price_w': 0,
            '_meter_sum': 0.0,
            '_meter_w': 0,
            '_growth_sum': 0.0,
            '_growth_w': 0,
        })
        deals = _int(r.get('deals'))
        g['deals'] += deals
        for src_key, sum_key, w_key in [
            ('avg_price', '_price_sum', '_price_w'),
            ('avg_meter', '_meter_sum', '_meter_w'),
            ('growth_pct', '_growth_sum', '_growth_w'),
        ]:
            val = _num(r.get(src_key))
            if val is not None and deals > 0:
                g[sum_key] += val * deals
                g[w_key] += deals
    out = []
    for g in grouped.values():
        g['avg_price'] = g.pop('_price_sum') / g.pop('_price_w') if g.get('_price_w') else None
        g.pop('_price_w', None)
        g['avg_meter'] = g.pop('_meter_sum') / g.pop('_meter_w') if g.get('_meter_w') else None
        g.pop('_meter_w', None)
        g['growth_pct'] = g.pop('_growth_sum') / g.pop('_growth_w') if g.get('_growth_w') else None
        g.pop('_growth_w', None)
        out.append(g)

    if metric == 'price':
        out.sort(key=lambda x: (_num(x.get('avg_price')) is not None, _num(x.get('avg_price')) or 0, _int(x.get('deals'))), reverse=True)
    elif metric == 'growth':
        out.sort(key=lambda x: (_num(x.get('growth_pct')) is not None, _num(x.get('growth_pct')) or -999999, _int(x.get('deals'))), reverse=True)
    else:
        out.sort(key=lambda x: (_int(x.get('deals')), _num(x.get('avg_price')) or 0), reverse=True)
    return out[:limit]


def get_rating_rows_v75(dimension='building', metric='deals', limit=10):
    rows = []
    for _source, url, table, table_kind in _v75_source_tables():
        # Цена и рост имеют смысл по sales; активность/ликвидность — sale+rent.
        if metric in ('price', 'growth') and table_kind != 'sale':
            continue
        rows.extend(_v75_fetch_rating_from_table(url, table, table_kind, dimension=dimension, metric=metric, limit=max(limit * 3, 20)) or [])
    return _v75_merge_rating_rows(rows, dimension=dimension, metric=metric, limit=limit)


def _v75_ranking_parse(ranking_type):
    rt = str(ranking_type or '').lower()
    dimension = 'area' if 'area' in rt or 'район' in rt else 'building'
    if 'price' in rt or 'цен' in rt:
        metric = 'price'
    elif 'growth' in rt or 'рост' in rt:
        metric = 'growth'
    elif 'liquid' in rt or 'ликвид' in rt:
        metric = 'liquidity'
    else:
        metric = 'deals'
    return dimension, metric


async def send_ranking_report(message, ranking_type="building_deals"):
    user_id = message.from_user.id
    await send_processing(message)
    dimension, metric = _v75_ranking_parse(ranking_type)

    title_map = {
        ('building', 'deals'): '📊 Рейтинг зданий по количеству сделок',
        ('area', 'deals'): '📊 Рейтинг районов по количеству сделок',
        ('building', 'price'): '💰 Рейтинг зданий по средней цене',
        ('area', 'price'): '💰 Рейтинг районов по средней цене',
        ('building', 'growth'): '📈 Рейтинг зданий по росту цены',
        ('area', 'growth'): '📈 Рейтинг районов по росту цены',
        ('building', 'liquidity'): '💧 Рейтинг зданий по ликвидности',
        ('area', 'liquidity'): '💧 Рейтинг районов по ликвидности',
    }
    title = title_map.get((dimension, metric), '📊 Рейтинг рынка')

    rows = safe_call(get_rating_rows_v75, dimension, metric, 10, default=[]) or []
    if not rows and metric == 'growth':
        # Если в DLD нет достаточной истории по датам, показываем активность вместо пустого экрана.
        rows = safe_call(get_rating_rows_v75, dimension, 'deals', 10, default=[]) or []
        title += '\n<em>Недостаточно истории для точного роста — показана ликвидность по сделкам.</em>'

    if not rows:
        await message.answer(
            "⚠️ <b>Рейтинг</b>\n\n"
            "Не удалось собрать рейтинг из archive/live.\n\n"
            "Проверьте, что переменные Railway указывают на правильные базы:\n"
            "• LIVE_DATABASE_URL\n"
            "• ARCHIVE_DATABASE_URL",
            reply_markup=ranking_menu(user_id),
        )
        return

    html = f"<b>{title}</b>\n\n"
    for i, r in enumerate(rows[:10], 1):
        name = r.get('area_name_en') if dimension == 'area' else r.get('building_name_en')
        area = r.get('area_name_en')
        html += f"{i}. <b>{name or '-'}</b>\n"
        if dimension == 'building' and area:
            html += f"📍 Район: {area}\n"
        html += f"📊 Сделки: <b>{format_int(r.get('deals'))}</b>\n"
        if r.get('avg_price') is not None:
            html += f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n"
        if r.get('avg_meter') is not None:
            html += f"📐 Средняя цена за метр: <b>{format_money(r.get('avg_meter'))}</b>\n"
        if r.get('growth_pct') is not None:
            html += f"📈 Рост цены: <b>{float(r.get('growth_pct')):.1f}%</b>\n"
        html += "\n"

    html += (
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
        "Рейтинг построен по объединённому слою archive + live. "
        "Для инвестора высокий объём сделок означает лучшую ликвидность, "
        "более понятную рыночную цену и меньший риск зависнуть в продаже."
    )
    set_last_report(user_id, 'Рейтинг рынка', html, 'dubai')
    await message.answer(html, reply_markup=_final_actions_menu(user_id, 'dubai'))

print("Loaded ratings fix v75 only")




# =========================
# v95 BEST OBJECT MENU ADDON
# Scope: adds a separate "🏆 Лучший объект" scenario only.
# Existing building/area/deals/ratings/economic engine flows are untouched.
# =========================

def best_object_deal_type_menu(user_id):
    return kb([
        ["🏠 Продажа", "🔑 Аренда"],
        ["📊 Неважно", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def best_object_format_menu(user_id):
    return kb([
        ["🏢 Апартаменты", "🏘 Таунхаус"],
        ["🏡 Вилла", "🌍 Plot / Land"],
        ["📊 Неважно", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def best_object_budget_menu(user_id):
    return kb([
        ["до 1M AED", "1–2M AED"],
        ["2–3M AED", "3–5M AED"],
        ["5M+ AED", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def best_object_rooms_menu(user_id):
    return kb([
        ["Studio", "1 BR", "2 BR"],
        ["3 BR", "4 BR", "5 BR+"],
        ["📊 Неважно", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def best_object_goal_menu(user_id):
    return kb([
        ["🏡 Для жизни", "🔑 Для аренды"],
        ["📈 Для перепродажи", "💰 Максимальный ROI"],
        ["⚖️ Сбалансировано", tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def _v95_num(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _v95_budget_bounds(label):
    if not label:
        return None, None
    return _budget_bounds(label) if '_budget_bounds' in globals() else parse_budget_range(label)


def _v95_is_rent(deal_type):
    return is_rent_deal(deal_type) if 'is_rent_deal' in globals() else is_rent_deal_type(deal_type)


def _v95_format_clause(fmt, rent=False):
    if not fmt:
        return "", []
    f = str(fmt).lower()
    if any(x in f for x in ["апартамент", "apartment", "flat", "unit", "studio"]):
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s OR property_sub_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%unit%", "%flat%", "%apartment%", "%studio%"]
    if any(x in f for x in ["таун", "townhouse", "town house", "town"]):
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%town%", "%town%"]
    if any(x in f for x in ["вил", "villa"]):
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%villa%", "%villa%"]
    if any(x in f for x in ["plot", "land", "зем", "плот", "участ"]):
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s OR property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%land%", "%land%", "%plot%", "%plot%"]
    return "", []


def _v95_rooms_clause(rooms):
    if not rooms:
        return "", []
    return property_condition(rooms)


def _v95_value_source(deal_type):
    if _v95_is_rent(deal_type):
        return {
            "base": rent_base_from(),
            "value": "rent_price",
            "meter": "rent_meter_price",
            "date_filter": "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'",
            "deal_name": "аренда",
        }
    return {
        "base": base_from(),
        "value": PRICE,
        "meter": METER_PRICE,
        "date_filter": "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'",
        "deal_name": "продажа",
    }


def _v95_budget_clause(value_expr, budget):
    bmin, bmax = _v95_budget_bounds(budget)
    parts, params = [], []
    if bmin is not None:
        parts.append(f"{value_expr} >= %s")
        params.append(bmin)
    if bmax is not None:
        parts.append(f"{value_expr} <= %s")
        params.append(bmax)
    if not parts:
        return "", []
    return "AND " + " AND ".join(parts), params


def _v95_score(row, goal):
    deals = _v95_num(row.get("deals"))
    avg_price = _v95_num(row.get("avg_price"))
    avg_meter = _v95_num(row.get("avg_meter"))
    min_price = _v95_num(row.get("min_price"))
    max_price = _v95_num(row.get("max_price"))
    liquidity = min(deals / 300.0, 1.0) * 42
    price_eff = max(0, 24 - (avg_meter / 2200.0)) if avg_meter else 6
    spread = min(((max_price - min_price) / avg_price) * 10, 18) if avg_price and max_price and min_price else 6
    stability = min(deals / 100.0, 1.0) * 16
    score = liquidity + price_eff + spread + stability
    g = str(goal or '').lower()
    if "аренд" in g or "roi" in g:
        score += liquidity * 0.20 + price_eff * 0.15
    if "перепрод" in g:
        score += spread * 0.35 + liquidity * 0.15
    if "жизни" in g:
        score += stability * 0.30
    return round(min(score, 100), 1)


def _v95_query_top(kind, state, relaxed_budget=False, limit=3):
    deal_type = state.get("deal_type") or "🏠 Продажа"
    src = _v95_value_source(deal_type)
    value_expr = src["value"]
    meter_expr = src["meter"]
    fmt_sql, fmt_args = _v95_format_clause(state.get("object_format"), _v95_is_rent(deal_type))
    room_sql, room_args = _v95_rooms_clause(state.get("rooms"))
    budget_sql, budget_args = ("", []) if relaxed_budget else _v95_budget_clause(value_expr, state.get("budget"))
    group_col = "area_name_en" if kind == "area" else "building_name_en"
    extra_select = "COUNT(DISTINCT building_name_en) AS buildings," if kind == "area" else "MAX(area_name_en) AS area_name_en,"
    not_empty = f"AND NULLIF({group_col}::text, '') IS NOT NULL"
    params = fmt_args + room_args + budget_args + [limit]
    sql = f"""
        SELECT
            {group_col} AS name,
            {extra_select}
            COUNT(*) AS deals,
            AVG({value_expr}) AS avg_price,
            MIN({value_expr}) AS min_price,
            MAX({value_expr}) AS max_price,
            AVG({meter_expr}) AS avg_meter,
            MIN(safe_date) AS first_deal,
            MAX(safe_date) AS last_deal
        {src['base']}
          {fmt_sql}
          {room_sql}
          {budget_sql}
          {src['date_filter']}
          {not_empty}
          AND {value_expr} IS NOT NULL
          AND {value_expr} > 0
        GROUP BY {group_col}
        HAVING COUNT(*) >= 3
        ORDER BY deals DESC, avg_price ASC NULLS LAST
        LIMIT %s
    """
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        for r in rows:
            r["score"] = _v95_score(r, state.get("goal"))
        return sorted(rows, key=lambda r: (_v95_num(r.get("score")), _v95_num(r.get("deals"))), reverse=True)[:limit]
    except Exception as e:
        print("V95_TOP_QUERY_ERROR", kind, repr(e))
        return []


def _v95_top_areas_and_buildings(state):
    notes = []
    areas = _v95_query_top("area", state, relaxed_budget=False, limit=3)
    buildings = _v95_query_top("building", state, relaxed_budget=False, limit=3)
    if not areas or not buildings:
        notes.append("По точному бюджету выборка узкая, поэтому я расширил бюджетный фильтр и показываю ближайшие рабочие варианты, а не останавливаю сценарий.")
        areas = areas or _v95_query_top("area", state, relaxed_budget=True, limit=3)
        buildings = buildings or _v95_query_top("building", state, relaxed_budget=True, limit=3)
    return areas, buildings, notes


def _v95_line(i, row, kind):
    name = row.get("name") or "—"
    area = row.get("area_name_en")
    text = f"{i}. <b>{name}</b>"
    if kind == "building" and area:
        text += f" — {area}"
    text += "\n"
    text += f"   📊 Сделок: <b>{format_int(row.get('deals'))}</b> · 🎯 индекс: <b>{row.get('score')}/100</b>\n"
    text += f"   💰 Средняя цена: <b>{format_money(row.get('avg_price'))}</b> · 📐 за м²/ft²: <b>{format_money(row.get('avg_meter'))}</b>\n"
    return text



def _v100_apply_router_fallback_to_state(base_state, fallback_step):
    """Build a legacy v95 state from router fallback step.
    Important: fallback must be executed, not printed to user as English debug text.
    """
    st = dict(base_state or {})
    try:
        fmt_map = {
            "apartment": "Apartment",
            "townhouse": "Townhouse",
            "villa": "Villa",
            "land": "Plot",
            "plot": "Plot",
            "office": "Office",
            "shop": "Shop",
            "retail": "Shop",
            "commercial": "Commercial",
            "penthouse": "Penthouse",
            "duplex": "Duplex",
        }
        pf = getattr(fallback_step, "property_format", None)
        br = getattr(fallback_step, "bedrooms", None)
        bmin = getattr(fallback_step, "budget_min", None)
        bmax = getattr(fallback_step, "budget_max", None)

        if pf is None:
            st["object_format"] = None
        elif pf in fmt_map:
            st["object_format"] = fmt_map.get(pf)

        if pf in {"land", "plot", "office", "shop", "retail", "commercial", "warehouse", "full_building"}:
            st["rooms"] = None
        elif br is None:
            st["rooms"] = None
        else:
            st["rooms"] = str(br).title().replace(" Br", " BR")

        # If router expanded budget numerically, old helper cannot parse numeric bands safely.
        # For recovery we either keep original exact label or remove budget if it was widened.
        if bmin is None and bmax is None:
            st["budget"] = None
        elif (bmin != _v95_budget_bounds(base_state.get("budget"))[0]) or (bmax != _v95_budget_bounds(base_state.get("budget"))[1]):
            st["budget"] = None

    except Exception as e:
        print("V100_FALLBACK_STATE_ERROR:", repr(e))
    return st


def _v100_try_router_fallbacks(payload, normalized):
    """Execute router fallback cascade and return first non-empty result."""
    if not payload:
        return [], [], []
    notes = []
    try:
        # Skip first step because it is the exact filter already tried.
        for st in payload.sql_plan.fallback_steps[1:]:
            candidate_state = _v100_apply_router_fallback_to_state(normalized, st)
            areas, buildings, local_notes = _v95_top_areas_and_buildings(candidate_state)
            if areas or buildings:
                notes.append("Точная выборка была узкой, поэтому я автоматически расширил фильтр: " + getattr(st, "reason", "fallback"))
                notes.extend(local_notes or [])
                return areas, buildings, notes
    except Exception as e:
        print("V100_ROUTER_FALLBACK_EXEC_ERROR:", repr(e))
    return [], [], notes

def build_best_object_report_v95(state):
    areas, buildings, notes = _v95_top_areas_and_buildings(state)
    if not areas and not buildings:
        return no_data_message("Лучший объект")
    best_area = areas[0] if areas else None
    best_building = buildings[0] if buildings else None
    deal_type = state.get("deal_type") or "неважно"
    obj_format = state.get("object_format") or "любой формат"
    budget = state.get("budget") or "не указан"
    rooms = state.get("rooms") or "неважно"
    goal = state.get("goal") or "⚖️ Сбалансировано"

    chosen = best_building or best_area or {}
    avg_price = _v95_num(chosen.get("avg_price"), None)
    min_price = _v95_num(chosen.get("min_price"), None)
    good_entry = avg_price * 0.92 if avg_price else None
    strong_entry = avg_price * 0.88 if avg_price else None

    html = (
        "🏆 <b>Лучший объект</b>\n\n"
        f"📊 <b>Сделка:</b> {deal_type}\n"
        f"🏠 <b>Формат:</b> {obj_format}\n"
        f"💰 <b>Бюджет:</b> {budget}\n"
        f"🛏 <b>Комнаты:</b> {rooms}\n"
        f"🎯 <b>Цель:</b> {goal}\n\n"
    )
    if notes:
        html += "📌 <b>Адаптивная логика</b>\n" + "\n".join([f"• {n}" for n in notes]) + "\n\n"

    if best_area:
        html += f"🥇 <b>Лучший район:</b> {best_area.get('name')}\n"
    if best_building:
        html += f"🥇 <b>Лучший объект / здание:</b> {best_building.get('name')}"
        if best_building.get('area_name_en'):
            html += f" — {best_building.get('area_name_en')}"
        html += "\n"
    html += (
        f"💰 <b>Средний ориентир:</b> {format_money(avg_price)}\n"
        f"✅ <b>Комфортный вход:</b> {format_money(good_entry)} или ниже\n"
        f"🔥 <b>Сильная точка входа:</b> {format_money(strong_entry)} или ближе к нижним DLD-сделкам {format_money(min_price)}\n\n"
    )

    html += "🏙 <b>Топ-3 района под цель</b>\n\n"
    for i, r in enumerate(areas[:3], 1):
        html += _v95_line(i, r, "area") + "\n"

    html += "🏢 <b>Топ-3 объекта / здания</b>\n\n"
    for i, r in enumerate(buildings[:3], 1):
        html += _v95_line(i, r, "building") + "\n"

    deal_word = "аренды" if _v95_is_rent(deal_type) else "покупки"
    html += (
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"По выбранной цели лучший маршрут — начинать с топ-района и затем проверять конкретный объект из топа. "
        f"Для {deal_word} важны не только средняя цена, но и количество DLD-сделок: чем выше ликвидность, тем легче выйти из объекта, сдать его или защитить цену при переговорах.\n\n"
        f"Если цель — <b>{goal}</b>, приоритет такой: 1) ликвидность района, 2) цена входа ниже среднего DLD, 3) понятная комнатность/формат, 4) наличие похожих сделок, 5) юридическая чистота и реальные условия объекта.\n\n"
        "📌 <b>Практическая стратегия:</b> сначала берём варианты из топ-3 районов, затем внутри них проверяем топ-3 здания/проекта, после этого сравниваем конкретный юнит с последними сделками по этажу, виду, площади, состоянию и сервисным платежам."
    )
    return html

print("Loaded v95 best object menu addon only")


async def main():
    print("Dubai DLD Analytics Bot started")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Telegram webhook cleared before polling")
    except Exception as e:
        print("WEBHOOK_CLEAR_ERROR", repr(e))
    await dp.start_polling(bot)


# =========================
# ECONOMY SUMMARY ONLY PATCH v78
# Scope: do not touch menus, handlers, search, ratings, PDF, DB connections.
# This block only overrides economic conclusion text generation.
# =========================

def _econ_float_v78(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _econ_int_v78(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _econ_money_v78(value):
    try:
        return format_money(value)
    except Exception:
        try:
            return f"{float(value):,.0f} AED".replace(",", " ")
        except Exception:
            return "нет данных"


def _econ_pct_v78(value):
    try:
        return f"{float(value):.1f}%"
    except Exception:
        return "нет данных"


def _econ_scope_label_v78(scope, name):
    try:
        clean_name = _display_scope_name_v71(name) if '_display_scope_name_v71' in globals() else name
    except Exception:
        clean_name = name
    if scope == "building":
        return f"зданию {clean_name}" if clean_name else "зданию"
    if scope == "area":
        return f"району {clean_name}" if clean_name else "району"
    return "рынку Дубая"


def _econ_exit_horizon_v78(deals, growth_12=None, growth_36=None):
    g12 = _econ_float_v78(growth_12)
    g36 = _econ_float_v78(growth_36)
    if deals >= 500 and ((g12 is not None and g12 >= 5) or (g36 is not None and g36 >= 15)):
        return "12–24 месяца", "рынок ликвидный, при покупке ниже средней цены можно рассматривать быстрый выход"
    if deals >= 100:
        return "24–36 месяцев", "лучше дать рынку время на подтверждение роста и выйти после накопления новой статистики сделок"
    return "36–60 месяцев", "выборка узкая, поэтому безопаснее закладывать более длинный горизонт и проверять объект вручную"


def _econ_rent_models_v78(avg_price, avg_meter=None, deals=0, scope=None):
    price = _econ_float_v78(avg_price)
    if not price or price <= 0:
        return None

    # Conservative Dubai investment modelling based on DLD sales price only.
    # This is not an external Airbnb feed; it is an internal indicative model.
    if price <= 900_000:
        gross_yield = 0.075
        str_multiplier = 1.30
        occ_fee_factor = 0.72
    elif price <= 1_800_000:
        gross_yield = 0.068
        str_multiplier = 1.25
        occ_fee_factor = 0.70
    elif price <= 3_500_000:
        gross_yield = 0.058
        str_multiplier = 1.18
        occ_fee_factor = 0.68
    else:
        gross_yield = 0.045
        str_multiplier = 1.12
        occ_fee_factor = 0.66

    # If liquidity is weak, reduce model confidence slightly.
    if deals and deals < 30:
        gross_yield *= 0.92
        occ_fee_factor *= 0.95

    long_rent = price * gross_yield
    str_gross = long_rent * str_multiplier
    str_net = str_gross * occ_fee_factor
    daily_gross = str_gross / 365
    daily_net = str_net / 365
    return {
        "long_rent": long_rent,
        "gross_yield": gross_yield * 100,
        "str_gross": str_gross,
        "str_net": str_net,
        "daily_gross": daily_gross,
        "daily_net": daily_net,
        "str_premium": ((str_net - long_rent) / long_rent * 100) if long_rent else None,
    }


def _econ_resale_projection_v78(avg_price, deals=0, growth_12=None, growth_36=None):
    price = _econ_float_v78(avg_price)
    if not price or price <= 0:
        return None

    g12 = _econ_float_v78(growth_12)
    g36 = _econ_float_v78(growth_36)

    # Use available growth where present; otherwise conservative assumptions by liquidity.
    if g12 is not None:
        annual_growth = max(min(g12 / 100.0, 0.18), -0.08)
    elif g36 is not None:
        annual_growth = max(min((g36 / 100.0) / 3, 0.14), -0.05)
    elif deals >= 500:
        annual_growth = 0.06
    elif deals >= 100:
        annual_growth = 0.045
    else:
        annual_growth = 0.03

    entry_target = price * (0.94 if deals >= 100 else 0.90)
    projections = []
    for y in [1, 3, 5]:
        resale = price * ((1 + annual_growth) ** y)
        profit_from_target = resale - entry_target
        projections.append((y, resale, profit_from_target))
    return {
        "annual_growth": annual_growth * 100,
        "entry_target": entry_target,
        "projections": projections,
    }


def _build_360_conclusion(row, scope=None, name=None, report_kind=None):
    """Premium expanded economy conclusion. Overrides old short version only."""
    row = row or {}
    deals = _econ_int_v78(row.get("deals"), 0)
    avg_price = _econ_float_v78(row.get("avg_price"))
    avg_meter = _econ_float_v78(row.get("avg_meter"))
    min_price = _econ_float_v78(row.get("min_price"))
    max_price = _econ_float_v78(row.get("max_price"))
    growth_12 = _econ_float_v78(row.get("growth_12") or row.get("price_change_12m") or row.get("avg_price_change_12m"))
    growth_36 = _econ_float_v78(row.get("growth_36") or row.get("price_change_36m") or row.get("avg_price_change_36m"))

    if deals >= 500:
        liquidity = "очень высокая"
        liquidity_note = "глубокая выборка DLD, рынок легче читать, объект проще сравнивать и потенциально быстрее перепродавать"
        risk = "низкий риск ликвидности"
    elif deals >= 100:
        liquidity = "хорошая"
        liquidity_note = "сделок достаточно для рыночного ориентира, но конкретный объект всё равно нужно сверять по этажу, виду и состоянию"
        risk = "умеренный риск ликвидности"
    elif deals > 0:
        liquidity = "ограниченная"
        liquidity_note = "данные есть, но выборка не идеальна; вывод нужно подтверждать последними сделками и реальными предложениями"
        risk = "повышенный риск ошибки оценки"
    else:
        liquidity = "недостаточная"
        liquidity_note = "по выбранному фильтру нет стабильной выборки; нужно расширить период, убрать комнатность или анализировать более крупный рынок"
        risk = "недостаточно данных для уверенного решения"

    rent = _econ_rent_models_v78(avg_price, avg_meter, deals, scope)
    resale = _econ_resale_projection_v78(avg_price, deals, growth_12, growth_36)
    exit_horizon, exit_reason = _econ_exit_horizon_v78(deals, growth_12, growth_36)
    scope_label = _econ_scope_label_v78(scope, name)

    lines = []
    lines.append("\n\n🧠 <b>Экономическое заключение 360°</b>\n")
    lines.append(f"<b>1) Рыночная база по {scope_label}</b>")
    lines.append(f"• Количество DLD-сделок в выборке: <b>{format_int(deals) if 'format_int' in globals() else deals}</b>.")
    lines.append(f"• Ликвидность: <b>{liquidity}</b> — {liquidity_note}.")
    lines.append(f"• Средняя цена покупки: <b>{_econ_money_v78(avg_price)}</b>.")
    lines.append(f"• Средняя цена за метр: <b>{_econ_money_v78(avg_meter)}</b>.")
    if min_price or max_price:
        lines.append(f"• Диапазон сделок: <b>{_econ_money_v78(min_price)}</b> — <b>{_econ_money_v78(max_price)}</b>.")

    if rent:
        lines.append("\n<b>2) Арендная модель</b>")
        lines.append(f"• Ориентир среднегодовой долгосрочной аренды: <b>{_econ_money_v78(rent['long_rent'])}</b> в год.")
        lines.append(f"• Валовая долгосрочная доходность: <b>{_econ_pct_v78(rent['gross_yield'])}</b> годовых.")
        lines.append(f"• Ориентир посуточной аренды: <b>{_econ_money_v78(rent['daily_gross'])}</b> в сутки до расходов.")
        lines.append(f"• Модель краткосрочной аренды после загрузки, комиссий и управления: <b>{_econ_money_v78(rent['str_net'])}</b> в год.")
        if rent.get('str_premium') is not None:
            lines.append(f"• Премия/дисконт краткосрочной аренды к долгосрочной модели: <b>{_econ_pct_v78(rent['str_premium'])}</b>.")
        lines.append("• Важно: посуточная модель здесь является ориентиром на базе цены и типовой доходности; перед покупкой её нужно подтверждать реальными ставками аренды, загрузкой, правилами здания и комиссией управляющей компании.")
    else:
        lines.append("\n<b>2) Арендная модель</b>")
        lines.append("• Для арендной модели недостаточно цены покупки. Рекомендуется расширить фильтр или смотреть аналитику по району без комнатности.")

    if resale:
        lines.append("\n<b>3) Перепродажа и горизонт выхода</b>")
        lines.append(f"• Рекомендуемая цена входа: <b>до {_econ_money_v78(resale['entry_target'])}</b>.")
        lines.append(f"• Рабочая модель роста цены: <b>{_econ_pct_v78(resale['annual_growth'])}</b> в год.")
        for y, resale_price, profit in resale["projections"]:
            lines.append(f"• Через {y} г.: ориентир перепродажи <b>{_econ_money_v78(resale_price)}</b>, потенциальная разница к цене входа <b>{_econ_money_v78(profit)}</b>.")
        lines.append(f"• Лучший горизонт выхода: <b>{exit_horizon}</b> — {exit_reason}.")
    else:
        lines.append("\n<b>3) Перепродажа и горизонт выхода</b>")
        lines.append("• Для прогноза перепродажи недостаточно стабильной цены покупки. Сначала нужно получить надёжную DLD-выборку.")

    lines.append("\n<b>4) Инвестиционный вывод</b>")
    if avg_price and deals >= 100:
        lines.append(f"• Сценарий выглядит пригодным для анализа: <b>{risk}</b>.")
        lines.append("• Покупать имеет смысл только ниже или около рыночной средней цены DLD, особенно если объект уступает по этажу, виду, ремонту или срокам передачи.")
        lines.append("• Для сильной сделки нужно сравнить конкретный юнит с последними DLD-сделками, текущими конкурентами на рынке, сервисными сборами, арендным спросом и срочностью продавца.")
    elif avg_price and deals > 0:
        lines.append(f"• Сценарий требует осторожности: <b>{risk}</b>.")
        lines.append("• Лучше расширить период, убрать комнатность или проверить соседние здания/районы, чтобы не принимать решение по слишком узкой статистике.")
    else:
        lines.append("• По выбранному фильтру профессиональный вывод делать рано: данных недостаточно.")
        lines.append("• Практичный следующий шаг — выбрать «всё время», снять комнатность или перейти на аналитику района/Дубая.")

    return "\n".join(lines)


def show_smart_recommendation(goal, budget, timing, risk, rows):
    """Expanded smart-investment conclusion only. Menus and flow are unchanged."""
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."

    best = rows[0]
    area = best.get("area") or "—"
    prop = best.get("property") or "—"
    avg_price = _econ_float_v78(best.get("avg_price"))
    avg_meter = _econ_float_v78(best.get("avg_meter"))
    deals = _econ_int_v78(best.get("deals"), 0)
    min_price = _econ_float_v78(best.get("min_price")) or (avg_price * 0.90 if avg_price else None)
    max_price = _econ_float_v78(best.get("max_price")) or (avg_price * 1.10 if avg_price else None)

    rent = _econ_rent_models_v78(avg_price, avg_meter, deals, "area")
    resale = _econ_resale_projection_v78(avg_price, deals)
    exit_horizon, exit_reason = _econ_exit_horizon_v78(deals)

    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        f"🎯 <b>Цель:</b> {goal}\n"
        f"💰 <b>Бюджет:</b> {budget}\n"
        f"⏱ <b>Горизонт:</b> {timing}\n"
        f"⚖️ <b>Риск-профиль:</b> {risk}\n\n"
        "🏆 <b>Лучший сценарий</b>\n\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"📊 <b>DLD-сделок в выборке:</b> {format_int(deals) if 'format_int' in globals() else deals}\n"
        f"💰 <b>Средняя цена покупки:</b> {_econ_money_v78(avg_price)}\n"
        f"✅ <b>Целевая цена входа:</b> {_econ_money_v78(min_price)} — {_econ_money_v78(avg_price * 0.95 if avg_price else None)}\n"
        f"📐 <b>Средняя цена за метр:</b> {_econ_money_v78(avg_meter)}\n\n"
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
    )

    if rent:
        text += (
            "<b>Арендная экономика</b>\n"
            f"• Среднегодовая долгосрочная аренда: <b>{_econ_money_v78(rent['long_rent'])}</b>.\n"
            f"• Валовая доходность: <b>{_econ_pct_v78(rent['gross_yield'])}</b> годовых.\n"
            f"• Ориентир посуточной аренды: <b>{_econ_money_v78(rent['daily_gross'])}</b> в сутки до расходов.\n"
            f"• Краткосрочная аренда после загрузки, комиссий и управления: <b>{_econ_money_v78(rent['str_net'])}</b> в год.\n\n"
        )
    else:
        text += "<b>Арендная экономика</b>\n• Недостаточно данных для надёжной арендной модели.\n\n"

    if resale:
        text += "<b>Перепродажа</b>\n"
        text += f"• Рекомендуемая цена входа: <b>до {_econ_money_v78(resale['entry_target'])}</b>.\n"
        for y, resale_price, profit in resale["projections"]:
            text += f"• Через {y} г.: ориентир выхода <b>{_econ_money_v78(resale_price)}</b>, потенциальная разница <b>{_econ_money_v78(profit)}</b>.\n"
        text += f"• Лучший горизонт выхода: <b>{exit_horizon}</b> — {exit_reason}.\n\n"

    if deals >= 300:
        verdict = "сильный инвестиционный профиль: высокая ликвидность и понятная рыночная база"
    elif deals >= 80:
        verdict = "рабочий инвестиционный профиль: данные позволяют оценивать рынок, но конкретный юнит нужно проверять отдельно"
    elif deals > 0:
        verdict = "осторожный сценарий: выборка есть, но её лучше усилить более широким периодом или соседними объектами"
    else:
        verdict = "предварительный сценарий: по выбранному фильтру мало DLD-данных, решение принимать рано"

    text += (
        "<b>Стратегия</b>\n"
        f"• Вывод: <b>{verdict}</b>.\n"
        "• Покупать лучше только при дисконте к DLD-средней, понятной арендной ставке и ликвидном здании.\n"
        "• Перед внесением депозита нужно проверить: последние сделки по конкретному зданию, этаж, вид, площадь, сервисные сборы, дату передачи, состояние объекта и юридическую чистоту.\n"
    )

    if len(rows) > 1:
        text += "\n📋 <b>Альтернативы для сравнения</b>\n\n"
        for i, r in enumerate(rows[1:], 2):
            text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {_econ_money_v78(r.get('avg_price'))} · 📊 {format_int(r.get('deals')) if 'format_int' in globals() else r.get('deals')} сделок\n\n"

    return text


# =========================
# v83 REPORT SCENARIO SAFE FIX ONLY
# Fixes technical-error crash in analytics scenarios for Villa/Townhouse/Land/Commercial.
# Menus, buttons and all other flows are unchanged.
# =========================

_V83_RISKY_PROP_MARKERS = ("villa", "вилл", "town", "таун", "land", "зем", "commercial", "коммер", "office", "shop")


def _v83_is_risky_prop(prop):
    p = str(prop or "").lower()
    return any(x in p for x in _V83_RISKY_PROP_MARKERS)


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    """Safe smart stats fallback. Never lets a narrow property filter crash the scenario."""
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    # For villa/townhouse/land/commercial DLD schemas are often inconsistent.
    # Keep the exact first attempt, but then safely broaden faster.
    if _v83_is_risky_prop(prop):
        attempts = [
            (prop, period, deal_type),
            (None, period, deal_type),
            (prop, None, deal_type),
            (None, None, deal_type),
        ]
    for p, per, dt in attempts:
        try:
            row = get_stats(scope, name, p, per, dt)
            if row and _int(row.get("deals")) > 0:
                return row, p, per, dt
        except Exception as e:
            print("GET_STATS_SMART_V83_ERROR:", repr(e), "scope=", scope, "name=", name, "prop=", p, "period=", per, "deal_type=", dt)
            continue
    return None, prop, period, deal_type


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=7, unit_query=None):
    """Safe latest-deals fallback for non-apartment formats."""
    attempts = [
        (prop, period, deal_type),
        (prop, None, deal_type),
        (None, period, deal_type),
        (None, None, deal_type),
    ]
    if _v83_is_risky_prop(prop):
        attempts = [
            (prop, period, deal_type),
            (None, period, deal_type),
            (prop, None, deal_type),
            (None, None, deal_type),
        ]
    for p, per, dt in attempts:
        try:
            rows = get_latest_deals(scope, name, p, per, dt, limit=limit, unit_query=unit_query)
            if rows:
                return rows, p, per, dt
        except Exception as e:
            print("GET_LATEST_DEALS_SMART_V83_ERROR:", repr(e), "scope=", scope, "name=", name, "prop=", p, "period=", per, "deal_type=", dt)
            continue
    return [], prop, period, deal_type


async def send_full_report(message, scope, name=None, prop=None, period=None, deal_type=None, title_prefix="Полная аналитика"):
    """Same report behaviour, but no crash if economics/statistics fail on Villa/Townhouse/etc."""
    user_id = message.from_user.id
    await send_processing(message)
    try:
        row, used_prop, used_period, used_deal_type = get_stats_smart(scope, name, prop, period, deal_type)
        if not row or not _int(row.get("deals")):
            await message.answer(no_data_message(title_prefix), reply_markup=report_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
            return

        title = _human_report_title(scope, name, title_prefix)
        html = show_stats(f"<b>{title}</b>", row, used_prop, used_period, used_deal_type)
        try:
            html += _build_360_conclusion(row, scope, name, title_prefix)
        except Exception as e:
            print("BUILD_360_V83_ERROR:", repr(e))
            html += (
                "\n\n🧠 <b>Экономическое заключение 360°</b>\n\n"
                "По выбранному фильтру удалось получить базовую DLD-статистику, "
                "но для полной финансовой модели недостаточно стабильных параметров. "
                "Рекомендуется расширить период, снять комнатность/тип объекта или проверить аналитику по району."
            )
        if (used_prop, used_period, used_deal_type) != (prop, period, deal_type):
            html += "\n\nℹ️ По точному фильтру выборка была узкой, поэтому показана ближайшая стабильная DLD-выборка."
        set_last_report(user_id, title, html, scope)
        await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))
    except Exception as e:
        print("SEND_FULL_REPORT_V83_ERROR:", repr(e), "scope=", scope, "name=", name, "prop=", prop, "period=", period, "deal_type=", deal_type)
        await message.answer(no_data_message(title_prefix), reply_markup=report_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))

print("Loaded v83 report scenario safe fix only")


# =========================
# v84 PROPERTY TYPE FILTER FIX ONLY
# Purpose: strengthen Villa / Townhouse / Land / Commercial mapping in analytics filters.
# Menus, handlers, keyboards, texts and report logic are not changed.
# =========================

_V84_PROPERTY_ALIASES = {
    "studio": ["studio"],
    "1 br": ["1 br", "1 b/r", "1 bedroom", "one bedroom"],
    "2 br": ["2 br", "2 b/r", "2 bedroom", "two bedroom"],
    "3 br": ["3 br", "3 b/r", "3 bedroom", "three bedroom"],
    "4 br": ["4 br", "4 b/r", "4 bedroom", "four bedroom"],
    "5 br+": ["5 br", "5 b/r", "5 bedroom", "6 br", "6 b/r", "6 bedroom", "7 br", "8 br", "9 br"],
    "apartment": ["apartment", "flat", "unit"],
    "villa": ["villa"],
    "townhouse": ["townhouse", "town house", "town-home", "town home", "th"],
    "land": ["land", "plot", "parcel"],
    "plot": ["land", "plot", "parcel"],
    "commercial": ["commercial", "office", "shop", "retail", "warehouse", "building"],
    "office": ["office", "commercial"],
    "shop": ["shop", "retail", "commercial"],
    "penthouse": ["penthouse"],
}


def _v84_prop_key(prop):
    p = str(prop or "").strip().lower()
    p = p.replace("/", " ").replace("-", " ")
    p = re.sub(r"\s+", " ", p)
    if p in ["villa", "вилла", "виллы"]:
        return "villa"
    if p in ["townhouse", "town house", "таунхаус", "таунхаусы"]:
        return "townhouse"
    if p in ["land", "plot", "земля", "участок", "плот"]:
        return "land"
    if p in ["commercial", "office", "shop", "retail", "коммерческая", "офис", "магазин"]:
        return "commercial" if p in ["commercial", "коммерческая"] else p
    if p in ["apartment", "flat", "unit", "апартамент", "квартира"]:
        return "apartment"
    if p in ["studio", "студия"]:
        return "studio"
    return p


def _v84_property_text_expr():
    return "LOWER(" \
        "COALESCE(rooms_en::text, '') || ' ' || " \
        "COALESCE(property_type_en::text, '') || ' ' || " \
        "COALESCE(property_sub_type_en::text, '') || ' ' || " \
        "COALESCE(property_usage_en::text, '')" \
        ")"


def property_condition(prop):
    """v84 override: robust sale/unified property filter for apartments, villas, townhouses, land and commercial."""
    if not prop:
        return "", []

    key = _v84_prop_key(prop)
    aliases = _V84_PROPERTY_ALIASES.get(key)
    txt_expr = _v84_property_text_expr()

    if key in ["1 br", "2 br", "3 br", "4 br"]:
        n = key.split()[0]
        aliases = _V84_PROPERTY_ALIASES[key]
        parts = [f"{txt_expr} LIKE %s" for _ in aliases]
        parts.append("COALESCE(rooms_en::text, '') = %s")
        return "AND (" + " OR ".join(parts) + ")", [f"%{a}%" for a in aliases] + [n]

    if key == "5 br+":
        aliases = _V84_PROPERTY_ALIASES[key]
        return "AND (" + " OR ".join([f"{txt_expr} LIKE %s" for _ in aliases]) + ")", [f"%{a}%" for a in aliases]

    if aliases:
        return "AND (" + " OR ".join([f"{txt_expr} LIKE %s" for _ in aliases]) + ")", [f"%{a}%" for a in aliases]

    safe = str(prop or "").strip().lower()
    if not safe:
        return "", []
    return f"AND ({txt_expr} LIKE %s)", [f"%{safe}%"]


def rent_property_condition(prop):
    """v84 override: same robust filter for rent/unified rent layer."""
    return property_condition(prop)


# =========================
# FORMAT COMPARISON + SMART ECONOMICS DEEPENING v90
# =========================
# Scope: only the new format-comparison logic and the final economic conclusion text.
# Existing menus, search, database architecture, PDF, ratings and deal flows are untouched.

_V90_FORMATS = ["Apartment", "Villa", "Townhouse"]


def _v90_num(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _v90_int(x, default=0):
    try:
        if x is None:
            return default
        return int(float(x))
    except Exception:
        return default


def _v90_pct_diff(a, b):
    a = _v90_num(a)
    b = _v90_num(b)
    if not a or not b:
        return None
    try:
        return (a - b) / b * 100
    except Exception:
        return None


def _v90_signed_pct(x):
    if x is None:
        return "нет данных"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.1f}%"


def _v90_format_ru(fmt):
    f = str(fmt or '').lower()
    if 'town' in f:
        return 'таунхаусы'
    if 'villa' in f:
        return 'виллы'
    return 'апартаменты'


def _v90_one_format_stats(scope, area, fmt, period, budget=None):
    """Collect sale + rent context for one format, safely and adaptively."""
    sale_row = None
    rent_row = None
    used_scope = scope or 'dubai'
    used_area = area
    used_period = period

    # 1) requested scope/period
    try:
        sale_row, used_prop, used_p, _ = get_stats_smart(used_scope, used_area, fmt, used_period, 'sale')
    except Exception as e:
        print('V90_FORMAT_SALE_ERROR:', fmt, repr(e))
        sale_row, used_prop, used_p = None, fmt, used_period

    # 2) expand period if empty
    if (not sale_row or _v90_int(sale_row.get('deals')) <= 0) and used_period:
        try:
            sale_row, used_prop, used_p, _ = get_stats_smart(used_scope, used_area, fmt, None, 'sale')
            used_period = None
        except Exception as e:
            print('V90_FORMAT_SALE_ALLTIME_ERROR:', fmt, repr(e))
            sale_row = None

    if not sale_row or _v90_int(sale_row.get('deals')) <= 0:
        return None

    # Budget is adaptive: do not drop the row, only mark it as outside budget.
    in_budget = _row_matches_budget(sale_row, budget) if budget else True

    try:
        rent_row, _, _, _ = get_stats_smart(used_scope, used_area, fmt, used_period, 'rent')
    except Exception as e:
        print('V90_FORMAT_RENT_ERROR:', fmt, repr(e))
        rent_row = None

    avg_price = _v90_num(sale_row.get('avg_price'))
    avg_meter = _v90_num(sale_row.get('avg_meter'))
    deals = _v90_int(sale_row.get('deals'))
    avg_rent = _v90_num((rent_row or {}).get('avg_price'))
    rent_deals = _v90_int((rent_row or {}).get('deals'))
    yield_pct = (avg_rent / avg_price * 100) if avg_price and avg_rent else None

    # A conservative DLD-only model: liquidity affects resale confidence.
    liquidity_index = min(100, round(deals / 250 * 100, 1))
    if deals >= 1000:
        liquidity_text = 'очень высокая'
        growth_base = 0.075
    elif deals >= 300:
        liquidity_text = 'высокая'
        growth_base = 0.06
    elif deals >= 100:
        liquidity_text = 'средняя'
        growth_base = 0.045
    else:
        liquidity_text = 'ограниченная'
        growth_base = 0.03

    # Risk adjustment by format. This is not an external promise; it is a scenario model.
    f = str(fmt).lower()
    if 'apartment' in f:
        exit_risk = 'ниже из-за широкой базы покупателей и арендаторов'
        growth_adj = 0.00
    elif 'villa' in f:
        exit_risk = 'выше по чеку, но сильнее при дефиците качественных семейных объектов'
        growth_adj = 0.012
    else:
        exit_risk = 'средний: чек ниже виллы, но семейный спрос сильнее, чем у части апартаментов'
        growth_adj = 0.008

    annual_growth = growth_base + growth_adj
    resale_1y = avg_price * (1 + annual_growth) if avg_price else None
    resale_3y = avg_price * ((1 + annual_growth) ** 3) if avg_price else None
    resale_5y = avg_price * ((1 + annual_growth) ** 5) if avg_price else None

    score = _score_format_row({**sale_row, 'format': fmt}, 'сбалансировано')
    # Encourage actual budget fit without killing alternatives.
    if in_budget:
        score += 8
    else:
        score -= 8
    if yield_pct:
        score += min(yield_pct, 12) * 1.2

    return {
        'format': fmt,
        'sale': sale_row,
        'rent': rent_row,
        'deals': deals,
        'rent_deals': rent_deals,
        'avg_price': avg_price,
        'min_price': _v90_num(sale_row.get('min_price')),
        'max_price': _v90_num(sale_row.get('max_price')),
        'avg_meter': avg_meter,
        'avg_rent': avg_rent,
        'yield_pct': yield_pct,
        'liquidity_index': liquidity_index,
        'liquidity_text': liquidity_text,
        'annual_growth': annual_growth * 100,
        'resale_1y': resale_1y,
        'resale_3y': resale_3y,
        'resale_5y': resale_5y,
        'score': round(score, 1),
        'in_budget': in_budget,
        'used_period': used_period,
        'exit_risk': exit_risk,
        'budget_segment': _budget_label_from_row(sale_row),
    }


def _v90_collect_format_comparison(scope='dubai', area=None, budget=None, goal=None, period=None):
    rows = []
    notes = []
    for fmt in _V90_FORMATS:
        r = _v90_one_format_stats(scope, area, fmt, period, budget)
        if r:
            rows.append(r)
        else:
            notes.append(f'по формату {_v90_format_ru(fmt)} недостаточно стабильных DLD-данных')

    # If an area is too narrow, compare Dubai-wide instead of stopping.
    if not rows and scope == 'area':
        notes.append('по выбранному району данных мало — для ориентира сравнение расширено до рынка Дубая')
        for fmt in _V90_FORMATS:
            r = _v90_one_format_stats('dubai', None, fmt, period, budget)
            if r:
                rows.append(r)

    if not rows:
        return [], notes

    rows.sort(key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True)

    if budget and not any(r.get('in_budget') for r in rows):
        alt = '; '.join([f"{_v90_format_ru(r['format'])} — {format_money(r['avg_price'])} ({r['budget_segment']})" for r in rows[:3]])
        notes.append('в выбранном бюджете стабильной DLD-выборки нет; можно рассмотреть ближайшие рабочие бюджеты: ' + alt)
    elif budget:
        out = [r for r in rows if not r.get('in_budget')]
        if out:
            notes.append('часть форматов находится вне выбранного бюджета и оставлена как ориентир для сравнения: ' + ', '.join(_v90_format_ru(r['format']) for r in out))

    return rows, notes


def _v90_compare_sentence(best, other):
    fmt_best = _v90_format_ru(best.get('format'))
    fmt_other = _v90_format_ru(other.get('format'))
    price_diff = _v90_pct_diff(best.get('avg_price'), other.get('avg_price'))
    meter_diff = _v90_pct_diff(best.get('avg_meter'), other.get('avg_meter'))
    deals_diff = _v90_pct_diff(best.get('deals'), other.get('deals'))
    yield_diff = None
    if best.get('yield_pct') is not None and other.get('yield_pct') is not None:
        yield_diff = best.get('yield_pct') - other.get('yield_pct')

    text = f"• По сравнению с форматом <b>{fmt_other}</b>: "
    details = []
    if price_diff is not None:
        details.append(f"средний чек {_v90_signed_pct(price_diff)}")
    if meter_diff is not None:
        details.append(f"цена за м² {_v90_signed_pct(meter_diff)}")
    if deals_diff is not None:
        details.append(f"ликвидность по сделкам {_v90_signed_pct(deals_diff)}")
    if yield_diff is not None:
        sign = '+' if yield_diff >= 0 else ''
        details.append(f"доходность {sign}{yield_diff:.1f} п.п.")
    if not details:
        details.append('данных для точного процентного сравнения недостаточно')
    return text + '; '.join(details) + f". Поэтому <b>{fmt_best}</b> выглядит сильнее именно по выбранному профилю, если входить не выше рыночного ориентира."


def _v90_deep_economic_article(best, rows, goal=None, budget=None, area=None):
    fmt = best.get('format')
    fmt_ru = _v90_format_ru(fmt)
    avg_price = best.get('avg_price')
    avg_meter = best.get('avg_meter')
    avg_rent = best.get('avg_rent')
    yield_pct = best.get('yield_pct')
    good_low = best.get('min_price') or (avg_price * 0.90 if avg_price else None)
    good_high = avg_price * 0.95 if avg_price else None

    article = (
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"Победитель сравнения — <b>{fmt_ru}</b>. Это не просто выбор по названию формата, а результат сопоставления трёх ключевых моделей: "
        "апартаменты как самый ликвидный массовый актив, виллы как дефицитный семейный актив с крупным чеком, "
        "и таунхаусы как промежуточный формат между ценой входа, семейным спросом и потенциалом перепродажи.\n\n"
        "<b>1) Почему выбран именно этот формат</b>\n"
        f"У формата <b>{fmt_ru}</b> сейчас лучшая комбинация по выбранному профилю: "
        f"сделок в выборке — <b>{format_int(best.get('deals'))}</b>, "
        f"средняя цена — <b>{format_money(avg_price)}</b>, "
        f"средняя цена за м² — <b>{format_money(avg_meter)}</b>. "
        f"Ликвидность по DLD-сделкам: <b>{best.get('liquidity_text')}</b>. "
        f"Риск выхода: {best.get('exit_risk')}.\n\n"
        "<b>2) Сравнение с альтернативами</b>\n"
    )

    alternatives = [r for r in rows if r.get('format') != fmt]
    if alternatives:
        for other in alternatives:
            article += _v90_compare_sentence(best, other) + "\n"
    else:
        article += "• По другим форматам в выбранном фильтре недостаточно стабильных DLD-данных; анализ не останавливается, но вывод строится на доступной выборке.\n"

    article += "\n<b>3) Арендная логика</b>\n"
    if avg_rent:
        article += (
            f"Среднегодовой ориентир аренды по DLD-модели — <b>{format_money(avg_rent)}</b>. "
            f"Ориентировочная валовая доходность — <b>{format_pct(yield_pct)}</b>. "
            "Для точного решения по конкретному объекту нужно отдельно сверить фактическую текущую аренду, вакантность, сервисные платежи и состояние юнита.\n\n"
        )
    else:
        article += (
            "По аренде в выбранной связке данных недостаточно для честной точной доходности. "
            "В таком случае правильная стратегия — использовать DLD-продажи как основу цены входа, а аренду проверять дополнительно по конкретному зданию/community.\n\n"
        )

    article += (
        "<b>4) Перепродажа и горизонт выхода</b>\n"
        f"Ориентир перепродажи по осторожной DLD-модели: через 1 год — <b>{format_money(best.get('resale_1y'))}</b>, "
        f"через 3 года — <b>{format_money(best.get('resale_3y'))}</b>, "
        f"через 5 лет — <b>{format_money(best.get('resale_5y'))}</b>. "
        f"Модельный темп роста: около <b>{best.get('annual_growth'):.1f}% в год</b>; это сценарный ориентир, а не гарантия рынка.\n\n"
        "<b>5) Рекомендуемая цена входа</b>\n"
        f"Для покупки интересная зона входа — <b>{format_money(good_low)}</b> — <b>{format_money(good_high)}</b>. "
        "Если объект выше средней DLD-цены, он должен иметь понятное оправдание: вид, этаж, планировка, состояние, срочность аренды, редкость предложения или сильный дисконт к аналогам.\n\n"
        "<b>6) Итоговая стратегия</b>\n"
        f"По бюджету <b>{budget or 'без жёсткого лимита'}</b> логика такая: сначала выбрать формат <b>{fmt_ru}</b>, затем сузить выбор до районов с максимальной ликвидностью, после этого — до зданий/проектов с частыми DLD-сделками. "
        "Финальное решение нужно принимать не по красивой цене в объявлении, а по сравнению с последними DLD-сделками, реальной арендой и качеством конкретного объекта."
    )
    return article


def build_format_comparison_report(scope='dubai', area=None, budget=None, goal=None, period=None):
    rows, notes = _v90_collect_format_comparison(scope=scope, area=area, budget=budget, goal=goal, period=period)
    if not rows:
        return None, []

    best = rows[0]
    scope_title = 'Dubai' if scope == 'dubai' else (area or 'выбранный район')
    used_period = best.get('used_period', period)

    response = (
        "⚖️ <b>Сравнение форматов</b>\n"
        f"📍 Рынок: <b>{scope_title}</b>\n"
        f"💰 Бюджет: <b>{budget or 'не указан'}</b>\n"
        f"🎯 Цель: <b>{goal or 'сбалансировано'}</b>\n"
        f"📅 Период: <b>{period_label(used_period)}</b>\n\n"
    )

    if notes:
        response += "📌 <b>Адаптивный фильтр</b>\n"
        for n in notes:
            response += f"• {n}\n"
        response += "\n"

    response += "📊 <b>Сводная таблица форматов</b>\n\n"
    for r in rows:
        response += (
            f"🏠 <b>{_v90_format_ru(r.get('format')).capitalize()}</b>\n"
            f"📊 Сделок: <b>{format_int(r.get('deals'))}</b>\n"
            f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n"
            f"📐 Цена за м²: <b>{format_money(r.get('avg_meter'))}</b>\n"
            f"🏦 Средняя годовая аренда: <b>{format_money(r.get('avg_rent'))}</b>\n"
            f"📈 Ориентир доходности: <b>{format_pct(r.get('yield_pct'))}</b>\n"
            f"🎯 Индекс выгоды: <b>{r.get('score')}/100</b>\n\n"
        )

    response += f"🏆 <b>Лучший формат:</b> {_v90_format_ru(best.get('format')).capitalize()}\n\n"
    response += _v90_deep_economic_article(best, rows, goal=goal, budget=budget, area=area)
    response += "\n\n📍 Нажмите <b>Лучшие районы</b>, чтобы я продолжил по ёлочке: формат → район → здание → стратегия входа."
    return response, rows


def show_smart_recommendation(goal, budget, timing, risk, rows):
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."

    best = rows[0]
    area = best.get('area') or '—'
    prop = best.get('property') or '—'
    good_low = best.get('min_price') or ((_v90_num(best.get('avg_price')) or 0) * 0.90 if best.get('avg_price') else None)
    good_high = (_v90_num(best.get('avg_price')) or 0) * 0.95 if best.get('avg_price') else None

    # Add apartment/villa/townhouse comparison to the final smart-pick conclusion.
    compare_rows, compare_notes = _v90_collect_format_comparison(
        scope='area',
        area=area,
        budget=budget,
        goal=goal,
        period='36',
    )
    if not compare_rows:
        compare_rows, compare_notes = _v90_collect_format_comparison(
            scope='dubai',
            area=None,
            budget=budget,
            goal=goal,
            period='36',
        )

    # Prefer the actual selected format if it exists in comparison, otherwise use comparison winner.
    selected_key = _v84_prop_key(prop)
    selected_compare = None
    for r in compare_rows:
        if _v84_prop_key(r.get('format')) == selected_key or selected_key in str(r.get('format')).lower():
            selected_compare = r
            break
    compare_best = selected_compare or (compare_rows[0] if compare_rows else None)

    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        "🏆 <b>Лучший сценарий</b>\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"📊 <b>Сделки:</b> {format_int(best.get('deals'))}\n"
        f"💰 <b>Средняя цена:</b> {format_money(best.get('avg_price'))}\n"
        f"✅ <b>Комфортная цена входа:</b> {format_money(good_low)} — {format_money(good_high)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(best.get('avg_meter'))}\n\n"
    )

    if compare_notes:
        text += "📌 <b>Адаптивный анализ</b>\n"
        for n in compare_notes[:3]:
            text += f"• {n}\n"
        text += "\n"

    if compare_rows:
        text += "⚖️ <b>Сравнение форматов: апартаменты / виллы / таунхаусы</b>\n\n"
        for r in compare_rows:
            marker = '🏆 ' if r is compare_best else '▫️ '
            text += (
                f"{marker}<b>{_v90_format_ru(r.get('format')).capitalize()}</b>: "
                f"{format_money(r.get('avg_price'))}, "
                f"{format_int(r.get('deals'))} сделок, "
                f"доходность {format_pct(r.get('yield_pct'))}, "
                f"индекс {r.get('score')}/100\n"
            )
        text += "\n"
        text += _v90_deep_economic_article(compare_best, compare_rows, goal=goal, budget=budget, area=area)
    else:
        text += (
            "🧠 <b>Экономическое заключение 360°</b>\n\n"
            f"Для инвестиции лучший баланс сейчас показывает <b>{prop}</b> в районе <b>{area}</b>. "
            "Ориентир — покупать ниже средней цены DLD, проверять ликвидность здания и избегать объектов с завышенной ценой входа."
        )

    text += "\n\n📋 <b>Альтернативы</b>\n\n"
    for i, r in enumerate(rows[1:], 2):
        text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {format_money(r.get('avg_price'))} · 📊 {format_int(r.get('deals'))} сделок\n\n"

    text += (
        "⚠️ <b>Важно:</b> это аналитический ориентир по DLD. Перед покупкой нужно отдельно проверить конкретный объект: "
        "этаж, вид, состояние, сервисные платежи, срочность продавца, арендный контракт и юридическую чистоту сделки."
    )
    return text

print('Loaded v90 deep format comparison economics only')


# =========================
# v91 DEEP ECONOMICS + AREA OUTPUT FIX ONLY
# Scope:
# 1) connect the full 360 economic conclusion to final reports;
# 2) restore deal area display using robust area extraction / price ÷ price-per-meter fallback;
# 3) keep all menus, states, buttons and existing flows unchanged.
# =========================

def _v91_available_area_expr():
    """Return robust SQL expression for area in m² across live/archive schema variants."""
    candidates = [
        "actual_area",
        "procedure_area",
        "area_size",
        "property_size",
        "property_area",
        "built_up_area",
        "bua",
        "plot_area",
        "land_area",
        "meter_area",
        "actual_size",
    ]
    try:
        cols = available_columns()
    except Exception:
        cols = set()

    existing = [c for c in candidates if c in cols]
    if not existing:
        # Safe default for the known archive schema used in this project.
        existing = ["actual_area"]

    parts = []
    for c in existing:
        try:
            parts.append(num_sql(c))
        except Exception:
            pass
    if not parts:
        return "NULL::numeric"
    return "COALESCE(" + ", ".join(parts) + ")"


def _v91_safe_number(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _v91_enrich_area_rows(rows):
    """If DLD did not return area, calculate it from price / price-per-m² where possible."""
    if not rows:
        return rows
    for r in rows:
        try:
            area = _v91_safe_number(r.get("area_size"))
            price = _v91_safe_number(r.get("price"))
            meter = _v91_safe_number(r.get("meter_price"))
            if (not area or area <= 0) and price and meter and meter > 0:
                r["area_size"] = price / meter
        except Exception:
            continue
    return rows


def get_latest_deals(scope, name, prop=None, period=None, deal_type=None, limit=5, unit_query=None):
    """v91 override: same logic, but area is extracted robustly and back-calculated if needed."""
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    p_sql = period_condition(period)
    value_expr = deal_value_expr(deal_type)
    unit_sql, unit_args = make_unit_condition(unit_query)
    area_expr = _v91_available_area_expr()

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
                        {area_expr} AS area_size,
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
                return _v91_enrich_area_rows(rows)
    except Exception as e:
        print("GET_LATEST_DEALS_V91_AREA_ERROR:", repr(e))
        return []


def _v91_format_label(prop):
    p = str(prop or "").lower()
    if any(x in p for x in ["villa", "вилл"]):
        return "виллы"
    if any(x in p for x in ["town", "таун"]):
        return "таунхаусы"
    if any(x in p for x in ["land", "plot", "зем"]):
        return "земельные участки"
    if any(x in p for x in ["office", "shop", "commercial", "коммер"]):
        return "коммерческая недвижимость"
    if any(x in p for x in ["1", "2", "3", "4", "5", "studio", "flat", "unit", "apartment", "br", "b/r"]):
        return "апартаменты"
    return "выбранный формат"


def _v91_strategy_by_format(prop):
    p = str(prop or "").lower()
    if "town" in p or "таун" in p:
        return (
            "Таунхаус обычно работает как промежуточный инвестиционный актив: вход ниже полноценной виллы, "
            "семейный спрос выше, чем у части апартаментов, а ликвидность зависит от community и качества проекта. "
            "Главный плюс — баланс между lifestyle-спросом и перепродажей. Главный риск — более узкая аудитория, чем у апартаментов."
        )
    if "villa" in p or "вилл" in p:
        return (
            "Вилла — более капиталоёмкий актив. Она может давать сильный прирост при дефиците семейного продукта, "
            "но требует большего бюджета, более длинного горизонта и аккуратной проверки community, участка, состояния и сервисных расходов. "
            "Это формат не для быстрой спекуляции, а для стратегического входа."
        )
    if "land" in p or "plot" in p or "зем" in p:
        return (
            "Земельный участок — отдельная модель. Здесь доходность зависит не от текущей аренды, а от потенциала застройки, "
            "плотности, назначения, инфраструктуры и будущего спроса. Такой актив требует ручной юридической и градостроительной проверки."
        )
    return (
        "Апартаменты — самый ликвидный и массовый формат. Их проще сравнивать по DLD, проще сдавать, проще перепродавать, "
        "и у них шире база покупателей. Главный риск — высокая конкуренция внутри здания и района, поэтому входить нужно ниже средней цены по последним сделкам."
    )


def _v91_generic_deep_article(row, scope=None, name=None, report_kind=None, prop=None, period=None, deal_type=None):
    deals = _v90_int(row.get("deals") if row else 0)
    avg_price = _v90_num(row.get("avg_price") if row else None)
    min_price = _v90_num(row.get("min_price") if row else None)
    max_price = _v90_num(row.get("max_price") if row else None)
    avg_meter = _v90_num(row.get("avg_meter") if row else None)
    is_rent = is_rent_deal_type(deal_type)
    fmt_label = _v91_format_label(prop or row.get("property_sub_types") or row.get("property_types"))
    location = name or ("Dubai" if scope == "dubai" else "выбранная локация")

    if deals >= 1000:
        liquidity = "очень высокая"
        exit_risk = "низкий"
        growth_model = 0.075
    elif deals >= 300:
        liquidity = "высокая"
        exit_risk = "умеренный"
        growth_model = 0.06
    elif deals >= 100:
        liquidity = "средняя"
        exit_risk = "контролируемый, но требует проверки конкретного объекта"
        growth_model = 0.045
    else:
        liquidity = "ограниченная"
        exit_risk = "повышенный из-за небольшой выборки"
        growth_model = 0.03

    good_low = min_price if min_price and min_price > 0 else (avg_price * 0.90 if avg_price else None)
    good_high = avg_price * 0.95 if avg_price else None
    resale_1 = avg_price * (1 + growth_model) if avg_price and not is_rent else None
    resale_3 = avg_price * ((1 + growth_model) ** 3) if avg_price and not is_rent else None
    resale_5 = avg_price * ((1 + growth_model) ** 5) if avg_price and not is_rent else None

    text = (
        "\n\n🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"<b>Объект анализа:</b> {location}; формат — <b>{fmt_label}</b>; "
        f"выборка DLD — <b>{format_int(deals)}</b> сделок.\n\n"
        "<b>1) Рыночный ориентир</b>\n"
        f"Средний рыночный уровень по выбранному фильтру — <b>{format_money(avg_price)}</b>. "
        f"Средняя цена за м² — <b>{format_money(avg_meter)}</b>. "
        f"Диапазон сделок в DLD: от <b>{format_money(min_price)}</b> до <b>{format_money(max_price)}</b>. "
        "Этот диапазон нужен не для слепого копирования, а для понимания реальной зоны переговоров и отсечения переоценённых вариантов.\n\n"
        "<b>2) Ликвидность и риск выхода</b>\n"
        f"Ликвидность по DLD-сделкам: <b>{liquidity}</b>. "
        f"Риск выхода из позиции: <b>{exit_risk}</b>. "
        "Чем выше количество сделок, тем легче проверить справедливую цену, быстрее найти покупателя/арендатора и ниже риск зависнуть с объектом.\n\n"
        "<b>3) Логика формата</b>\n"
        f"{_v91_strategy_by_format(fmt_label)}\n\n"
    )

    if not is_rent:
        text += (
            "<b>4) Цена входа и перепродажа</b>\n"
            f"Интересная зона входа: <b>{format_money(good_low)}</b> — <b>{format_money(good_high)}</b>. "
            "Покупка выше средней DLD-цены допустима только если у объекта есть объективная премия: вид, этаж, планировка, редкость, состояние, готовая аренда или сильный дефицит аналогов.\n\n"
            f"Сценарный ориентир перепродажи: через 1 год — <b>{format_money(resale_1)}</b>, "
            f"через 3 года — <b>{format_money(resale_3)}</b>, "
            f"через 5 лет — <b>{format_money(resale_5)}</b>. "
            f"Модельный темп роста заложен осторожно: около <b>{growth_model * 100:.1f}% в год</b>; это ориентир по ликвидности, а не гарантия рынка.\n\n"
        )
    else:
        text += (
            "<b>4) Арендная логика</b>\n"
            f"Среднегодовой ориентир аренды — <b>{format_money(avg_price)}</b>. "
            "Сильная арендная сделка — это объект, который находится ниже среднего рынка, но не имеет слабого состояния, плохого вида или юридических ограничений. "
            "Для финального решения нужно проверить фактический контракт, срок окончания аренды, сервисные платежи и реальную вакантность.\n\n"
        )

    # Add comparative format context where possible, but never break the old flow.
    try:
        compare_rows, compare_notes = _v90_collect_format_comparison(
            scope=scope or "dubai",
            area=name if scope == "area" else None,
            budget=None,
            goal="сбалансировано",
            period=period,
        )
        if compare_rows:
            best = compare_rows[0]
            text += "<b>5) Сравнение форматов: апартаменты / виллы / таунхаусы</b>\n"
            for r in compare_rows:
                text += (
                    f"• <b>{_v90_format_ru(r.get('format')).capitalize()}</b>: "
                    f"{format_int(r.get('deals'))} сделок, "
                    f"средняя цена {format_money(r.get('avg_price'))}, "
                    f"цена за м² {format_money(r.get('avg_meter'))}, "
                    f"арендная доходность {format_pct(r.get('yield_pct'))}, "
                    f"индекс {r.get('score')}/100.\n"
                )
            text += (
                f"\nПо сравнительной модели сильнее выглядит <b>{_v90_format_ru(best.get('format'))}</b>, "
                "потому что этот формат даёт лучший баланс цены входа, ликвидности, арендной логики и вероятности выхода.\n\n"
            )
        else:
            text += "<b>5) Сравнение форматов</b>\nПо апартаментам, виллам и таунхаусам в текущем фильтре недостаточно единой выборки; для сравнения лучше расширить период или смотреть весь Dubai.\n\n"
    except Exception as e:
        print("V91_COMPARE_CONTEXT_ERROR:", repr(e))

    text += (
        "<b>6) Практическая рекомендация</b>\n"
        "Не входить только потому, что цена выглядит красивой. Правильная последовательность: "
        "сначала проверить формат и район, затем последние DLD-сделки по зданию/community, затем площадь, этаж, вид, состояние, сервисные сборы, арендный контракт и мотивацию продавца. "
        "Оптимальная покупка — та, где цена ниже или около DLD-средней, но качество объекта не хуже рынка."
    )
    return text


def _build_360_conclusion(row, scope=None, name=None, report_kind=None):
    """v91 override: full economic article instead of short legacy placeholder."""
    try:
        return _v91_generic_deep_article(row, scope=scope, name=name, report_kind=report_kind)
    except Exception as e:
        print("BUILD_360_V91_ERROR:", repr(e))
        deals = _int(row.get("deals") if row else 0) or 0
        avg_price = row.get("avg_price") if row else None
        avg_meter = row.get("avg_meter") if row else None
        return (
            "\n\n🧠 <b>Экономическое заключение 360°</b>\n\n"
            f"Выборка: <b>{format_int(deals)}</b> сделок. "
            f"Средняя цена: <b>{format_money(avg_price)}</b>, средняя цена за метр: <b>{format_money(avg_meter)}</b>. "
            "Для точного решения нужно сравнить конкретный объект с последними DLD-сделками, площадью, этажом, видом, состоянием, сервисными сборами и реальной арендной ставкой."
        )


def show_smart_recommendation(goal, budget, timing, risk, rows):
    """v91 override: investment selection always ends with expanded comparative economics."""
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."

    best = rows[0]
    area = best.get('area') or '—'
    prop = best.get('property') or '—'
    good_low = best.get('min_price') or ((_v90_num(best.get('avg_price')) or 0) * 0.90 if best.get('avg_price') else None)
    good_high = (_v90_num(best.get('avg_price')) or 0) * 0.95 if best.get('avg_price') else None

    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        "🏆 <b>Лучший сценарий</b>\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"📊 <b>Сделки:</b> {format_int(best.get('deals'))}\n"
        f"💰 <b>Средняя цена:</b> {format_money(best.get('avg_price'))}\n"
        f"✅ <b>Комфортная цена входа:</b> {format_money(good_low)} — {format_money(good_high)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(best.get('avg_meter'))}\n\n"
    )

    try:
        compare_rows, compare_notes = _v90_collect_format_comparison(
            scope='area',
            area=area,
            budget=budget,
            goal=goal,
            period='36',
        )
        if not compare_rows:
            compare_rows, compare_notes = _v90_collect_format_comparison(
                scope='dubai',
                area=None,
                budget=budget,
                goal=goal,
                period='36',
            )

        if compare_notes:
            text += "📌 <b>Адаптивный сравнительный фильтр</b>\n"
            for n in compare_notes[:4]:
                text += f"• {n}\n"
            text += "\n"

        if compare_rows:
            selected_key = _v84_prop_key(prop)
            selected_compare = None
            for r in compare_rows:
                if _v84_prop_key(r.get('format')) == selected_key or selected_key in str(r.get('format')).lower():
                    selected_compare = r
                    break
            compare_best = selected_compare or compare_rows[0]

            text += "⚖️ <b>Сравнение форматов</b>\n\n"
            for r in compare_rows:
                mark = "🏆" if r is compare_best else "▫️"
                text += (
                    f"{mark} <b>{_v90_format_ru(r.get('format')).capitalize()}</b>: "
                    f"{format_money(r.get('avg_price'))}, "
                    f"{format_int(r.get('deals'))} сделок, "
                    f"доходность {format_pct(r.get('yield_pct'))}, "
                    f"индекс {r.get('score')}/100\n"
                )
            text += "\n"
            text += _v90_deep_economic_article(compare_best, compare_rows, goal=goal, budget=budget, area=area)
        else:
            text += _v91_generic_deep_article(best, scope="area", name=area, prop=prop, period="36", deal_type="sale")
    except Exception as e:
        print("SMART_RECOMMENDATION_V91_COMPARE_ERROR:", repr(e))
        text += _v91_generic_deep_article(best, scope="area", name=area, prop=prop, period="36", deal_type="sale")

    if len(rows) > 1:
        text += "\n\n📋 <b>Альтернативы</b>\n\n"
        for i, r in enumerate(rows[1:], 2):
            text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {format_money(r.get('avg_price'))} · 📊 {format_int(r.get('deals'))} сделок\n\n"

    text += (
        "\n⚠️ <b>Важно:</b> это аналитический ориентир по DLD. Перед покупкой нужно отдельно проверить конкретный объект: "
        "этаж, вид, состояние, сервисные платежи, срочность продавца, арендный контракт и юридическую чистоту сделки."
    )
    return text

print("Loaded v91 deep economics and area output fix only")


# =========================
# v92 EXTERNAL ECONOMIC ENGINE ONLY
# Scope: connect a separate economic_engine.py calculator/template module.
# Menus, states, buttons, DB logic and existing flows are not changed.
# =========================
try:
    from economic_engine import build_economic_report as _EXT_BUILD_ECONOMIC_REPORT
except Exception as _econ_import_error:
    _EXT_BUILD_ECONOMIC_REPORT = None
    print("ECONOMIC_ENGINE_IMPORT_ERROR:", repr(_econ_import_error))


def _v92_build_comparison_for_context(scope=None, name=None, prop=None, period=None, deal_type=None, budget=None, goal=None):
    try:
        area = name if scope == "area" else None
        rows, notes = _v90_collect_format_comparison(
            scope=scope or "dubai",
            area=area,
            budget=budget,
            goal=goal,
            period=period,
        )
        if not rows and scope != "dubai":
            rows, notes = _v90_collect_format_comparison(
                scope="dubai",
                area=None,
                budget=budget,
                goal=goal,
                period=period,
            )
        return rows or []
    except Exception as e:
        print("V92_CONTEXT_COMPARE_ERROR:", repr(e))
        return []


def _build_360_conclusion(row, scope=None, name=None, report_kind=None):
    """v92 override: full report is generated by external economic_engine.py."""
    try:
        if _EXT_BUILD_ECONOMIC_REPORT:
            comparisons = _v92_build_comparison_for_context(scope=scope, name=name, period=None)
            return _EXT_BUILD_ECONOMIC_REPORT(
                selected=row or {},
                comparisons=comparisons,
                goal=report_kind or "аналитика DLD",
                budget=None,
                period=None,
                area=name or (row or {}).get("area"),
                deal_type=None,
            )
    except Exception as e:
        print("BUILD_360_V92_ENGINE_ERROR:", repr(e))
    try:
        return _v91_generic_deep_article(row, scope=scope, name=name, report_kind=report_kind)
    except Exception as e:
        print("BUILD_360_V92_FALLBACK_ERROR:", repr(e))
        return "\n\n🧠 <b>Экономическое заключение 360°</b>\n\nНедостаточно данных для полного экономического отчёта. Расширьте период или фильтр."


def show_smart_recommendation(goal, budget, timing, risk, rows):
    """v92 override: investment scenario always uses the external economic engine."""
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."

    best = rows[0]
    area = best.get('area') or '—'
    prop = best.get('property') or best.get('format') or '—'
    good_low = best.get('min_price') or ((_v90_num(best.get('avg_price')) or 0) * 0.90 if best.get('avg_price') else None)
    good_high = (_v90_num(best.get('avg_price')) or 0) * 0.95 if best.get('avg_price') else None

    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        "🏆 <b>Лучший сценарий</b>\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"📊 <b>Сделки:</b> {format_int(best.get('deals'))}\n"
        f"💰 <b>Средняя цена:</b> {format_money(best.get('avg_price'))}\n"
        f"✅ <b>Комфортная цена входа:</b> {format_money(good_low)} — {format_money(good_high)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(best.get('avg_meter'))}\n"
    )

    try:
        compare_rows, compare_notes = _v90_collect_format_comparison(
            scope='area',
            area=area,
            budget=budget,
            goal=goal,
            period='36',
        )
        if not compare_rows:
            compare_rows, compare_notes = _v90_collect_format_comparison(
                scope='dubai',
                area=None,
                budget=budget,
                goal=goal,
                period='36',
            )
    except Exception as e:
        print("SMART_RECOMMENDATION_V92_COMPARE_ERROR:", repr(e))
        compare_rows, compare_notes = [], []

    if _EXT_BUILD_ECONOMIC_REPORT:
        text += _EXT_BUILD_ECONOMIC_REPORT(
            selected=best,
            comparisons=compare_rows,
            goal=goal,
            budget=budget,
            period=timing,
            area=area,
            deal_type="sale",
        )
    else:
        text += _v91_generic_deep_article(best, scope="area", name=area, prop=prop, period="36", deal_type="sale")

    if len(rows) > 1:
        text += "\n\n📋 <b>Альтернативы</b>\n\n"
        for i, r in enumerate(rows[1:], 2):
            text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {format_money(r.get('avg_price'))} · 📊 {format_int(r.get('deals'))} сделок\n\n"

    return text

print("Loaded v92 external economic engine integration only")


# =========================
# v96 INTELLIGENCE ROUTER BRIDGE
# =========================
# This layer keeps the existing v95 bot intact, but routes the newest scenarios
# through intelligence_router.py when the file is available in the project root.
# It does not remove old functions; it only normalizes state, filters and output.


def _ir_v96_text(value):
    return str(value or "").strip()


def _ir_v96_state_to_kwargs(state):
    """Convert current main.py state keys to intelligence_router vocabulary."""
    state = state or {}
    return {
        "intent": state.get("intent") or state.get("report_kind") or state.get("step") or "best_object",
        "deal_type": state.get("deal_type"),
        "scope": state.get("scope"),
        "area": state.get("area") or (state.get("name") if state.get("scope") == "area" else None),
        "building": state.get("building") or (state.get("name") if state.get("scope") == "building" else None),
        "property_format": state.get("object_format") or state.get("property_format") or state.get("property"),
        "bedrooms": state.get("rooms") or state.get("bedrooms") or state.get("property"),
        "budget": state.get("budget"),
        "period": state.get("period"),
        "goal": state.get("goal"),
        "risk": state.get("risk"),
        "language": state.get("language", "ru"),
        "previous_context": state,
    }


def _ir_v96_prepare(state, raw_text=""):
    if not INTELLIGENCE_ROUTER_AVAILABLE or not IR_prepare_request:
        return None
    try:
        kwargs = _ir_v96_state_to_kwargs(state)
        # Best object and smart pick must be recognized explicitly.
        if str(state.get("step", "")).startswith("best_object") or state.get("goal"):
            kwargs["intent"] = "best_object" if state.get("object_format") is not None or state.get("rooms") is not None else kwargs.get("intent")
        return IR_prepare_request(raw_text or " ".join(str(v) for v in state.values() if v), **kwargs)
    except Exception as e:
        print("IR_V96_PREPARE_ERROR:", repr(e))
        return None


def _ir_v96_deal_type(payload, fallback=None):
    try:
        dt = payload.request.deal_type if payload else None
        if dt == "rent":
            return "🔑 Аренда"
        if dt == "both":
            return None
        return "🏠 Продажа"
    except Exception:
        return fallback


def _ir_v96_property(payload, state):
    """Prefer canonical format; if apartment + bedrooms selected, use bedrooms for DLD filtering."""
    if payload:
        req = payload.request
        if req.bedrooms:
            return req.bedrooms.title().replace(" Br", " BR")
        fmt = req.property_format
        if fmt == "apartment":
            return "Apartment"
        if fmt == "townhouse":
            return "Townhouse"
        if fmt == "villa":
            return "Villa"
        if fmt in {"land", "plot"}:
            return "Plot"
        if fmt:
            return str(fmt).title()
    return state.get("rooms") or state.get("object_format") or state.get("property")


def _ir_v96_budget_note(payload):
    if not payload:
        return ""
    try:
        bmin = payload.request.budget_min
        bmax = payload.request.budget_max
        if bmin is None and bmax is None:
            return ""
        return f"Бюджет нормализован: {format_money(bmin) if bmin is not None else 'без нижней границы'} — {format_money(bmax) if bmax is not None else 'без верхней границы'}"
    except Exception:
        return ""


def _ir_v96_notes_html(payload):
    if not payload or not getattr(payload, "notes", None):
        return ""
    try:
        lines = []
        used = set()
        for n in payload.notes[:6]:
            src = getattr(n, "source", None)
            tgt = getattr(n, "target", None)
            code = getattr(n, "code", "")
            key = (src, tgt, code)
            if key in used:
                continue
            used.add(key)
            if src and tgt:
                lines.append(f"• {src} → <b>{tgt}</b>")
        bn = _ir_v96_budget_note(payload)
        if bn:
            lines.append(f"• {bn}")
        if not lines:
            return ""
        return "📌 <b>Intelligence Router</b>\n" + "\n".join(lines) + "\n\n"
    except Exception as e:
        print("IR_V96_NOTES_ERROR:", repr(e))
        return ""


def _ir_v96_apply_to_state_for_report(state):
    """Normalize state before the old report functions call SQL."""
    payload = _ir_v96_prepare(state, "report")
    if not payload:
        return state, None
    req = payload.request
    normalized = dict(state)
    normalized["deal_type"] = _ir_v96_deal_type(payload, state.get("deal_type"))
    normalized["property"] = _ir_v96_property(payload, state)
    if req.scope == "area" and req.area:
        normalized["scope"] = "area"
        normalized["name"] = req.area
    elif req.scope == "building" and req.building:
        normalized["scope"] = "building"
        normalized["name"] = req.building
    if req.period_months:
        normalized["period"] = str(req.period_months)
    return normalized, payload


# Override selected report execution: same old UI, but normalized through intelligence_router first.
async def _execute_selected_report_v72(message, state):
    state, payload = _ir_v96_apply_to_state_for_report(state or {})
    kind = state.get("report_kind") or "full"
    scope = state.get("scope", "dubai")
    name = state.get("name")
    prop = _skip_to_none_v86(state.get("property"))
    period = _skip_to_none_v86(state.get("period"))
    deal_type = _skip_to_none_v86(state.get("deal_type"))

    state["property"] = prop
    state["period"] = period
    state["deal_type"] = deal_type
    if payload:
        state["ir_report_type"] = payload.report_plan.report_type
        state["ir_source_mode"] = payload.routing.source_mode
        state["ir_primary_table"] = payload.routing.primary_table

    user_states[message.from_user.id] = {**state, "step": "result", "history": state.get("history", [])}

    if kind == "deals":
        await send_deals_report(message, scope, name, prop, period, deal_type)
    elif kind == "period":
        await send_period_report(message, scope, name, prop, period, deal_type)
    elif kind == "top_buildings":
        await send_ranking_report(message, "active")
    else:
        await send_full_report(message, scope, name, prop, period, deal_type, _report_kind_label_v72(kind))


def _v96_goal_text(payload, state):
    if not payload:
        return state.get("goal") or "⚖️ Сбалансировано"
    mapping = {
        "life": "для жизни",
        "rent_income": "для арендного дохода",
        "short_term_rent": "для посуточной аренды",
        "resale": "для перепродажи",
        "roi": "для максимального ROI",
        "capital_growth": "для роста капитала",
        "balanced": "сбалансированная стратегия",
    }
    return mapping.get(payload.request.goal, payload.request.goal)



def _v100_apply_router_fallback_to_state(base_state, fallback_step):
    """Build a legacy v95 state from router fallback step.
    Important: fallback must be executed, not printed to user as English debug text.
    """
    st = dict(base_state or {})
    try:
        fmt_map = {
            "apartment": "Apartment",
            "townhouse": "Townhouse",
            "villa": "Villa",
            "land": "Plot",
            "plot": "Plot",
            "office": "Office",
            "shop": "Shop",
            "retail": "Shop",
            "commercial": "Commercial",
            "penthouse": "Penthouse",
            "duplex": "Duplex",
        }
        pf = getattr(fallback_step, "property_format", None)
        br = getattr(fallback_step, "bedrooms", None)
        bmin = getattr(fallback_step, "budget_min", None)
        bmax = getattr(fallback_step, "budget_max", None)

        if pf is None:
            st["object_format"] = None
        elif pf in fmt_map:
            st["object_format"] = fmt_map.get(pf)

        if pf in {"land", "plot", "office", "shop", "retail", "commercial", "warehouse", "full_building"}:
            st["rooms"] = None
        elif br is None:
            st["rooms"] = None
        else:
            st["rooms"] = str(br).title().replace(" Br", " BR")

        # If router expanded budget numerically, old helper cannot parse numeric bands safely.
        # For recovery we either keep original exact label or remove budget if it was widened.
        if bmin is None and bmax is None:
            st["budget"] = None
        elif (bmin != _v95_budget_bounds(base_state.get("budget"))[0]) or (bmax != _v95_budget_bounds(base_state.get("budget"))[1]):
            st["budget"] = None

    except Exception as e:
        print("V100_FALLBACK_STATE_ERROR:", repr(e))
    return st


def _v100_try_router_fallbacks(payload, normalized):
    """Execute router fallback cascade and return first non-empty result."""
    if not payload:
        return [], [], []
    notes = []
    try:
        # Skip first step because it is the exact filter already tried.
        for st in payload.sql_plan.fallback_steps[1:]:
            candidate_state = _v100_apply_router_fallback_to_state(normalized, st)
            areas, buildings, local_notes = _v95_top_areas_and_buildings(candidate_state)
            if areas or buildings:
                notes.append("Точная выборка была узкой, поэтому я автоматически расширил фильтр: " + getattr(st, "reason", "fallback"))
                notes.extend(local_notes or [])
                return areas, buildings, notes
    except Exception as e:
        print("V100_ROUTER_FALLBACK_EXEC_ERROR:", repr(e))
    return [], [], notes

def build_best_object_report_v95(state):
    """v96 override: best-object flow is now normalized by intelligence_router.
    The SQL selection still uses existing main.py helpers, so no DB architecture is broken.
    """
    payload = _ir_v96_prepare(state, "best object")
    normalized = dict(state or {})
    if payload:
        req = payload.request
        normalized["deal_type"] = _ir_v96_deal_type(payload, state.get("deal_type"))
        # For best-object display and SQL we keep format and rooms separate.
        # Apartment + 2BR must not become object_format='2 BR'.
        fmt_map = {
            "apartment": "Apartment",
            "townhouse": "Townhouse",
            "villa": "Villa",
            "land": "Plot",
            "plot": "Plot",
            "office": "Office",
            "shop": "Shop",
            "retail": "Shop",
            "commercial": "Commercial",
            "penthouse": "Penthouse",
            "duplex": "Duplex",
        }
        normalized["object_format"] = fmt_map.get(req.property_format, state.get("object_format"))
        if req.property_format in {"land", "plot", "office", "shop", "retail", "commercial", "warehouse", "full_building"}:
            # Non-residential/land formats must never keep an old bedroom filter.
            normalized["rooms"] = None
        else:
            normalized["rooms"] = req.bedrooms.title().replace(" Br", " BR") if req.bedrooms else state.get("rooms")
        if req.budget_min is not None or req.budget_max is not None:
            # Keep original label for old SQL budget helper, but use normalized in display notes.
            normalized["budget"] = state.get("budget")
        normalized["goal"] = _v96_goal_text(payload, state)

    areas, buildings, notes = _v95_top_areas_and_buildings(normalized)
    if not areas and not buildings:
        # v100: execute router fallback cascade instead of printing English debug fallback steps to user.
        fb_areas, fb_buildings, fb_notes = _v100_try_router_fallbacks(payload, normalized)
        if fb_areas or fb_buildings:
            areas, buildings, notes = fb_areas, fb_buildings, fb_notes
        else:
            html = no_data_message("Лучший объект")
            html += "\n\n🧭 <b>Что я уже проверил</b>\n"
            html += "• точный фильтр;\n• расширение периода;\n• расширение бюджета;\n• снятие комнатности;\n• снятие формата объекта.\n\n"
            html += "📌 <b>Рекомендация:</b> попробуйте выбрать «Пропустить» в формате/комнатности или бюджет шире. Я не смешиваю аренду и продажу, чтобы не показать ложную аналитику."
            return html

    best_area = areas[0] if areas else None
    best_building = buildings[0] if buildings else None
    deal_type = normalized.get("deal_type") or "неважно"
    obj_format = normalized.get("object_format") or "любой формат"
    budget = normalized.get("budget") or "не указан"
    rooms = normalized.get("rooms") or "неважно"
    goal = normalized.get("goal") or "сбалансированная стратегия"

    chosen = best_building or best_area or {}
    avg_price = _v95_num(chosen.get("avg_price"), None)
    min_price = _v95_num(chosen.get("min_price"), None)
    good_entry = avg_price * 0.92 if avg_price else None
    strong_entry = avg_price * 0.88 if avg_price else None

    html = "🏆 <b>Лучший объект</b>\n\n"
    html += _ir_v96_notes_html(payload)
    html += (
        f"📊 <b>Сделка:</b> {deal_type}\n"
        f"🏠 <b>Формат:</b> {obj_format}\n"
        f"💰 <b>Бюджет:</b> {budget}\n"
        f"🛏 <b>Комнаты:</b> {rooms}\n"
        f"🎯 <b>Цель:</b> {goal}\n"
    )
    if payload:
        html += f"🧭 <b>Источник:</b> {payload.routing.source_mode}\n"
    html += "\n"

    if notes:
        html += "📌 <b>Адаптивная логика</b>\n" + "\n".join([f"• {n}" for n in notes]) + "\n\n"

    if best_area:
        html += f"🥇 <b>Лучший район:</b> {best_area.get('name')}\n"
    if best_building:
        html += f"🥇 <b>Лучший объект / здание:</b> {best_building.get('name')}"
        if best_building.get('area_name_en'):
            html += f" — {best_building.get('area_name_en')}"
        html += "\n"

    html += (
        f"💰 <b>Средний ориентир:</b> {format_money(avg_price)}\n"
        f"✅ <b>Комфортный вход:</b> {format_money(good_entry)} или ниже\n"
        f"🔥 <b>Сильная точка входа:</b> {format_money(strong_entry)} или ближе к нижним DLD-сделкам {format_money(min_price)}\n\n"
    )

    html += "🏙 <b>Топ-3 района под цель</b>\n\n"
    for i, r in enumerate(areas[:3], 1):
        html += _v95_line(i, r, "area") + "\n"

    html += "🏢 <b>Топ-3 объекта / здания</b>\n\n"
    for i, r in enumerate(buildings[:3], 1):
        html += _v95_line(i, r, "building") + "\n"

    # Stronger explanation using router economic context.
    deal_word = "аренды" if _v95_is_rent(deal_type) else "покупки"
    rent_block = ""
    resale_block = ""
    if payload and payload.economic_context.get("needs_rent_model"):
        rent_block = (
            "\n<b>Арендная стратегия:</b> после выбора объекта нужно сравнить годовую аренду по rent-DLD, "
            "оценить long-term rent, short-term потенциал, occupancy, сервисные платежи и чистую доходность. "
            "Если rent-выборка по зданию узкая, система должна перейти на район, а не смешивать аренду с продажами.\n"
        )
    if payload and payload.economic_context.get("needs_resale_model"):
        resale_block = (
            "\n<b>Стратегия перепродажи:</b> оптимальный вход — ниже среднего DLD. "
            "Для выхода через 1–3 года важны ликвидность района, количество похожих сделок, spread между min/avg/max и цена за м²/ft².\n"
        )

    html += (
        "🧠 <b>Экономическое заключение 360°</b>\n\n"
        f"По выбранной цели лучший маршрут — начинать с топ-района и затем проверять конкретный объект из топа. "
        f"Для {deal_word} важны не только средняя цена, но и количество DLD-сделок: чем выше ликвидность, тем легче выйти из объекта, сдать его или защитить цену при переговорах.\n"
        f"{rent_block}{resale_block}\n"
        f"Если цель — <b>{goal}</b>, приоритет такой: 1) ликвидность района, 2) цена входа ниже среднего DLD, "
        "3) понятная комнатность/формат, 4) наличие похожих сделок, 5) юридическая чистота и реальные условия объекта.\n\n"
        "📌 <b>Практическая стратегия:</b> сначала берём варианты из топ-3 районов, затем внутри них проверяем топ-3 здания/проекта, "
        "после этого сравниваем конкретный юнит с последними сделками по этажу, виду, площади, состоянию и сервисным платежам."
    )
    return html

print("Loaded v99 intelligence_router bridge / full-flow fixes")


# =========================
# v101 ECONOMIC CONSISTENCY FIX ONLY
# Purpose: fix wrong economic conclusions without changing menus or flows.
# Fixes:
# - comparison formats must not fallback to "all properties";
# - out-of-budget villas/townhouses cannot win a 1-2M scenario;
# - dirty min prices like 8 410 AED are excluded from entry range;
# - Studio/1BR/2BR are treated as apartment bedroom segments, not separate asset formats;
# - rent/yield must be internally consistent.
# =========================

def _v101_budget_bounds(label):
    try:
        return _budget_bounds(label)
    except Exception:
        return (None, None)


def _v101_asset_format(value):
    p = str(value or '').strip().lower()
    if any(x in p for x in ['studio', '1 br', '1br', '1 b/r', '2 br', '2br', '2 b/r', '3 br', '3br', '3 b/r', '4 br', '4br', 'bedroom']):
        return 'Apartment'
    if any(x in p for x in ['villa', 'вилл']):
        return 'Villa'
    if any(x in p for x in ['town', 'таун', 'тонхаус']):
        return 'Townhouse'
    if any(x in p for x in ['land', 'plot', 'зем', 'плот']):
        return 'Land'
    if any(x in p for x in ['office', 'shop', 'commercial', 'retail']):
        return 'Commercial'
    return 'Apartment'


def _v101_clean_min_price(min_price, avg_price=None, budget=None):
    mn = _v90_num(min_price, None)
    avg = _v90_num(avg_price, None)
    bmin, bmax = _v101_budget_bounds(budget)
    if mn is None or mn <= 0:
        return None
    # DLD sometimes returns price-per-area or corrupted tiny values as min_price.
    if avg and mn < avg * 0.35:
        return None
    if bmin and mn < bmin * 0.50:
        return None
    if bmax and mn > bmax * 1.80:
        return None
    return mn


def _v101_entry_range(row, budget=None):
    avg = _v90_num((row or {}).get('avg_price'), None)
    mn = _v101_clean_min_price((row or {}).get('min_price'), avg, budget)
    if avg:
        low = mn if mn and mn >= avg * 0.70 else avg * 0.90
        high = avg * 0.95
        bmin, bmax = _v101_budget_bounds(budget)
        # Do not show a "comfortable entry" below the selected budget floor unless the market is actually below budget.
        if bmin and avg >= bmin * 0.80:
            low = max(low, bmin * 0.90)
        if bmax:
            high = min(high, bmax * 1.05)
            if low > high:
                low = avg * 0.90
                high = avg * 0.95
        return low, high
    return mn, None


def _v101_avg_in_budget(avg_price, budget, tolerance=0.12):
    bmin, bmax = _v101_budget_bounds(budget)
    avg = _v90_num(avg_price, None)
    if avg is None:
        return False
    if bmin is not None and avg < bmin * (1 - tolerance):
        return False
    if bmax is not None and avg > bmax * (1 + tolerance):
        return False
    return True


def _row_matches_budget(row, budget):
    """v101 override: budget fit for investment comparison is based on realistic average price, not dirty min/max range."""
    bmin, bmax = _v101_budget_bounds(budget)
    if bmin is None and bmax is None:
        return True
    avg = _v90_num((row or {}).get('avg_price'), None)
    if avg is None:
        return False
    if bmin is not None and avg < bmin * 0.80:
        return False
    if bmax is not None and avg > bmax * 1.15:
        return False
    return True


def _v101_get_stats_strict(scope, area, prop, period, deal_type):
    """Strict stats: can expand period, but never removes property format/bedroom filter."""
    attempts = [(period, scope, area)]
    if period:
        attempts.append((None, scope, area))
    if scope == 'area':
        attempts.append((period, 'dubai', None))
        if period:
            attempts.append((None, 'dubai', None))
    for per, sc, ar in attempts:
        try:
            row = get_stats(sc, ar, prop, per, deal_type)
            if row and _v90_int(row.get('deals')) > 0:
                return row, sc, ar, per
        except Exception as e:
            print('V101_STRICT_STATS_ERROR:', sc, ar, prop, per, deal_type, repr(e))
    return None, scope, area, period


def _v90_one_format_stats(scope, area, fmt, period, budget=None):
    """v101 override: format comparison must stay strict by format; no fallback to all-property rows."""
    sale_row, used_scope, used_area, used_period = _v101_get_stats_strict(scope or 'dubai', area, fmt, period, 'sale')
    if not sale_row or _v90_int(sale_row.get('deals')) <= 0:
        return None

    avg_price = _v90_num(sale_row.get('avg_price'), None)
    deals = _v90_int(sale_row.get('deals'))
    if avg_price is None or avg_price <= 0 or deals <= 0:
        return None

    in_budget = _row_matches_budget(sale_row, budget) if budget else True

    rent_row, _, _, _ = _v101_get_stats_strict(used_scope, used_area, fmt, used_period, 'rent')
    avg_rent = _v90_num((rent_row or {}).get('avg_price'), None)
    rent_deals = _v90_int((rent_row or {}).get('deals'))
    yield_pct = None
    if avg_price and avg_rent and 0 < avg_rent < avg_price * 0.20:
        yield_pct = avg_rent / avg_price * 100
    elif avg_price and avg_rent:
        # Do not propagate impossible rent numbers.
        avg_rent = None

    avg_meter = _v90_num(sale_row.get('avg_meter'), None)
    liquidity_index = min(100, round(deals / 250 * 100, 1))
    if deals >= 1000:
        liquidity_text = 'очень высокая'; growth_base = 0.075
    elif deals >= 300:
        liquidity_text = 'высокая'; growth_base = 0.060
    elif deals >= 100:
        liquidity_text = 'средняя'; growth_base = 0.045
    else:
        liquidity_text = 'ограниченная'; growth_base = 0.030

    af = _v101_asset_format(fmt).lower()
    if af == 'apartment':
        exit_risk = 'ниже из-за широкой базы покупателей и арендаторов'; growth_adj = 0.000
    elif af == 'villa':
        exit_risk = 'выше по чеку, но сильнее при дефиците качественных семейных объектов'; growth_adj = 0.012
    else:
        exit_risk = 'средний: чек ниже виллы, но семейный спрос сильнее, чем у части апартаментов'; growth_adj = 0.008
    annual_growth = growth_base + growth_adj

    score = _score_format_row({**sale_row, 'format': fmt}, 'сбалансировано')
    score += 10 if in_budget else -30
    if yield_pct:
        score += min(yield_pct, 12) * 1.2

    return {
        'format': _v101_asset_format(fmt),
        'sale': sale_row,
        'rent': rent_row,
        'deals': deals,
        'rent_deals': rent_deals,
        'avg_price': avg_price,
        'min_price': _v101_clean_min_price(sale_row.get('min_price'), avg_price, budget),
        'max_price': _v90_num(sale_row.get('max_price'), None),
        'avg_meter': avg_meter,
        'avg_rent': avg_rent,
        'yield_pct': yield_pct,
        'liquidity_index': liquidity_index,
        'liquidity_text': liquidity_text,
        'annual_growth': annual_growth * 100,
        'resale_1y': avg_price * ((1 + annual_growth) ** 1),
        'resale_3y': avg_price * ((1 + annual_growth) ** 3),
        'resale_5y': avg_price * ((1 + annual_growth) ** 5),
        'score': round(score, 1),
        'in_budget': in_budget,
        'used_period': used_period,
        'exit_risk': exit_risk,
        'budget_segment': _budget_label_from_row(sale_row),
        'used_scope': used_scope,
        'used_area': used_area,
    }


def _v90_collect_format_comparison(scope='dubai', area=None, budget=None, goal=None, period=None):
    """v101 override: compare only real format-specific rows and rank in-budget rows first."""
    rows, notes = [], []
    seen = set()
    for fmt in _V90_FORMATS:
        r = _v90_one_format_stats(scope, area, fmt, period, budget)
        if not r:
            notes.append(f'по формату {_v90_format_ru(fmt)} недостаточно стабильных DLD-данных')
            continue
        # Protect against hidden all-property fallback duplicates.
        key = (round(_v90_num(r.get('avg_price'), 0) or 0), _v90_int(r.get('deals')), round(_v90_num(r.get('avg_meter'), 0) or 0))
        if key in seen:
            notes.append(f'формат {_v90_format_ru(fmt)} исключён: данные совпали с другим форматом, вероятен общий fallback, а не отдельная выборка')
            continue
        seen.add(key)
        rows.append(r)

    if not rows and scope == 'area':
        notes.append('по выбранному району данных мало — для ориентира сравнение расширено до рынка Дубая')
        return _v90_collect_format_comparison('dubai', None, budget, goal, period)

    if not rows:
        return [], notes

    if budget:
        in_budget = [r for r in rows if r.get('in_budget')]
        out_budget = [r for r in rows if not r.get('in_budget')]
        if in_budget:
            rows = sorted(in_budget, key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True) + \
                   sorted(out_budget, key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True)
            if out_budget:
                notes.append('форматы вне бюджета оставлены только как benchmark, но не могут быть победителем: ' + ', '.join(_v90_format_ru(r['format']) for r in out_budget))
        else:
            rows.sort(key=lambda x: abs((_v90_num(x.get('avg_price'), 0) or 0) - ((_v101_budget_bounds(budget)[0] or _v101_budget_bounds(budget)[1] or 0))), reverse=False)
            notes.append('в выбранном бюджете стабильной DLD-выборки по форматам нет; показаны ближайшие ценовые ориентиры, не как финальная рекомендация')
    else:
        rows.sort(key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True)
    return rows, notes


def show_smart_recommendation(goal, budget, timing, risk, rows):
    """v101 override: clean entry range and pass budget-consistent comparisons to economic engine."""
    if not rows:
        return "❌ По этим параметрам не найдено достаточно сильных вариантов.\n\nПопробуйте расширить бюджет или выбрать другой риск-профиль."

    best = dict(rows[0])
    area = best.get('area') or '—'
    prop = best.get('property') or best.get('format') or '—'
    good_low, good_high = _v101_entry_range(best, budget)

    text = (
        "🧠 <b>Инвестиционный подбор</b>\n\n"
        "🏆 <b>Лучший сценарий</b>\n"
        f"📍 <b>Район:</b> {area}\n"
        f"🏠 <b>Формат:</b> {prop}\n"
        f"📊 <b>Сделки:</b> {format_int(best.get('deals'))}\n"
        f"💰 <b>Средняя цена:</b> {format_money(best.get('avg_price'))}\n"
        f"✅ <b>Комфортная цена входа:</b> {format_money(good_low)} — {format_money(good_high)}\n"
        f"📐 <b>Средняя цена за метр:</b> {format_money(best.get('avg_meter'))}\n"
    )

    try:
        compare_rows, compare_notes = _v90_collect_format_comparison(scope='area', area=area, budget=budget, goal=goal, period='36')
        if not compare_rows:
            compare_rows, compare_notes = _v90_collect_format_comparison(scope='dubai', area=None, budget=budget, goal=goal, period='36')
    except Exception as e:
        print('SMART_RECOMMENDATION_V101_COMPARE_ERROR:', repr(e))
        compare_rows, compare_notes = [], []

    # Add selected apartment segment to comparisons when user result is Studio/BR and format comparison found only broad apartment data.
    selected_format = _v101_asset_format(prop)
    selected_for_engine = dict(best)
    selected_for_engine['format'] = selected_format
    selected_for_engine['bedrooms'] = prop
    selected_for_engine['min_price'] = _v101_clean_min_price(best.get('min_price'), best.get('avg_price'), budget)

    if selected_format == 'Apartment' and best.get('avg_price'):
        has_apartment_in_budget = any(_v101_asset_format(r.get('format')) == 'Apartment' and r.get('in_budget') for r in compare_rows)
        if not has_apartment_in_budget:
            compare_rows = [{
                'format': 'Apartment',
                'deals': best.get('deals'),
                'avg_price': best.get('avg_price'),
                'min_price': selected_for_engine.get('min_price'),
                'max_price': best.get('max_price'),
                'avg_meter': best.get('avg_meter'),
                'avg_rent': best.get('avg_rent'),
                'yield_pct': best.get('yield_pct'),
                'score': 80,
                'in_budget': True,
                'liquidity_text': 'высокая' if _v90_int(best.get('deals')) >= 1000 else 'средняя',
            }] + compare_rows

    # Keep in-budget rows first; out-of-budget rows are benchmark only and should not win.
    if budget and any(r.get('in_budget') for r in compare_rows):
        compare_for_engine = [r for r in compare_rows if r.get('in_budget')]
    else:
        compare_for_engine = compare_rows[:]

    if compare_notes:
        text += "\n📌 <b>Адаптивный фильтр</b>\n"
        for n in compare_notes[:3]:
            text += f"• {n}\n"

    if _EXT_BUILD_ECONOMIC_REPORT:
        text += _EXT_BUILD_ECONOMIC_REPORT(
            selected=selected_for_engine,
            comparisons=compare_for_engine,
            goal=goal,
            budget=budget,
            period=timing,
            area=area,
            deal_type='sale',
        )
    else:
        text += _v91_generic_deep_article(best, scope='area', name=area, prop=prop, period='36', deal_type='sale')

    if len(rows) > 1:
        text += "\n\n📋 <b>Альтернативы</b>\n\n"
        for i, r in enumerate(rows[1:], 2):
            text += f"{i}. <b>{r.get('area')}</b> · {r.get('property')}\n   💰 {format_money(r.get('avg_price'))} · 📊 {format_int(r.get('deals'))} сделок\n\n"

    return text

print('Loaded v101 economic consistency fix only')


# =========================
# v103 BUILDING FORMAT + STRICT COMPARISON FIX
# Scope: fix apartment towers being analyzed as townhouse/villa; prevent Dubai-wide contamination
# in building/area comparison; hide economic comparison when comparable data is insufficient.
# =========================

V103_BUILDING_FORMAT_OVERRIDES = {
    "grande signature residences": "Apartment",
    "grande signature": "Apartment",
    "grande": "Apartment",
    "address residences dubai opera": "Apartment",
    "the address residences dubai opera": "Apartment",
    "address opera": "Apartment",
    "binghatti corner": "Apartment",
}

V103_APARTMENT_BUILDING_KEYWORDS = [
    "residence", "residences", "tower", "towers", "apartments", "apartment",
    "grande", "address", "opera", "binghatti", "burj", "marina gate", "damac maison",
]


def _v103_norm_name(value):
    try:
        return normalize_search_text(value)
    except Exception:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _v103_forced_format_for_building(name):
    q = _v103_norm_name(name)
    if not q:
        return None
    if q in V103_BUILDING_FORMAT_OVERRIDES:
        return V103_BUILDING_FORMAT_OVERRIDES[q]
    for key, fmt in V103_BUILDING_FORMAT_OVERRIDES.items():
        if key and key in q:
            return fmt
    # Safe heuristic: named vertical residential towers are apartments, not villas/townhouses.
    if any(k in q for k in V103_APARTMENT_BUILDING_KEYWORDS):
        return "Apartment"
    return None


def _v103_apply_selected_format_override(selected, scope=None, name=None):
    row = dict(selected or {})
    if scope == "building":
        fmt = _v103_forced_format_for_building(name or row.get("building_name_en") or row.get("building"))
        if fmt:
            row["format"] = fmt
            row["property"] = fmt
            row["forced_format"] = True
            row["format_note"] = "Формат принудительно определён по типу здания: apartment tower."
    # Studio/BR are bedroom segments of Apartment, not separate asset classes.
    if _v101_asset_format(row.get("format") or row.get("property")) == "Apartment":
        row["format"] = "Apartment"
    return row


# v103: strict stats may expand period only. It must never jump from selected area/building to Dubai-wide
# inside comparison, because that creates fake villa/townhouse benchmarks for apartment towers.
def _v101_get_stats_strict(scope, area, prop, period, deal_type):
    attempts = [(period, scope, area)]
    if period:
        attempts.append((None, scope, area))
    for per, sc, ar in attempts:
        try:
            row = get_stats(sc, ar, prop, per, deal_type)
            if row and _v90_int(row.get('deals')) > 0:
                return row, sc, ar, per
        except Exception as e:
            print('V103_STRICT_STATS_ERROR:', sc, ar, prop, per, deal_type, repr(e))
    return None, scope, area, period


def _v90_one_format_stats(scope, area, fmt, period, budget=None):
    """v103 strict format stats: same scope/area only; no broad fallback contamination."""
    sale_row, used_scope, used_area, used_period = _v101_get_stats_strict(scope or 'dubai', area, fmt, period, 'sale')
    if not sale_row or _v90_int(sale_row.get('deals')) <= 0:
        return None

    avg_price = _v90_num(sale_row.get('avg_price'), None)
    deals = _v90_int(sale_row.get('deals'))
    if avg_price is None or avg_price <= 0 or deals <= 0:
        return None

    in_budget = _row_matches_budget(sale_row, budget) if budget else True

    rent_row, _, _, _ = _v101_get_stats_strict(used_scope, used_area, fmt, used_period, 'rent')
    avg_rent = _v90_num((rent_row or {}).get('avg_price'), None)
    rent_deals = _v90_int((rent_row or {}).get('deals'))
    yield_pct = None
    if avg_price and avg_rent and 0 < avg_rent < avg_price * 0.20:
        yield_pct = avg_rent / avg_price * 100
    elif avg_price and avg_rent:
        avg_rent = None

    avg_meter = _v90_num(sale_row.get('avg_meter'), None)
    liquidity_index = min(100, round(deals / 250 * 100, 1))
    if deals >= 1000:
        liquidity_text = 'очень высокая'; growth_base = 0.075
    elif deals >= 300:
        liquidity_text = 'высокая'; growth_base = 0.060
    elif deals >= 100:
        liquidity_text = 'средняя'; growth_base = 0.045
    else:
        liquidity_text = 'ограниченная'; growth_base = 0.030

    af = _v101_asset_format(fmt).lower()
    if af == 'apartment':
        exit_risk = 'ниже из-за широкой базы покупателей и арендаторов'; growth_adj = 0.000
    elif af == 'villa':
        exit_risk = 'выше по чеку, но сильнее при дефиците качественных семейных объектов'; growth_adj = 0.012
    else:
        exit_risk = 'средний: чек ниже виллы, но семейный спрос сильнее, чем у части апартаментов'; growth_adj = 0.008
    annual_growth = growth_base + growth_adj

    score = _score_format_row({**sale_row, 'format': fmt}, 'сбалансировано')
    score += 10 if in_budget else -30
    if yield_pct:
        score += min(yield_pct, 12) * 1.2
    score = max(0, min(100, round(score, 1)))

    return {
        'format': _v101_asset_format(fmt),
        'sale': sale_row,
        'rent': rent_row,
        'deals': deals,
        'rent_deals': rent_deals,
        'avg_price': avg_price,
        'min_price': _v101_clean_min_price(sale_row.get('min_price'), avg_price, budget),
        'max_price': _v90_num(sale_row.get('max_price'), None),
        'avg_meter': avg_meter,
        'avg_rent': avg_rent,
        'yield_pct': yield_pct,
        'liquidity_index': liquidity_index,
        'liquidity_text': liquidity_text,
        'annual_growth': annual_growth * 100,
        'resale_1y': avg_price * ((1 + annual_growth) ** 1),
        'resale_3y': avg_price * ((1 + annual_growth) ** 3),
        'resale_5y': avg_price * ((1 + annual_growth) ** 5),
        'score': score,
        'in_budget': in_budget,
        'used_period': used_period,
        'exit_risk': exit_risk,
        'budget_segment': _budget_label_from_row(sale_row),
        'used_scope': used_scope,
        'used_area': used_area,
        'comparison_scope_valid': True,
    }


def _v90_collect_format_comparison(scope='dubai', area=None, budget=None, goal=None, period=None):
    """v103: compare only formats available in the exact requested market.
    Area/building requests must not silently expand to Dubai-wide villas/townhouses.
    """
    # Building-level format comparison is usually meaningless. A tower is normally one asset class;
    # comparing it to Dubai-wide villas created the previous bug.
    if scope == 'building':
        return [], ['сравнение форматов отключено для конкретного здания, чтобы не смешивать здание с рынком Дубая']

    rows, notes = [], []
    seen = set()
    for fmt in _V90_FORMATS:
        r = _v90_one_format_stats(scope, area, fmt, period, budget)
        if not r:
            notes.append(f'по формату {_v90_format_ru(fmt)} недостаточно стабильных DLD-данных в этом же рынке')
            continue
        key = (round(_v90_num(r.get('avg_price'), 0) or 0), _v90_int(r.get('deals')), round(_v90_num(r.get('avg_meter'), 0) or 0))
        if key in seen:
            notes.append(f'формат {_v90_format_ru(fmt)} исключён: данные совпали с другим форматом, вероятен общий fallback')
            continue
        seen.add(key)
        rows.append(r)

    if not rows:
        return [], notes

    if budget:
        in_budget = [r for r in rows if r.get('in_budget')]
        out_budget = [r for r in rows if not r.get('in_budget')]
        if in_budget:
            rows = sorted(in_budget, key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True) + \
                   sorted(out_budget, key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True)
            if out_budget:
                notes.append('форматы вне бюджета оставлены только как benchmark и не могут быть победителем: ' + ', '.join(_v90_format_ru(r['format']) for r in out_budget))
        else:
            rows = []
            notes.append('в выбранном бюджете нет устойчивой единой выборки по форматам; сравнение скрыто, чтобы не рекомендовать актив вне бюджета')
    else:
        rows.sort(key=lambda x: (x.get('score') or 0, x.get('deals') or 0), reverse=True)

    # Need at least two formats for a real comparison.
    if len(rows) < 2:
        notes.append('для честного сравнения нужно минимум два формата в одном и том же рынке; секция сравнения скрыта')
    return rows, notes


def _v92_build_comparison_for_context(scope=None, name=None, prop=None, period=None, deal_type=None, budget=None, goal=None):
    """v103: strict comparison context. No automatic Dubai fallback from building/area."""
    try:
        if scope == 'building':
            return []
        area = name if scope == 'area' else None
        rows, notes = _v90_collect_format_comparison(
            scope=scope or 'dubai',
            area=area,
            budget=budget,
            goal=goal,
            period=period,
        )
        if len(rows) < 2:
            return []
        return rows or []
    except Exception as e:
        print('V103_CONTEXT_COMPARE_ERROR:', repr(e))
        return []


def _build_360_conclusion(row, scope=None, name=None, report_kind=None):
    """v103: external engine with forced building format and strict comparison isolation."""
    try:
        selected = _v103_apply_selected_format_override(row or {}, scope=scope, name=name)
        if _EXT_BUILD_ECONOMIC_REPORT:
            comparisons = _v92_build_comparison_for_context(scope=scope, name=name, period=None)
            return _EXT_BUILD_ECONOMIC_REPORT(
                selected=selected,
                comparisons=comparisons,
                goal=report_kind or 'аналитика DLD',
                budget=None,
                period=None,
                area=name or selected.get('area'),
                deal_type=None,
            )
    except Exception as e:
        print('BUILD_360_V103_ENGINE_ERROR:', repr(e))
    try:
        return _v91_generic_deep_article(row, scope=scope, name=name, report_kind=report_kind)
    except Exception as e:
        print('BUILD_360_V103_FALLBACK_ERROR:', repr(e))
        return "\n\n🧠 <b>Экономическое заключение 360°</b>\n\nНедостаточно данных для полного экономического отчёта. Расширьте период или фильтр."


async def send_period_report(message, scope, name=None, prop=None, period=None, deal_type=None):
    """v103: do not append economic conclusion when period comparison itself is insufficient."""
    user_id = message.from_user.id
    await send_processing(message)
    period = period or '12'
    comparison = get_comparison(scope, name, prop, period, deal_type)
    if not comparison:
        await message.answer(no_data_message('Сравнение периодов'), reply_markup=report_menu(user_id) if scope in ['building', 'area'] else main_menu(user_id))
        return
    current, previous = comparison
    if not current or not previous or not _int(current.get('deals')) or not _int(previous.get('deals')):
        await message.answer(no_data_message('Сравнение периодов'), reply_markup=report_menu(user_id) if scope in ['building', 'area'] else main_menu(user_id))
        return
    title = _human_report_title(scope, name, 'Сравнение периодов')
    html = show_comparison(f"<b>{title}</b>", current, previous, period, deal_type)
    html += _build_360_conclusion(current, scope, name, 'period')
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))


print('Loaded v103 building format and strict comparison fix')


if __name__ == "__main__":
    asyncio.run(main())
