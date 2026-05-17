from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart

from dotenv import load_dotenv

import asyncio
import os
import re
import math
import time
import traceback
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import Optional, List, Dict, Tuple, Any


# ============================================================
# Dubai DLD Intelligence Bot — main.py vNext Ultra
# ------------------------------------------------------------
# Architecture:
# 1) Telegram bot UI / UX here.
# 2) Heavy economic analytics come from INTELLIGENCE DB:
#    - building_roi_summary
#    - area_roi_summary
#    - building_period_comparison
#    - area_period_comparison
#    - market_period_summary
#    - investment_recommendations
# 3) Latest deals are read from ARCHIVE + LIVE raw DLD databases.
# 4) No Telegram polling in updater. Only this file runs Telegram bot.
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

LIVE_DATABASE_URL = (
    os.getenv("LIVE_DATABASE_URL")
    or os.getenv("DLD_TRANSACTIONS_URL")
    or DATABASE_URL
)

ARCHIVE_DATABASE_URL = (
    os.getenv("ARCHIVE_DATABASE_URL")
    or os.getenv("ARCHIVE_DB_URL")
    or DATABASE_URL
)

INTELLIGENCE_DATABASE_URL = (
    os.getenv("INTELLIGENCE_DATABASE_URL")
    or os.getenv("INTELLIGENCE_DB_URL")
    or DATABASE_URL
)

