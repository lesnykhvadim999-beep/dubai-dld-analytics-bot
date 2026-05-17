# -*- coding: utf-8 -*-
"""
Dubai DLD Intelligence Bot — vNext Ultra Multilingual + PDF Reports

Production goals:
- RU / EN / AR full UI translation
- Archive + Live + Intelligence architecture
- Strict sale/rent separation
- Smart Investment AI
- Building / Area / ROI / Period analytics
- Latest deals with pagination
- Lead CTA with 10 min anti-spam
- Admin dashboard
- PDF export for final analytics and deal lists

ENV required:
BOT_TOKEN
DATABASE_URL or LIVE_DATABASE_URL
ARCHIVE_DATABASE_URL optional
INTELLIGENCE_DATABASE_URL optional
ADMIN_IDS optional, comma-separated telegram IDs
LEAD_BOT_URL optional, default https://t.me/dubai_fpr_lead_bot
"""

import asyncio
import csv
import html
import io
import json
import logging
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("dubai_dld_intelligence_bot")

# =============================================================================
# ENV
# =============================================================================

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

LIVE_DATABASE_URL = (
    os.getenv("LIVE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or ""
).strip()

ARCHIVE_DATABASE_URL = (
    os.getenv("ARCHIVE_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or LIVE_DATABASE_URL
    or ""
).strip()

INTELLIGENCE_DATABASE_URL = (
    os.getenv("INTELLIGENCE_DATABASE_URL")
    or os.getenv("INTEL_DATABASE_URL")
    or os.getenv("DATABASE_URL")
    or LIVE_DATABASE_URL
    or ""
).strip()

if not LIVE_DATABASE_URL:
    raise RuntimeError("DATABASE_URL / LIVE_DATABASE_URL is not set")

LEAD_BOT_URL = (os.getenv("LEAD_BOT_URL") or "https://t.me/dubai_fpr_lead_bot").strip()
LEAD_COOLDOWN_SECONDS = int(os.getenv("LEAD_COOLDOWN_SECONDS") or "600")
ADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS") or "").split(",")
    if x.strip().isdigit()
}

PAGE_SIZE = 10
MAX_REPORT_ROWS = 80

# =============================================================================
# BOT
# =============================================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =============================================================================
# TRANSLATIONS
# =============================================================================

SUPPORTED_LANGS = ("ru", "en", "ar")

I18N: Dict[str, Dict[str, str]] = {
    "ru": {
        "choose_lang": "Выберите язык:",
        "lang_ru": "🇷🇺 Русский",
        "lang_en": "🇬🇧 English",
        "lang_ar": "🇦🇪 العربية",
        "lang_selected": "✅ Язык выбран: <b>Русский</b>",
        "welcome": (
            "🏛 <b>Dubai DLD Intelligence Terminal</b>\n\n"
            "Профессиональная аналитическая система по рынку недвижимости Дубая на основе DLD, архива, live-данных и intelligence-слоя.\n\n"
            "Что умеет бот:\n"
            "• анализировать районы и здания;\n"
            "• считать ROI, доходность, ликвидность и динамику;\n"
            "• сравнивать периоды 30/90/180/365 дней;\n"
            "• показывать последние сделки;\n"
            "• оценивать выгодность конкретной цены;\n"
            "• подбирать инвестиционные варианты под цель, бюджет, риск и горизонт;\n"
            "• формировать PDF-отчёты.\n\n"
            "Select language / Выберите язык:"
        ),
        "main_menu": "🏛 <b>Главное меню</b>\nВыберите модуль аналитики:",
        "smart_ai": "🧠 Инвестиционный подбор",
        "building_intel": "🏢 Аналитика здания",
        "area_intel": "🏙 Аналитика района",
        "period_compare": "📈 Сравнение периодов",
        "market_momentum": "🔥 Рыночный импульс",
        "best_roi": "💰 Лучший ROI",
        "latest_deals": "🧾 Последние сделки",
        "check_deal": "📉 Проверить выгодность",
        "villas": "🏘 Виллы / Таунхаусы",
        "commercial": "💼 Коммерция / Участки",
        "settings": "⚙️ Настройки",
        "admin": "👑 Admin Dashboard",
        "back": "⬅️ Назад",
        "main": "🏠 Главное меню",
        "next10": "➡️ Следующие 10",
        "prev10": "⬅️ Предыдущие 10",
        "consult": "💼 Получить консультацию",
        "download_pdf": "📄 Скачать PDF",
        "enter_building": "🏢 Введите название здания:",
        "enter_area": "🏙 Введите название района:",
        "not_found": "⚠️ Данных по выбранному запросу пока недостаточно. Попробуйте другой район, здание или фильтр.",
        "loading": "⏳ Считаю аналитику по DLD базе...",
        "sale": "Продажа",
        "rent": "Аренда",
        "all_time": "Всё время",
        "month1": "1 месяц",
        "month3": "3 месяца",
        "month6": "6 месяцев",
        "year1": "1 год",
        "choose_period": "🗓 Выберите период:",
        "choose_deal_type": "🔑 Выберите тип сделки:",
        "choose_property_type": "🏘 Выберите тип недвижимости:",
        "choose_rooms": "🛏 Выберите комнатность:",
        "choose_goal": "🎯 Выберите цель покупки:",
        "choose_horizon": "⏳ Выберите горизонт:",
        "choose_risk": "⚖️ Выберите риск-профиль:",
        "enter_budget": "💵 Введите бюджет в AED:",
        "enter_price": "💵 Введите цену объекта в AED:",
        "investment": "Инвестиция",
        "living": "Жить самому",
        "resale": "Перепродажа",
        "short_rent": "Краткосрочная аренда",
        "long_rent": "Долгосрочная аренда",
        "capital_growth": "Рост капитала",
        "cashflow": "Пассивный cashflow",
        "low_risk": "Низкий риск",
        "balanced": "Баланс",
        "aggressive": "Агрессивно",
        "apartment": "Апартаменты",
        "villa": "Вилла",
        "townhouse": "Таунхаус",
        "office": "Офис",
        "retail": "Ритейл",
        "plot": "Участок",
        "studio": "Studio",
        "any": "Любой",
        "report_ready": "📄 PDF-отчёт готов.",
        "lead_cooldown": "⏳ Заявку можно оставить раз в 10 минут. Попробуйте немного позже.",
        "lead_text": "💼 Нажмите кнопку ниже, чтобы перейти в бот консультации:",
        "admin_denied": "⛔️ Доступ только для администратора.",
        "admin_title": "👑 <b>Admin Dashboard</b>",
        "stats_total": "Всего пользователей",
        "stats_today": "Пользователей сегодня",
        "stats_30d": "Пользователей за 30 дней",
        "stats_actions": "Действий всего",
        "stats_leads": "Переходов в заявку",
        "top_queries": "Популярные запросы",
        "pdf_error": "⚠️ Не удалось создать PDF. Проверьте, что установлен reportlab.",
    },
    "en": {
        "choose_lang": "Choose language:",
        "lang_ru": "🇷🇺 Русский",
        "lang_en": "🇬🇧 English",
        "lang_ar": "🇦🇪 العربية",
        "lang_selected": "✅ Language selected: <b>English</b>",
        "welcome": (
            "🏛 <b>Dubai DLD Intelligence Terminal</b>\n\n"
            "Professional Dubai real estate intelligence system powered by DLD, archive data, live data and an intelligence analytics layer.\n\n"
            "The bot can:\n"
            "• analyze areas and buildings;\n"
            "• calculate ROI, yield, liquidity and market dynamics;\n"
            "• compare 30/90/180/365 day periods;\n"
            "• show latest transactions;\n"
            "• check if a deal is undervalued or overpriced;\n"
            "• select investment options by goal, budget, risk and horizon;\n"
            "• generate PDF reports.\n\n"
            "Select language / Выберите язык:"
        ),
        "main_menu": "🏛 <b>Main Menu</b>\nChoose an analytics module:",
        "smart_ai": "🧠 Smart Investment AI",
        "building_intel": "🏢 Building Intelligence",
        "area_intel": "🏙 Area Intelligence",
        "period_compare": "📈 Period Comparison",
        "market_momentum": "🔥 Market Momentum",
        "best_roi": "💰 Best ROI",
        "latest_deals": "🧾 Latest Deals",
        "check_deal": "📉 Check Deal Value",
        "villas": "🏘 Villas / Townhouses",
        "commercial": "💼 Commercial / Plots",
        "settings": "⚙️ Settings",
        "admin": "👑 Admin Dashboard",
        "back": "⬅️ Back",
        "main": "🏠 Main Menu",
        "next10": "➡️ Next 10",
        "prev10": "⬅️ Previous 10",
        "consult": "💼 Get Consultation",
        "download_pdf": "📄 Download PDF",
        "enter_building": "🏢 Enter building name:",
        "enter_area": "🏙 Enter area name:",
        "not_found": "⚠️ Not enough data for this request. Try another area, building or filter.",
        "loading": "⏳ Calculating analytics from DLD data...",
        "sale": "Sale",
        "rent": "Rent",
        "all_time": "All time",
        "month1": "1 month",
        "month3": "3 months",
        "month6": "6 months",
        "year1": "1 year",
        "choose_period": "🗓 Choose period:",
        "choose_deal_type": "🔑 Choose deal type:",
        "choose_property_type": "🏘 Choose property type:",
        "choose_rooms": "🛏 Choose rooms:",
        "choose_goal": "🎯 Choose purchase goal:",
        "choose_horizon": "⏳ Choose horizon:",
        "choose_risk": "⚖️ Choose risk profile:",
        "enter_budget": "💵 Enter budget in AED:",
        "enter_price": "💵 Enter property price in AED:",
        "investment": "Investment",
        "living": "Personal living",
        "resale": "Resale",
        "short_rent": "Short-term rental",
        "long_rent": "Long-term rental",
        "capital_growth": "Capital appreciation",
        "cashflow": "Passive cashflow",
        "low_risk": "Low risk",
        "balanced": "Balanced",
        "aggressive": "Aggressive",
        "apartment": "Apartment",
        "villa": "Villa",
        "townhouse": "Townhouse",
        "office": "Office",
        "retail": "Retail",
        "plot": "Plot",
        "studio": "Studio",
        "any": "Any",
        "report_ready": "📄 PDF report is ready.",
        "lead_cooldown": "⏳ You can submit a consultation request once every 10 minutes. Please try later.",
        "lead_text": "💼 Press the button below to open the consultation bot:",
        "admin_denied": "⛔️ Admin access only.",
        "admin_title": "👑 <b>Admin Dashboard</b>",
        "stats_total": "Total users",
        "stats_today": "Users today",
        "stats_30d": "Users in 30 days",
        "stats_actions": "Total actions",
        "stats_leads": "Lead clicks",
        "top_queries": "Top queries",
        "pdf_error": "⚠️ Could not generate PDF. Please make sure reportlab is installed.",
    },
    "ar": {
        "choose_lang": "اختر اللغة:",
        "lang_ru": "🇷🇺 Русский",
        "lang_en": "🇬🇧 English",
        "lang_ar": "🇦🇪 العربية",
        "lang_selected": "✅ تم اختيار اللغة: <b>العربية</b>",
        "welcome": (
            "🏛 <b>Dubai DLD Intelligence Terminal</b>\n\n"
            "نظام احترافي لتحليل سوق عقارات دبي باستخدام بيانات DLD والأرشيف والبيانات الحية وطبقة ذكاء تحليلية.\n\n"
            "يمكن للبوت:\n"
            "• تحليل المناطق والمباني؛\n"
            "• حساب ROI والعائد والسيولة والحركة السوقية؛\n"
            "• مقارنة فترات 30/90/180/365 يوم؛\n"
            "• عرض أحدث الصفقات؛\n"
            "• تقييم جاذبية السعر؛\n"
            "• اختيار فرص استثمارية حسب الهدف والميزانية والمخاطر؛\n"
            "• إنشاء تقارير PDF.\n\n"
            "Select language / Выберите язык:"
        ),
        "main_menu": "🏛 <b>القائمة الرئيسية</b>\nاختر وحدة التحليل:",
        "smart_ai": "🧠 الذكاء الاستثماري",
        "building_intel": "🏢 تحليل المبنى",
        "area_intel": "🏙 تحليل المنطقة",
        "period_compare": "📈 مقارنة الفترات",
        "market_momentum": "🔥 زخم السوق",
        "best_roi": "💰 أفضل ROI",
        "latest_deals": "🧾 أحدث الصفقات",
        "check_deal": "📉 تقييم الصفقة",
        "villas": "🏘 فلل / تاون هاوس",
        "commercial": "💼 تجاري / أراضي",
        "settings": "⚙️ الإعدادات",
        "admin": "👑 لوحة الإدارة",
        "back": "⬅️ رجوع",
        "main": "🏠 القائمة الرئيسية",
        "next10": "➡️ التالي 10",
        "prev10": "⬅️ السابق 10",
        "consult": "💼 احصل على استشارة",
        "download_pdf": "📄 تحميل PDF",
        "enter_building": "🏢 أدخل اسم المبنى:",
        "enter_area": "🏙 أدخل اسم المنطقة:",
        "not_found": "⚠️ لا توجد بيانات كافية لهذا الطلب. جرّب منطقة أو مبنى أو فلتر آخر.",
        "loading": "⏳ يتم حساب التحليلات من بيانات DLD...",
        "sale": "بيع",
        "rent": "إيجار",
        "all_time": "كل الوقت",
        "month1": "شهر واحد",
        "month3": "3 أشهر",
        "month6": "6 أشهر",
        "year1": "سنة واحدة",
        "choose_period": "🗓 اختر الفترة:",
        "choose_deal_type": "🔑 اختر نوع الصفقة:",
        "choose_property_type": "🏘 اختر نوع العقار:",
        "choose_rooms": "🛏 اختر عدد الغرف:",
        "choose_goal": "🎯 اختر هدف الشراء:",
        "choose_horizon": "⏳ اختر الأفق الزمني:",
        "choose_risk": "⚖️ اختر مستوى المخاطر:",
        "enter_budget": "💵 أدخل الميزانية بالدرهم:",
        "enter_price": "💵 أدخل سعر العقار بالدرهم:",
        "investment": "استثمار",
        "living": "سكن شخصي",
        "resale": "إعادة بيع",
        "short_rent": "إيجار قصير",
        "long_rent": "إيجار طويل",
        "capital_growth": "نمو رأس المال",
        "cashflow": "دخل ثابت",
        "low_risk": "مخاطر منخفضة",
        "balanced": "متوازن",
        "aggressive": "مرتفع المخاطر",
        "apartment": "شقة",
        "villa": "فيلا",
        "townhouse": "تاون هاوس",
        "office": "مكتب",
        "retail": "تجزئة",
        "plot": "أرض",
        "studio": "Studio",
        "any": "أي",
        "report_ready": "📄 تقرير PDF جاهز.",
        "lead_cooldown": "⏳ يمكن طلب الاستشارة مرة كل 10 دقائق.",
        "lead_text": "💼 اضغط الزر أدناه لفتح بوت الاستشارة:",
        "admin_denied": "⛔️ للمشرف فقط.",
        "admin_title": "👑 <b>لوحة الإدارة</b>",
        "stats_total": "إجمالي المستخدمين",
        "stats_today": "مستخدمو اليوم",
        "stats_30d": "مستخدمو 30 يوم",
        "stats_actions": "إجمالي الإجراءات",
        "stats_leads": "ضغطات الاستشارة",
        "top_queries": "أشهر الطلبات",
        "pdf_error": "⚠️ تعذر إنشاء PDF. تأكد من تثبيت reportlab.",
    },
}


def tr(lang: str, key: str) -> str:
    lang = lang if lang in SUPPORTED_LANGS else "ru"
    return I18N.get(lang, I18N["ru"]).get(key, I18N["ru"].get(key, key))


# =============================================================================
# IN-MEMORY STATE
# =============================================================================

USER_STATE: Dict[int, Dict[str, Any]] = {}
LAST_REPORTS: Dict[int, Dict[str, Any]] = {}
LAST_LEAD_TS: Dict[int, float] = {}

# =============================================================================
# DB HELPERS
# =============================================================================

def _connect(url: str):
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def live_conn():
    conn = _connect(LIVE_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def archive_conn():
    conn = _connect(ARCHIVE_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def intel_conn():
    conn = _connect(INTELLIGENCE_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.close()


def execute(conn, sql: str, params: Optional[Sequence[Any]] = None, fetch: bool = True):
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        if fetch:
            return cur.fetchall()
        conn.commit()
        return []


def one(conn, sql: str, params: Optional[Sequence[Any]] = None):
    rows = execute(conn, sql, params, True)
    return rows[0] if rows else None


_SCHEMA_CACHE: Dict[Tuple[str, str], List[str]] = {}


def table_columns(conn, table_name: str, schema: str = "public") -> List[str]:
    key = (schema, table_name)
    if key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[key]
    rows = execute(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        [schema, table_name],
    )
    cols = [r["column_name"] for r in rows]
    _SCHEMA_CACHE[key] = cols
    return cols


def table_exists(conn, table_name: str, schema: str = "public") -> bool:
    r = one(
        conn,
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.tables
          WHERE table_schema=%s AND table_name=%s
        ) AS ok
        """,
        [schema, table_name],
    )
    return bool(r and r["ok"])


def pick_col(cols: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    low = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def fmt_num(v: Any, decimals: int = 0) -> str:
    if v is None:
        return "—"
    try:
        if isinstance(v, Decimal):
            v = float(v)
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return "—"
        if decimals <= 0:
            return f"{f:,.0f}".replace(",", " ")
        return f"{f:,.{decimals}f}".replace(",", " ")
    except Exception:
        return str(v)


def fmt_money(v: Any) -> str:
    n = fmt_num(v, 0)
    return "—" if n == "—" else f"{n} AED"


def fmt_pct(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}%"
    except Exception:
        return str(v)


def clean_text(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v).strip()
    if not s or s.lower() in ("none", "null", "nan", "-", "not specified"):
        return "—"
    return s


def normalize_query(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# =============================================================================
# STATS DB
# =============================================================================

def ensure_stats_tables():
    try:
        with intel_conn() as conn:
            execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language TEXT DEFAULT 'ru',
                    first_seen TIMESTAMP DEFAULT NOW(),
                    last_seen TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bot_actions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    action TEXT,
                    payload TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS bot_leads (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """,
                fetch=False,
            )
            log.info("Bot stats tables ready")
    except Exception as e:
        log.exception("Stats tables init failed: %s", e)


def get_lang(user_id: int) -> str:
    if user_id in USER_STATE and USER_STATE[user_id].get("lang"):
        return USER_STATE[user_id]["lang"]
    try:
        with intel_conn() as conn:
            r = one(conn, "SELECT language FROM bot_users WHERE user_id=%s", [user_id])
            if r and r.get("language") in SUPPORTED_LANGS:
                USER_STATE.setdefault(user_id, {})["lang"] = r["language"]
                return r["language"]
    except Exception:
        pass
    return "ru"


def set_lang(user_id: int, lang: str):
    lang = lang if lang in SUPPORTED_LANGS else "ru"
    USER_STATE.setdefault(user_id, {})["lang"] = lang
    try:
        with intel_conn() as conn:
            execute(conn, "UPDATE bot_users SET language=%s, last_seen=NOW() WHERE user_id=%s", [lang, user_id], False)
    except Exception:
        pass


def register_user(message_or_call: Any):
    try:
        u = message_or_call.from_user
        lang = USER_STATE.get(u.id, {}).get("lang") or "ru"
        with intel_conn() as conn:
            execute(
                conn,
                """
                INSERT INTO bot_users (user_id, username, first_name, last_name, language, first_seen, last_seen)
                VALUES (%s,%s,%s,%s,%s,NOW(),NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    username=EXCLUDED.username,
                    first_name=EXCLUDED.first_name,
                    last_name=EXCLUDED.last_name,
                    last_seen=NOW()
                """,
                [u.id, u.username, u.first_name, u.last_name, lang],
                False,
            )
    except Exception as e:
        log.warning("register_user failed: %s", e)


def log_action(user_id: int, action: str, payload: str = ""):
    try:
        with intel_conn() as conn:
            execute(
                conn,
                "INSERT INTO bot_actions(user_id, action, payload) VALUES(%s,%s,%s)",
                [user_id, action, payload[:1000]],
                False,
            )
    except Exception as e:
        log.warning("log_action failed: %s", e)


# =============================================================================
# KEYBOARDS
# =============================================================================

def kb_language() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=tr("ru", "lang_ru"), callback_data="lang:ru")
    b.button(text=tr("en", "lang_en"), callback_data="lang:en")
    b.button(text=tr("ar", "lang_ar"), callback_data="lang:ar")
    b.adjust(1)
    return b.as_markup()


def kb_main(lang: str, user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    items = [
        ("smart_ai", "m:smart"),
        ("building_intel", "m:building"),
        ("area_intel", "m:area"),
        ("period_compare", "m:period"),
        ("market_momentum", "m:momentum"),
        ("best_roi", "m:roi"),
        ("latest_deals", "m:latest"),
        ("check_deal", "m:check"),
        ("villas", "m:villas"),
        ("commercial", "m:commercial"),
        ("settings", "m:settings"),
    ]
    for key, cb in items:
        b.button(text=tr(lang, key), callback_data=cb)
    if user_id in ADMIN_IDS:
        b.button(text=tr(lang, "admin"), callback_data="admin:home")
    # premium full-width layout
    b.adjust(1, 1, 2, 2, 2, 2, 1, 1)
    return b.as_markup()


def kb_back_main(lang: str, with_pdf: bool = False, with_consult: bool = True) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if with_pdf:
        b.button(text=tr(lang, "download_pdf"), callback_data="pdf:last")
    if with_consult:
        b.button(text=tr(lang, "consult"), callback_data="lead:open")
    b.button(text=tr(lang, "back"), callback_data="m:back")
    b.button(text=tr(lang, "main"), callback_data="m:main")
    b.adjust(1)
    return b.as_markup()


def kb_consult(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text=tr(lang, "consult"), url=LEAD_BOT_URL))
    b.button(text=tr(lang, "main"), callback_data="m:main")
    return b.as_markup()


def kb_deal_type(lang: str, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔑 " + tr(lang, "sale"), callback_data=f"{prefix}:sale")
    b.button(text="🔑 " + tr(lang, "rent"), callback_data=f"{prefix}:rent")
    b.button(text=tr(lang, "back"), callback_data="m:main")
    b.adjust(2, 1)
    return b.as_markup()


def kb_period(lang: str, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    periods = [("30", "month1"), ("90", "month3"), ("180", "month6"), ("365", "year1")]
    for val, key in periods:
        b.button(text=tr(lang, key), callback_data=f"{prefix}:{val}")
    b.button(text=tr(lang, "back"), callback_data="m:main")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_property_type(lang: str, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for val, key in [
        ("apartment", "apartment"),
        ("villa", "villa"),
        ("townhouse", "townhouse"),
        ("office", "office"),
        ("retail", "retail"),
        ("plot", "plot"),
        ("any", "any"),
    ]:
        b.button(text=tr(lang, key), callback_data=f"{prefix}:{val}")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def kb_rooms(lang: str, prefix: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for val in ["studio", "1BR", "2BR", "3BR", "4BR", "5BR+", "any"]:
        text = tr(lang, "studio") if val == "studio" else (tr(lang, "any") if val == "any" else val)
        b.button(text=text, callback_data=f"{prefix}:{val}")
    b.adjust(3, 3, 1)
    return b.as_markup()


def kb_smart_goal(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for val, key in [
        ("investment", "investment"),
        ("living", "living"),
        ("resale", "resale"),
        ("short_rent", "short_rent"),
        ("long_rent", "long_rent"),
        ("capital_growth", "capital_growth"),
        ("cashflow", "cashflow"),
    ]:
        b.button(text=tr(lang, key), callback_data=f"smart_goal:{val}")
    b.button(text=tr(lang, "back"), callback_data="m:main")
    b.adjust(1)
    return b.as_markup()


def kb_horizon(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for val, label in [("1", "1 year"), ("3", "3 years"), ("5", "5 years"), ("10", "10 years")]:
        b.button(text=label, callback_data=f"smart_horizon:{val}")
    b.adjust(2, 2)
    return b.as_markup()


def kb_risk(lang: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for val, key in [("low", "low_risk"), ("balanced", "balanced"), ("aggressive", "aggressive")]:
        b.button(text=tr(lang, key), callback_data=f"smart_risk:{val}")
    b.adjust(1)
    return b.as_markup()


def kb_pagination(lang: str, context: str, page: int, has_next: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text=tr(lang, "prev10"), callback_data=f"page:{context}:{page-1}")
    if has_next:
        b.button(text=tr(lang, "next10"), callback_data=f"page:{context}:{page+1}")
    b.button(text=tr(lang, "download_pdf"), callback_data="pdf:last")
    b.button(text=tr(lang, "consult"), callback_data="lead:open")
    b.button(text=tr(lang, "main"), callback_data="m:main")
    b.adjust(1)
    return b.as_markup()


# =============================================================================
# ANALYTICS QUERY HELPERS
# =============================================================================

def safe_like(q: str) -> str:
    q = normalize_query(q)
    return f"%{q}%"


def select_from_intelligence(table: str, search_col_candidates: Sequence[str], query: str, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        with intel_conn() as conn:
            if not table_exists(conn, table):
                return []
            cols = table_columns(conn, table)
            search_col = pick_col(cols, search_col_candidates)
            if not search_col:
                return []
            # preferred ordering
            order_col = pick_col(cols, ["investment_score", "roi", "gross_roi", "avg_roi", "total_deals", "sales_count", "rent_count"])
            order_sql = f"ORDER BY {sql_ident(order_col)} DESC NULLS LAST" if order_col else ""
            sql = f"""
                SELECT *
                FROM public.{sql_ident(table)}
                WHERE {sql_ident(search_col)} ILIKE %s
                {order_sql}
                LIMIT %s
            """
            return list(execute(conn, sql, [safe_like(query), limit]))
    except Exception as e:
        log.exception("select_from_intelligence failed: %s", e)
        return []


def get_table_count(table: str) -> int:
    try:
        with intel_conn() as conn:
            if not table_exists(conn, table):
                return 0
            r = one(conn, f"SELECT COUNT(*) AS c FROM public.{sql_ident(table)}")
            return int(r["c"] or 0) if r else 0
    except Exception:
        return 0


def row_value(row: Dict[str, Any], candidates: Sequence[str]) -> Any:
    if not row:
        return None
    lower = {str(k).lower(): k for k in row.keys()}
    for c in candidates:
        k = lower.get(c.lower())
        if k is not None:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return None


def format_building_report(lang: str, rows: List[Dict[str, Any]], query: str) -> str:
    if not rows:
        return tr(lang, "not_found")
    r = rows[0]
    building = clean_text(row_value(r, ["building_name", "building", "building_name_en", "project_name", "project_name_en"]))
    area = clean_text(row_value(r, ["area_name", "area", "area_name_en"]))
    prop = clean_text(row_value(r, ["property_type", "property_type_en", "prop_type_en"]))
    rooms = clean_text(row_value(r, ["rooms", "rooms_en", "unit_segment", "unit_type"]))
    avg_sale = row_value(r, ["avg_sale_price", "average_sale_price", "median_sale_price", "sale_price_avg"])
    avg_rent = row_value(r, ["avg_rent", "average_rent", "median_rent", "rent_avg"])
    psf = row_value(r, ["avg_price_per_sqft", "avg_psf", "price_per_sqft", "median_psf"])
    roi = row_value(r, ["roi", "gross_roi", "avg_roi", "yield", "gross_yield"])
    sales = row_value(r, ["sales_count", "total_sales", "sale_count"])
    rents = row_value(r, ["rent_count", "total_rents", "rents_count"])
    score = row_value(r, ["investment_score", "score"])
    liquidity = row_value(r, ["liquidity_score", "liquidity", "liquidity_level"])
    trend = row_value(r, ["trend", "momentum", "market_momentum"])

    if lang == "ru":
        conclusion = (
            "🧠 <b>Экономическое заключение</b>\n"
            f"Объект <b>{html.escape(building)}</b> в районе <b>{html.escape(area)}</b> показывает "
            f"ROI около <b>{fmt_pct(roi)}</b>. "
            f"При средней аренде <b>{fmt_money(avg_rent)}</b> и средней цене продажи <b>{fmt_money(avg_sale)}</b> "
            "здание можно рассматривать как инвестиционный актив, если ликвидность и объём сделок подтверждают стабильный спрос. "
            "Для покупки сильнее всего выглядят сегменты с высоким ROI, достаточным количеством сделок и умеренной ценой за sqft."
        )
        title = "🏢 <b>Аналитика здания</b>"
        labels = {
            "building": "Здание", "area": "Район", "prop": "Тип", "rooms": "Комнаты",
            "avg_sale": "Средняя цена продажи", "avg_rent": "Средняя аренда",
            "psf": "Цена за sqft/м²", "roi": "ROI", "sales": "Сделок продажи",
            "rents": "Сделок аренды", "score": "Investment score", "liquidity": "Ликвидность", "trend": "Тренд"
        }
    elif lang == "ar":
        conclusion = (
            "🧠 <b>الخلاصة الاقتصادية</b>\n"
            f"المبنى <b>{html.escape(building)}</b> في <b>{html.escape(area)}</b> يحقق ROI تقريبي <b>{fmt_pct(roi)}</b>. "
            "كلما زاد حجم الصفقات والسيولة أصبح الأصل أكثر جاذبية للاستثمار."
        )
        title = "🏢 <b>تحليل المبنى</b>"
        labels = {
            "building": "المبنى", "area": "المنطقة", "prop": "النوع", "rooms": "الغرف",
            "avg_sale": "متوسط سعر البيع", "avg_rent": "متوسط الإيجار",
            "psf": "السعر لكل قدم/متر", "roi": "ROI", "sales": "صفقات البيع",
            "rents": "صفقات الإيجار", "score": "درجة الاستثمار", "liquidity": "السيولة", "trend": "الاتجاه"
        }
    else:
        conclusion = (
            "🧠 <b>Professional Economic Conclusion</b>\n"
            f"<b>{html.escape(building)}</b> in <b>{html.escape(area)}</b> shows estimated ROI of <b>{fmt_pct(roi)}</b>. "
            f"With average rent around <b>{fmt_money(avg_rent)}</b> and average sale price around <b>{fmt_money(avg_sale)}</b>, "
            "the asset can be considered investment-grade if liquidity and transaction volume remain strong."
        )
        title = "🏢 <b>Building Intelligence</b>"
        labels = {
            "building": "Building", "area": "Area", "prop": "Type", "rooms": "Rooms",
            "avg_sale": "Average sale price", "avg_rent": "Average rent",
            "psf": "Price per sqft/m²", "roi": "ROI", "sales": "Sales deals",
            "rents": "Rent deals", "score": "Investment score", "liquidity": "Liquidity", "trend": "Trend"
        }

    lines = [
        title,
        "",
        f"🏢 <b>{labels['building']}:</b> {html.escape(building)}",
        f"📍 <b>{labels['area']}:</b> {html.escape(area)}",
        f"🏘 <b>{labels['prop']}:</b> {html.escape(prop)}",
        f"🛏 <b>{labels['rooms']}:</b> {html.escape(rooms)}",
        "",
        f"💰 <b>{labels['avg_sale']}:</b> {fmt_money(avg_sale)}",
        f"🏦 <b>{labels['avg_rent']}:</b> {fmt_money(avg_rent)}",
        f"📐 <b>{labels['psf']}:</b> {fmt_money(psf)}",
        f"📊 <b>{labels['roi']}:</b> {fmt_pct(roi)}",
        "",
        f"🧾 <b>{labels['sales']}:</b> {fmt_num(sales)}",
        f"🔑 <b>{labels['rents']}:</b> {fmt_num(rents)}",
        f"⭐️ <b>{labels['score']}:</b> {fmt_num(score, 1)}",
        f"💧 <b>{labels['liquidity']}:</b> {clean_text(liquidity)}",
        f"🔥 <b>{labels['trend']}:</b> {clean_text(trend)}",
        "",
        conclusion,
    ]
    return "\n".join(lines)


def format_area_report(lang: str, rows: List[Dict[str, Any]], query: str) -> str:
    if not rows:
        return tr(lang, "not_found")
    # aggregate best row + top rows
    r = rows[0]
    area = clean_text(row_value(r, ["area_name", "area", "area_name_en"]))
    avg_roi = row_value(r, ["roi", "gross_roi", "avg_roi", "yield"])
    avg_sale = row_value(r, ["avg_sale_price", "average_sale_price", "median_sale_price"])
    avg_rent = row_value(r, ["avg_rent", "average_rent", "median_rent"])
    sales = row_value(r, ["sales_count", "total_sales"])
    rents = row_value(r, ["rent_count", "total_rents"])
    score = row_value(r, ["investment_score", "score"])

    if lang == "ru":
        title = "🏙 <b>Аналитика района</b>"
        conclusion = (
            f"Район <b>{html.escape(area)}</b> показывает ориентировочную доходность <b>{fmt_pct(avg_roi)}</b>. "
            "Для профессионального выбора лучше смотреть не только средний ROI, но и количество сделок, ликвидность, "
            "динамику цены и разницу между продажей и арендой. Самые сильные варианты — здания с устойчивым спросом и "
            "арендой выше среднего рынка."
        )
        labels = ("Район", "Средняя продажа", "Средняя аренда", "ROI", "Продажи", "Аренды", "Score")
    elif lang == "ar":
        title = "🏙 <b>تحليل المنطقة</b>"
        conclusion = f"المنطقة <b>{html.escape(area)}</b> تظهر عائداً تقريبياً <b>{fmt_pct(avg_roi)}</b>."
        labels = ("المنطقة", "متوسط البيع", "متوسط الإيجار", "ROI", "مبيعات", "إيجارات", "Score")
    else:
        title = "🏙 <b>Area Intelligence</b>"
        conclusion = (
            f"<b>{html.escape(area)}</b> shows estimated yield of <b>{fmt_pct(avg_roi)}</b>. "
            "Professional selection should compare ROI, transaction volume, liquidity, price momentum and rental depth."
        )
        labels = ("Area", "Average sale", "Average rent", "ROI", "Sales", "Rents", "Score")

    return "\n".join([
        title, "",
        f"📍 <b>{labels[0]}:</b> {html.escape(area)}",
        f"💰 <b>{labels[1]}:</b> {fmt_money(avg_sale)}",
        f"🏦 <b>{labels[2]}:</b> {fmt_money(avg_rent)}",
        f"📊 <b>{labels[3]}:</b> {fmt_pct(avg_roi)}",
        f"🧾 <b>{labels[4]}:</b> {fmt_num(sales)}",
        f"🔑 <b>{labels[5]}:</b> {fmt_num(rents)}",
        f"⭐️ <b>{labels[6]}:</b> {fmt_num(score, 1)}",
        "",
        "🧠 <b>" + ("Экономическое заключение" if lang == "ru" else "Professional Conclusion" if lang == "en" else "الخلاصة") + "</b>",
        conclusion,
    ])


def query_building(query: str) -> List[Dict[str, Any]]:
    # Prefer intelligence_roi, then market stats
    for table in ["intelligence_roi", "intelligence_market_stats", "intelligence_sales"]:
        rows = select_from_intelligence(table, ["building_name", "building", "building_name_en", "project_name_en", "project_name"], query, 10)
        if rows:
            return rows
    return []


def query_area(query: str) -> List[Dict[str, Any]]:
    for table in ["intelligence_market_stats", "intelligence_roi", "intelligence_sales"]:
        rows = select_from_intelligence(table, ["area_name", "area", "area_name_en"], query, 10)
        if rows:
            return rows
    return []


def top_roi(prop_filter: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        with intel_conn() as conn:
            if not table_exists(conn, "intelligence_roi"):
                return []
            cols = table_columns(conn, "intelligence_roi")
            roi_col = pick_col(cols, ["roi", "gross_roi", "avg_roi", "yield", "gross_yield"])
            if not roi_col:
                return []
            prop_col = pick_col(cols, ["property_type", "property_type_en", "prop_type_en"])
            where = ""
            params: List[Any] = []
            if prop_filter and prop_filter != "any" and prop_col:
                where = f"WHERE {sql_ident(prop_col)} ILIKE %s"
                params.append(f"%{prop_filter}%")
            sql = f"""
                SELECT *
                FROM public.intelligence_roi
                {where}
                ORDER BY {sql_ident(roi_col)} DESC NULLS LAST
                LIMIT %s
            """
            params.append(limit)
            return list(execute(conn, sql, params))
    except Exception as e:
        log.exception("top_roi failed: %s", e)
        return []


def top_momentum(limit: int = 10) -> List[Dict[str, Any]]:
    try:
        with intel_conn() as conn:
            table = "intelligence_period_comparison" if table_exists(conn, "intelligence_period_comparison") else "intelligence_market_stats"
            if not table_exists(conn, table):
                return []
            cols = table_columns(conn, table)
            order_col = pick_col(cols, ["price_growth_pct", "rent_growth_pct", "momentum_score", "investment_score", "roi"])
            order_sql = f"ORDER BY {sql_ident(order_col)} DESC NULLS LAST" if order_col else ""
            return list(execute(conn, f"SELECT * FROM public.{sql_ident(table)} {order_sql} LIMIT %s", [limit]))
    except Exception as e:
        log.exception("top_momentum failed: %s", e)
        return []


def format_ranked(lang: str, rows: List[Dict[str, Any]], title_ru: str, title_en: str, title_ar: str) -> str:
    if not rows:
        return tr(lang, "not_found")
    title = title_ru if lang == "ru" else title_ar if lang == "ar" else title_en
    lines = [f"<b>{title}</b>", ""]
    for i, r in enumerate(rows, 1):
        building = clean_text(row_value(r, ["building_name", "building", "building_name_en", "project_name", "project_name_en"]))
        area = clean_text(row_value(r, ["area_name", "area", "area_name_en"]))
        prop = clean_text(row_value(r, ["property_type", "property_type_en", "prop_type_en"]))
        roi = row_value(r, ["roi", "gross_roi", "avg_roi", "yield"])
        score = row_value(r, ["investment_score", "score", "momentum_score"])
        sale = row_value(r, ["avg_sale_price", "median_sale_price", "average_sale_price"])
        rent = row_value(r, ["avg_rent", "median_rent", "average_rent"])
        lines.append(
            f"<b>#{i}</b> · {html.escape(building)}\n"
            f"📍 {html.escape(area)} · 🏘 {html.escape(prop)}\n"
            f"📊 ROI: <b>{fmt_pct(roi)}</b> · ⭐️ Score: <b>{fmt_num(score,1)}</b>\n"
            f"💰 Sale: {fmt_money(sale)} · 🏦 Rent: {fmt_money(rent)}"
        )
        lines.append("────────────")
    return "\n".join(lines)


# =============================================================================
# LATEST DEALS
# =============================================================================

def source_tables_for_deals(deal_type: str) -> List[Tuple[str, str]]:
    # (db, table)
    if deal_type == "rent":
        return [("archive", "dld_rent_archive"), ("live", "dld_rents_full")]
    return [("archive", "dld_sale_archive"), ("live", "dld_transactions_full")]


def fetch_deals(
    deal_type: str,
    building: Optional[str] = None,
    area: Optional[str] = None,
    period_days: Optional[int] = None,
    offset: int = 0,
    limit: int = PAGE_SIZE + 1,
) -> List[Dict[str, Any]]:
    all_rows: List[Dict[str, Any]] = []
    for dbname, table in source_tables_for_deals(deal_type):
        try:
            conn_cm = archive_conn if dbname == "archive" else live_conn
            with conn_cm() as conn:
                if not table_exists(conn, table):
                    continue
                cols = table_columns(conn, table)
                date_col = pick_col(cols, ["transaction_date", "instance_date", "contract_start_date", "contract_date", "load_timestamp", "created_at"])
                amount_col = pick_col(cols, ["actual_worth", "amount", "contract_amount", "annual_amount", "sale_price", "price"])
                building_col = pick_col(cols, ["building_name_en", "building_en", "building_name", "project_name_en", "project_en", "master_project_en"])
                project_col = pick_col(cols, ["project_name_en", "project_en", "master_project_en"])
                area_col = pick_col(cols, ["area_name_en", "area_en", "area_name"])
                prop_col = pick_col(cols, ["property_type_en", "prop_type_en", "property_usage_en"])
                subtype_col = pick_col(cols, ["property_sub_type_en", "prop_sub_type_en"])
                rooms_col = pick_col(cols, ["rooms_en", "rooms"])
                size_col = pick_col(cols, ["actual_area", "procedure_area", "area"])
                psf_col = pick_col(cols, ["meter_sale_price", "price_per_sqm", "price_per_sqft"])
                id_col = pick_col(cols, ["transaction_id", "transaction_number", "contract_id", "id"])
                proc_col = pick_col(cols, ["procedure_name_en", "procedure_name", "reg_type_en"])

                select_parts = []
                mapping = {
                    "deal_date": date_col,
                    "amount": amount_col,
                    "building": building_col,
                    "project": project_col,
                    "area": area_col,
                    "property_type": prop_col,
                    "subtype": subtype_col,
                    "rooms": rooms_col,
                    "size": size_col,
                    "psf": psf_col,
                    "source_id": id_col,
                    "procedure": proc_col,
                }
                for alias, col in mapping.items():
                    if col:
                        select_parts.append(f"{sql_ident(col)} AS {alias}")
                    else:
                        select_parts.append(f"NULL AS {alias}")
                select_parts.append(f"'{dbname}:{table}' AS source_table")

                where = []
                params: List[Any] = []
                if building and building_col:
                    where.append(f"{sql_ident(building_col)} ILIKE %s")
                    params.append(safe_like(building))
                if area and area_col:
                    where.append(f"{sql_ident(area_col)} ILIKE %s")
                    params.append(safe_like(area))
                if period_days and date_col:
                    where.append(f"{sql_ident(date_col)}::date >= CURRENT_DATE - INTERVAL '{int(period_days)} days'")
                where_sql = "WHERE " + " AND ".join(where) if where else ""
                order_sql = f"ORDER BY {sql_ident(date_col)} DESC NULLS LAST" if date_col else ""

                sql = f"""
                    SELECT {", ".join(select_parts)}
                    FROM public.{sql_ident(table)}
                    {where_sql}
                    {order_sql}
                    LIMIT %s
                """
                params.append(offset + limit + 30)
                rows = execute(conn, sql, params)
                all_rows.extend([dict(r) for r in rows])
        except Exception as e:
            log.warning("fetch deals failed for %s.%s: %s", dbname, table, e)

    # strict de-duplication
    seen = set()
    deduped = []
    for r in all_rows:
        key = (
            clean_text(r.get("source_id")),
            clean_text(r.get("deal_date")),
            clean_text(r.get("building")),
            clean_text(r.get("area")),
            clean_text(r.get("amount")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    def sort_key(r):
        d = r.get("deal_date")
        if isinstance(d, datetime):
            return d
        try:
            return datetime.fromisoformat(str(d)[:10])
        except Exception:
            return datetime.min

    deduped.sort(key=sort_key, reverse=True)
    return deduped[offset: offset + limit]


def format_deals(lang: str, deal_type: str, rows: List[Dict[str, Any]], title_extra: str = "") -> str:
    if not rows:
        return tr(lang, "not_found")
    if lang == "ru":
        title = f"🧾 <b>Последние сделки</b> · {tr(lang, deal_type)}"
        labels = {
            "date": "Дата", "building": "Здание", "project": "Проект", "area": "Район",
            "prop": "Тип", "subtype": "Подтип", "rooms": "Комнаты", "unit": "Unit",
            "size": "Площадь", "amount": "Сумма", "psf": "Цена за м²/sqft", "procedure": "Процедура"
        }
    elif lang == "ar":
        title = f"🧾 <b>أحدث الصفقات</b> · {tr(lang, deal_type)}"
        labels = {
            "date": "التاريخ", "building": "المبنى", "project": "المشروع", "area": "المنطقة",
            "prop": "النوع", "subtype": "الفرعي", "rooms": "الغرف", "unit": "Unit",
            "size": "المساحة", "amount": "القيمة", "psf": "السعر", "procedure": "الإجراء"
        }
    else:
        title = f"🧾 <b>Latest Deals</b> · {tr(lang, deal_type)}"
        labels = {
            "date": "Date", "building": "Building", "project": "Project", "area": "Area",
            "prop": "Type", "subtype": "Subtype", "rooms": "Rooms", "unit": "Unit",
            "size": "Size", "amount": "Amount", "psf": "Price per m²/sqft", "procedure": "Procedure"
        }

    lines = [title]
    if title_extra:
        lines.append(f"🔎 {html.escape(title_extra)}")
    lines.append("")

    for i, r in enumerate(rows[:PAGE_SIZE], 1):
        amount = r.get("amount")
        size = r.get("size")
        psf = r.get("psf")
        if not psf and amount and size:
            try:
                psf = float(amount) / float(size)
            except Exception:
                psf = None

        lines.append(f"<b>#{i}</b> · {'🔑' if deal_type == 'rent' else '💰'} <b>{tr(lang, deal_type)}</b>")
        lines.append(f"🗓 <b>{labels['date']}:</b> {clean_text(r.get('deal_date'))}")
        lines.append(f"🏢 <b>{labels['building']}:</b> {html.escape(clean_text(r.get('building')))}")
        lines.append(f"🏗 <b>{labels['project']}:</b> {html.escape(clean_text(r.get('project')))}")
        lines.append(f"📍 <b>{labels['area']}:</b> {html.escape(clean_text(r.get('area')))}")
        lines.append(f"🏘 <b>{labels['prop']}:</b> {html.escape(clean_text(r.get('property_type')))}")
        lines.append(f"🔹 <b>{labels['subtype']}:</b> {html.escape(clean_text(r.get('subtype')))}")
        lines.append(f"🛏 <b>{labels['rooms']}:</b> {html.escape(clean_text(r.get('rooms')))}")
        lines.append(f"📐 <b>{labels['size']}:</b> {fmt_num(size, 1)}")
        lines.append(f"💵 <b>{labels['amount']}:</b> {fmt_money(amount)}")
        lines.append(f"📊 <b>{labels['psf']}:</b> {fmt_money(psf)}")
        lines.append(f"📄 <b>{labels['procedure']}:</b> {html.escape(clean_text(r.get('procedure')))}")
        lines.append("────────────")
    return "\n".join(lines)


# =============================================================================
# SMART INVESTMENT AI
# =============================================================================

def run_smart_ai(user_id: int) -> Tuple[str, List[Dict[str, Any]]]:
    lang = get_lang(user_id)
    st = USER_STATE.get(user_id, {})
    goal = st.get("goal")
    budget = st.get("budget")
    prop = st.get("property_type")
    rooms = st.get("rooms")
    horizon = st.get("horizon")
    risk = st.get("risk")

    rows = top_roi(prop if prop != "any" else None, 20)
    if budget:
        # filter using average sale price if possible
        filtered = []
        for r in rows:
            sale = row_value(r, ["avg_sale_price", "median_sale_price", "average_sale_price"])
            try:
                if sale is None or float(sale) <= float(budget) * 1.15:
                    filtered.append(r)
            except Exception:
                filtered.append(r)
        rows = filtered or rows

    if rooms and rooms != "any":
        rr = []
        for r in rows:
            room_v = clean_text(row_value(r, ["rooms", "rooms_en", "unit_segment", "unit_type"])).lower()
            if rooms.lower().replace("br", "") in room_v or rooms.lower() in room_v:
                rr.append(r)
        rows = rr or rows

    rows = rows[:5]
    if not rows:
        return tr(lang, "not_found"), []

    if lang == "ru":
        lines = [
            "🧠 <b>Smart Investment AI — профессиональный подбор</b>",
            "",
            f"🎯 <b>Цель:</b> {tr(lang, goal) if goal in I18N[lang] else clean_text(goal)}",
            f"💵 <b>Бюджет:</b> {fmt_money(budget)}",
            f"🏘 <b>Тип:</b> {tr(lang, prop) if prop in I18N[lang] else clean_text(prop)}",
            f"🛏 <b>Комнаты:</b> {clean_text(rooms)}",
            f"⏳ <b>Горизонт:</b> {clean_text(horizon)} лет",
            f"⚖️ <b>Риск:</b> {tr(lang, risk + '_risk') if risk == 'low' else tr(lang, risk) if risk in I18N[lang] else clean_text(risk)}",
            "",
            "🏆 <b>Лучшие варианты по intelligence-базе</b>",
            "",
        ]
    elif lang == "ar":
        lines = [
            "🧠 <b>الذكاء الاستثماري</b>", "",
            f"🎯 <b>الهدف:</b> {clean_text(goal)}",
            f"💵 <b>الميزانية:</b> {fmt_money(budget)}",
            f"🏘 <b>النوع:</b> {clean_text(prop)}",
            f"🛏 <b>الغرف:</b> {clean_text(rooms)}",
            f"⏳ <b>الأفق:</b> {clean_text(horizon)} سنوات",
            f"⚖️ <b>المخاطر:</b> {clean_text(risk)}",
            "", "🏆 <b>أفضل الخيارات</b>", "",
        ]
    else:
        lines = [
            "🧠 <b>Smart Investment AI — Professional Selection</b>",
            "",
            f"🎯 <b>Goal:</b> {clean_text(goal)}",
            f"💵 <b>Budget:</b> {fmt_money(budget)}",
            f"🏘 <b>Type:</b> {clean_text(prop)}",
            f"🛏 <b>Rooms:</b> {clean_text(rooms)}",
            f"⏳ <b>Horizon:</b> {clean_text(horizon)} years",
            f"⚖️ <b>Risk:</b> {clean_text(risk)}",
            "",
            "🏆 <b>Best intelligence-based matches</b>",
            "",
        ]

    for i, r in enumerate(rows, 1):
        building = clean_text(row_value(r, ["building_name", "building", "building_name_en", "project_name_en", "project_name"]))
        area = clean_text(row_value(r, ["area_name", "area", "area_name_en"]))
        roi = row_value(r, ["roi", "gross_roi", "avg_roi", "yield"])
        sale = row_value(r, ["avg_sale_price", "median_sale_price", "average_sale_price"])
        rent = row_value(r, ["avg_rent", "median_rent", "average_rent"])
        score = row_value(r, ["investment_score", "score"])
        liquidity = row_value(r, ["liquidity", "liquidity_score"])
        expected_1y = None
        expected_3y = None
        try:
            expected_1y = float(rent or 0)
            expected_3y = expected_1y * 3
        except Exception:
            pass

        lines.append(f"<b>#{i} · {html.escape(building)}</b>")
        lines.append(f"📍 {html.escape(area)}")
        lines.append(f"💰 Sale: {fmt_money(sale)}")
        lines.append(f"🏦 Rent: {fmt_money(rent)}")
        lines.append(f"📊 ROI: <b>{fmt_pct(roi)}</b>")
        lines.append(f"⭐️ Score: <b>{fmt_num(score, 1)}</b>")
        lines.append(f"💧 Liquidity: {clean_text(liquidity)}")
        if expected_1y:
            lines.append(f"📈 Gross income 1Y: {fmt_money(expected_1y)}")
            lines.append(f"📈 Gross income 3Y: {fmt_money(expected_3y)}")
        lines.append("────────────")

    if lang == "ru":
        lines.extend([
            "",
            "🧠 <b>Вывод аналитика</b>",
            "Приоритет стоит отдавать объектам, где одновременно есть высокий ROI, достаточное количество сделок, понятная ликвидность и арендный спрос. "
            "Если цель — перепродажа, важнее динамика цены и ликвидность. Если цель — cashflow, важнее средняя аренда и стабильность спроса. "
            "Для низкого риска лучше выбирать районы и здания с большим объёмом сделок, даже если ROI немного ниже."
        ])
    elif lang == "en":
        lines.extend([
            "",
            "🧠 <b>Analyst Conclusion</b>",
            "Priority should be given to assets combining strong ROI, sufficient transaction volume, clear liquidity and rental depth. "
            "For resale, price momentum and liquidity matter most. For cashflow, rental depth and yield stability are more important."
        ])
    else:
        lines.extend(["", "🧠 <b>الخلاصة</b>", "الأولوية للأصول ذات ROI قوي وسيولة جيدة وحجم صفقات كافٍ."])

    return "\n".join(lines), rows


# =============================================================================
# PDF EXPORT
# =============================================================================

def strip_html_for_pdf(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def make_pdf_bytes(title: str, content: str, lang: str = "ru") -> Optional[bytes]:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except Exception:
        return None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.4 * cm,
        leftMargin=1.4 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm,
    )

    font_name = "Helvetica"
    # Try common DejaVu for Cyrillic/Arabic support
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ]:
        try:
            if os.path.exists(path):
                pdfmetrics.registerFont(TTFont("DejaVuSans", path))
                font_name = "DejaVuSans"
                break
        except Exception:
            pass

    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "NormalCustom",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=13,
    )
    heading = ParagraphStyle(
        "HeadingCustom",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=15,
        leading=19,
        spaceAfter=12,
    )

    clean = strip_html_for_pdf(content)
    story = [
        Paragraph(html.escape(title), heading),
        Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", normal),
        Spacer(1, 0.3 * cm),
    ]
    for line in clean.splitlines():
        if not line.strip():
            story.append(Spacer(1, 0.15 * cm))
        else:
            story.append(Paragraph(html.escape(line), normal))
    doc.build(story)
    return buffer.getvalue()


async def send_report_pdf(message_or_call: Any):
    user_id = message_or_call.from_user.id
    lang = get_lang(user_id)
    report = LAST_REPORTS.get(user_id)
    if not report:
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.answer(tr(lang, "not_found"), show_alert=True)
        return

    pdf_bytes = make_pdf_bytes(report.get("title", "Dubai DLD Report"), report.get("content", ""), lang)
    if not pdf_bytes:
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.message.answer(tr(lang, "pdf_error"))
        else:
            await message_or_call.answer(tr(lang, "pdf_error"))
        return

    filename = f"dubai_dld_report_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    file = BufferedInputFile(pdf_bytes, filename=filename)
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.answer_document(file, caption=tr(lang, "report_ready"))
    else:
        await message_or_call.answer_document(file, caption=tr(lang, "report_ready"))


# =============================================================================
# HANDLERS
# =============================================================================

@router.message(CommandStart())
async def start(message: Message):
    register_user(message)
    log_action(message.from_user.id, "start")
    await message.answer(tr("ru", "welcome"), reply_markup=kb_language())


@router.message(Command("menu"))
async def menu_cmd(message: Message):
    register_user(message)
    lang = get_lang(message.from_user.id)
    await message.answer(tr(lang, "main_menu"), reply_markup=kb_main(lang, message.from_user.id))


@router.callback_query(F.data.startswith("lang:"))
async def lang_cb(call: CallbackQuery):
    register_user(call)
    lang = call.data.split(":", 1)[1]
    set_lang(call.from_user.id, lang)
    log_action(call.from_user.id, "language", lang)
    await call.message.answer(tr(lang, "lang_selected"))
    await call.message.answer(tr(lang, "main_menu"), reply_markup=kb_main(lang, call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "m:main")
async def main_cb(call: CallbackQuery):
    register_user(call)
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "main_menu"), reply_markup=kb_main(lang, call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "m:back")
async def back_cb(call: CallbackQuery):
    await main_cb(call)


@router.callback_query(F.data == "m:settings")
async def settings_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "choose_lang"), reply_markup=kb_language())
    await call.answer()


@router.callback_query(F.data == "m:building")
async def building_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["mode"] = "await_building"
    await call.message.answer(tr(lang, "enter_building"), reply_markup=kb_back_main(lang, with_consult=False))
    await call.answer()


@router.callback_query(F.data == "m:area")
async def area_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["mode"] = "await_area"
    await call.message.answer(tr(lang, "enter_area"), reply_markup=kb_back_main(lang, with_consult=False))
    await call.answer()


@router.callback_query(F.data == "m:latest")
async def latest_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["mode"] = "latest_type"
    await call.message.answer(tr(lang, "choose_deal_type"), reply_markup=kb_deal_type(lang, "latest_type"))
    await call.answer()


@router.callback_query(F.data.startswith("latest_type:"))
async def latest_type_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    deal_type = call.data.split(":", 1)[1]
    USER_STATE.setdefault(call.from_user.id, {})["latest_deal_type"] = deal_type
    await call.message.answer(tr(lang, "choose_period"), reply_markup=kb_period(lang, "latest_period"))
    await call.answer()


@router.callback_query(F.data.startswith("latest_period:"))
async def latest_period_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    days = int(call.data.split(":", 1)[1])
    deal_type = USER_STATE.setdefault(call.from_user.id, {}).get("latest_deal_type", "sale")
    await call.message.answer(tr(lang, "loading"))
    rows = fetch_deals(deal_type, period_days=days, offset=0)
    content = format_deals(lang, deal_type, rows, title_extra=f"{days} days")
    LAST_REPORTS[call.from_user.id] = {"title": f"Latest {deal_type} deals {days}d", "content": content}
    USER_STATE[call.from_user.id]["page_context"] = json.dumps({"kind": "latest", "deal_type": deal_type, "days": days})
    await call.message.answer(content, reply_markup=kb_pagination(lang, "latest", 0, len(rows) > PAGE_SIZE))
    await call.answer()


@router.callback_query(F.data.startswith("page:"))
async def page_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    _, context, page_s = call.data.split(":")
    page = int(page_s)
    st = USER_STATE.setdefault(call.from_user.id, {})
    payload = {}
    try:
        payload = json.loads(st.get("page_context", "{}"))
    except Exception:
        pass
    if payload.get("kind") == "latest":
        deal_type = payload.get("deal_type", "sale")
        days = payload.get("days")
        rows = fetch_deals(deal_type, period_days=days, offset=page * PAGE_SIZE)
        content = format_deals(lang, deal_type, rows, title_extra=f"{days} days · page {page+1}")
        LAST_REPORTS[call.from_user.id] = {"title": f"Latest {deal_type} deals page {page+1}", "content": content}
        await call.message.answer(content, reply_markup=kb_pagination(lang, "latest", page, len(rows) > PAGE_SIZE))
    await call.answer()


@router.callback_query(F.data == "m:roi")
async def roi_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    rows = top_roi(limit=10)
    content = format_ranked(lang, rows, "💰 <b>Лучший ROI</b>", "💰 <b>Best ROI</b>", "💰 <b>أفضل ROI</b>")
    LAST_REPORTS[call.from_user.id] = {"title": "Best ROI", "content": content}
    await call.message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
    await call.answer()


@router.callback_query(F.data == "m:momentum")
async def momentum_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    rows = top_momentum(limit=10)
    content = format_ranked(lang, rows, "🔥 <b>Рыночный импульс</b>", "🔥 <b>Market Momentum</b>", "🔥 <b>زخم السوق</b>")
    LAST_REPORTS[call.from_user.id] = {"title": "Market Momentum", "content": content}
    await call.message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
    await call.answer()


@router.callback_query(F.data == "m:period")
async def period_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    await call.message.answer(tr(lang, "choose_period"), reply_markup=kb_period(lang, "period_days"))
    await call.answer()


@router.callback_query(F.data.startswith("period_days:"))
async def period_days_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    days = int(call.data.split(":")[1])
    try:
        with intel_conn() as conn:
            if table_exists(conn, "intelligence_period_comparison"):
                cols = table_columns(conn, "intelligence_period_comparison")
                order_col = pick_col(cols, ["price_growth_pct", "rent_growth_pct", "momentum_score", "investment_score"])
                period_col = pick_col(cols, ["period_days", "days", "period"])
                where = ""
                params = []
                if period_col:
                    where = f"WHERE {sql_ident(period_col)}::text ILIKE %s"
                    params.append(f"%{days}%")
                order = f"ORDER BY {sql_ident(order_col)} DESC NULLS LAST" if order_col else ""
                rows = execute(conn, f"SELECT * FROM public.intelligence_period_comparison {where} {order} LIMIT 10", params)
            else:
                rows = []
    except Exception as e:
        log.exception("period comparison failed: %s", e)
        rows = []
    content = format_ranked(lang, list(rows), f"📈 <b>Сравнение периода: {days} дней</b>", f"📈 <b>Period Comparison: {days} days</b>", f"📈 <b>مقارنة الفترة: {days} يوم</b>")
    LAST_REPORTS[call.from_user.id] = {"title": f"Period comparison {days}d", "content": content}
    await call.message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
    await call.answer()


@router.callback_query(F.data.in_({"m:villas", "m:commercial"}))
async def segment_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    if call.data == "m:villas":
        rows = top_roi("villa", 10) + top_roi("townhouse", 10)
        rows = rows[:10]
        content = format_ranked(lang, rows, "🏘 <b>Виллы / Таунхаусы</b>", "🏘 <b>Villas / Townhouses</b>", "🏘 <b>فلل / تاون هاوس</b>")
    else:
        rows = top_roi("office", 10) + top_roi("retail", 10) + top_roi("plot", 10)
        rows = rows[:10]
        content = format_ranked(lang, rows, "💼 <b>Коммерция / Участки</b>", "💼 <b>Commercial / Plots</b>", "💼 <b>تجاري / أراضي</b>")
    LAST_REPORTS[call.from_user.id] = {"title": "Segment analytics", "content": content}
    await call.message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
    await call.answer()


@router.callback_query(F.data == "m:check")
async def check_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["mode"] = "check_building"
    await call.message.answer(tr(lang, "enter_building"), reply_markup=kb_back_main(lang, with_consult=False))
    await call.answer()


@router.callback_query(F.data == "m:smart")
async def smart_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {}).clear()
    USER_STATE.setdefault(call.from_user.id, {})["lang"] = lang
    USER_STATE[call.from_user.id]["mode"] = "smart_goal"
    await call.message.answer(tr(lang, "choose_goal"), reply_markup=kb_smart_goal(lang))
    await call.answer()


@router.callback_query(F.data.startswith("smart_goal:"))
async def smart_goal_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["goal"] = call.data.split(":", 1)[1]
    USER_STATE[call.from_user.id]["mode"] = "smart_budget"
    await call.message.answer(tr(lang, "enter_budget"))
    await call.answer()


@router.callback_query(F.data.startswith("smart_prop:"))
async def smart_prop_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["property_type"] = call.data.split(":", 1)[1]
    await call.message.answer(tr(lang, "choose_rooms"), reply_markup=kb_rooms(lang, "smart_rooms"))
    await call.answer()


@router.callback_query(F.data.startswith("smart_rooms:"))
async def smart_rooms_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["rooms"] = call.data.split(":", 1)[1]
    await call.message.answer(tr(lang, "choose_horizon"), reply_markup=kb_horizon(lang))
    await call.answer()


@router.callback_query(F.data.startswith("smart_horizon:"))
async def smart_horizon_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["horizon"] = call.data.split(":", 1)[1]
    await call.message.answer(tr(lang, "choose_risk"), reply_markup=kb_risk(lang))
    await call.answer()


@router.callback_query(F.data.startswith("smart_risk:"))
async def smart_risk_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["risk"] = call.data.split(":", 1)[1]
    content, rows = run_smart_ai(call.from_user.id)
    LAST_REPORTS[call.from_user.id] = {"title": "Smart Investment AI", "content": content}
    await call.message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
    await call.answer()


@router.callback_query(F.data == "lead:open")
async def lead_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    now = time.time()
    last = LAST_LEAD_TS.get(call.from_user.id, 0)
    if now - last < LEAD_COOLDOWN_SECONDS:
        await call.answer(tr(lang, "lead_cooldown"), show_alert=True)
        return
    LAST_LEAD_TS[call.from_user.id] = now
    log_action(call.from_user.id, "lead_click")
    try:
        with intel_conn() as conn:
            execute(conn, "INSERT INTO bot_leads(user_id, username) VALUES(%s,%s)", [call.from_user.id, call.from_user.username], False)
    except Exception:
        pass
    await call.message.answer(tr(lang, "lead_text"), reply_markup=kb_consult(lang))
    await call.answer()


@router.callback_query(F.data == "pdf:last")
async def pdf_cb(call: CallbackQuery):
    await send_report_pdf(call)
    await call.answer()


@router.callback_query(F.data == "admin:home")
async def admin_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    if call.from_user.id not in ADMIN_IDS:
        await call.answer(tr(lang, "admin_denied"), show_alert=True)
        return
    try:
        with intel_conn() as conn:
            total = one(conn, "SELECT COUNT(*) AS c FROM bot_users")["c"]
            today = one(conn, "SELECT COUNT(*) AS c FROM bot_users WHERE last_seen::date=CURRENT_DATE")["c"]
            d30 = one(conn, "SELECT COUNT(*) AS c FROM bot_users WHERE last_seen >= NOW() - INTERVAL '30 days'")["c"]
            actions = one(conn, "SELECT COUNT(*) AS c FROM bot_actions")["c"]
            leads = one(conn, "SELECT COUNT(*) AS c FROM bot_leads")["c"]
            top = execute(conn, """
                SELECT action, COUNT(*) AS c
                FROM bot_actions
                GROUP BY action
                ORDER BY c DESC
                LIMIT 7
            """)
    except Exception as e:
        log.exception("admin failed: %s", e)
        total = today = d30 = actions = leads = 0
        top = []
    lines = [
        tr(lang, "admin_title"), "",
        f"👥 <b>{tr(lang, 'stats_total')}:</b> {fmt_num(total)}",
        f"📅 <b>{tr(lang, 'stats_today')}:</b> {fmt_num(today)}",
        f"🗓 <b>{tr(lang, 'stats_30d')}:</b> {fmt_num(d30)}",
        f"🧭 <b>{tr(lang, 'stats_actions')}:</b> {fmt_num(actions)}",
        f"💼 <b>{tr(lang, 'stats_leads')}:</b> {fmt_num(leads)}",
        "",
        f"🔥 <b>{tr(lang, 'top_queries')}:</b>",
    ]
    for r in top:
        lines.append(f"• {html.escape(str(r['action']))}: {fmt_num(r['c'])}")
    lines.extend([
        "",
        f"📊 intelligence_sales: {fmt_num(get_table_count('intelligence_sales'))}",
        f"📊 intelligence_rents: {fmt_num(get_table_count('intelligence_rents'))}",
        f"📊 intelligence_roi: {fmt_num(get_table_count('intelligence_roi'))}",
        f"📊 intelligence_market_stats: {fmt_num(get_table_count('intelligence_market_stats'))}",
        f"📊 intelligence_period_comparison: {fmt_num(get_table_count('intelligence_period_comparison'))}",
    ])
    await call.message.answer("\n".join(lines), reply_markup=kb_back_main(lang, with_consult=False))
    await call.answer()


@router.message()
async def text_router(message: Message):
    register_user(message)
    user_id = message.from_user.id
    lang = get_lang(user_id)
    text = normalize_query(message.text or "")
    state = USER_STATE.setdefault(user_id, {})

    if not text:
        return

    mode = state.get("mode")
    log_action(user_id, mode or "text", text)

    if mode == "await_building":
        await message.answer(tr(lang, "loading"))
        rows = query_building(text)
        content = format_building_report(lang, rows, text)
        LAST_REPORTS[user_id] = {"title": f"Building report {text}", "content": content}
        await message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
        return

    if mode == "await_area":
        await message.answer(tr(lang, "loading"))
        rows = query_area(text)
        content = format_area_report(lang, rows, text)
        LAST_REPORTS[user_id] = {"title": f"Area report {text}", "content": content}
        await message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
        return

    if mode == "smart_budget":
        budget = re.sub(r"[^\d.]", "", text)
        if not budget:
            await message.answer(tr(lang, "enter_budget"))
            return
        state["budget"] = float(budget)
        await message.answer(tr(lang, "choose_property_type"), reply_markup=kb_property_type(lang, "smart_prop"))
        return

    if mode == "check_building":
        state["check_building"] = text
        state["mode"] = "check_type"
        await message.answer(tr(lang, "choose_property_type"), reply_markup=kb_property_type(lang, "check_prop"))
        return

    if mode == "check_price":
        price = re.sub(r"[^\d.]", "", text)
        if not price:
            await message.answer(tr(lang, "enter_price"))
            return
        state["check_price"] = float(price)
        building = state.get("check_building", "")
        rows = query_building(building)
        if not rows:
            await message.answer(tr(lang, "not_found"), reply_markup=kb_back_main(lang))
            return
        r = rows[0]
        market = row_value(r, ["avg_sale_price", "median_sale_price", "average_sale_price"])
        roi = row_value(r, ["roi", "gross_roi", "avg_roi", "yield"])
        rent = row_value(r, ["avg_rent", "median_rent", "average_rent"])
        diff = None
        verdict = "—"
        try:
            diff = (float(price) - float(market)) / float(market) * 100
            if diff <= -7:
                verdict = "🔥 Undervalued / ниже рынка"
            elif diff >= 7:
                verdict = "⚠️ Overpriced / выше рынка"
            else:
                verdict = "✅ Fair price / близко к рынку"
        except Exception:
            pass
        content = (
            f"📉 <b>{tr(lang, 'check_deal')}</b>\n\n"
            f"🏢 <b>Building:</b> {html.escape(clean_text(building))}\n"
            f"💵 <b>Your price:</b> {fmt_money(price)}\n"
            f"📊 <b>Market average:</b> {fmt_money(market)}\n"
            f"📈 <b>Difference:</b> {fmt_pct(diff)}\n"
            f"🏦 <b>Expected rent:</b> {fmt_money(rent)}\n"
            f"📊 <b>ROI:</b> {fmt_pct(roi)}\n\n"
            f"🧠 <b>Verdict:</b> {verdict}"
        )
        LAST_REPORTS[user_id] = {"title": f"Deal check {building}", "content": content}
        await message.answer(content, reply_markup=kb_back_main(lang, with_pdf=True))
        return

    # default: show localized menu
    await message.answer(tr(lang, "main_menu"), reply_markup=kb_main(lang, user_id))


@router.callback_query(F.data.startswith("check_prop:"))
async def check_prop_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["check_property_type"] = call.data.split(":", 1)[1]
    USER_STATE[call.from_user.id]["mode"] = "check_rooms"
    await call.message.answer(tr(lang, "choose_rooms"), reply_markup=kb_rooms(lang, "check_rooms"))
    await call.answer()


@router.callback_query(F.data.startswith("check_rooms:"))
async def check_rooms_cb(call: CallbackQuery):
    lang = get_lang(call.from_user.id)
    USER_STATE.setdefault(call.from_user.id, {})["check_rooms"] = call.data.split(":", 1)[1]
    USER_STATE[call.from_user.id]["mode"] = "check_price"
    await call.message.answer(tr(lang, "enter_price"))
    await call.answer()


# =============================================================================
# STARTUP
# =============================================================================

async def main():
    log.info("=" * 80)
    log.info("Dubai DLD Intelligence Bot vNext Ultra Multilingual started")
    log.info("LIVE_DATABASE_URL source: %s", "custom" if os.getenv("LIVE_DATABASE_URL") else "DATABASE_URL fallback")
    log.info("ARCHIVE_DATABASE_URL source: %s", "custom" if os.getenv("ARCHIVE_DATABASE_URL") else "DATABASE_URL fallback")
    log.info("INTELLIGENCE_DATABASE_URL source: %s", "custom" if os.getenv("INTELLIGENCE_DATABASE_URL") else "DATABASE_URL fallback")
    log.info("Lead bot URL: %s", LEAD_BOT_URL)
    log.info("Admin IDs count: %s", len(ADMIN_IDS))
    ensure_stats_tables()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Telegram webhook cleared before polling")
    except Exception as e:
        log.warning("Webhook clear failed: %s", e)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
