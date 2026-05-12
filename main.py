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
    return f"NULLIF(regexp_replace({column}, '[^0-9.]', '', 'g'), '')::numeric"


PRICE = num_sql("actual_worth")
METER_PRICE = num_sql("meter_sale_price")
RENT_VALUE = num_sql("rent_value")

BUILDING_NAME = """
COALESCE(
    NULLIF(building_name_en, ''),
    NULLIF(project_name_en, ''),
    NULLIF(master_project_en, '')
)
"""


TEXTS = {
    "ru": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nВыберите язык:",
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
        "error": "⚠️ Возникла ошибка при расчёте. Пришлите лог Railway, сэр.",
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
        "error": "⚠️ Calculation error. Please send Railway logs.",
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
        "error": "⚠️ حدث خطأ في الحساب.",
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
    """
    Строгий поиск района.

    Важно:
    - JVC не ищем как слово JVC в DLD, потому что в DLD его часто нет.
    - JVC подменяем на реальные DLD area_name_en:
      Al Barsha South Fourth / Fifth / Al Hebiah First.
    - Downtown подменяем на Burj Khalifa.
    - Marina подменяем на Marsa Dubai.
    """
    values = [v for v in area_alias_values(query) if v]

    if not values:
        return "AND 1=0", []

    params = []
    parts = []

    for value in values:
        parts.append("area_name_en ILIKE %s")
        params.append(f"%{value}%")

    return "AND (" + " OR ".join(parts) + ")", params



BUILDING_ALIASES = {
    "address opera": ["address", "opera"],
    "the address opera": ["address", "opera"],
    "address residences dubai opera": ["address", "opera"],
    "address residence dubai opera": ["address", "opera"],
    "dubai opera address": ["address", "opera"],

    "grande": ["grande"],
    "grande signature": ["grande"],
    "grande signature residences": ["grande"],

    "burj vista": ["burj", "vista"],
    "marina gate": ["marina", "gate"],
    "binghatti corner": ["binghatti", "corner"],
    "stax": ["stax"],
}


STOP_WORDS = {
    "the", "a", "an", "of", "by", "at", "in", "on",
    "dubai", "residence", "residences", "tower", "towers",
    "apartment", "apartments", "building", "block", "phase",
    "hotel", "homes", "home"
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
        COALESCE(project_name_en, '') || ' ' ||
        COALESCE(master_project_en, '') || ' ' ||
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


def make_deal_type_condition(deal_type):
    if not deal_type:
        return "", []

    d = deal_type.lower()

    if "rent" in d or "арен" in d or "إيجار" in d:
        return "AND (procedure_name_en ILIKE %s OR procedure_name_en ILIKE %s OR rent_value IS NOT NULL)", ["%rent%", "%lease%"]

    if "sale" in d or "прод" in d or "بيع" in d:
        return "AND (procedure_name_en ILIKE %s OR procedure_name_en ILIKE %s OR actual_worth IS NOT NULL)", ["%sale%", "%sell%"]

    return "", []



def scope_condition(scope, name, original_query=None):
    if scope == "building":
        return f" AND {BUILDING_NAME} = %s", [name]

    if scope == "area":
        query = original_query or name
        q = clean_query(query).lower()

        reverse_virtual = {
            "jvc": "jvc",
            "downtown dubai": "downtown",
            "dubai marina": "dubai marina",
            "jlt": "jlt",
        }

        if q in reverse_virtual:
            q = reverse_virtual[q]
            query = q

        if q in AREA_ALIASES:
            area_sql, area_params = make_area_exact_condition(query)
            return " " + area_sql, area_params

        return " AND area_name_en = %s", [name]

    return "", []


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


def find_buildings(query, limit=10):
    search_sql, params = make_building_condition(query)
    q = normalize_search_text(query)
    tokens = smart_query_tokens(query)
    expr = building_search_expression()

    # ranking params:
    # exact full phrase, startswith, and token count hits
    rank_params = [q, f"{q}%"] + [f"%{t}%" for t in tokens] + params + [limit]

    token_score_sql = " + ".join([f"CASE WHEN {expr} ILIKE %s THEN 1 ELSE 0 END" for _ in tokens]) or "0"

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    CASE
                        WHEN {expr} = %s THEN 1000
                        WHEN {expr} ILIKE %s THEN 700
                        ELSE 0
                    END
                    + ({token_score_sql}) * 100
                    + LEAST(COUNT(*), 500) AS relevance_score
                {base_from()}
                  AND {BUILDING_NAME} IS NOT NULL
                  {search_sql}
                GROUP BY {BUILDING_NAME}, area_name_en
                ORDER BY relevance_score DESC, deals DESC
                LIMIT %s
            """, rank_params)
            return cur.fetchall()


def find_areas(query, limit=10):
    q = clean_query(query).lower()
    area_sql, params = make_area_exact_condition(query)

    # Known market names should return one friendly virtual area, not technical DLD areas.
    if q in AREA_ALIASES:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        %s AS area_name_en,
                        COUNT(*) AS deals,
                        COUNT(DISTINCT {BUILDING_NAME}) AS buildings
                    {base_from()}
                      {area_sql}
                      AND area_name_en IS NOT NULL
                      AND area_name_en <> ''
                    LIMIT 1
                """, [virtual_area_name(query)] + params)
                row = cur.fetchone()
                return [row] if row and row.get("deals") else []

    params.append(limit)
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    area_name_en,
                    COUNT(*) AS deals,
                    COUNT(DISTINCT {BUILDING_NAME}) AS buildings
                {base_from()}
                  {area_sql}
                  AND area_name_en IS NOT NULL
                  AND area_name_en <> ''
                GROUP BY area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    where, params = scope_condition(scope, name, original_query=name)

    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)

    params += prop_args + deal_args

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT {BUILDING_NAME}) AS buildings,
                    COUNT(DISTINCT area_name_en) AS areas,
                    AVG({PRICE}) AS avg_price,
                    MIN({PRICE}) AS min_price,
                    MAX({PRICE}) AS max_price,
                    AVG({METER_PRICE}) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    STRING_AGG(DISTINCT NULLIF(rooms_en, ''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en, ''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en, ''), ', ') AS property_sub_types
                {base_from()}
                  {where}
                  {prop_sql}
                  {deal_sql}
                  {period_condition(period)}
                  AND {PRICE} IS NOT NULL
            """, params)
            return cur.fetchone()



def get_unit_summary(scope="building", name=None, prop=None, period=None, deal_type=None):
    where, params = scope_condition(scope, name, original_query=name)
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    params += prop_args + deal_args

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    MIN({PRICE}) AS min_price,
                    MAX({PRICE}) AS max_price,
                    AVG({METER_PRICE}) AS avg_meter,
                    percentile_cont(0.25) WITHIN GROUP (ORDER BY {PRICE}) AS p25_price,
                    percentile_cont(0.50) WITHIN GROUP (ORDER BY {PRICE}) AS median_price,
                    percentile_cont(0.75) WITHIN GROUP (ORDER BY {PRICE}) AS p75_price
                {base_from()}
                  {where}
                  {prop_sql}
                  {deal_sql}
                  {period_condition(period)}
                  AND {PRICE} IS NOT NULL
            """, params)
            return cur.fetchone()

