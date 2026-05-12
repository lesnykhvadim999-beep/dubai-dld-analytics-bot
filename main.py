from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

from dotenv import load_dotenv

import asyncio
import os
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

user_languages = {}
user_states = {}

TABLE = "public.dld_transactions_full"


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
        "main_menu": "🏠 Главное меню.\n\nВыберите раздел:",
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
        "enter_building": "🏢 Введите название здания.\n\nМожно полностью или частично:\n• Grande\n• Marina\n• Sobha\n• Anantara\n• JVC\n• Downtown",
        "enter_area": "🏙 Введите название района.\n\nНапример:\n• Business Bay\n• Downtown Dubai\n• Dubai Marina\n• JVC",
        "not_found": "❌ Ничего не найдено. Попробуйте другое название.",
        "choose_building": "🔎 <b>Найдено несколько вариантов.</b>\n\nВыберите нужное здание:",
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
        "error": "⚠️ Возникла ошибка при расчёте. Я уже вижу, где искать, сэр.",
    },
    "en": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nChoose language:",
        "lang_selected": "✅ Language selected: <b>English</b>\n\nMain menu:",
        "main_menu": "🏠 Main menu.\n\nChoose section:",
        "view_deals": "📊 View deals",
        "area_stats": "🏙 Area statistics",
        "dubai_stats": "🌆 Dubai statistics",
        "top_active": "🚀 Top active buildings",
        "top_price": "💰 Top by average price",
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
        "enter_building": "🏢 Enter building name.\n\nFull or partial:\n• Grande\n• Marina\n• Sobha\n• Anantara\n• JVC\n• Downtown",
        "enter_area": "🏙 Enter area name.\n\nExample:\n• Business Bay\n• Downtown Dubai\n• Dubai Marina\n• JVC",
        "not_found": "❌ Nothing found. Try another name.",
        "choose_building": "🔎 <b>Several options found.</b>\n\nChoose building:",
        "choose_property": "🏠 Choose property type / bedrooms:",
        "choose_period": "📅 Choose period:",
        "choose_report": "📊 What to show?",
        "full_report": "📊 Full analytics",
        "last_deals": "🧾 Latest deals",
        "period_compare": "📈 Period comparison",
        "undervalued": "📉 Check undervalued deal",
        "enter_price": "💰 Enter property price in AED.\n\nExample: 2500000",
        "enter_size": "📐 Enter property size in sq.ft.\n\nExample: 850",
        "loading": "⏳ Calculating DLD analytics...",
        "error": "⚠️ Calculation error.",
    },
    "ar": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nاختر اللغة:",
        "lang_selected": "✅ تم اختيار اللغة: <b>العربية</b>\n\nالقائمة الرئيسية:",
        "main_menu": "🏠 القائمة الرئيسية.\n\nاختر القسم:",
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
        "enter_building": "🏢 اكتب اسم المبنى.\n\nكامل أو جزئي:\n• Grande\n• Marina\n• Sobha\n• Anantara\n• JVC\n• Downtown",
        "enter_area": "🏙 اكتب اسم المنطقة.\n\nمثال:\n• Business Bay\n• Downtown Dubai\n• Dubai Marina\n• JVC",
        "not_found": "❌ لا توجد نتائج. جرب اسماً آخر.",
        "choose_building": "🔎 <b>تم العثور على عدة خيارات.</b>\n\nاختر المبنى:",
        "choose_property": "🏠 اختر نوع العقار / الغرف:",
        "choose_period": "📅 اختر الفترة:",
        "choose_report": "📊 ماذا تريد أن ترى؟",
        "full_report": "📊 تحليل كامل",
        "last_deals": "🧾 آخر الصفقات",
        "period_compare": "📈 مقارنة الفترات",
        "undervalued": "📉 فحص فرصة أقل من السوق",
        "enter_price": "💰 أدخل سعر العقار بالدرهم.\n\nمثال: 2500000",
        "enter_size": "📐 أدخل المساحة بالقدم المربع.\n\nمثال: 850",
        "loading": "⏳ يتم حساب تحليلات DLD...",
        "error": "⚠️ حدث خطأ في الحساب.",
    }
}


PROPERTY_OPTIONS = [
    "Studio",
    "1 BR",
    "2 BR",
    "3 BR",
    "4 BR",
    "5 BR+",
    "Apartment",
    "Villa",
    "Townhouse",
    "Penthouse",
    "Office",
    "Shop"
]


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
    return kb([
        ["🇷🇺 Русский"],
        ["🇬🇧 English"],
        ["🇦🇪 العربية"]
    ])