LEAD_BOT_USERNAME = os.getenv("LEAD_BOT_USERNAME", "dubai_fpr_lead_bot")
LEAD_BOT_URL = f"https://t.me/{LEAD_BOT_USERNAME.lstrip('@')}"
LEAD_COOLDOWN_SECONDS = int(os.getenv("LEAD_COOLDOWN_SECONDS", "600"))

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set()
for _admin_id in ADMIN_IDS_RAW.replace(";", ",").split(","):
    _admin_id = _admin_id.strip()
    if _admin_id.isdigit():
        ADMIN_IDS.add(int(_admin_id))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL and not LIVE_DATABASE_URL and not ARCHIVE_DATABASE_URL and not INTELLIGENCE_DATABASE_URL:
    raise RuntimeError("At least one database URL must be set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

user_languages: Dict[int, str] = {}
user_states: Dict[int, Dict[str, Any]] = {}
recent_message_guard: Dict[Tuple[int, str], float] = {}

LEAD_BUTTON_TEXT_RU = "💼 Получить консультацию"
ADMIN_BUTTON_TEXT = "👑 Admin Dashboard"

SMART_GOALS = [
    "💰 Passive Cashflow",
    "🏠 Personal Living",
    "🔁 Resale / Flip",
    "🏖 Short-Term Rental",
    "🔑 Long-Term Rental",
    "📈 Capital Appreciation",
    "💎 Luxury Preservation",
    "⚖️ Balanced Investment",
]

SMART_HORIZONS = ["1 year", "3 years", "5 years", "10 years"]
SMART_RISKS = ["🟢 Low Risk", "🟡 Balanced", "🔴 Aggressive"]

SALE_TABLES = [
    ("archive", ARCHIVE_DATABASE_URL, "public", "dld_sale_archive"),
    ("live", LIVE_DATABASE_URL, "public", "dld_transactions_full"),
]

RENT_TABLES = [
    ("archive", ARCHIVE_DATABASE_URL, "public", "dld_rent_archive"),
    ("live", LIVE_DATABASE_URL, "public", "dld_rents_full"),
]

COLUMN_CANDIDATES = {
    "date": [
        "transaction_date", "instance_date", "procedure_date", "date",
        "registration_date", "contract_start_date", "contract_end_date"
    ],
    "area": [
        "area_name_en", "area_en", "area_name", "area", "master_project_en",
        "master_project", "community"
    ],
    "building": [
        "building_name_en", "building_en", "building_name", "building",
        "project_name_en", "project_en", "project_name", "project",
        "master_project_en", "property_name"
    ],
    "project": [
        "project_name_en", "project_en", "project_name", "project",
        "master_project_en", "master_project"
    ],
    "property_type": [
        "property_type_en", "prop_type_en", "property_type", "property_usage_en",
        "property_usage"
    ],
    "unit_type": [
        "property_sub_type_en", "prop_sub_type_en", "property_sub_type",
        "unit_type_en", "unit_type", "property_type_en", "prop_type_en",
        "property_type"
    ],
    "rooms": ["rooms_en", "rooms", "bedrooms", "bedroom", "beds", "room"],
    "unit": [
        "unit_number", "unit_no", "unit", "property_number", "property_no",
        "property_id", "property_number_en", "unit_number_en"
    ],
    "size_sqm": [
        "actual_area", "procedure_area", "area_sqm", "property_size_sqm",
        "size_sqm", "property_area", "area"
    ],
    "size_sqft": [
        "area_sqft", "property_size_sqft", "size_sqft", "built_up_area_sqft",
        "bua_sqft"
    ],
    "price": [
        "actual_worth", "amount", "price", "sale_price", "value",
        "transaction_amount", "procedure_value", "trans_value"
    ],
    "rent": [
        "annual_amount", "contract_amount", "rent_value", "rent_amount",
        "amount", "price", "annual_rent", "contract_value"
    ],
    "procedure": [
        "procedure_name_en", "procedure_name", "procedure", "transaction_type_en",
        "transaction_type", "reg_type_en"
    ],
    "transaction_id": ["transaction_id", "transaction_number", "contract_id", "id"],
    "parking": ["has_parking", "parking"],
    "freehold": ["is_free_hold", "freehold"],
    "offplan": ["is_offplan", "offplan"],
    "nearest_metro": ["nearest_metro_en", "nearest_metro"],
    "nearest_mall": ["nearest_mall_en", "nearest_mall"],
    "nearest_landmark": ["nearest_landmark_en", "nearest_landmark"],
}

AREA_ALIASES = {
    "jvc": ["JVC", "Jumeirah Village Circle", "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jumeirah village circle": ["Jumeirah Village Circle", "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "dubai marina": ["Dubai Marina", "Marsa Dubai"],
    "marina": ["Dubai Marina", "Marsa Dubai"],
    "downtown": ["Downtown Dubai", "Burj Khalifa"],
    "downtown dubai": ["Downtown Dubai", "Burj Khalifa"],
    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
    "jlt": ["Jumeirah Lakes Towers", "JLT"],
    "sobha": ["Sobha Hartland", "Sobha"],
}


# ============================================================
# DB helpers
# ============================================================

def conn(url: str):
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def intel_conn():
    return conn(INTELLIGENCE_DATABASE_URL)


def clean_query(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def norm(value: Optional[str]) -> str:
    return clean_query(value).lower()


def fmt_int(value) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", " ")
    except Exception:
        return str(value or 0)


def fmt_money(value, suffix="AED") -> str:
    try:
        if value is None:
            return "нет данных"
        return f"{float(value):,.0f} {suffix}".replace(",", " ")
    except Exception:
        return str(value)


def fmt_num(value, digits=1) -> str:
    try:
        if value is None:
            return "нет данных"
        return f"{float(value):,.{digits}f}".replace(",", " ")
    except Exception:
        return str(value)


def fmt_pct(value, digits=1, pp=False) -> str:
    try:
        if value is None:
            return "нет данных"
        sign = "+" if float(value) > 0 else ""
        suffix = " п.п." if pp else "%"
        return f"{sign}{float(value):.{digits}f}{suffix}"
    except Exception:
        return "нет данных"


def safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def table_exists(db_url: str, schema: str, table: str) -> bool:
    try:
        with conn(db_url) as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema=%s AND table_name=%s
                    ) AS ok
                    """,
                    (schema, table),
                )
                return bool(cur.fetchone()["ok"])
    except Exception:
        return False


_COLUMN_CACHE: Dict[Tuple[str, str, str], List[str]] = {}


def table_columns(db_url: str, schema: str, table: str) -> List[str]:
    key = (db_url, schema, table)
    if key in _COLUMN_CACHE:
        return _COLUMN_CACHE[key]

    try:
        with conn(db_url) as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s
                    ORDER BY ordinal_position
                    """,
                    (schema, table),
                )
                cols = [r["column_name"] for r in cur.fetchall()]
                _COLUMN_CACHE[key] = cols
                return cols
    except Exception as e:
        print("COLUMN_DETECT_ERROR:", schema, table, repr(e))
        return []


def pick_col(columns: List[str], logical: str) -> Optional[str]:
    low = {c.lower(): c for c in columns}
    for cand in COLUMN_CANDIDATES.get(logical, []):
        if cand.lower() in low:
            return low[cand.lower()]
    return None


def q(col: str) -> str:
    return '"' + str(col).replace('"', '""') + '"'


def text_expr(columns: List[str], logical: str, fallback="''") -> str:
    col = pick_col(columns, logical)
    if not col:
        return fallback
    return f"NULLIF(TRIM(COALESCE({q(col)}::text, '')), '')"


def num_expr(columns: List[str], logical: str, fallback="NULL::numeric") -> str:
    col = pick_col(columns, logical)
    if not col:
        return fallback
    return f"NULLIF(REGEXP_REPLACE(COALESCE({q(col)}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


def date_expr(columns: List[str]) -> str:
    col = pick_col(columns, "date")
    if not col:
        return "NULL::date"
    return f"""
        CASE
            WHEN NULLIF(TRIM({q(col)}::text), '') IS NULL THEN NULL::date
            ELSE NULLIF(TRIM({q(col)}::text), '')::date
        END
    """


def latest_deals_sql(columns: List[str], schema: str, table: str, deal_type: str) -> str:
    amount_logical = "price" if deal_type == "sale" else "rent"
    amount = num_expr(columns, amount_logical)
    sqm = num_expr(columns, "size_sqm")
    sqft = num_expr(columns, "size_sqft")

    sqft_final = f"""
        CASE
            WHEN {sqft} IS NOT NULL AND {sqft} > 0 THEN {sqft}
            WHEN {sqm} IS NOT NULL AND {sqm} > 0 THEN {sqm} * 10.7639
            ELSE NULL
        END
    """

    psf = f"""
        CASE
            WHEN ({sqft_final}) IS NOT NULL AND ({sqft_final}) > 0 AND ({amount}) IS NOT NULL
            THEN ({amount}) / ({sqft_final})
            ELSE NULL
        END
    """

    return f"""
        SELECT
            '{deal_type}'::text AS deal_type,
            {date_expr(columns)} AS deal_date,
            {text_expr(columns, 'area')} AS area_name,
            {text_expr(columns, 'building')} AS building_name,
            {text_expr(columns, 'project')} AS project_name,
            {text_expr(columns, 'property_type')} AS property_type,
            {text_expr(columns, 'unit_type')} AS unit_type,
            {text_expr(columns, 'rooms')} AS rooms,
            {text_expr(columns, 'unit', 'NULL')} AS unit_number,
            ({sqm})::numeric AS size_sqm,
            ({sqft_final})::numeric AS size_sqft,
            ({amount})::numeric AS amount,
            ({psf})::numeric AS price_psf,
            {text_expr(columns, 'procedure')} AS procedure_name,
            {text_expr(columns, 'transaction_id', 'NULL')} AS source_transaction_id,
            {text_expr(columns, 'parking', 'NULL')} AS parking,
            {text_expr(columns, 'freehold', 'NULL')} AS freehold,
            {text_expr(columns, 'offplan', 'NULL')} AS offplan
        FROM "{schema}"."{table}"
        WHERE ({amount}) IS NOT NULL
          AND ({amount}) > 0
    """


def fetch_raw_latest_deals(
    deal_type: str,
    building: Optional[str] = None,
    area: Optional[str] = None,
    property_type: Optional[str] = None,
    unit_segment: Optional[str] = None,
    offset: int = 0,
    limit: int = 10,
) -> List[dict]:
    sources = SALE_TABLES if deal_type == "sale" else RENT_TABLES
    all_rows = []

    for source_name, db_url, schema, table in sources:
        if not db_url or not table_exists(db_url, schema, table):
            continue

        cols = table_columns(db_url, schema, table)
        if not cols:
            continue

        base_sql = latest_deals_sql(cols, schema, table, deal_type)
        where = []
        params = []

        if building:
            where.append("""
                (
                    LOWER(COALESCE(building_name::text, '')) ILIKE %s
                    OR LOWER(COALESCE(project_name::text, '')) ILIKE %s
                )
            """)
            b = f"%{norm(building)}%"
            params += [b, b]

        if area:
            aliases = AREA_ALIASES.get(norm(area), [area])
            parts = []
            for alias in aliases:
                parts.append("LOWER(COALESCE(area_name::text, '')) ILIKE %s")
                params.append(f"%{norm(alias)}%")
            where.append("(" + " OR ".join(parts) + ")")

        if property_type and property_type != "All":
            where.append("LOWER(COALESCE(property_type::text, '') || ' ' || COALESCE(unit_type::text, '')) ILIKE %s")
            params.append(f"%{norm(property_type)}%")

        if unit_segment and unit_segment != "All":
            seg = norm(unit_segment)
            if seg == "studio":
                where.append("LOWER(COALESCE(rooms::text, '') || ' ' || COALESCE(unit_type::text, '')) ILIKE %s")
                params.append("%studio%")
            elif re.match(r"^\d+br$", seg):
                n = seg.replace("br", "")
                where.append("""
                    (
                        LOWER(COALESCE(rooms::text, '')) = %s
                        OR LOWER(COALESCE(rooms::text, '') || ' ' || COALESCE(unit_type::text, '')) ILIKE %s
                        OR LOWER(COALESCE(rooms::text, '') || ' ' || COALESCE(unit_type::text, '')) ILIKE %s
                    )
                """)
                params += [n, f"%{n} br%", f"%{n} bedroom%"]

        sql = f"SELECT * FROM ({base_sql}) x"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY deal_date DESC NULLS LAST LIMIT %s OFFSET %s"
        params += [limit + 30, 0]

        try:
            with conn(db_url) as c:
                with c.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    for r in rows:
                        r["source_db"] = source_name
                    all_rows.extend(rows)
        except Exception as e:
            print("LATEST_DEALS_SOURCE_ERROR:", source_name, table, repr(e))

    # Dedupe and strict filter after merge.
    seen = set()
    clean = []
    for r in sorted(all_rows, key=lambda x: str(x.get("deal_date") or ""), reverse=True):
        key = (
            str(r.get("source_transaction_id") or ""),
            str(r.get("deal_date") or ""),
            str(r.get("building_name") or ""),
            str(r.get("amount") or ""),
            str(r.get("size_sqft") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        clean.append(r)

    return clean[offset:offset + limit]


# ============================================================
# Telegram UI
# ============================================================

WELCOME = """🏙 <b>Dubai DLD Intelligence Terminal</b>

<b>RU</b>
Профессиональный AI‑аналитик рынка недвижимости Дубая на базе DLD:
• ROI, rental yield и инвестиционный скоринг
• Аналитика зданий и районов
• Сравнение периодов 30 / 90 / 180 / 365 дней
• Продажи и аренда строго отдельно
• Последние сделки archive + live
• Лучшие здания по доходности и ликвидности
• Smart Investment AI: что купить, где выгоднее и почему
• Premium economic conclusions — как у профессионального аналитика

<b>EN</b>
A professional Dubai real estate intelligence terminal powered by DLD data:
• ROI, rental yield and investment scoring
• Building and area analytics
• 30 / 90 / 180 / 365 day period comparison
• Sale and rent separated strictly
• Latest transactions from archive + live data
• Best buildings by yield, liquidity and momentum
• Smart Investment AI with professional economic conclusions

Выберите язык / Choose language:"""

TEXTS = {
    "ru": {
        "main_menu": "🏛 <b>Главное меню</b>\n\nВыберите аналитический модуль:",
        "lang_selected": "✅ Язык выбран: <b>Русский</b>\n\nСистема готова, сэр.",
        "back": "⬅️ Назад",
        "main": "🏛 Главное меню",
        "settings": "⚙️ Настройки",
        "loading": "⏳ Загружаю DLD intelligence...",
        "not_found": "❌ Данных по этому запросу не найдено.",
        "enter_building": "🏢 <b>Введите название здания</b>\n\nНапример:\n• Grande\n• Marina Gate\n• Binghatti Corner\n• Address Opera",
        "enter_area": "🏙 <b>Введите район</b>\n\nНапример:\n• JVC\n• Dubai Marina\n• Business Bay\n• Downtown",
        "choose_property": "🏠 <b>Выберите тип недвижимости:</b>",
        "choose_rooms": "🛏 <b>Выберите комнатность / сегмент:</b>",
        "choose_deal": "📊 <b>Выберите тип сделки:</b>",
        "choose_period": "📈 <b>Выберите период сравнения:</b>",
        "enter_budget": "💰 Введите бюджет в AED.\n\nНапример: 2500000",
    },
    "en": {
        "main_menu": "🏛 <b>Main menu</b>\n\nChoose analytics module:",
        "lang_selected": "✅ Language selected: <b>English</b>",
        "back": "⬅️ Back",
        "main": "🏛 Main menu",
        "settings": "⚙️ Settings",
        "loading": "⏳ Loading DLD intelligence...",
        "not_found": "❌ No data found for this request.",
        "enter_building": "🏢 <b>Enter building name</b>\n\nExamples:\n• Grande\n• Marina Gate\n• Binghatti Corner\n• Address Opera",
        "enter_area": "🏙 <b>Enter area</b>\n\nExamples:\n• JVC\n• Dubai Marina\n• Business Bay\n• Downtown",
        "choose_property": "🏠 <b>Choose property type:</b>",
        "choose_rooms": "🛏 <b>Choose rooms / segment:</b>",
        "choose_deal": "📊 <b>Choose deal type:</b>",
        "choose_period": "📈 <b>Choose comparison period:</b>",
        "enter_budget": "💰 Enter budget in AED.\n\nExample: 2500000",
    },
    "ar": {
        "main_menu": "🏛 <b>القائمة الرئيسية</b>\n\nاختر وحدة التحليل:",
        "lang_selected": "✅ تم اختيار اللغة: <b>العربية</b>",
        "back": "⬅️ رجوع",
        "main": "🏛 القائمة الرئيسية",
        "settings": "⚙️ الإعدادات",
        "loading": "⏳ جاري تحميل تحليلات DLD...",
        "not_found": "❌ لا توجد بيانات لهذا الطلب.",
        "enter_building": "🏢 <b>اكتب اسم المبنى</b>",
        "enter_area": "🏙 <b>اكتب اسم المنطقة</b>",
        "choose_property": "🏠 <b>اختر نوع العقار:</b>",
        "choose_rooms": "🛏 <b>اختر الغرف / الفئة:</b>",
        "choose_deal": "📊 <b>اختر نوع الصفقة:</b>",
        "choose_period": "📈 <b>اختر فترة المقارنة:</b>",
        "enter_budget": "💰 أدخل الميزانية بالدرهم.",
    },
}


def lang(user_id: int) -> str:
    return user_languages.get(user_id, "ru")


def tr(user_id: int, key: str) -> str:
    return TEXTS.get(lang(user_id), TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))


def kb(rows: List[List[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True,
        input_field_placeholder="Dubai DLD Intelligence"
    )


def language_menu():
    return kb([["🇷🇺 Русский"], ["🇬🇧 English"], ["🇦🇪 العربية"]])


def main_menu(user_id: int):
    rows = [
        ["🧠 Smart Investment AI"],
        ["🏢 Building Intelligence", "🏙 Area Intelligence"],
        ["📈 Period Comparison", "🔥 Market Momentum"],
        ["💰 Best ROI", "🏆 Top Recommendations"],
        ["🧾 Latest Deals", "📉 Check Deal Value"],
        ["🏘 Villas / Townhouses", "💼 Commercial / Plots"],
        [LEAD_BUTTON_TEXT_RU],
    ]
    if is_admin(user_id):
        rows.append([ADMIN_BUTTON_TEXT])
    rows.append([tr(user_id, "settings")])
    return kb(rows)


def back_main_menu(user_id: int):
    return kb([[tr(user_id, "back"), tr(user_id, "main")]])


def property_menu(user_id: int):
    return kb([
        ["Apartment", "Villa"],
        ["Townhouse", "Commercial"],
        ["Office", "Retail"],
        ["Plot", "All"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def rooms_menu(user_id: int):
    return kb([
        ["Studio", "1BR", "2BR"],
        ["3BR", "4BR", "5BR+"],
        ["Penthouse", "All"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def deal_menu(user_id: int):
    return kb([
        ["🏠 Sale", "🔑 Rent"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def period_menu(user_id: int):
    return kb([
        ["30d", "90d"],
        ["180d", "365d"],
        [tr(user_id, "back"), tr(user_id, "main")]
    ])


def latest_action_menu(user_id: int, has_next=True):
    rows = []
    if has_next:
        rows.append(["➡️ Следующие 10"])
    rows.append([tr(user_id, "back"), tr(user_id, "main")])
    return kb(rows)


def reset(user_id: int):
    user_states[user_id] = {}


def push_state(user_id: int, state: Dict[str, Any]):
    old = user_states.get(user_id, {})
    history = old.get("history", [])
    if old.get("step"):
        history.append({k: v for k, v in old.items() if k != "history"})
    state["history"] = history
    user_states[user_id] = state


def go_back(user_id: int) -> Dict[str, Any]:
    st = user_states.get(user_id, {})
    hist = st.get("history", [])
    if hist:
        prev = hist.pop()
        prev["history"] = hist
        user_states[user_id] = prev
        return prev
    reset(user_id)
    return {}


# ============================================================
# Intelligence queries
# ============================================================

def intel_query_one(sql: str, params=None) -> Optional[dict]:
    try:
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute(sql, params or [])
                return cur.fetchone()
    except Exception as e:
        print("INTEL_ONE_ERROR:", repr(e))
        return None


def intel_query_all(sql: str, params=None) -> List[dict]:
    try:
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute(sql, params or [])
                return cur.fetchall()
    except Exception as e:
        print("INTEL_ALL_ERROR:", repr(e))
        return []


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def ensure_bot_stats_tables():
    try:
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        full_name TEXT,
                        language TEXT,
                        first_seen TIMESTAMP DEFAULT NOW(),
                        last_seen TIMESTAMP DEFAULT NOW(),
                        last_lead_at TIMESTAMP,
                        message_count BIGINT DEFAULT 0
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_events (
                        id BIGSERIAL PRIMARY KEY,
                        user_id BIGINT,
                        event_type TEXT,
                        event_text TEXT,
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_created ON bot_events(created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_events_user ON bot_events(user_id)")
            c.commit()
    except Exception as e:
        print("BOT_STATS_TABLE_ERROR:", repr(e))


def track_event(message: Message, event_type: str = "message", event_text: Optional[str] = None):
    try:
        user = message.from_user
        if not user:
            return
        ensure_bot_stats_tables()
        full_name = " ".join([x for x in [user.first_name, user.last_name] if x])
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_users (user_id, username, full_name, language, first_seen, last_seen, message_count)
                    VALUES (%s, %s, %s, %s, NOW(), NOW(), 1)
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        full_name = EXCLUDED.full_name,
                        language = EXCLUDED.language,
                        last_seen = NOW(),
                        message_count = bot_users.message_count + 1
                    """,
                    (user.id, user.username, full_name, user_languages.get(user.id, "ru")),
                )
                cur.execute(
                    """
                    INSERT INTO bot_events (user_id, event_type, event_text, created_at)
                    VALUES (%s, %s, %s, NOW())
                    """,
                    (user.id, event_type, event_text or message.text or ""),
                )
            c.commit()
    except Exception as e:
        print("TRACK_EVENT_ERROR:", repr(e))


def lead_allowed(user_id: int) -> Tuple[bool, int]:
    try:
        ensure_bot_stats_tables()
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM (NOW() - last_lead_at)) AS diff FROM bot_users WHERE user_id=%s", [user_id])
                row = cur.fetchone()
                if not row or row.get("diff") is None:
                    return True, 0
                diff = int(row.get("diff") or 0)
                if diff >= LEAD_COOLDOWN_SECONDS:
                    return True, 0
                return False, LEAD_COOLDOWN_SECONDS - diff
    except Exception as e:
        print("LEAD_ALLOWED_ERROR:", repr(e))
        return True, 0


def mark_lead_shown(user_id: int):
    try:
        ensure_bot_stats_tables()
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute("UPDATE bot_users SET last_lead_at=NOW() WHERE user_id=%s", [user_id])
                cur.execute("INSERT INTO bot_events (user_id, event_type, event_text, created_at) VALUES (%s, 'lead_cta', %s, NOW())", [user_id, LEAD_BOT_USERNAME])
            c.commit()
    except Exception as e:
        print("MARK_LEAD_ERROR:", repr(e))


def lead_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💼 Получить консультацию / Get consultation", url=LEAD_BOT_URL)]]
    )


async def send_result_with_cta(message: Message, text: str, reply_markup=None):
    user_id = message.from_user.id
    await message.answer(text, reply_markup=reply_markup or main_menu(user_id))
    allowed, wait_seconds = lead_allowed(user_id)
    if allowed:
        mark_lead_shown(user_id)
        await message.answer(
            "💼 <b>Хотите разобрать объект с брокером?</b>\nОставьте заявку — команда First Place Realtor свяжется с вами.",
            reply_markup=lead_inline_keyboard(),
        )


def admin_stats_text() -> str:
    ensure_bot_stats_tables()
    try:
        with intel_conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM bot_users")
                total_users = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_users WHERE last_seen::date = CURRENT_DATE")
                active_today = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_users WHERE last_seen >= NOW() - INTERVAL '30 days'")
                active_30d = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_events WHERE created_at::date = CURRENT_DATE")
                events_today = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_events WHERE created_at >= NOW() - INTERVAL '30 days'")
                events_30d = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_events WHERE event_type='lead_cta' AND created_at::date = CURRENT_DATE")
                leads_today = cur.fetchone()["c"]
                cur.execute("SELECT COUNT(*) AS c FROM bot_events WHERE event_type='lead_cta' AND created_at >= NOW() - INTERVAL '30 days'")
                leads_30d = cur.fetchone()["c"]
                cur.execute("""
                    SELECT event_text, COUNT(*) AS c
                    FROM bot_events
                    WHERE created_at >= NOW() - INTERVAL '30 days'
                      AND event_text IS NOT NULL
                      AND event_text <> ''
                    GROUP BY event_text
                    ORDER BY c DESC
                    LIMIT 8
                """)
                popular = cur.fetchall()
        text = (
            "👑 <b>Admin Dashboard</b>\n\n"
            f"👥 Total users: <b>{fmt_int(total_users)}</b>\n"
            f"📅 Active today: <b>{fmt_int(active_today)}</b>\n"
            f"🗓 Active 30 days: <b>{fmt_int(active_30d)}</b>\n\n"
            f"⚙️ Actions today: <b>{fmt_int(events_today)}</b>\n"
            f"📊 Actions 30 days: <b>{fmt_int(events_30d)}</b>\n\n"
            f"💼 Lead CTA today: <b>{fmt_int(leads_today)}</b>\n"
            f"📈 Lead CTA 30 days: <b>{fmt_int(leads_30d)}</b>\n\n"
            "🔥 <b>Popular actions / queries 30d:</b>\n"
        )
        for i, r in enumerate(popular, 1):
            val = str(r.get("event_text") or "")[:60]
            text += f"{i}. {val} — <b>{fmt_int(r.get('c'))}</b>\n"
        return text
    except Exception as e:
        return f"⚠️ Admin stats error: {repr(e)}"


def smart_goal_menu(user_id: int):
    return kb([
        ["💰 Passive Cashflow", "📈 Capital Appreciation"],
        ["🏖 Short-Term Rental", "🔑 Long-Term Rental"],
        ["🔁 Resale / Flip", "🏠 Personal Living"],
        ["💎 Luxury Preservation", "⚖️ Balanced Investment"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def smart_horizon_menu(user_id: int):
    return kb([
        ["1 year", "3 years"],
        ["5 years", "10 years"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def smart_risk_menu(user_id: int):
    return kb([
        ["🟢 Low Risk"],
        ["🟡 Balanced"],
        ["🔴 Aggressive"],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def adjust_smart_score(row: dict, goal: str, horizon: str, risk: str) -> float:
    base = safe_float(row.get("investment_score")) or 0
    roi = safe_float(row.get("gross_roi_percent")) or 0
    liquidity = safe_float(row.get("liquidity_score")) or 0
    avg_price = safe_float(row.get("avg_sale_price")) or 0
    score = base

    g = norm(goal)
    r = norm(risk)
    h = norm(horizon)

    if "cashflow" in g or "long-term" in g or "short-term" in g:
        score += min(20, max(0, roi - 5) * 4)
    if "resale" in g or "capital" in g or "flip" in g:
        score += min(15, liquidity / 8)
    if "living" in g or "luxury" in g:
        score += min(10, liquidity / 12)
    if "low risk" in r:
        score += min(15, liquidity / 7)
        if roi < 4:
            score -= 8
    elif "aggressive" in r:
        score += min(15, max(0, roi - 6) * 3)
    if "1 year" in h:
        score += min(10, liquidity / 10)
    elif "5" in h or "10" in h:
        score += min(10, base / 10)

    return max(0, min(100, score))


def search_buildings(qtext: str, limit=10) -> List[dict]:
    qv = f"%{clean_query(qtext)}%"
    return intel_query_all(
        """
        SELECT
            area_name,
            building_name,
            MAX(investment_score) AS investment_score,
            MAX(gross_roi_percent) AS gross_roi_percent,
            SUM(sales_count) AS sales_count,
            SUM(rents_count) AS rents_count
        FROM building_roi_summary
        WHERE building_name ILIKE %s
        GROUP BY area_name, building_name
        ORDER BY investment_score DESC NULLS LAST, (SUM(sales_count)+SUM(rents_count)) DESC
        LIMIT %s
        """,
        [qv, limit],
    )


def search_areas(qtext: str, limit=10) -> List[dict]:
    aliases = AREA_ALIASES.get(norm(qtext), [qtext])
    parts = []
    params = []
    for a in aliases:
        parts.append("area_name ILIKE %s")
        params.append(f"%{a}%")

    sql = f"""
        SELECT
            area_name,
            MAX(investment_score) AS investment_score,
            MAX(gross_roi_percent) AS gross_roi_percent,
            SUM(sales_count) AS sales_count,
            SUM(rents_count) AS rents_count
        FROM area_roi_summary
        WHERE {" OR ".join(parts)}
        GROUP BY area_name
        ORDER BY investment_score DESC NULLS LAST, (SUM(sales_count)+SUM(rents_count)) DESC
        LIMIT %s
    """
    params.append(limit)
    return intel_query_all(sql, params)


def building_summary(building: str, property_type=None, unit_segment=None) -> List[dict]:
    where = ["building_name ILIKE %s"]
    params = [f"%{building}%"]

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        if unit_segment == "5BR+":
            where.append("unit_segment IN ('5BR','6BR','7BR','8BR','9BR','10BR')")
        else:
            where.append("unit_segment ILIKE %s")
            params.append(f"%{unit_segment}%")

    return intel_query_all(
        f"""
        SELECT *
        FROM building_roi_summary
        WHERE {" AND ".join(where)}
        ORDER BY investment_score DESC NULLS LAST, sales_count DESC NULLS LAST
        LIMIT 8
        """,
        params,
    )


def area_summary(area: str, property_type=None, unit_segment=None) -> List[dict]:
    aliases = AREA_ALIASES.get(norm(area), [area])
    parts = []
    params = []
    for a in aliases:
        parts.append("area_name ILIKE %s")
        params.append(f"%{a}%")

    where = ["(" + " OR ".join(parts) + ")"]

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        if unit_segment == "5BR+":
            where.append("unit_segment IN ('5BR','6BR','7BR','8BR','9BR','10BR')")
        else:
            where.append("unit_segment ILIKE %s")
            params.append(f"%{unit_segment}%")

    return intel_query_all(
        f"""
        SELECT *
        FROM area_roi_summary
        WHERE {" AND ".join(where)}
        ORDER BY investment_score DESC NULLS LAST, sales_count DESC NULLS LAST
        LIMIT 10
        """,
        params,
    )


def building_period(building: str, period_code: str, property_type=None, unit_segment=None) -> List[dict]:
    where = ["building_name ILIKE %s", "period_code=%s"]
    params = [f"%{building}%", period_code]

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        where.append("unit_segment ILIKE %s")
        params.append(f"%{unit_segment}%")

    return intel_query_all(
        f"""
        SELECT *
        FROM building_period_comparison
        WHERE {" AND ".join(where)}
        ORDER BY momentum_score DESC NULLS LAST
        LIMIT 8
        """,
        params,
    )


def area_period(area: str, period_code: str, property_type=None, unit_segment=None) -> List[dict]:
    aliases = AREA_ALIASES.get(norm(area), [area])
    parts = []
    params = []
    for a in aliases:
        parts.append("area_name ILIKE %s")
        params.append(f"%{a}%")

    where = ["(" + " OR ".join(parts) + ")", "period_code=%s"]
    params.append(period_code)

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        where.append("unit_segment ILIKE %s")
        params.append(f"%{unit_segment}%")

    return intel_query_all(
        f"""
        SELECT *
        FROM area_period_comparison
        WHERE {" AND ".join(where)}
        ORDER BY momentum_score DESC NULLS LAST
        LIMIT 10
        """,
        params,
    )


def top_recommendations(limit=10, property_type=None, unit_segment=None) -> List[dict]:
    where = ["1=1"]
    params = []

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        where.append("unit_segment ILIKE %s")
        params.append(f"%{unit_segment}%")

    params.append(limit)

    return intel_query_all(
        f"""
        SELECT *
        FROM investment_recommendations
        WHERE {" AND ".join(where)}
        ORDER BY investment_score DESC NULLS LAST
        LIMIT %s
        """,
        params,
    )


def best_roi(limit=10, property_type=None, unit_segment=None) -> List[dict]:
    where = ["gross_roi_percent IS NOT NULL"]
    params = []

    if property_type and property_type != "All":
        where.append("property_type ILIKE %s")
        params.append(f"%{property_type}%")

    if unit_segment and unit_segment != "All":
        where.append("unit_segment ILIKE %s")
        params.append(f"%{unit_segment}%")

    params.append(limit)

    return intel_query_all(
        f"""
        SELECT *
        FROM building_roi_summary
        WHERE {" AND ".join(where)}
        ORDER BY gross_roi_percent DESC NULLS LAST, investment_score DESC NULLS LAST
        LIMIT %s
        """,
        params,
    )


def market_momentum(period_code="90d", limit=10) -> List[dict]:
    return intel_query_all(
        """
        SELECT *
        FROM area_period_comparison
        WHERE period_code=%s
        ORDER BY momentum_score DESC NULLS LAST, volume_change_percent DESC NULLS LAST
        LIMIT %s
        """,
        [period_code, limit],
    )


# ============================================================
# Report formatting
# ============================================================

def score_icon(value) -> str:
    x = safe_float(value)
    if x is None:
        return "⚪"
    if x >= 75:
        return "🟢"
    if x >= 55:
        return "🟡"
    return "🔴"


def deal_icon(deal_type: str) -> str:
    return "🏠" if deal_type == "sale" else "🔑"


def format_building_report(building: str, rows: List[dict]) -> str:
    if not rows:
        return "❌ Нет intelligence-данных по этому зданию. Возможно, updater ещё не пересчитал аналитику."

    best = rows[0]
    text = (
        f"🏢 <b>Building Intelligence</b>\n"
        f"<b>{best.get('building_name') or building}</b>\n"
        f"📍 {best.get('area_name') or '-'}\n\n"
        f"🏆 <b>Лучший сегмент по данным DLD:</b>\n"
        f"🏠 {best.get('property_type')} / {best.get('unit_segment')}\n"
        f"💰 Медианная цена: <b>{fmt_money(best.get('median_sale_price'))}</b>\n"
        f"📐 Медиана за sqft: <b>{fmt_money(best.get('median_sale_psf'))}</b>\n"
        f"🔑 Медианная аренда: <b>{fmt_money(best.get('median_rent'))}</b>\n"
        f"📈 Gross ROI: <b>{fmt_pct(best.get('gross_roi_percent'))}</b>\n"
        f"💎 Investment score: <b>{fmt_num(best.get('investment_score'), 0)}/100</b> {score_icon(best.get('investment_score'))}\n"
        f"💧 Liquidity: <b>{fmt_num(best.get('liquidity_score'), 0)}/100</b>\n"
        f"📊 Выборка: {fmt_int(best.get('sales_count'))} sales / {fmt_int(best.get('rents_count'))} rents\n\n"
        f"🧠 <b>Professional conclusion:</b>\n{best.get('economic_conclusion') or 'нет данных'}\n"
    )

    if len(rows) > 1:
        text += "\n📋 <b>Другие сегменты:</b>\n"
        for i, r in enumerate(rows[1:6], 2):
            text += (
                f"{i}. {r.get('property_type')} / {r.get('unit_segment')} — "
                f"ROI {fmt_pct(r.get('gross_roi_percent'))}, "
                f"score {fmt_num(r.get('investment_score'), 0)}/100, "
                f"price {fmt_money(r.get('median_sale_price'))}\n"
            )

    return text


def format_area_report(area: str, rows: List[dict]) -> str:
    if not rows:
        return "❌ Нет intelligence-данных по этому району."

    best = rows[0]
    text = (
        f"🏙 <b>Area Intelligence</b>\n"
        f"<b>{best.get('area_name') or area}</b>\n\n"
        f"🏆 <b>Лучший сегмент района:</b>\n"
        f"🏠 {best.get('property_type')} / {best.get('unit_segment')}\n"
        f"💰 Медианная цена: <b>{fmt_money(best.get('median_sale_price'))}</b>\n"
        f"📐 Медиана за sqft: <b>{fmt_money(best.get('median_sale_psf'))}</b>\n"
        f"🔑 Медианная аренда: <b>{fmt_money(best.get('median_rent'))}</b>\n"
        f"📈 Gross ROI: <b>{fmt_pct(best.get('gross_roi_percent'))}</b>\n"
        f"💎 Investment score: <b>{fmt_num(best.get('investment_score'), 0)}/100</b> {score_icon(best.get('investment_score'))}\n"
        f"💧 Liquidity: <b>{fmt_num(best.get('liquidity_score'), 0)}/100</b>\n"
        f"📊 Выборка: {fmt_int(best.get('sales_count'))} sales / {fmt_int(best.get('rents_count'))} rents\n\n"
        f"🧠 <b>Professional conclusion:</b>\n{best.get('economic_conclusion') or 'нет данных'}\n"
    )

    if len(rows) > 1:
        text += "\n📋 <b>Лучшие альтернативы в районе:</b>\n"
        for i, r in enumerate(rows[1:8], 2):
            text += (
                f"{i}. {r.get('property_type')} / {r.get('unit_segment')} — "
                f"ROI {fmt_pct(r.get('gross_roi_percent'))}, "
                f"score {fmt_num(r.get('investment_score'), 0)}/100\n"
            )

    return text


def format_period_report(title: str, rows: List[dict], scope="building") -> str:
    if not rows:
        return "❌ Нет данных для сравнения периодов."

    r = rows[0]
    text = (
        f"📈 <b>Period Comparison</b>\n"
        f"{title}\n"
        f"Период: <b>{r.get('period_code')}</b>\n"
        f"Сегмент: <b>{r.get('property_type')} / {r.get('unit_segment')}</b>\n\n"
        f"📊 <b>Сделки</b>\n"
        f"Sales: {fmt_int(r.get('current_sales_count'))} сейчас / {fmt_int(r.get('previous_sales_count'))} ранее\n"
        f"Rents: {fmt_int(r.get('current_rents_count'))} сейчас / {fmt_int(r.get('previous_rents_count'))} ранее\n"
        f"Volume change: <b>{fmt_pct(r.get('volume_change_percent'))}</b>\n\n"
        f"💰 <b>Цена</b>\n"
        f"Median sale: {fmt_money(r.get('current_median_sale_price'))} / {fmt_money(r.get('previous_median_sale_price'))}\n"
        f"Change: <b>{fmt_pct(r.get('sale_price_change_percent'))}</b>\n"
        f"PSF change: <b>{fmt_pct(r.get('sale_psf_change_percent'))}</b>\n\n"
        f"🔑 <b>Аренда и ROI</b>\n"
        f"Median rent: {fmt_money(r.get('current_median_rent'))} / {fmt_money(r.get('previous_median_rent'))}\n"
        f"Rent change: <b>{fmt_pct(r.get('rent_change_percent'))}</b>\n"
        f"ROI: {fmt_pct(r.get('current_roi_percent'))} / {fmt_pct(r.get('previous_roi_percent'))}\n"
        f"ROI change: <b>{fmt_pct(r.get('roi_change_pp'), pp=True)}</b>\n\n"
        f"🌡 Momentum: <b>{fmt_num(r.get('momentum_score'), 0)}/100</b> — {r.get('trend_label') or '-'}\n\n"
        f"🧠 <b>Professional conclusion:</b>\n{r.get('professional_conclusion') or 'нет данных'}"
    )

    if len(rows) > 1:
        text += "\n\n📋 <b>Другие сегменты:</b>\n"
        for i, x in enumerate(rows[1:6], 2):
            text += (
                f"{i}. {x.get('property_type')} / {x.get('unit_segment')} — "
                f"momentum {fmt_num(x.get('momentum_score'), 0)}/100, "
                f"price {fmt_pct(x.get('sale_price_change_percent'))}, "
                f"rent {fmt_pct(x.get('rent_change_percent'))}\n"
            )

    return text


def format_recommendations(rows: List[dict], title="🏆 Top Recommendations") -> str:
    if not rows:
        return "❌ Пока нет investment recommendations. Проверьте, что intelligence_updater уже отработал цикл."

    text = f"{title}\n\n"
    for i, r in enumerate(rows, 1):
        text += (
            f"{i}. <b>{r.get('building_name')}</b>\n"
            f"📍 {r.get('area_name')}\n"
            f"🏠 {r.get('property_type')} / {r.get('unit_segment')}\n"
            f"💎 Score: <b>{fmt_num(r.get('investment_score'), 0)}/100</b> {score_icon(r.get('investment_score'))}\n"
            f"📈 ROI: <b>{fmt_pct(r.get('gross_roi_percent'))}</b>\n"
            f"💧 Liquidity: <b>{fmt_num(r.get('liquidity_score'), 0)}/100</b>\n"
            f"💰 Avg price: <b>{fmt_money(r.get('avg_sale_price'))}</b>\n"
            f"🔑 Median rent: <b>{fmt_money(r.get('median_rent'))}</b>\n"
            f"🧠 {r.get('recommendation') or ''}\n\n"
        )
    return text


def format_latest_deals(rows: List[dict], offset: int, deal_type: str, title: str) -> str:
    if not rows:
        return "❌ Сделки не найдены по выбранному фильтру."

    text = (
        f"🧾 <b>Latest Deals</b>\n"
        f"{title}\n"
        f"Показано: <b>{offset + 1}–{offset + len(rows)}</b>\n\n"
    )

    for i, r in enumerate(rows, offset + 1):
        size = r.get("size_sqft")
        amount = r.get("amount")
        psf = r.get("price_psf")

        text += (
            f"<b>{i}. {deal_icon(deal_type)} {str(deal_type).upper()}</b>\n"
            f"🗓 Date: <b>{r.get('deal_date') or '—'}</b>\n"
            f"🏢 Building: <b>{r.get('building_name') or r.get('project_name') or 'not specified'}</b>\n"
            f"🏗 Project: <b>{r.get('project_name') or 'not specified'}</b>\n"
            f"📍 Area: <b>{r.get('area_name') or 'not specified'}</b>\n"
            f"🏠 Type: <b>{r.get('property_type') or 'not specified'}</b>\n"
            f"🧩 Subtype: <b>{r.get('unit_type') or 'not specified'}</b>\n"
            f"🛏 Rooms: <b>{r.get('rooms') or 'not specified in DLD'}</b>\n"
            f"🔢 Unit: <b>{r.get('unit_number') or 'not exposed by DLD dataset'}</b>\n"
            f"📐 Size: <b>{fmt_num(size, 0)} sqft</b>\n"
            f"💰 Amount: <b>{fmt_money(amount)}</b>\n"
            f"📏 Price/sqft: <b>{fmt_money(psf)}</b>\n"
            f"📄 Procedure: <b>{r.get('procedure_name') or '—'}</b>\n"
            f"🗄 Source: <b>{r.get('source_db')}</b>\n\n"
        )

    return text


def format_deal_value_result(row: dict, user_price: float) -> str:
    avg = safe_float(row.get("median_sale_price") or row.get("avg_sale_price"))
    rent = safe_float(row.get("median_rent") or row.get("avg_rent"))
    roi = safe_float(row.get("gross_roi_percent"))
    score = safe_float(row.get("investment_score"))

    diff = None
    if avg:
        diff = (user_price - avg) / avg * 100

    if diff is None:
        verdict = "⚪ Недостаточно данных для точного отклонения от рынка."
    elif diff <= -10:
        verdict = "🟢 Ниже рынка. Потенциально интересная точка входа."
    elif diff <= 3:
        verdict = "🟡 Около рынка. Нужно смотреть вид, этаж, состояние и мотивацию продавца."
    else:
        verdict = "🔴 Выше медианы рынка. Нужен торг или сильное качество объекта."

    yearly = rent or 0
    three = yearly * 3
    six = yearly * 6

    text = (
        f"📉 <b>Deal Value Check</b>\n\n"
        f"🏢 Building: <b>{row.get('building_name')}</b>\n"
        f"📍 Area: <b>{row.get('area_name')}</b>\n"
        f"🏠 Segment: <b>{row.get('property_type')} / {row.get('unit_segment')}</b>\n\n"
        f"💰 Your price: <b>{fmt_money(user_price)}</b>\n"
        f"📊 Market median: <b>{fmt_money(avg)}</b>\n"
        f"📌 Difference: <b>{fmt_pct(diff)}</b>\n"
        f"🔑 Median yearly rent: <b>{fmt_money(rent)}</b>\n"
        f"📈 Gross ROI: <b>{fmt_pct(roi)}</b>\n"
        f"💎 Investment score: <b>{fmt_num(score, 0)}/100</b> {score_icon(score)}\n\n"
        f"💵 <b>Gross rental income estimate</b>\n"
        f"1 year: <b>{fmt_money(yearly)}</b>\n"
        f"3 years: <b>{fmt_money(three)}</b>\n"
        f"6 years: <b>{fmt_money(six)}</b>\n\n"
        f"🧠 <b>Verdict:</b>\n{verdict}\n\n"
        f"📋 <b>Professional note:</b>\n{row.get('economic_conclusion') or 'нет данных'}"
    )
    return text


# ============================================================
# State prompts
# ============================================================

async def prompt_for_state(message: Message, state: Dict[str, Any]):
    user_id = message.from_user.id
    step = state.get("step")

    if step in ["building_query", "latest_building", "period_building", "deal_building"]:
        await message.answer(tr(user_id, "enter_building"), reply_markup=back_main_menu(user_id))
    elif step in ["area_query", "period_area"]:
        await message.answer(tr(user_id, "enter_area"), reply_markup=back_main_menu(user_id))
    elif step == "smart_goal":
        await message.answer("🧠 <b>Smart Investment AI</b>\n\nВыберите цель покупки:", reply_markup=smart_goal_menu(user_id))
    elif step in ["choose_property", "smart_property"]:
        await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
    elif step in ["choose_rooms", "smart_rooms"]:
        await message.answer(tr(user_id, "choose_rooms"), reply_markup=rooms_menu(user_id))
    elif step == "smart_horizon":
        await message.answer("⏳ Выберите горизонт инвестирования:", reply_markup=smart_horizon_menu(user_id))
    elif step == "smart_risk":
        await message.answer("⚖️ Выберите риск-профиль:", reply_markup=smart_risk_menu(user_id))
    elif step == "choose_deal":
        await message.answer(tr(user_id, "choose_deal"), reply_markup=deal_menu(user_id))
    elif step == "choose_period":
        await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
    else:
        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))


# ============================================================
# Handlers
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    track_event(message, "start", "/start")
    reset(user_id)
    await message.answer(WELCOME, reply_markup=language_menu())


@dp.message(lambda m: m.text in ["🇷🇺 Русский", "🇬🇧 English", "🇦🇪 العربية"])
async def language_handler(message: Message):
    user_id = message.from_user.id
    track_event(message, "language", message.text)
    if message.text == "🇷🇺 Русский":
        user_languages[user_id] = "ru"
    elif message.text == "🇬🇧 English":
        user_languages[user_id] = "en"
    else:
        user_languages[user_id] = "ar"

    reset(user_id)
    await message.answer(tr(user_id, "lang_selected"), reply_markup=main_menu(user_id))


@dp.message()
async def main_handler(message: Message):
    user_id = message.from_user.id
    text = clean_query(message.text)
    track_event(message, "message", text)

    # Anti-duplicate guard.
    guard_key = (user_id, text)
    now = time.time()
    if recent_message_guard.get(guard_key, 0) and now - recent_message_guard[guard_key] < 1.2:
        print("DUPLICATE_MESSAGE_IGNORED:", user_id, text)
        return
    recent_message_guard[guard_key] = now

    state = user_states.get(user_id, {})

    try:
        if text == tr(user_id, "main"):
            reset(user_id)
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return

        if text == tr(user_id, "back"):
            prev = go_back(user_id)
            await prompt_for_state(message, prev)
            return

        if text == tr(user_id, "settings"):
            push_state(user_id, {"step": "settings"})
            await message.answer(WELCOME, reply_markup=language_menu())
            return

        if text == LEAD_BUTTON_TEXT_RU:
            allowed, wait_seconds = lead_allowed(user_id)
            if not allowed:
                minutes = max(1, int(wait_seconds // 60) + 1)
                await message.answer(
                    f"⏳ Заявку можно открывать раз в 10 минут. Попробуйте через ~{minutes} мин.",
                    reply_markup=main_menu(user_id)
                )
                return
            mark_lead_shown(user_id)
            await message.answer(
                "💼 <b>Получить консультацию</b>\n\nНажмите кнопку ниже, чтобы перейти в lead-бот First Place Realtor.",
                reply_markup=lead_inline_keyboard()
            )
            return

        if text == ADMIN_BUTTON_TEXT or text == "/admin":
            if not is_admin(user_id):
                await message.answer("⛔️ Admin access denied.", reply_markup=main_menu(user_id))
                return
            await message.answer(admin_stats_text(), reply_markup=main_menu(user_id))
            return

        # ---------------- Main menu ----------------

        if text == "🏢 Building Intelligence":
            push_state(user_id, {"step": "building_query", "mode": "building_intelligence"})
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_main_menu(user_id))
            return

        if text == "🏙 Area Intelligence":
            push_state(user_id, {"step": "area_query", "mode": "area_intelligence"})
            await message.answer(tr(user_id, "enter_area"), reply_markup=back_main_menu(user_id))
            return

        if text == "📈 Period Comparison":
            push_state(user_id, {"step": "period_scope"})
            await message.answer(
                "📈 <b>Period Comparison</b>\n\nЧто сравниваем?",
                reply_markup=kb([["🏢 Building Period", "🏙 Area Period"], [tr(user_id, "back"), tr(user_id, "main")]])
            )
            return

        if text == "🔥 Market Momentum":
            await message.answer(tr(user_id, "loading"))
            rows = market_momentum("90d", 10)
            await send_result_with_cta(message, format_period_list_momentum(rows), reply_markup=main_menu(user_id))
            return

        if text == "💰 Best ROI":
            push_state(user_id, {"step": "best_roi_property"})
            await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
            return

        if text == "🏆 Top Recommendations":
            push_state(user_id, {"step": "top_rec_property"})
            await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
            return

        if text == "🧾 Latest Deals":
            push_state(user_id, {"step": "latest_deal_type"})
            await message.answer(tr(user_id, "choose_deal"), reply_markup=deal_menu(user_id))
            return

        if text == "📉 Check Deal Value":
            push_state(user_id, {"step": "deal_building"})
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_main_menu(user_id))
            return

        if text == "🧠 Smart Investment AI":
            push_state(user_id, {"step": "smart_goal"})
            await message.answer(
                "🧠 <b>Smart Investment AI</b>\n\nВыберите цель покупки. От этого зависит логика отбора: ROI, ликвидность, перепродажа, аренда или сохранение капитала.",
                reply_markup=smart_goal_menu(user_id)
            )
            return

        if text == "🏘 Villas / Townhouses":
            await message.answer(tr(user_id, "loading"))
            rows = top_recommendations(10, property_type="Villa") + top_recommendations(10, property_type="Townhouse")
            rows = sorted(rows, key=lambda r: safe_float(r.get("investment_score")) or 0, reverse=True)[:10]
            await send_result_with_cta(message, format_recommendations(rows, "🏘 <b>Villas / Townhouses Intelligence</b>"), reply_markup=main_menu(user_id))
            return

        if text == "💼 Commercial / Plots":
            await message.answer(tr(user_id, "loading"))
            rows = top_recommendations(10, property_type="Commercial") + top_recommendations(10, property_type="Plot")
            rows = sorted(rows, key=lambda r: safe_float(r.get("investment_score")) or 0, reverse=True)[:10]
            await send_result_with_cta(message, format_recommendations(rows, "💼 <b>Commercial / Plots Intelligence</b>"), reply_markup=main_menu(user_id))
            return

        # ---------------- Building intelligence flow ----------------

        if state.get("step") == "building_query":
            await message.answer("🔎 Ищу здание в intelligence-базе...")
            candidates = search_buildings(text)
            if not candidates:
                await message.answer(tr(user_id, "not_found"), reply_markup=back_main_menu(user_id))
                return

            if len(candidates) == 1:
                building = candidates[0]["building_name"]
                rows = building_summary(building)
                reset(user_id)
                await send_result_with_cta(message, format_building_report(building, rows), reply_markup=main_menu(user_id))
                return

            state["step"] = "choose_building_result"
            state["suggestions"] = [r["building_name"] for r in candidates]
            user_states[user_id] = state

            response = "🔎 <b>Выберите здание:</b>\n\n"
            buttons = []
            for i, r in enumerate(candidates, 1):
                response += (
                    f"{i}. <b>{r.get('building_name')}</b>\n"
                    f"📍 {r.get('area_name')} · ROI {fmt_pct(r.get('gross_roi_percent'))} · score {fmt_num(r.get('investment_score'), 0)}\n"
                )
                buttons.append([r["building_name"]])
            buttons.append([tr(user_id, "back"), tr(user_id, "main")])
            await message.answer(response, reply_markup=kb(buttons))
            return

        if state.get("step") == "choose_building_result":
            if text not in state.get("suggestions", []):
                await message.answer("Выберите здание кнопкой или нажмите Назад.", reply_markup=back_main_menu(user_id))
                return
            rows = building_summary(text)
            reset(user_id)
            await send_result_with_cta(message, format_building_report(text, rows), reply_markup=main_menu(user_id))
            return

        # ---------------- Area intelligence flow ----------------

        if state.get("step") == "area_query":
            await message.answer("🔎 Ищу район в intelligence-базе...")
            rows_found = search_areas(text)
            if not rows_found:
                await message.answer(tr(user_id, "not_found"), reply_markup=back_main_menu(user_id))
                return

            selected = rows_found[0]["area_name"]
            rows = area_summary(selected)
            reset(user_id)
            await send_result_with_cta(message, format_area_report(selected, rows), reply_markup=main_menu(user_id))
            return

        # ---------------- Period comparison ----------------

        if state.get("step") == "period_scope":
            if text == "🏢 Building Period":
                state["step"] = "period_building"
                state["scope"] = "building"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_building"), reply_markup=back_main_menu(user_id))
                return
            if text == "🏙 Area Period":
                state["step"] = "period_area"
                state["scope"] = "area"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_main_menu(user_id))
                return
            await message.answer("Выберите Building Period или Area Period.")
            return

        if state.get("step") in ["period_building", "period_area"]:
            state["name"] = text
            state["step"] = "choose_period"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
            return

        if state.get("step") == "choose_period":
            if text not in ["30d", "90d", "180d", "365d"]:
                await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
                return
            await message.answer(tr(user_id, "loading"))
            name = state.get("name")
            if state.get("scope") == "building":
                rows = building_period(name, text)
                title = f"🏢 <b>{name}</b>"
            else:
                rows = area_period(name, text)
                title = f"🏙 <b>{name}</b>"
            reset(user_id)
            await send_result_with_cta(message, format_period_report(title, rows), reply_markup=main_menu(user_id))
            return

        # ---------------- Best ROI / Top rec filters ----------------

        if state.get("step") in ["best_roi_property", "top_rec_property"]:
            state["property_type"] = text
            state["step"] = "best_roi_rooms" if state.get("step") == "best_roi_property" else "top_rec_rooms"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_rooms"), reply_markup=rooms_menu(user_id))
            return

        if state.get("step") == "best_roi_rooms":
            await message.answer(tr(user_id, "loading"))
            rows = best_roi(10, state.get("property_type"), text)
            reset(user_id)
            await send_result_with_cta(message, format_best_roi(rows), reply_markup=main_menu(user_id))
            return

        if state.get("step") == "top_rec_rooms":
            await message.answer(tr(user_id, "loading"))
            rows = top_recommendations(10, state.get("property_type"), text)
            reset(user_id)
            await send_result_with_cta(message, format_recommendations(rows), reply_markup=main_menu(user_id))
            return

        # ---------------- Latest deals flow ----------------

        if state.get("step") == "latest_deal_type":
            if text == "🏠 Sale":
                state["deal_type"] = "sale"
            elif text == "🔑 Rent":
                state["deal_type"] = "rent"
            else:
                await message.answer(tr(user_id, "choose_deal"), reply_markup=deal_menu(user_id))
                return
            state["step"] = "latest_scope"
            user_states[user_id] = state
            await message.answer(
                "🧾 <b>Latest Deals</b>\n\nКак фильтруем?",
                reply_markup=kb([["🏢 By Building", "🏙 By Area"], ["🌆 Dubai-wide"], [tr(user_id, "back"), tr(user_id, "main")]])
            )
            return

        if state.get("step") == "latest_scope":
            if text == "🏢 By Building":
                state["step"] = "latest_building"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_building"), reply_markup=back_main_menu(user_id))
                return
            if text == "🏙 By Area":
                state["step"] = "latest_area"
                user_states[user_id] = state
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_main_menu(user_id))
                return
            if text == "🌆 Dubai-wide":
                state["building"] = None
                state["area"] = None
                state["offset"] = 0
                state["step"] = "latest_show"
                user_states[user_id] = state
                await show_latest_deals(message, state)
                return
            await message.answer("Выберите фильтр кнопкой.")
            return

        if state.get("step") == "latest_building":
            state["building"] = text
            state["area"] = None
            state["offset"] = 0
            state["step"] = "latest_show"
            user_states[user_id] = state
            await show_latest_deals(message, state)
            return

        if state.get("step") == "latest_area":
            state["area"] = text
            state["building"] = None
            state["offset"] = 0
            state["step"] = "latest_show"
            user_states[user_id] = state
            await show_latest_deals(message, state)
            return

        if state.get("step") == "latest_show":
            if text == "➡️ Следующие 10":
                state["offset"] = int(state.get("offset") or 0) + 10
                user_states[user_id] = state
                await show_latest_deals(message, state)
                return
            await message.answer("Нажмите «Следующие 10» или Главное меню.", reply_markup=latest_action_menu(user_id))
            return

        # ---------------- Deal value check ----------------

        if state.get("step") == "deal_building":
            state["building"] = text
            state["step"] = "deal_property"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
            return

        if state.get("step") == "deal_property":
            state["property_type"] = text
            state["step"] = "deal_rooms"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_rooms"), reply_markup=rooms_menu(user_id))
            return

        if state.get("step") == "deal_rooms":
            state["unit_segment"] = text
            state["step"] = "deal_price"
            user_states[user_id] = state
            await message.answer("💰 Введите цену объекта в AED.\n\nНапример: 2500000", reply_markup=back_main_menu(user_id))
            return

        if state.get("step") == "deal_price":
            try:
                price = float(text.replace(",", "").replace(" ", ""))
            except Exception:
                await message.answer("Введите только число. Например: 2500000")
                return

            building = state.get("building")
            rows = building_summary(building, state.get("property_type"), state.get("unit_segment"))
            reset(user_id)
            if not rows:
                await message.answer("❌ Недостаточно intelligence-данных для оценки сделки.", reply_markup=main_menu(user_id))
                return
            await send_result_with_cta(message, format_deal_value_result(rows[0], price), reply_markup=main_menu(user_id))
            return

        # ---------------- Smart investment ----------------

        if state.get("step") == "smart_goal":
            if text not in SMART_GOALS:
                await message.answer("Выберите цель кнопкой.", reply_markup=smart_goal_menu(user_id))
                return
            state["goal"] = text
            state["step"] = "smart_budget"
            user_states[user_id] = state
            await message.answer(tr(user_id, "enter_budget"), reply_markup=back_main_menu(user_id))
            return

        if state.get("step") == "smart_budget":
            try:
                budget = float(text.replace(",", "").replace(" ", ""))
            except Exception:
                await message.answer(tr(user_id, "enter_budget"), reply_markup=back_main_menu(user_id))
                return
            state["budget"] = budget
            state["step"] = "smart_property"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
            return

        if state.get("step") == "smart_property":
            state["property_type"] = text
            state["step"] = "smart_rooms"
            user_states[user_id] = state
            await message.answer(tr(user_id, "choose_rooms"), reply_markup=rooms_menu(user_id))
            return

        if state.get("step") == "smart_rooms":
            state["unit_segment"] = text
            state["step"] = "smart_horizon"
            user_states[user_id] = state
            await message.answer("⏳ Выберите горизонт инвестирования:", reply_markup=smart_horizon_menu(user_id))
            return

        if state.get("step") == "smart_horizon":
            if text not in SMART_HORIZONS:
                await message.answer("Выберите горизонт кнопкой.", reply_markup=smart_horizon_menu(user_id))
                return
            state["horizon"] = text
            state["step"] = "smart_risk"
            user_states[user_id] = state
            await message.answer("⚖️ Выберите риск-профиль:", reply_markup=smart_risk_menu(user_id))
            return

        if state.get("step") == "smart_risk":
            if text not in SMART_RISKS:
                await message.answer("Выберите риск-профиль кнопкой.", reply_markup=smart_risk_menu(user_id))
                return
            await message.answer(tr(user_id, "loading"))
            state["risk"] = text
            rows = top_recommendations(40, state.get("property_type"), state.get("unit_segment"))
            budget = safe_float(state.get("budget"))
            if budget:
                rows = [r for r in rows if not safe_float(r.get("avg_sale_price")) or safe_float(r.get("avg_sale_price")) <= budget * 1.15]
            for r in rows:
                r["smart_adjusted_score"] = adjust_smart_score(r, state.get("goal"), state.get("horizon"), state.get("risk"))
            rows = sorted(rows, key=lambda r: safe_float(r.get("smart_adjusted_score")) or 0, reverse=True)[:10]
            final_text = format_smart_ai(rows, budget, state.get("goal"), state.get("horizon"), state.get("risk"))
            reset(user_id)
            await send_result_with_cta(message, final_text, reply_markup=main_menu(user_id))
            return

        # Free text fallback: try building search.
        if len(text) >= 2 and not text.startswith("/"):
            candidates = search_buildings(text, 5)
            if candidates:
                buttons = [[r["building_name"]] for r in candidates]
                buttons.append([tr(user_id, "main")])
                push_state(user_id, {"step": "choose_building_result", "suggestions": [r["building_name"] for r in candidates]})
                response = "🔎 Похоже, вы ищете здание:\n\n"
                for i, r in enumerate(candidates, 1):
                    response += f"{i}. <b>{r.get('building_name')}</b> — {r.get('area_name')}\n"
                await message.answer(response, reply_markup=kb(buttons))
                return

        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))

    except Exception as e:
        print("MAIN_HANDLER_ERROR:", repr(e))
        traceback.print_exc()
        await message.answer(
            "⚠️ Произошла ошибка при расчёте. Пришлите Deploy Logs, сэр.",
            reply_markup=main_menu(user_id)
        )


async def show_latest_deals(message: Message, state: Dict[str, Any]):
    user_id = message.from_user.id
    await message.answer(tr(user_id, "loading"))

    rows = fetch_raw_latest_deals(
        deal_type=state.get("deal_type") or "sale",
        building=state.get("building"),
        area=state.get("area"),
        offset=int(state.get("offset") or 0),
        limit=10,
    )

    title_parts = []
    if state.get("building"):
        title_parts.append(f"🏢 {state.get('building')}")
    if state.get("area"):
        title_parts.append(f"📍 {state.get('area')}")
    if not title_parts:
        title_parts.append("🌆 Dubai-wide")

    response = format_latest_deals(rows, int(state.get("offset") or 0), state.get("deal_type") or "sale", " · ".join(title_parts))
    await send_result_with_cta(message, response, reply_markup=latest_action_menu(user_id, has_next=bool(rows)))


def format_best_roi(rows: List[dict]) -> str:
    if not rows:
        return "❌ Нет данных по ROI."
    text = "💰 <b>Best ROI Buildings</b>\n\n"
    for i, r in enumerate(rows, 1):
        text += (
            f"{i}. <b>{r.get('building_name')}</b>\n"
            f"📍 {r.get('area_name')}\n"
            f"🏠 {r.get('property_type')} / {r.get('unit_segment')}\n"
            f"📈 ROI: <b>{fmt_pct(r.get('gross_roi_percent'))}</b>\n"
            f"💎 Score: <b>{fmt_num(r.get('investment_score'), 0)}/100</b>\n"
            f"💰 Median price: <b>{fmt_money(r.get('median_sale_price'))}</b>\n"
            f"🔑 Median rent: <b>{fmt_money(r.get('median_rent'))}</b>\n\n"
        )
    return text


def format_period_list_momentum(rows: List[dict]) -> str:
    if not rows:
        return "❌ Нет данных по market momentum."
    text = "🔥 <b>Market Momentum — 90d</b>\n\n"
    for i, r in enumerate(rows, 1):
        text += (
            f"{i}. <b>{r.get('area_name')}</b>\n"
            f"🏠 {r.get('property_type')} / {r.get('unit_segment')}\n"
            f"🌡 Momentum: <b>{fmt_num(r.get('momentum_score'), 0)}/100</b> — {r.get('trend_label')}\n"
            f"💰 Price: {fmt_pct(r.get('sale_price_change_percent'))}\n"
            f"🔑 Rent: {fmt_pct(r.get('rent_change_percent'))}\n"
            f"📈 ROI: {fmt_pct(r.get('roi_change_pp'), pp=True)}\n\n"
        )
    return text


def format_smart_ai(rows: List[dict], budget: Optional[float], goal: Optional[str] = None, horizon: Optional[str] = None, risk: Optional[str] = None) -> str:
    if not rows:
        return "❌ По этим параметрам нет сильных рекомендаций."

    best = rows[0]
    adjusted = best.get("smart_adjusted_score") or best.get("investment_score")
    rent = safe_float(best.get("median_rent")) or 0
    avg_price = safe_float(best.get("avg_sale_price")) or 0
    roi = safe_float(best.get("gross_roi_percent")) or 0

    gross_1y = rent
    gross_3y = rent * 3
    gross_5y = rent * 5

    text = (
        f"🧠 <b>Smart Investment AI — Professional Selection</b>\n\n"
        f"🎯 Goal: <b>{goal or 'Balanced Investment'}</b>\n"
        f"💰 Budget: <b>{fmt_money(budget)}</b>\n"
        f"⏳ Horizon: <b>{horizon or '-'}</b>\n"
        f"⚖️ Risk: <b>{risk or '-'}</b>\n\n"
        f"🏆 <b>Best match:</b>\n"
        f"🏢 <b>{best.get('building_name')}</b>\n"
        f"📍 {best.get('area_name')}\n"
        f"🏠 {best.get('property_type')} / {best.get('unit_segment')}\n"
        f"💎 Smart score: <b>{fmt_num(adjusted, 0)}/100</b> {score_icon(adjusted)}\n"
        f"📈 Gross ROI: <b>{fmt_pct(roi)}</b>\n"
        f"💧 Liquidity: <b>{fmt_num(best.get('liquidity_score'), 0)}/100</b>\n"
        f"💰 Avg price: <b>{fmt_money(avg_price)}</b>\n"
        f"🔑 Median rent: <b>{fmt_money(rent)}</b>\n\n"
        f"💵 <b>Expected gross rental income</b>\n"
        f"1 year: <b>{fmt_money(gross_1y)}</b>\n"
        f"3 years: <b>{fmt_money(gross_3y)}</b>\n"
        f"5 years: <b>{fmt_money(gross_5y)}</b>\n\n"
        f"🧠 <b>Professional recommendation:</b>\n{best.get('reason') or best.get('recommendation') or 'нет данных'}\n"
    )

    if len(rows) > 1:
        text += "\n📋 <b>Alternatives:</b>\n"
        for i, r in enumerate(rows[1:7], 2):
            score = r.get("smart_adjusted_score") or r.get("investment_score")
            text += (
                f"{i}. <b>{r.get('building_name')}</b> · {r.get('area_name')} · "
                f"ROI {fmt_pct(r.get('gross_roi_percent'))} · "
                f"score {fmt_num(score, 0)}/100\n"
            )

    return text


async def main():
    print("=" * 80)
    print("Dubai DLD Intelligence Bot vNext Ultra started")
    print("LIVE_DATABASE_URL source:", "custom" if LIVE_DATABASE_URL != DATABASE_URL else "DATABASE_URL fallback")
    print("ARCHIVE_DATABASE_URL source:", "custom" if ARCHIVE_DATABASE_URL != DATABASE_URL else "DATABASE_URL fallback")
    print("INTELLIGENCE_DATABASE_URL source:", "custom" if INTELLIGENCE_DATABASE_URL != DATABASE_URL else "DATABASE_URL fallback")
    print("Lead bot URL:", LEAD_BOT_URL)
    print("Admin IDs count:", len(ADMIN_IDS))
    ensure_bot_stats_tables()
    print("Bot stats tables ready")
    print("=" * 80)

    # Clear webhook to avoid conflict if Railway restarted after webhook experiments.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Telegram webhook cleared before polling")
    except Exception as e:
        print("Webhook clear warning:", repr(e))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