def get_comparison(scope="dubai", name=None, prop=None, period=None, deal_type=None):
    if not period:
        return None

    where, base_params = scope_condition(scope, name, original_query=name)

    params_current = list(base_params)
    params_previous = list(base_params)

    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)

    params_current += prop_args + deal_args
    params_previous += prop_args + deal_args

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  {where}
                  {prop_sql}
                  {deal_sql}
                  {period_condition(period)}
                  AND {PRICE} IS NOT NULL
            """, params_current)
            current = cur.fetchone()

            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  {where}
                  {prop_sql}
                  {deal_sql}
                  {period_previous_condition(period)}
                  AND {PRICE} IS NOT NULL
            """, params_previous)
            previous = cur.fetchone()

    return current, previous


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7):
    where, params = scope_condition(scope, name, original_query=name)

    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    params += prop_args + deal_args + [limit]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    safe_date,
                    procedure_name_en,
                    rooms_en,
                    property_type_en,
                    property_sub_type_en,
                    {PRICE} AS price,
                    {METER_PRICE} AS meter_price,
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en
                {base_from()}
                  {where}
                  {prop_sql}
                  {deal_sql}
                  {period_condition(period)}
                  AND {PRICE} IS NOT NULL
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_top_active():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  AND {BUILDING_NAME} IS NOT NULL
                  AND {PRICE} IS NOT NULL
                GROUP BY {BUILDING_NAME}, area_name_en
                ORDER BY deals DESC
                LIMIT 10
            """)
            return cur.fetchall()


def get_top_price():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  AND {BUILDING_NAME} IS NOT NULL
                  AND {PRICE} IS NOT NULL
                GROUP BY {BUILDING_NAME}, area_name_en
                HAVING COUNT(*) >= 5
                ORDER BY avg_price DESC
                LIMIT 10
            """)
            return cur.fetchall()



def get_top_buildings_in_scope(scope="dubai", name=None, period=None, deal_type=None, limit=7):
    where, params = scope_condition(scope, name, original_query=name)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    params += deal_args + [limit]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  {where}
                  {deal_sql}
                  {period_condition(period)}
                  AND {BUILDING_NAME} IS NOT NULL
                  AND {PRICE} IS NOT NULL
                GROUP BY {BUILDING_NAME}, area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, params)
            return cur.fetchall()



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