def main_menu(user_id):
    return kb([
        [tr(user_id, "view_deals")],
        [tr(user_id, "area_stats"), tr(user_id, "dubai_stats")],
        [tr(user_id, "top_active"), tr(user_id, "top_price")],
        [tr(user_id, "building_search"), tr(user_id, "settings")]
    ])


def back_menu(user_id):
    return kb([[tr(user_id, "back"), tr(user_id, "main")]])


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


def report_menu(user_id):
    return kb([
        [tr(user_id, "full_report")],
        [tr(user_id, "period_compare"), tr(user_id, "last_deals")],
        [tr(user_id, "undervalued")],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def format_money(value):
    if value is None:
        return "нет данных"
    return f"{float(value):,.0f} AED".replace(",", " ")


def format_pct(value):
    if value is None:
        return "нет данных"
    sign = "+" if float(value) > 0 else ""
    return f"{sign}{float(value):.1f}%"


def normalize_search_query(query):
    q = query.strip()

    aliases = {
        "jvc": "Jumeirah Village Circle",
        "jlt": "Jumeirah Lakes Towers",
        "downtown": "Downtown Dubai Burj Khalifa",
        "dubai downtown": "Downtown Dubai Burj Khalifa",
        "marina": "Dubai Marina Marsa Dubai",
        "business bay": "Business Bay",
        "palm": "Palm Jumeirah",
        "sobha": "Sobha Hartland",
        "creek": "Dubai Creek Harbour Creek",
    }

    return aliases.get(q.lower(), q)


def make_search_words(query):
    q = normalize_search_query(query)
    words = [w.strip() for w in q.replace("-", " ").replace("_", " ").split() if len(w.strip()) >= 2]
    return list(dict.fromkeys(words))[:8]


def make_or_search_condition(words, columns):
    conditions = []
    params = []

    for word in words:
        like = f"%{word}%"
        part = "(" + " OR ".join([f"{col} ILIKE %s" for col in columns]) + ")"
        conditions.append(part)
        params.extend([like] * len(columns))

    if not conditions:
        return "AND 1=0", []

    return "AND (" + " OR ".join(conditions) + ")", params


def period_condition(period_key):
    if period_key == "3":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '3 months'"
    if period_key == "6":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if period_key == "12":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if period_key == "36":
        return "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'"
    return ""


def period_previous_condition(period_key):
    if period_key == "3":
        return "AND safe_date < CURRENT_DATE - INTERVAL '3 months' AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if period_key == "6":
        return "AND safe_date < CURRENT_DATE - INTERVAL '6 months' AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if period_key == "12":
        return "AND safe_date < CURRENT_DATE - INTERVAL '12 months' AND safe_date >= CURRENT_DATE - INTERVAL '24 months'"
    if period_key == "36":
        return "AND safe_date < CURRENT_DATE - INTERVAL '36 months' AND safe_date >= CURRENT_DATE - INTERVAL '72 months'"
    return ""


def property_condition(prop):
    if not prop:
        return "", []

    p = prop.lower()

    if p == "studio":
        return "AND (rooms_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%studio%", "%studio%"]

    if p in ["1 br", "2 br", "3 br", "4 br"]:
        rooms = p.split()[0]
        return "AND (rooms_en ILIKE %s OR rooms_en = %s)", [f"%{rooms}%", rooms]

    if p == "5 br+":
        return """
        AND (
            rooms_en ILIKE %s OR rooms_en ILIKE %s OR rooms_en ILIKE %s
            OR rooms_en ILIKE %s OR rooms_en ILIKE %s
        )
        """, ["%5%", "%6%", "%7%", "%8%", "%9%"]

    if p == "villa":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%villa%", "%villa%"]

    if p == "townhouse":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%townhouse%", "%townhouse%"]

    if p == "penthouse":
        return "AND property_sub_type_en ILIKE %s", ["%penthouse%"]

    if p == "apartment":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%apartment%", "%flat%"]

    if p == "office":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%office%", "%office%"]

    if p == "shop":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%shop%", "%shop%"]

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
    words = make_search_words(query)

    if not words:
        return []

    search_sql, search_params = make_or_search_condition(
        words,
        ["building_name_en", "project_name_en", "master_project_en", "area_name_en"]
    )

    params = search_params + [limit]

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals
                {base_from()}
                  AND {BUILDING_NAME} IS NOT NULL
                  {search_sql}
                GROUP BY {BUILDING_NAME}, area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, params)
            return cur.fetchall()


def get_dubai_stats():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    COUNT(DISTINCT {BUILDING_NAME}) AS buildings,
                    COUNT(DISTINCT area_name_en) AS areas,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  AND {PRICE} IS NOT NULL
            """)
            return cur.fetchone()


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


def get_area_stats(area):
    words = make_search_words(area)

    if not words:
        return []

    search_sql, search_params = make_or_search_condition(
        words,
        ["area_name_en", "project_name_en", "master_project_en", "building_name_en"]
    )

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    area_name_en,
                    COUNT(*) AS deals,
                    COUNT(DISTINCT {BUILDING_NAME}) AS buildings,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal
                {base_from()}
                  {search_sql}
                  AND {PRICE} IS NOT NULL
                GROUP BY area_name_en
                ORDER BY deals DESC
                LIMIT 10
            """, search_params)
            return cur.fetchall()


def get_building_report(building, prop=None, period=None):
    prop_sql, prop_args = property_condition(prop)
    p_sql = period_condition(period)

    params = [building] + prop_args

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    {BUILDING_NAME} AS building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    MIN({PRICE}) AS min_price,
                    MAX({PRICE}) AS max_price,
                    AVG({METER_PRICE}) AS avg_meter,
                    MIN(safe_date) AS first_deal,
                    MAX(safe_date) AS last_deal,
                    COUNT(DISTINCT rooms_en) AS rooms_variants,
                    STRING_AGG(DISTINCT NULLIF(rooms_en, ''), ', ') AS rooms_list,
                    STRING_AGG(DISTINCT NULLIF(property_type_en, ''), ', ') AS property_types,
                    STRING_AGG(DISTINCT NULLIF(property_sub_type_en, ''), ', ') AS property_sub_types
                {base_from()}
                  AND {BUILDING_NAME} = %s
                  AND {PRICE} IS NOT NULL
                  {prop_sql}
                  {p_sql}
                GROUP BY {BUILDING_NAME}, area_name_en
                LIMIT 1
            """, params)
            return cur.fetchone()


def get_period_comparison(building, prop=None, period=None):
    if not period:
        return None

    prop_sql, prop_args = property_condition(prop)

    current_params = [building] + prop_args
    previous_params = [building] + prop_args

    current_condition = period_condition(period)
    previous_condition = period_previous_condition(period)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  AND {BUILDING_NAME} = %s
                  AND {PRICE} IS NOT NULL
                  {prop_sql}
                  {current_condition}
            """, current_params)
            current = cur.fetchone()

            cur.execute(f"""
                SELECT
                    COUNT(*) AS deals,
                    AVG({PRICE}) AS avg_price,
                    AVG({METER_PRICE}) AS avg_meter
                {base_from()}
                  AND {BUILDING_NAME} = %s
                  AND {PRICE} IS NOT NULL
                  {prop_sql}
                  {previous_condition}
            """, previous_params)
            previous = cur.fetchone()

    return current, previous