def quick_area_report(display_name, row, comparison=None, top_buildings=None):
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранному району."

    text = (
        f"🏙 <b>Статистика района: {display_name}</b>\n\n"
        f"📊 Сделок: <b>{row['deals']:,}</b>\n"
        f"🏢 Зданий: <b>{row.get('buildings') or 0:,}</b>\n"
        f"💰 Средняя цена: <b>{format_money(row['avg_price'])}</b>\n"
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


def smart_pick_candidates(goal, budget_text, risk, timing):
    bmin, bmax = parse_budget_range(budget_text)
    ptypes = recommended_property_types(goal, budget_text)
    results = []
    with db() as conn:
        with conn.cursor() as cur:
            for display_area, real_areas in smart_area_universe(goal):
                area_conditions = " OR ".join(["area_name_en ILIKE %s"] * len(real_areas))
                area_params = [f"%{a}%" for a in real_areas]
                best_rows = []
                for prop in ptypes:
                    prop_sql, prop_args = property_condition(prop)
                    cur.execute(f"""
                        SELECT COUNT(*) AS deals,
                               COUNT(DISTINCT {BUILDING_NAME}) AS buildings,
                               AVG({PRICE}) AS avg_price,
                               MIN({PRICE}) AS min_price,
                               MAX({PRICE}) AS max_price,
                               AVG({METER_PRICE}) AS avg_meter
                        {base_from()}
                          AND ({area_conditions})
                          {prop_sql}
                          AND {PRICE} IS NOT NULL
                          AND {PRICE} >= %s
                          AND {PRICE} <= %s
                          AND safe_date >= CURRENT_DATE - INTERVAL '24 months'
                    """, area_params + prop_args + [bmin, bmax])
                    row = cur.fetchone()
                    if row and row.get("deals"):
                        deals = int(row["deals"])
                        avg_price = float(row["avg_price"] or 0)
                        budget_mid = (bmin + bmax) / 2
                        affordability = 100 - min(100, abs(avg_price - budget_mid) / max(budget_mid, 1) * 100)
                        liquidity = min(100, deals / 30 * 100)
                        risk_bonus = 12 if risk == "низкий риск" and deals >= 30 else 8 if risk == "агрессивно" else 6
                        goal_bonus = 10 if prop in ["Studio", "1 BR"] else 5
                        score = liquidity * 0.45 + affordability * 0.35 + risk_bonus + goal_bonus
                        best_rows.append({"area": display_area, "property": prop, "deals": deals, "buildings": row.get("buildings") or 0, "avg_price": avg_price, "min_price": row.get("min_price"), "max_price": row.get("max_price"), "avg_meter": row.get("avg_meter"), "score": score})
                if best_rows:
                    results.append(sorted(best_rows, key=lambda x: x["score"], reverse=True)[0])
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


def show_stats(title, row, prop=None, period=None, deal_type=None):
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранным фильтрам."

    return (
        f"{title}\n\n"
        f"Фильтры:\n"
        f"📊 Сделка: <b>{deal_type or 'все'}</b>\n"
        f"🏠 Тип/комнаты: <b>{prop or 'все'}</b>\n"
        f"📅 Период: <b>{period_label(period)}</b>\n\n"
        f"📊 Сделок: <b>{row['deals']:,}</b>\n"
        f"🏢 Зданий: <b>{row.get('buildings') or 0}</b>\n"
        f"📍 Районов: <b>{row.get('areas') or 0}</b>\n"
        f"💰 Средняя цена: <b>{format_money(row['avg_price'])}</b>\n"
        f"🔻 Минимальная цена: <b>{format_money(row['min_price'])}</b>\n"
        f"🔺 Максимальная цена: <b>{format_money(row['max_price'])}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(row['avg_meter'])}</b>\n"
        f"🗓 Первая сделка: <b>{row['first_deal']}</b>\n"
        f"🗓 Последняя сделка: <b>{row['last_deal']}</b>\n\n"
        f"🛏 Комнаты: {row.get('rooms_list') or 'нет данных'}\n"
        f"🏗 Типы: {row.get('property_types') or 'нет данных'}\n"
        f"🏘 Подтипы: {row.get('property_sub_types') or 'нет данных'}"
    )


def show_comparison(title, current, previous):
    if not current or not previous:
        return "❌ Недостаточно данных для сравнения."

    deals_change = pct_change(current["deals"], previous["deals"])
    price_change = pct_change(current["avg_price"], previous["avg_price"])
    meter_change = pct_change(current["avg_meter"], previous["avg_meter"])

    return (
        f"{title}\n\n"
        f"<b>Текущий период:</b>\n"
        f"📊 Сделок: <b>{current['deals']:,}</b>\n"
        f"💰 Средняя цена: <b>{format_money(current['avg_price'])}</b>\n"
        f"📐 Цена за метр: <b>{format_money(current['avg_meter'])}</b>\n\n"
        f"<b>Предыдущий такой же период:</b>\n"
        f"📊 Сделок: <b>{previous['deals']:,}</b>\n"
        f"💰 Средняя цена: <b>{format_money(previous['avg_price'])}</b>\n"
        f"📐 Цена за метр: <b>{format_money(previous['avg_meter'])}</b>\n\n"
        f"<b>Динамика:</b>\n"
        f"📊 Сделки: <b>{format_pct(deals_change)}</b>\n"
        f"💰 Средняя цена: <b>{format_pct(price_change)}</b>\n"
        f"📐 Цена за метр: <b>{format_pct(meter_change)}</b>"
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
    await message.answer("🔎 Ищу похожие здания...")
    rows = find_buildings(text)

    if not rows:
        await message.answer(tr(user_id, "not_found"), reply_markup=back_menu(user_id))
        user_states[user_id] = {
            "step": "building_query",
            "scope": "building",
            "history": user_states.get(user_id, {}).get("history", [])
        }
        return

    suggestions = [r["building_name_en"] for r in rows if r["building_name_en"]]

    user_states[user_id] = {
        "step": "choose_building",
        "scope": "building",
        "suggestions": suggestions,
        "history": user_states.get(user_id, {}).get("history", [])
    }

    buttons = [[name] for name in suggestions[:10]]
    buttons.append([tr(user_id, "back"), tr(user_id, "main")])

    response = tr(user_id, "choose_building") + "\n\n"
    for i, r in enumerate(rows, 1):
        response += f"{i}. {r['building_name_en']} — {r['area_name_en']} ({format_int(r['deals'])} сделок)\n"

    await message.answer(response, reply_markup=kb(buttons))

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
            push_state(user_id, {"step": "choose_deal_type", "scope": "dubai", "name": None, "force_report": "last"})
            await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
            return

        if text == "📉 Проверить сделку":
            push_state(user_id, {"step": "building_query", "scope": "building", "force_report": "undervalued"})
            await message.answer("📉 Сначала введите здание, по которому нужно проверить сделку.", reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "top_active"):
            await message.answer(tr(user_id, "loading"))
            rows = get_top_active()
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
            rows = get_top_price()
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
            rows = smart_pick_candidates(state.get("smart_goal"), state.get("smart_budget"), state.get("smart_risk"), state.get("smart_timing"))
            response = show_smart_recommendation(state.get("smart_goal"), state.get("smart_budget"), state.get("smart_timing"), state.get("smart_risk"), rows)
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
                state["step"] = "choose_report"
                user_states[user_id] = state
                text = tr(user_id, "last_deals")
            else:
                state["step"] = "choose_report"
                user_states[user_id] = state
                await message.answer(tr(user_id, "choose_report"), reply_markup=report_menu(user_id))
                return

        if state.get("step") == "choose_report":
            scope = state.get("scope", "dubai")
            name = state.get("name")
            prop = state.get("property")
            period = state.get("period")
            deal_type = state.get("deal_type")

            if text == tr(user_id, "full_report"):
                await message.answer(tr(user_id, "loading"))
                row = get_stats(scope, name, prop, period, deal_type)
                title = "🌆 <b>Статистика Дубая</b>"
                if scope == "building":
                    title = f"🏢 <b>{name}</b>"
                elif scope == "area":
                    title = f"🏙 <b>{name}</b>"

                await message.answer(show_stats(title, row, prop, period, deal_type), reply_markup=report_menu(user_id))
                return

            if text == "💼 Экономическое резюме":
                if scope == "dubai":
                    await message.answer("Сначала выберите конкретное здание или район.", reply_markup=main_menu(user_id))
                    return
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
                current, previous = get_comparison(scope, name, prop, period, deal_type)
                title = "📈 <b>Сравнение периодов</b>"
                if name:
                    title += f"\n{name}"
                await message.answer(show_comparison(title, current, previous), reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "last_deals"):
                await message.answer(tr(user_id, "loading"))
                rows = get_latest_deals(scope, name, prop, period, deal_type)
                if not rows:
                    await message.answer("❌ Нет сделок по выбранным фильтрам.", reply_markup=report_menu(user_id))
                    return

                response = "🧾 <b>Последние сделки</b>\n"
                if name:
                    response += f"📍 {name}\n"
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
        await message.answer(tr(user_id, "error"), reply_markup=main_menu(user_id))


async def main():
    print("Dubai DLD Analytics Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