def get_latest_deals(building, prop=None, period=None, limit=5):
    prop_sql, prop_args = property_condition(prop)
    p_sql = period_condition(period)

    params = [building] + prop_args + [limit]

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
                  AND {BUILDING_NAME} = %s
                  AND {PRICE} IS NOT NULL
                  {prop_sql}
                  {p_sql}
                ORDER BY safe_date DESC NULLS LAST
                LIMIT %s
            """, params)
            return cur.fetchall()


def compare_value(building, price, size, prop=None, period=None):
    report = get_building_report(building, prop, period)
    if not report or not report["avg_price"]:
        return None

    market_avg = float(report["avg_price"])
    user_price = float(price)
    diff = user_price - market_avg
    diff_pct = (diff / market_avg) * 100 if market_avg else 0

    user_ppsqft = user_price / float(size) if float(size) else None

    return {
        "report": report,
        "user_price": user_price,
        "user_ppsqft": user_ppsqft,
        "market_avg": market_avg,
        "diff": diff,
        "diff_pct": diff_pct
    }


def pct_change(current, previous):
    if previous is None or previous == 0 or current is None:
        return None
    return ((float(current) - float(previous)) / float(previous)) * 100


def show_report(row, prop=None, period=None):
    if not row:
        return "❌ Нет данных по выбранным фильтрам."

    return (
        f"🏢 <b>{row['building_name_en']}</b>\n"
        f"📍 Район: <b>{row['area_name_en']}</b>\n\n"
        f"Фильтры:\n"
        f"🏠 Тип/комнаты: <b>{prop or 'все'}</b>\n"
        f"📅 Период: <b>{period or 'всё время'}</b>\n\n"
        f"📊 Сделок: <b>{row['deals']}</b>\n"
        f"💰 Средняя цена: <b>{format_money(row['avg_price'])}</b>\n"
        f"🔻 Минимальная цена: <b>{format_money(row['min_price'])}</b>\n"
        f"🔺 Максимальная цена: <b>{format_money(row['max_price'])}</b>\n"
        f"📐 Средняя цена за метр: <b>{format_money(row['avg_meter'])}</b>\n"
        f"🗓 Первая сделка: <b>{row['first_deal']}</b>\n"
        f"🗓 Последняя сделка: <b>{row['last_deal']}</b>\n\n"
        f"🛏 Комнаты в базе: {row['rooms_list'] or 'нет данных'}\n"
        f"🏗 Типы: {row['property_types'] or 'нет данных'}\n"
        f"🏘 Подтипы: {row['property_sub_types'] or 'нет данных'}"
    )


@dp.message(CommandStart())
async def start_handler(message: Message):
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
            user_states[user_id] = {}
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return

        if text == tr(user_id, "back"):
            user_states[user_id] = {}
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return

        if text == tr(user_id, "settings"):
            await message.answer(tr(user_id, "choose_lang"), reply_markup=language_menu())
            return

        if text == tr(user_id, "building_search"):
            user_states[user_id] = {"step": "building_query"}
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "area_stats"):
            user_states[user_id] = {"step": "area_query"}
            await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
            return

        if text == tr(user_id, "dubai_stats"):
            await message.answer(tr(user_id, "loading"))
            s = get_dubai_stats()
            await message.answer(
                f"🌆 <b>Dubai DLD Market Analytics</b>\n\n"
                f"📊 Сделок в базе: <b>{s['deals']:,}</b>\n"
                f"🏢 Зданий: <b>{s['buildings']:,}</b>\n"
                f"📍 Районов: <b>{s['areas']:,}</b>\n"
                f"💰 Средняя цена сделки: <b>{format_money(s['avg_price'])}</b>\n"
                f"📐 Средняя цена за метр: <b>{format_money(s['avg_meter'])}</b>"
            )
            return

        if text == tr(user_id, "top_active"):
            rows = get_top_active()
            response = "🚀 <b>Топ активных зданий</b>\n\n"
            for i, r in enumerate(rows, 1):
                response += (
                    f"{i}. 🏢 <b>{r['building_name_en']}</b>\n"
                    f"📍 {r['area_name_en']}\n"
                    f"📊 Сделок: {r['deals']}\n"
                    f"💰 Средняя цена: {format_money(r['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(r['avg_meter'])}\n\n"
                )
            await message.answer(response)
            return

        if text == tr(user_id, "top_price"):
            rows = get_top_price()
            response = "💰 <b>Топ зданий по средней цене</b>\n\n"
            for i, r in enumerate(rows, 1):
                response += (
                    f"{i}. 🏢 <b>{r['building_name_en']}</b>\n"
                    f"📍 {r['area_name_en']}\n"
                    f"📊 Сделок: {r['deals']}\n"
                    f"💰 Средняя цена: {format_money(r['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(r['avg_meter'])}\n\n"
                )
            await message.answer(response)
            return

        if state.get("step") == "area_query":
            rows = get_area_stats(text)
            if not rows:
                await message.answer(tr(user_id, "not_found"), reply_markup=main_menu(user_id))
                return

            response = f"🏙 <b>Статистика района:</b> {text}\n\n"
            for r in rows:
                response += (
                    f"📍 <b>{r['area_name_en']}</b>\n"
                    f"🏢 Зданий: {r['buildings']}\n"
                    f"📊 Сделок: {r['deals']}\n"
                    f"💰 Средняя цена: {format_money(r['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(r['avg_meter'])}\n"
                    f"🗓 Первая сделка: {r['first_deal']}\n"
                    f"🗓 Последняя сделка: {r['last_deal']}\n\n"
                )

            user_states[user_id] = {}
            await message.answer(response, reply_markup=main_menu(user_id))
            return

        if state.get("step") == "building_query":
            await message.answer("🔎 Ищу похожие здания...")
            rows = find_buildings(text)

            if not rows:
                await message.answer(tr(user_id, "not_found"), reply_markup=main_menu(user_id))
                user_states[user_id] = {}
                return

            user_states[user_id] = {
                "step": "choose_building",
                "suggestions": [r["building_name_en"] for r in rows]
            }

            buttons = [[r["building_name_en"]] for r in rows]
            buttons.append([tr(user_id, "back"), tr(user_id, "main")])

            response = tr(user_id, "choose_building") + "\n\n"
            for i, r in enumerate(rows, 1):
                response += f"{i}. {r['building_name_en']} — {r['area_name_en']} ({r['deals']} сделок)\n"

            await message.answer(response, reply_markup=kb(buttons))
            return

        if state.get("step") == "choose_building":
            if text not in state.get("suggestions", []):
                await message.answer("Выберите здание кнопкой из списка.")
                return

            state["building"] = text
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

            state["step"] = "choose_report"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_report"), reply_markup=report_menu(user_id))
            return

        if state.get("step") == "choose_report":
            building = state.get("building")
            prop = state.get("property")
            period = state.get("period")

            if text == tr(user_id, "full_report"):
                await message.answer(tr(user_id, "loading"))
                row = get_building_report(building, prop, period)
                await message.answer(show_report(row, prop, period), reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "last_deals"):
                rows = get_latest_deals(building, prop, period)
                if not rows:
                    await message.answer("❌ Нет сделок по выбранным фильтрам.", reply_markup=report_menu(user_id))
                    return

                response = f"🧾 <b>Последние сделки:</b>\n🏢 {building}\n\n"
                for r in rows:
                    response += (
                        f"🗓 {r['safe_date']}\n"
                        f"🏠 {r['rooms_en'] or '-'} / {r['property_sub_type_en'] or r['property_type_en'] or '-'}\n"
                        f"💰 {format_money(r['price'])}\n"
                        f"📐 {format_money(r['meter_price'])} за метр\n\n"
                    )
                await message.answer(response, reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "period_compare"):
                if not period:
                    await message.answer("Для сравнения выберите период: 3 мес / 6 мес / 1 год / 3 года.", reply_markup=period_menu(user_id))
                    state["step"] = "choose_period"
                    user_states[user_id] = state
                    return

                current, previous = get_period_comparison(building, prop, period)

                price_change = pct_change(current["avg_price"], previous["avg_price"])
                meter_change = pct_change(current["avg_meter"], previous["avg_meter"])
                deals_change = pct_change(current["deals"], previous["deals"])

                response = (
                    f"📈 <b>Сравнение периодов</b>\n"
                    f"🏢 {building}\n"
                    f"🏠 Фильтр: {prop or 'все'}\n\n"
                    f"<b>Текущий период:</b>\n"
                    f"📊 Сделок: {current['deals']}\n"
                    f"💰 Средняя цена: {format_money(current['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(current['avg_meter'])}\n\n"
                    f"<b>Предыдущий период:</b>\n"
                    f"📊 Сделок: {previous['deals']}\n"
                    f"💰 Средняя цена: {format_money(previous['avg_price'])}\n"
                    f"📐 Цена за метр: {format_money(previous['avg_meter'])}\n\n"
                    f"<b>Динамика:</b>\n"
                    f"📊 Сделки: {format_pct(deals_change)}\n"
                    f"💰 Цена: {format_pct(price_change)}\n"
                    f"📐 Цена за метр: {format_pct(meter_change)}"
                )
                await message.answer(response, reply_markup=report_menu(user_id))
                return

            if text == tr(user_id, "undervalued"):
                state["step"] = "enter_price"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_price"), reply_markup=back_menu(user_id))
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
                state["building"],
                state["user_price"],
                size,
                state.get("property"),
                state.get("period")
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
                f"🏢 {state['building']}\n"
                f"🏠 Фильтр: {state.get('property') or 'все'}\n\n"
                f"💰 Цена объекта: <b>{format_money(result['user_price'])}</b>\n"
                f"📐 Цена объекта за sqft: <b>{format_money(result['user_ppsqft'])}</b>\n"
                f"📊 Средняя цена DLD: <b>{format_money(result['market_avg'])}</b>\n"
                f"📌 Отклонение: <b>{format_pct(diff_pct)}</b>\n\n"
                f"{verdict}"
            )

            user_states[user_id] = {}
            await message.answer(response, reply_markup=main_menu(user_id))
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
