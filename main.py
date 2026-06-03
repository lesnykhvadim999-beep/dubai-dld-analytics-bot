import os
import sys

# === CRON DISPATCH (must run BEFORE bot imports / BOT_TOKEN checks) ===
# Some Railway cron services in this project (dld-sale-updater, dld-rent-updater)
# share the same repo as the main bot but railway.toml forces
# `startCommand=python main.py`. Without a per-service override they used to
# crash with `BOT_TOKEN is not set` because they don't have BOT_TOKEN.
# We dispatch by RAILWAY_SERVICE_NAME (set by Railway automatically) and
# also accept a manual CRON_TARGET env override.
_cron_target = (os.getenv("CRON_TARGET") or "").strip().lower()
if not _cron_target:
    _svc = (os.getenv("RAILWAY_SERVICE_NAME") or "").strip().lower()
    if _svc == "dld-sale-updater":
        _cron_target = "sales_scraper"
    elif _svc == "dld-rent-updater":
        _cron_target = "dld_sync"

if _cron_target in ("sales_scraper", "dld_sync"):
    import runpy
    print(f"[cron-dispatch] running {_cron_target}.py via main.py shim "
          f"(RAILWAY_SERVICE_NAME={os.getenv('RAILWAY_SERVICE_NAME')!r})",
          flush=True)
    runpy.run_module(_cron_target, run_name="__main__")
    sys.exit(0)
# === END CRON DISPATCH ===

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command

from dotenv import load_dotenv

import asyncio
import re
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

# ── Phase BL: behavior tracking (best-effort) ────────────────────────────────
_BT_OK = False
try:
    import sys as _bt_sys, os as _bt_os
    for _bt_p in ("/app/shared", r"C:\Projects\shared", "../shared"):
        if _bt_os.path.isdir(_bt_p) and _bt_p not in _bt_sys.path:
            _bt_sys.path.insert(0, _bt_p)
    from behavior_tracking import (
        log_interaction as _bt_log,
        feedback_kb as _bt_feedback_kb,
        is_enabled as _bt_is_enabled,
        is_feedback_enabled as _bt_fb_enabled,
        ensure_schema as _bt_ensure_schema,
    )
    from behavior_tracking.feedback import (
        parse_callback as _bt_parse_cb,
        record_feedback as _bt_record_fb,
    )
    try: _bt_ensure_schema()
    except Exception: pass
    _BT_OK = bool(_bt_is_enabled())
    print(f"[behavior_tracking] analytics init enabled={_BT_OK} fb={_bt_fb_enabled()}", flush=True)
except Exception as _bt_e:
    print(f"[behavior_tracking] analytics init failed: {_bt_e}", flush=True)
    def _bt_log(**kw): return None
    def _bt_feedback_kb(*a, **kw): return None
    def _bt_is_enabled(): return False
    def _bt_fb_enabled(): return False
    def _bt_parse_cb(*a, **kw): return None
    def _bt_record_fb(*a, **kw): return None

# Phase BM (Layer 9 + Layer 10) — long-term user memory + proactive agent
_BM_OK = False
try:
    import sys as _bm_sys, os as _bm_os
    for _bm_p in ("/app", r"C:\Projects", "../"):
        if _bm_os.path.isdir(_bm_p) and _bm_p not in _bm_sys.path:
            _bm_sys.path.insert(0, _bm_p)
    from shared.user_memory.integration import (
        enrich_context as _bm_enrich_context,
        record_turn as _bm_record_turn,
    )
    from shared.proactive_agent import (
        register_trigger as _bm_register_trigger,
        handle_opt_out_callback as _bm_opt_out_cb,
        OPT_OUT_CALLBACK_PREFIX as _BM_OPT_OUT_PREFIX,
    )
    _BM_OK = True
    print("[phase_bm] analytics init OK", flush=True)
except Exception as _bm_e:
    print(f"[phase_bm] analytics init failed: {_bm_e}", flush=True)
    def _bm_enrich_context(*a, **kw): return ""
    def _bm_record_turn(*a, **kw): return None
    def _bm_register_trigger(*a, **kw): return None
    def _bm_opt_out_cb(*a, **kw): return (False, "")
    _BM_OPT_OUT_PREFIX = "pa_optout_"


# Phase BM master cron — единый scheduler для агентов G/H/I/J/K/L.
# Запускается только в analytics (numReplicas=1 в railway.toml) чтобы избежать
# дубликатов. В resale/hub (numReplicas=2) PHASE_BM_CRON_DISABLED=1 в env.
try:
    if os.environ.get("PHASE_BM_CRON_DISABLED") != "1":
        from shared.scripts.phase_bm_master_cron import start_thread as _bm_cron_start
        _bm_cron_start()
except Exception as _bm_cron_err:
    print(f"[phase_bm_cron] master scheduler init failed: {_bm_cron_err}",
          flush=True)


def _bm_safe_record_turn(user_id, language=None, last_user_text=None):
    if not _BM_OK or not user_id:
        return
    try:
        _bm_record_turn(int(user_id),
                        bot_name="dubai-dld-analytics-bot",
                        language=language, last_user_text=last_user_text)
    except Exception:
        pass


def _bm_safe_enrich(user_id, query, language=None):
    if not _BM_OK or not user_id:
        return ""
    try:
        return _bm_enrich_context(int(user_id), query or "",
                                  bot_name="dubai-dld-analytics-bot",
                                  language=language) or ""
    except Exception:
        return ""


def _bm_is_opt_out_callback(data):
    return bool(data and str(data).startswith(_BM_OPT_OUT_PREFIX))


def _bm_handle_opt_out(data, user_id):
    if not _BM_OK:
        return (False, "")
    try:
        return _bm_opt_out_cb(data, int(user_id),
                              "dubai-dld-analytics-bot")
    except Exception:
        return (False, "")

# FSST: callback dedup + stale button middleware + health server
try:
    from fsst_core import (
        CallbackDeduplicator, is_stale_button_error,
        answer_stale, start_health_server,
    )
    _cb_dedup = CallbackDeduplicator()
    _fsst_ok = True
except Exception:
    class CallbackDeduplicator:  # type: ignore
        def is_dup_aiogram(self, cb): return False
    _cb_dedup = CallbackDeduplicator()
    _fsst_ok = False
    def is_stale_button_error(e): return False
    async def answer_stale(cb, lang="en"): pass
    def start_health_server(**kw): pass

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Stability hooks
try:
    from stability import (
        set_bot_name, log_event, ttl_cache, retry_on_transient_async,
        metrics, start_metrics_server, count_request, count_error, count_db,
        validate_price, validate_bedrooms, validate_area_name,
        validate_lang, validate_user_text, ValidationError,
    )
    set_bot_name("analytics-bot")
    _STAB_OK = True
except Exception as _stab_e:
    print(f"[stability] disabled: {_stab_e}", flush=True)
    _STAB_OK = False
    def log_event(level="INFO", **kw): pass
    def count_request(command="unknown"): pass
    def count_error(err_type="unknown"): pass
    def count_db(table, status="ok"): pass
    def start_metrics_server(port=None): return None
    def ttl_cache(maxsize=500, ttl=300):
        def d(f): return f
        return d
    def retry_on_transient_async(retries=3, base_delay=0.5, max_delay=30.0):
        def d(f): return f
        return d
    class ValidationError(ValueError):
        def __init__(self, code, message=""):
            self.code = code
            super().__init__(message or code)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()

# ── PDF FEATURE FLAG (manually disabled 2026-06-03 by Vadim) ─────────────────
# PDF generation is gracefully disabled across all bots. Code is preserved.
# Re-enable via:
#   1) UPDATE feature_flags SET enabled=TRUE WHERE name='pdf_generation';  -- in DB
#   2) OR remove env var PDF_DISABLED=1 on Railway service
def _pdf_enabled() -> bool:
    """Single source of truth for PDF feature flag. env var → DB → default True."""
    if (os.getenv("PDF_DISABLED") or "").strip() in ("1", "true", "True", "yes"):
        return False
    try:
        from shared.safety_nets.feature_flags import is_feature_enabled
        return is_feature_enabled("pdf_generation")
    except Exception:
        # fail-open only if env var not set
        return True

# v112: централизованный user tracking → bot_users (resale-DB)
try:
    from aiogram import BaseMiddleware
    from aiogram.types import Message as _Msg, CallbackQuery as _Cb
    import bot_user_tracker as _bot_user_tracker

    class _UserTrackingMiddleware(BaseMiddleware):
        async def __call__(self, handler, event, data):
            try:
                u = None
                action = "message"
                if isinstance(event, _Msg):
                    u = event.from_user; action = "message"
                elif isinstance(event, _Cb):
                    u = event.from_user; action = "callback"
                if u and u.id:
                    # LANG_FIX: prefer the user's IN-APP language (set via the
                    # 🇷🇺/🇬🇧/🇦🇪 buttons) over Telegram client locale. Otherwise
                    # every message overwrites the persisted pick with the
                    # TG-client locale (often 'en'), defeating hydration.
                    _picked = user_languages.get(u.id)
                    _lang_for_track = _picked if _picked else u.language_code
                    _bot_user_tracker.track_user_async(
                        telegram_id=u.id,
                        username=u.username,
                        first_name=u.first_name,
                        language=_lang_for_track,
                        action=action,
                    )
            except Exception:
                pass
            return await handler(event, data)

    dp.message.outer_middleware(_UserTrackingMiddleware())
    dp.callback_query.outer_middleware(_UserTrackingMiddleware())
except Exception as _e:
    print(f"[bot_users] tracker middleware skipped: {_e}", flush=True)

# v53: aiogram global error handler
try:
    import error_logger as _err_logger
except Exception:
    _err_logger = None

# B031: auto-incident response (maintenance + user notify + recovery)
import os as _os
_os.environ.setdefault("BOT_NAME", "analytics")
try:
    import error_watchdog as _ewd
except Exception as _ewd_err:
    print(f"[B031] error_watchdog import failed: {_ewd_err}", flush=True)
    _ewd = None


@dp.errors()
async def _analytics_global_error_handler(event):
    try:
        exc = event.exception
        upd = event.update
        user_id = None
        ctx = {}
        handler = "aiogram"
        if upd:
            if upd.message:
                user_id = upd.message.from_user.id if upd.message.from_user else None
                ctx["text"] = (upd.message.text or "")[:200]
                handler = "message_handler"
            elif upd.callback_query:
                user_id = upd.callback_query.from_user.id if upd.callback_query.from_user else None
                ctx["data"] = (upd.callback_query.data or "")[:200]
                handler = "callback_handler"
        import traceback as _tb
        print(f"[analytics-error] {type(exc).__name__}: {exc}", flush=True)
        if _err_logger:
            _err_logger.log_error("analytics", handler, str(exc),
                                    error_class=type(exc).__name__,
                                    user_id=user_id, context=ctx,
                                    tb=_tb.format_exc()[-1500:])
        # B031: maintenance mode + user-facing reply + admin alert
        if _ewd:
            try:
                await _ewd.handle_aiogram_error(event)
            except Exception as _ewd_call_err:
                print(f"[B031] handle_aiogram_error fail: {_ewd_call_err}", flush=True)
    except Exception:
        pass
    return True

TABLE = "public.dld_transactions_full"

user_languages = {}
user_states = {}
# v132: cache welcome logo file_id (avoids 1.5MB re-upload on every /start)
_ANALYTICS_LOGO_FILE_ID = None

# Memory-leak guard (2026-05-30, audit STEP 9): cap state dicts so they
# don't grow unbounded over weeks of uptime.
_MAX_STATE_USERS = int(os.environ.get("MAX_STATE_USERS", "5000"))


def _gc_state_dicts():
    """Hourly pruner — evicts oldest 25% if a state dict exceeds the cap.
    Note: user_languages persists in DB via set_lang/get_lang so we may evict
    from RAM freely; lang is re-read from DB on next message."""
    import time as _t
    while True:
        try:
            _t.sleep(3600)
            for name, d in (("user_languages", user_languages),
                            ("user_states", user_states)):
                if not isinstance(d, dict) or len(d) <= _MAX_STATE_USERS:
                    continue
                drop_n = len(d) - int(_MAX_STATE_USERS * 0.75)
                for k in list(d.keys())[:drop_n]:
                    d.pop(k, None)
                print(f"[gc] {name}: evicted {drop_n} ({len(d)} left)", flush=True)
        except Exception as _gc_err:
            print(f"[gc] error: {_gc_err}", flush=True)


import threading as _gc_th
_gc_th.Thread(target=_gc_state_dicts, name="state_gc", daemon=True).start()


def db():
    # v107: жёсткие таймауты, чтобы хэндлеры не зависали на медленных DLD-запросах.
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=8,
        options="-c statement_timeout=15000 -c idle_in_transaction_session_timeout=20000",
    )
    return conn


# FIX 2026-06-03 (METER_PRICE_PER_SOURCE — agent rev):
# Earlier fix #125 assumed legacy `meter_sale_price` is AED/sqft → applied ×10.7639.
# Direct DB verification on the Rent-sale-arhiv Railway DB shows otherwise:
#   - dld_sale_archive.meter_sale_price       p50 = 11_726 AED   → AED/m²  (NO multiplier)
#   - dld_transactions_full.meter_sale_price  p50 = 11_726 AED   → AED/m²  (same data, NO multiplier)
#   - dld_sales_unified VIEW                  p50 = 11_726 AED   → AED/m²  (NO multiplier)
#   - mv_area_24m_summary.avg_price_psf       p50 = 1_071  AED   → AED/sqft (×10.7639 needed)
# Because `base_from()` reads dld_transactions_full directly, METER_PRICE must NOT
# multiply. The multiplier stays only where avg_price_psf is read (mv_*, see
# smart_pick_candidates v111 — left untouched).
SQFT_TO_M2 = 10.7639


def num_sql(column):
    return f"NULLIF(regexp_replace(COALESCE({column}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"


PRICE = num_sql("actual_worth")
# dld_transactions_full.meter_sale_price is already AED/m² — no SQFT_TO_M2 multiplier.
METER_PRICE = f"({num_sql('meter_sale_price')})"
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
        "choose_lang": '🏙 <b>Dubai DLD Analytics</b>\n\nВаш аналитический помощник по рынку недвижимости Дубая.\n\nЧто умеет система:\n• искать здания и похожие названия;\n• показывать статистику по районам;\n• анализировать сделки DLD;\n• сравнивать периоды;\n• оценивать выгодность конкретной сделки;\n• подбирать район и формат юнита под бюджет и цель.\n\nВыберите язык:',
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
        "m_smart_pick": "🧠 Подбор",
        "m_best_obj": "🏆 Лучший объект",
        "m_area": "🏙 Район",
        "m_building": "🏢 Здание",
        "m_dubai": "🌆 Дубай",
        "m_deals": "🧾 Сделки",
        "m_ratings": "📊 Рейтинги",
        "m_compare": "⚖️ Сравнение",
        "no_data": "нет данных",
        "no_data_area": "❌ Нет данных по выбранному району.",
        "no_data_filters": "❌ Нет данных по выбранным фильтрам.",
        "no_data_named": "❌ Нет данных по «{name}».",
        "not_found_examples": "❌ Ничего не найдено. Попробуйте ввести иначе.\n\nПримеры:\n• Grande\n• Address Opera\n• Marina Gate\n• Burj Vista",
        "searching_buildings": "🔎 Ищу похожие здания...",
        "searching_buildings_db": "🔎 Ищу похожие здания в archive + live базе...",
        "searching_area_db": "🔎 Ищу район в archive + live базе...",
        "deals_dld_header": "🧾 <b>Сделки DLD</b>\n\nГде показать сделки?",
        "rankings_header": "📊 <b>Рейтинги рынка</b>\n\nВыберите рейтинг.",
        "rankings_pick_button": "Выберите рейтинг кнопкой.",
        "full_report_header": "📑 <b>Полный аналитический отчёт</b>\n\nВыберите масштаб:",
        "full_report_pick_scope": "Выберите масштаб отчёта.",
        "enter_area_example": "Введите название района (например: Dubai Marina):",
        "enter_areas_csv": "Введите названия районов через запятую:",
        "enter_areas_csv_long": "Введите названия районов через запятую (например: Dubai Marina, Business Bay, JVC):",
        "enter_building_short": "Введите название здания:",
        "not_understood_areas": "Не понял список. Введите районы через запятую.",
        "deals_pick_button": "Выберите область сделок кнопкой.",
        "lang_menu_header": "⚙️ <b>Язык интерфейса</b>\n\nВыберите язык.",
        "best_object_step1": "🏆 <b>Лучший объект</b>\n\nШаг 1 из 5 — выберите тип сделки:",
        "best_object_step2": "🏠 <b>Шаг 2 из 5</b>\n\nВыберите формат недвижимости.",
        "best_object_step3": "💰 <b>Шаг 3 из 5</b>\n\nВыберите бюджет.",
        "best_object_step4": "🛏 <b>Шаг 4 из 5</b>\n\nВыберите комнатность / наименование юнита.",
        "best_object_step5": "🎯 <b>Шаг 5 из 5</b>\n\nВыберите цель.",
        "best_object_intro": "🏆 <b>Лучший объект</b>\n\nЯ проведу по дереву выбора и подберу топ-3 района и топ-3 объекта/здания под цель, бюджет и формат.\n\nШаг 1 из 5 — выберите тип сделки:",
        "best_object_pick_deal": "Выберите тип сделки кнопкой.",
        "best_object_pick_format": "Выберите формат кнопкой.",
        "best_object_pick_budget": "Выберите бюджет кнопкой.",
        "best_object_pick_rooms": "Выберите комнатность кнопкой.",
        "best_object_pick_goal": "Выберите цель кнопкой.",
        "best_object_loading": "⌛️ <b>Ищу лучший объект</b>\n\n◇ Проверяю DLD-сделки по выбранным фильтрам.\n◇ Сравниваю районы, здания/проекты, ликвидность и цену входа.\n◇ Формирую топ-3 вариантов и вывод 360°.",
        "format_compare_header": "⚖️ <b>Сравнение форматов</b>\n\nВыберите рынок анализа:",
        "format_compare_intro": "⚖️ <b>Сравнение форматов</b>\n\nСравню апартаменты, виллы и таунхаусы по цене входа, ликвидности, приросту, выгодности и инвестиционной логике.\n\nВыберите рынок анализа:",
        "format_compare_pick_scope": "Выберите вариант кнопкой.",
        "format_compare_pick_area_list": "Выберите район из списка.",
        "format_compare_budget_header": "💰 Выберите ориентир бюджета.",
        "format_compare_goal_header": "🎯 Выберите инвестиционную цель.",
        "format_compare_period_header": "📅 Выберите период анализа.",
        "format_compare_pick_budget": "Выберите бюджет кнопкой.",
        "format_compare_pick_goal": "Выберите цель кнопкой.",
        "format_compare_pick_period": "Выберите период кнопкой.",
        "smart_pick_intro": "🧠 <b>Инвестиционный подбор</b>\n\nВыберите цель покупки.",
        "smart_budget_header": "💰 <b>Бюджет</b>\n\nВыберите ориентир бюджета.",
        "smart_timing_header": "📅 <b>Горизонт покупки</b>\n\nКогда планируется сделка?",
        "smart_risk_header": "🛡 <b>Профиль риска</b>\n\nВыберите подходящий стиль.",
        "rankings_market_pick": "📊 <b>Рейтинги рынка</b>\n\nВыберите, какой рейтинг построить.",
        "wizard_step2_property": "🏠 <b>Тип жилья?</b>",
        "wizard_step3_period": "📅 <b>Период?</b>",
        "pick_new_scenario": "🔁 Выберите новый сценарий.",
        "action_building_title": "🏢 <b>{name}</b>\n\nВыберите, что показать по зданию.",
        "action_area_title": "🏙 <b>{name}</b>\n\nВыберите, что показать по району.",
        "action_dubai_title": "🌆 <b>Рынок Дубая</b>\n\nВыберите аналитический сценарий.",
        "report_kind_full": "Обзор 360",
        "report_kind_deals": "Сделки DLD",
        "report_kind_period": "Динамика периодов",
        "report_kind_price": "Ценовая аналитика",
        "report_kind_top_buildings": "Топ зданий",
        "report_kind_default": "Аналитика",
        "report_step1_deal": "📊 <b>{kind}</b>\n\nКакие сделки?",
        "tech_error": "⚠️ Произошла техническая ошибка в сценарии. Нажмите «Главное меню» и повторите запрос.",
        "format_compare_loading": "⏳ <b>Сравниваю форматы</b>\n\n◇ Подключаю DLD-архив, live-базу и intelligence-слой.\n◇ Сравниваю апартаменты, виллы и таунхаусы.\n◇ Формирую инвестиционное заключение 360°.",
        "lead_rate_limited": "⌛️ Заявку можно отправить один раз в 10 минут. Попробуйте немного позже.",
        "lead_consult": "💼 <b>Консультация</b>\n\nОставьте заявку агенту:\n{url}",
        "pdf_after_selection": "📄 PDF можно сформировать после финального выбора района или здания.",
        "consult_link": "💼 Для консультации: https://t.me/dubai_fpr_lead_bot",
    },
    "en": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics</b>\n\nChoose language:",
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
        "error": '⚠️ This filter is too narrow for a stable sample. Try "All time", a different room type, or press "Back".',
        "choose_deal_type": "📊 Choose deal type:",
        "sale": "🏠 Sale",
        "rent": "🔑 Rent",
        "both": "📊 All",
        "m_smart_pick": "🧠 Smart Pick",
        "m_best_obj": "🏆 Best Property",
        "m_area": "🏙 Area",
        "m_building": "🏢 Building",
        "m_dubai": "🌆 Dubai",
        "m_deals": "🧾 Deals",
        "m_ratings": "📊 Rankings",
        "m_compare": "⚖️ Compare",
        "no_data": "no data",
        "no_data_area": "❌ No data for the selected area.",
        "no_data_filters": "❌ No data for the selected filters.",
        "no_data_named": "❌ No data for \"{name}\".",
        "not_found_examples": "❌ Nothing found. Try another query.\n\nExamples:\n• Grande\n• Address Opera\n• Marina Gate\n• Burj Vista",
        "searching_buildings": "🔎 Searching similar buildings...",
        "searching_buildings_db": "🔎 Searching buildings in archive + live DB...",
        "searching_area_db": "🔎 Searching area in archive + live DB...",
        "deals_dld_header": "🧾 <b>DLD Deals</b>\n\nWhere to show deals?",
        "rankings_header": "📊 <b>Market Rankings</b>\n\nChoose a ranking.",
        "rankings_pick_button": "Please pick a ranking from the buttons.",
        "full_report_header": "📑 <b>Full analytics report</b>\n\nChoose scope:",
        "full_report_pick_scope": "Please choose the report scope.",
        "enter_area_example": "Enter area name (e.g.: Dubai Marina):",
        "enter_areas_csv": "Enter area names separated by commas:",
        "enter_areas_csv_long": "Enter area names separated by commas (e.g.: Dubai Marina, Business Bay, JVC):",
        "enter_building_short": "Enter building name:",
        "not_understood_areas": "Could not parse the list. Please enter areas separated by commas.",
        "deals_pick_button": "Please pick the deals scope from the buttons.",
        "lang_menu_header": "⚙️ <b>Interface language</b>\n\nChoose language.",
        "best_object_step1": "🏆 <b>Best Property</b>\n\nStep 1 of 5 — choose deal type:",
        "best_object_step2": "🏠 <b>Step 2 of 5</b>\n\nChoose property format.",
        "best_object_step3": "💰 <b>Step 3 of 5</b>\n\nChoose budget.",
        "best_object_step4": "🛏 <b>Step 4 of 5</b>\n\nChoose bedrooms / unit type.",
        "best_object_step5": "🎯 <b>Step 5 of 5</b>\n\nChoose your goal.",
        "best_object_intro": "🏆 <b>Best Property</b>\n\nI'll walk you through a decision tree and pick top-3 areas and top-3 properties/buildings matching your goal, budget and format.\n\nStep 1 of 5 — choose deal type:",
        "best_object_pick_deal": "Please pick a deal type from the buttons.",
        "best_object_pick_format": "Please pick a format from the buttons.",
        "best_object_pick_budget": "Please pick a budget from the buttons.",
        "best_object_pick_rooms": "Please pick the number of bedrooms from the buttons.",
        "best_object_pick_goal": "Please pick a goal from the buttons.",
        "best_object_loading": "⌛️ <b>Searching for the best property</b>\n\n◇ Checking DLD deals for your filters.\n◇ Comparing areas, buildings/projects, liquidity and entry price.\n◇ Building top-3 options and 360° conclusion.",
        "format_compare_header": "⚖️ <b>Format comparison</b>\n\nChoose market to analyse:",
        "format_compare_intro": "⚖️ <b>Format comparison</b>\n\nI'll compare apartments, villas and townhouses by entry price, liquidity, growth, value and investment logic.\n\nChoose market to analyse:",
        "format_compare_pick_scope": "Please pick an option from the buttons.",
        "format_compare_pick_area_list": "Please pick an area from the list.",
        "format_compare_budget_header": "💰 Choose a budget reference.",
        "format_compare_goal_header": "🎯 Choose investment goal.",
        "format_compare_period_header": "📅 Choose analysis period.",
        "format_compare_pick_budget": "Please pick a budget from the buttons.",
        "format_compare_pick_goal": "Please pick a goal from the buttons.",
        "format_compare_pick_period": "Please pick a period from the buttons.",
        "smart_pick_intro": "🧠 <b>Investment Smart Pick</b>\n\nChoose purchase goal.",
        "smart_budget_header": "💰 <b>Budget</b>\n\nChoose a budget reference.",
        "smart_timing_header": "📅 <b>Purchase horizon</b>\n\nWhen are you planning the deal?",
        "smart_risk_header": "🛡 <b>Risk profile</b>\n\nChoose your style.",
        "rankings_market_pick": "📊 <b>Market Rankings</b>\n\nChoose which ranking to build.",
        "wizard_step2_property": "🏠 <b>Property type?</b>",
        "wizard_step3_period": "📅 <b>Period?</b>",
        "pick_new_scenario": "🔁 Choose a new scenario.",
        "action_building_title": "🏢 <b>{name}</b>\n\nWhat do you want to see for this building?",
        "action_area_title": "🏙 <b>{name}</b>\n\nWhat do you want to see for this area?",
        "action_dubai_title": "🌆 <b>Dubai market</b>\n\nChoose an analytics scenario.",
        "report_kind_full": "360 Overview",
        "report_kind_deals": "DLD Deals",
        "report_kind_period": "Period Dynamics",
        "report_kind_price": "Price Analytics",
        "report_kind_top_buildings": "Top Buildings",
        "report_kind_default": "Analytics",
        "report_step1_deal": "📊 <b>{kind}</b>\n\nWhich deals?",
        "tech_error": "⚠️ A technical error occurred in the scenario. Press \"Main menu\" and try again.",
        "format_compare_loading": "⏳ <b>Comparing formats</b>\n\n◇ Connecting DLD archive, live DB and intelligence layer.\n◇ Comparing apartments, villas and townhouses.\n◇ Building 360° investment conclusion.",
        "lead_rate_limited": "⌛️ You can submit a request once every 10 minutes. Please try again later.",
        "lead_consult": "💼 <b>Consultation</b>\n\nLeave a request for the agent:\n{url}",
        "pdf_after_selection": "📄 PDF can be generated after the final area or building selection.",
        "consult_link": "💼 For consultation: https://t.me/dubai_fpr_lead_bot",
    },
    "ar": {
        "choose_lang": "🏙 <b>Dubai DLD Analytics</b>\n\nاختر اللغة:",
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
        "error": "⚠️ هذا الفلتر ضيق جداً لعينة مستقرة. جرب «كل الفترة» أو نوع غرف آخر أو اضغط «رجوع».",
        "choose_deal_type": "📊 اختر نوع الصفقة:",
        "sale": "🏠 بيع",
        "rent": "🔑 إيجار",
        "both": "📊 الكل",
        "m_smart_pick": "🧠 اختيار ذكي",
        "m_best_obj": "🏆 أفضل عقار",
        "m_area": "🏙 المنطقة",
        "m_building": "🏢 المبنى",
        "m_dubai": "🌆 دبي",
        "m_deals": "🧾 الصفقات",
        "m_ratings": "📊 التصنيفات",
        "m_compare": "⚖️ مقارنة",
        "no_data": "لا توجد بيانات",
        "no_data_area": "❌ لا توجد بيانات للمنطقة المحددة.",
        "no_data_filters": "❌ لا توجد بيانات للفلاتر المحددة.",
        "no_data_named": "❌ لا توجد بيانات لـ «{name}».",
        "not_found_examples": "❌ لا توجد نتائج. جرب اسماً آخر.\n\nأمثلة:\n• Grande\n• Address Opera\n• Marina Gate\n• Burj Vista",
        "searching_buildings": "🔎 جاري البحث عن مبانٍ مشابهة...",
        "searching_buildings_db": "🔎 البحث في قاعدة الأرشيف والمباشرة...",
        "searching_area_db": "🔎 البحث عن المنطقة في الأرشيف والقاعدة المباشرة...",
        "deals_dld_header": "🧾 <b>صفقات DLD</b>\n\nأين تريد عرض الصفقات؟",
        "rankings_header": "📊 <b>تصنيفات السوق</b>\n\nاختر التصنيف.",
        "rankings_pick_button": "اختر التصنيف من الأزرار.",
        "full_report_header": "📑 <b>تقرير تحليلي كامل</b>\n\nاختر النطاق:",
        "full_report_pick_scope": "اختر نطاق التقرير.",
        "enter_area_example": "اكتب اسم المنطقة (مثال: Dubai Marina):",
        "enter_areas_csv": "اكتب أسماء المناطق مفصولة بفواصل:",
        "enter_areas_csv_long": "اكتب أسماء المناطق مفصولة بفواصل (مثال: Dubai Marina, Business Bay, JVC):",
        "enter_building_short": "اكتب اسم المبنى:",
        "not_understood_areas": "لم أفهم القائمة. اكتب المناطق مفصولة بفواصل.",
        "deals_pick_button": "اختر نطاق الصفقات من الأزرار.",
        "lang_menu_header": "⚙️ <b>لغة الواجهة</b>\n\nاختر اللغة.",
        "best_object_step1": "🏆 <b>أفضل عقار</b>\n\nالخطوة 1 من 5 — اختر نوع الصفقة:",
        "best_object_step2": "🏠 <b>الخطوة 2 من 5</b>\n\nاختر نوع العقار.",
        "best_object_step3": "💰 <b>الخطوة 3 من 5</b>\n\nاختر الميزانية.",
        "best_object_step4": "🛏 <b>الخطوة 4 من 5</b>\n\nاختر عدد الغرف / نوع الوحدة.",
        "best_object_step5": "🎯 <b>الخطوة 5 من 5</b>\n\nاختر الهدف.",
        "best_object_intro": "🏆 <b>أفضل عقار</b>\n\nسأمر معك بشجرة قرار وأختار أفضل 3 مناطق و3 عقارات/مبانٍ تناسب هدفك وميزانيتك ونمطك.\n\nالخطوة 1 من 5 — اختر نوع الصفقة:",
        "best_object_pick_deal": "اختر نوع الصفقة من الأزرار.",
        "best_object_pick_format": "اختر النوع من الأزرار.",
        "best_object_pick_budget": "اختر الميزانية من الأزرار.",
        "best_object_pick_rooms": "اختر عدد الغرف من الأزرار.",
        "best_object_pick_goal": "اختر الهدف من الأزرار.",
        "best_object_loading": "⌛️ <b>البحث عن أفضل عقار</b>\n\n◇ فحص صفقات DLD وفق الفلاتر.\n◇ مقارنة المناطق والمباني والسيولة وسعر الدخول.\n◇ توليد أفضل 3 خيارات وخلاصة 360°.",
        "format_compare_header": "⚖️ <b>مقارنة الأنواع</b>\n\nاختر السوق للتحليل:",
        "format_compare_intro": "⚖️ <b>مقارنة الأنواع</b>\n\nسأقارن الشقق والفيلات والتاون هاوس من حيث سعر الدخول والسيولة والنمو والقيمة ومنطق الاستثمار.\n\nاختر السوق للتحليل:",
        "format_compare_pick_scope": "اختر خياراً من الأزرار.",
        "format_compare_pick_area_list": "اختر المنطقة من القائمة.",
        "format_compare_budget_header": "💰 اختر مرجع الميزانية.",
        "format_compare_goal_header": "🎯 اختر هدف الاستثمار.",
        "format_compare_period_header": "📅 اختر فترة التحليل.",
        "format_compare_pick_budget": "اختر الميزانية من الأزرار.",
        "format_compare_pick_goal": "اختر الهدف من الأزرار.",
        "format_compare_pick_period": "اختر الفترة من الأزرار.",
        "smart_pick_intro": "🧠 <b>اختيار استثماري ذكي</b>\n\nاختر هدف الشراء.",
        "smart_budget_header": "💰 <b>الميزانية</b>\n\nاختر مرجع الميزانية.",
        "smart_timing_header": "📅 <b>أفق الشراء</b>\n\nمتى تخطط للصفقة؟",
        "smart_risk_header": "🛡 <b>ملف المخاطرة</b>\n\nاختر النمط المناسب.",
        "rankings_market_pick": "📊 <b>تصنيفات السوق</b>\n\nاختر التصنيف الذي تريد بناءه.",
        "wizard_step2_property": "🏠 <b>نوع العقار؟</b>",
        "wizard_step3_period": "📅 <b>الفترة؟</b>",
        "pick_new_scenario": "🔁 اختر سيناريو جديد.",
        "action_building_title": "🏢 <b>{name}</b>\n\nماذا تريد أن ترى عن هذا المبنى؟",
        "action_area_title": "🏙 <b>{name}</b>\n\nماذا تريد أن ترى عن هذه المنطقة؟",
        "action_dubai_title": "🌆 <b>سوق دبي</b>\n\nاختر سيناريو تحليلي.",
        "report_kind_full": "نظرة 360",
        "report_kind_deals": "صفقات DLD",
        "report_kind_period": "ديناميكيات الفترات",
        "report_kind_price": "تحليلات الأسعار",
        "report_kind_top_buildings": "أفضل المباني",
        "report_kind_default": "تحليلات",
        "report_step1_deal": "📊 <b>{kind}</b>\n\nأي صفقات؟",
        "tech_error": "⚠️ حدث خطأ تقني في السيناريو. اضغط «القائمة الرئيسية» وحاول مرة أخرى.",
        "format_compare_loading": "⏳ <b>مقارنة الأنواع</b>\n\n◇ توصيل أرشيف DLD والقاعدة المباشرة وطبقة الذكاء.\n◇ مقارنة الشقق والفيلات والتاون هاوس.\n◇ توليد خلاصة استثمارية 360°.",
        "lead_rate_limited": "⌛️ يمكنك إرسال طلب واحد كل 10 دقائق. حاول لاحقاً.",
        "lead_consult": "💼 <b>استشارة</b>\n\nاترك طلباً للوكيل:\n{url}",
        "pdf_after_selection": "📄 يمكن إنشاء PDF بعد اختيار المنطقة أو المبنى نهائياً.",
        "consult_link": "💼 للاستشارة: https://t.me/dubai_fpr_lead_bot",
    },
}


PROPERTY_OPTIONS = [
    "Studio", "1 BR", "2 BR", "3 BR", "4 BR", "5 BR+",
    "Apartment", "Villa", "Townhouse", "Penthouse", "Office", "Shop"
]


AREA_ALIASES = {
    # FIX 2026-06-03 (DLD_ANALYTICS_FIX): verified vs Rent-sale-arhiv (dld_sales_unified).
    # JVC official DLD names include "Jumeirah Village Circle" (modern label) and
    # legacy sub-community codes ("Al Yufrah 1", "Al Barsha South Fourth"). ILIKE
    # '%jumeirah%' was catching JBR/JLT/JVT — switched to ANY-equality match below.
    "jvc": ["Jumeirah Village Circle", "Al Yufrah 1", "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jumeirah village circle": ["Jumeirah Village Circle", "Al Yufrah 1", "Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "jvt": ["Jumeirah Village Triangle"],
    "jumeirah village triangle": ["Jumeirah Village Triangle"],
    "jbr": ["Jumeirah Beach Residence"],
    "jumeirah beach residence": ["Jumeirah Beach Residence"],

    "downtown": ["Downtown Dubai", "Burj Khalifa"],
    "downtown dubai": ["Downtown Dubai", "Burj Khalifa"],
    "dubai downtown": ["Downtown Dubai", "Burj Khalifa"],

    "dubai marina": ["Dubai Marina", "Marsa Dubai"],
    "marina": ["Dubai Marina", "Marsa Dubai"],
    "marsa dubai": ["Dubai Marina", "Marsa Dubai"],

    "business bay": ["Business Bay"],
    "palm": ["Palm Jumeirah"],
    "palm jumeirah": ["Palm Jumeirah"],
    "jlt": ["Jumeirah Lakes Towers", "Jumeirah Lake Towers"],
    "jumeirah lakes towers": ["Jumeirah Lakes Towers", "Jumeirah Lake Towers"],
    "creek": ["Dubai Creek Harbour", "Creek"],
    "dubai creek": ["Dubai Creek Harbour", "Creek"],
    "sobha": ["Sobha Hartland"],
    "sobha hartland": ["Sobha Hartland"],
    "damac hills": ["Damac Hills", "Damac Hills 2", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "dubailand": ["Dubai Land", "Wadi Al Safa 5", "Wadi Al Safa 7"],
    "dso": ["Dubai Silicon Oasis", "Silicon Oasis", "Nadd Hessa"],
    "silicon oasis": ["Dubai Silicon Oasis", "Silicon Oasis", "Nadd Hessa"],
    "sports city": ["Dubai Sports City", "Al Hebiah Fourth"],
    "dubai sports city": ["Dubai Sports City", "Al Hebiah Fourth"],
    "jumeirah golf estates": ["Jumeirah Golf Estates", "Me'Aisem First"],
    "arabian ranches": ["Arabian Ranches", "Wadi Al Safa 6", "Wadi Al Safa 7"],
    "mbr city": ["Mohammed Bin Rashid City", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "mohammed bin rashid city": ["Mohammed Bin Rashid City", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "dubai hills": ["Dubai Hills Estate", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "dubai hills estate": ["Dubai Hills Estate", "Hadaeq Sheikh Mohammed Bin Rashid"],
    "difc": ["DIFC", "Zaabeel Second"],
}

# FIX 2026-06-03 (DLD_ANALYTICS_FIX): aliases that must be matched EXACTLY
# (no ILIKE %x% — that catches false positives like JBR ⊂ "jumeirah" partial).
# Multi-word DLD area names go here; short tokens with no risk of collision
# (e.g. "creek") stay on legacy ILIKE path.
_AREA_EXACT_KEYS = {
    "jvc", "jumeirah village circle", "jvt", "jumeirah village triangle",
    "jbr", "jumeirah beach residence", "downtown", "downtown dubai", "dubai downtown",
    "dubai marina", "marina", "marsa dubai", "business bay", "palm", "palm jumeirah",
    "jlt", "jumeirah lakes towers", "damac hills", "dubailand", "dso", "silicon oasis",
    "sports city", "dubai sports city", "jumeirah golf estates", "arabian ranches",
    "mbr city", "mohammed bin rashid city", "dubai hills", "dubai hills estate", "difc",
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
    # Default language for new users is English; they pick from welcome screen.
    # LANG_FIX: user_languages is a process-local dict that resets on every
    # restart. Without hydration from bot_users.language, restarts silently
    # downgrade every existing user to EN until they hit /start again. On a
    # cache miss, pull the persisted language and back-fill the in-memory map.
    cached = user_languages.get(user_id)
    if cached:
        return cached
    try:
        from bot_user_tracker import get_persisted_lang
        persisted = get_persisted_lang(user_id)
        if persisted and persisted in TEXTS:
            user_languages[user_id] = persisted
            return persisted
    except Exception:
        pass
    return "en"


def tr(user_id, key):
    user_lang = lang(user_id)
    if user_lang not in TEXTS:
        user_lang = "en"
    return TEXTS.get(user_lang, TEXTS["en"]).get(key, TEXTS["en"].get(key, key))


def trf(user_id, key, **kwargs):
    """tr() with str.format() interpolation. Falls back safely if a placeholder is missing."""
    try:
        return tr(user_id, key).format(**kwargs)
    except Exception:
        return tr(user_id, key)


def _is_menu_btn(text, key):
    """Returns True if text matches the given key in ANY language (for robust handler matching)."""
    return text in {TEXTS[l].get(key, "") for l in TEXTS}


def kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def language_menu():
    # B044: EN first (default lang), then RU, then AR
    return kb([["🇬🇧 English"], ["🇷🇺 Русский"], ["🇦🇪 العربية"]])


def _main_menu_v64_legacy(user_id):
    """v64 legacy (orphan — переопределяется v72 ниже). Оставлено для истории."""
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
    """v55 compact: 4 rows. Bedrooms on one line; property types on next two lines.
    All previously available choices retained — just reorganised to feel lighter."""
    return kb([
        ["Studio", "1 BR", "2 BR"],
        ["3 BR", "4 BR", "5 BR+"],
        ["Apartment", "Villa", "Townhouse"],
        ["Penthouse", "Office", "Shop"],
        [tr(user_id, "skip"), tr(user_id, "back"), tr(user_id, "main")],
    ])


def period_menu(user_id):
    """v55 compact: 3 rows. Order matches user task — Last year / 3m / 6m first."""
    return kb([
        [tr(user_id, "p12"), tr(user_id, "p3"), tr(user_id, "p6")],
        [tr(user_id, "p36"), tr(user_id, "all_time"), tr(user_id, "skip")],
        [tr(user_id, "back"), tr(user_id, "main")],
    ])



def smart_goal_menu(user_id):
    return kb([
        ["⚡ Быстрый подбор"],
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
    # PDF row hidden via _pdf_enabled() — 2026-06-03 manual disable.
    rows = [
        ["🏆 Лучший формат"],
        ["🏙 Лучшие районы", "🏢 Лучшие здания"],
    ]
    if _pdf_enabled():
        rows.append(["📄 PDF", "💼 Заявка"])
    else:
        rows.append(["💼 Заявка"])
    rows.append(["🔁 Новый отчёт", tr(user_id, "main")])
    return kb(rows)


def result_menu(user_id, scope=None):
    """Адаптивное меню после готового результата: только релевантные действия."""
    # PDF row hidden when feature off — 2026-06-03 manual disable.
    pdf_row = ["📄 PDF", "💼 Заявка"] if _pdf_enabled() else ["💼 Заявка"]
    rows = [
        pdf_row,
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
    # PDF row hidden when feature off — 2026-06-03 manual disable.
    pdf_row = ["📄 PDF", "💼 Заявка"] if _pdf_enabled() else ["💼 Заявка"]
    return kb([
        ["📊 Аналитика", "💼 Резюме"],
        ["📈 Периоды", "🧾 Сделки"],
        pdf_row,
        [tr(user_id, "back"), tr(user_id, "main")],
    ])


def no_data_menu(user_id):
    """v200: компактное меню для экрана 'нет данных'.
    Показываем только два релевантных действия — изменить фильтр (Назад) и Главное меню.
    Прячем Аналитику/PDF/Заявку и т.п., потому что без данных они бесполезны и сбивают пользователя.
    """
    return kb([
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
    # Level 5 KG read-through with hardcoded fallback (hot-path safe, cached 5min).
    try:
        from shared.knowledge_graph.integration import resolve_aliases as _kg_resolve  # type: ignore
        kg_hit = _kg_resolve(q, category="area_alias",
                              legacy_map=AREA_ALIASES, default=None)
        if kg_hit:
            return kg_hit
    except Exception:
        pass
    return AREA_ALIASES.get(q, [clean_query(query)])


def make_area_exact_condition(query):
    # FIX 2026-06-03 (DLD_ANALYTICS_FIX): for known multi-word areas (JVC, JBR,
    # JLT, etc.) use EXACT equality via ANY(%s::text[]) instead of ILIKE '%x%'.
    # Old code matched JBR/JLT/JVT when user typed "jumeirah" — wrong dataset.
    values = [v for v in area_alias_values(query) if v]

    if not values:
        return "AND 1=0", []

    q_key = clean_query(query).lower()
    if q_key in _AREA_EXACT_KEYS:
        return (
            "AND COALESCE(area_name_en::text, '') = ANY(%s::text[])",
            [values],
        )

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
    # FIX 2026-05-29: short DLD registry names only (no marketing suffixes).
    "grande signature": ["grande"],
    "grande signature residences": ["grande"],
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
    # Level 5 KG read-through (timeout 100ms, falls back to hardcoded BUILDING_ALIASES).
    # If KG is unavailable or empty, returns the legacy mapping unchanged.
    try:
        from shared.knowledge_graph.integration import resolve_aliases as _kg_resolve  # type: ignore
        kg_hit = _kg_resolve(q, category="building_alias",
                              legacy_map=BUILDING_ALIASES, default=None)
        if kg_hit:
            return kg_hit
    except Exception:
        pass
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
    # FIX 2026-06-03 (DLD_ANALYTICS_FIX): default to 12 months when not specified.
    # Previously averaging 23 years of DLD archive (2002→2025) produced meaningless
    # numbers (JVC showed 192k deals × 1.5 inflation vs reality).
    # Explicit opt-in for full archive: pass period="all".
    if not period:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"

    p = str(period).strip().lower()

    if p in ["all", "all time", "all_time", "всё время", "все время"]:
        return ""
    if p in ["3", "3m", "3 мес", "3 месяца", "3 months"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '3 months'"
    if p in ["6", "6m", "6 мес", "6 месяцев", "6 months"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '6 months'"
    if p in ["12", "1", "1y", "1 год", "год", "12 months", "1 year"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"
    if p in ["24", "2y", "2 года", "24 months", "2 years"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '24 months'"
    if p in ["36", "3y", "3 года", "36 months", "3 years"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '36 months'"
    if p in ["60", "5y", "5 лет", "60 months", "5 years"]:
        return "AND safe_date >= CURRENT_DATE - INTERVAL '60 months'"

    # Unknown period token → default to 12mo rather than all-time.
    return "AND safe_date >= CURRENT_DATE - INTERVAL '12 months'"


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
    if p in ["all", "all time", "all_time", "всё время", "все время"]:
        return None  # explicit all-time
    if p in ["3", "3m", "3 мес", "3 месяца", "3 months"]:
        return 3
    if p in ["6", "6m", "6 мес", "6 месяцев", "6 months"]:
        return 6
    if p in ["12", "1", "1y", "1 год", "год", "12 months", "1 year"]:
        return 12
    if p in ["24", "2y", "2 года", "24 months", "2 years"]:
        return 24
    if p in ["36", "3y", "3 года", "36 months", "3 years"]:
        return 36
    if p in ["60", "5y", "5 лет", "60 months", "5 years"]:
        return 60
    # FIX 2026-06-03: default 12mo aligned with period_condition.
    return 12


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
    # NOTE: instance_date может приходить как ISO 'YYYY-MM-DD', legacy 'DD-MM-YYYY'
    # или 'DD/MM/YYYY' (Dubai Pulse). Используем shared safe_date_sql,
    # иначе regression — все DD-MM-YYYY → NULL → "нет данных".
    try:
        from safe_coerce import safe_date_sql as _sd
        date_expr = _sd("instance_date")
    except Exception:
        # Fallback на старый код, расширенный на DD-MM-YYYY (defensive).
        date_expr = (
            "CASE "
            "WHEN instance_date::text ~ '^\\d{4}-\\d{2}-\\d{2}' THEN (instance_date::text)::date "
            "WHEN instance_date::text ~ '^\\d{2}-\\d{2}-\\d{4}$' THEN to_date(instance_date::text, 'DD-MM-YYYY') "
            "WHEN instance_date::text ~ '^\\d{2}/\\d{2}/\\d{4}$' THEN to_date(instance_date::text, 'DD/MM/YYYY') "
            "ELSE NULL END"
        )
    return f"""
        FROM (
            SELECT
                *,
                {date_expr} AS safe_date
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
    # FIX 2026-06-03 (DLD_SQL_SWEEP): default 12mo date filter for find_areas legacy path.
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
                      AND safe_date >= CURRENT_DATE - INTERVAL '12 months'
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
        f"📐 Средняя цена за м²: <b>{format_money(avg_meter)}</b>\n\n"
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
        f"📐 Средняя цена за м²: <b>{format_money(row['avg_meter'])}</b>\n"
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
            f"📐 Цена за м²: <b>{format_pct(meter_change)}</b>\n"
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


_LEGACY_AREA_UNIVERSE = {
    "🏡 Для жизни": [
        ("Downtown Dubai", ["Burj Khalifa"]),
        ("Dubai Marina", ["Marsa Dubai"]),
        ("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]),
        ("Business Bay", ["Business Bay"]),
        ("Palm Jumeirah", ["Palm Jumeirah"]),
    ],
    "🔑 Аренда": [
        ("Dubai Marina", ["Marsa Dubai"]),
        ("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]),
        ("Business Bay", ["Business Bay"]),
        ("Downtown Dubai", ["Burj Khalifa"]),
        ("JLT", ["Jumeirah Lakes Towers"]),
    ],
    "_default": [
        ("JVC", ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"]),
        ("Business Bay", ["Business Bay"]),
        ("Dubai Marina", ["Marsa Dubai"]),
        ("Downtown Dubai", ["Burj Khalifa"]),
        ("Sobha Hartland", ["Sobha Hartland"]),
        ("JLT", ["Jumeirah Lakes Towers"]),
    ],
}


def _goal_to_ranking_key(goal: str) -> str:
    """Map UI goal label to area_rankings.goal key."""
    if goal == "🏡 Для жизни":
        return "living"
    if goal == "🔑 Аренда":
        return "rental"
    return "resale"


def smart_area_universe(goal, limit: int = 6):
    """DLD-data-driven area picker.

    First tries shared.area_rankings (refreshed weekly by area_rankings cron).
    Falls back to legacy hardcoded list if the table is empty / unreachable
    so wizards never break while #126 builders run.
    """
    try:
        from shared.area_rankings.query import query_area_rankings_top  # noqa: WPS433
        rkey = _goal_to_ranking_key(goal)
        rows = query_area_rankings_top(rkey, limit=limit)
        if rows:
            out = []
            for r in rows:
                name = r.get("area_name")
                syns = r.get("area_synonyms") or [name]
                if name:
                    out.append((name, syns))
            if out:
                return out
    except Exception as e:
        try:
            logger.warning("smart_area_universe: area_rankings read failed, fallback (%s)", e)
        except Exception:
            pass
    return _LEGACY_AREA_UNIVERSE.get(goal, _LEGACY_AREA_UNIVERSE["_default"])



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
                            # FIX (SMART_PICK_HUMAN): meter_sale_price in DLD archive is ALREADY per m^2
                            # (verified directly: p50 ~ 11.7K AED/m^2, raw_meter for JVC studios ~16.5K).
                            # Multiplying by SQFT_TO_M2 (10.7639) inflated to 178K/m^2 in legacy reports.
                            # Use raw meter_sale_price here. Period: 12 months (was 36 -> 192K deals).
                            raw_meter_expr = num_sql('meter_sale_price')
                            cur.execute(f"""
                                SELECT
                                    COUNT(*) AS deals,
                                    COUNT(DISTINCT building_name_en) AS buildings,
                                    AVG({PRICE}) AS avg_price,
                                    MIN({PRICE}) AS min_price,
                                    MAX({PRICE}) AS max_price,
                                    AVG({raw_meter_expr}) AS avg_meter,
                                    MIN(safe_date) AS first_deal,
                                    MAX(safe_date) AS last_deal
                                {base_from()}
                                  AND ({area_conditions})
                                  {prop_sql}
                                  AND {PRICE} IS NOT NULL
                                  AND {PRICE} >= %s
                                  AND {PRICE} <= %s
                                  AND safe_date >= CURRENT_DATE - INTERVAL '12 months'
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
        f"📐 Средняя цена за м²: <b>{format_money(best['avg_meter'])}</b>\n"
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
                    f"цена за м² {format_pct(meter_change)}. "
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
        f"📐 Средняя цена за м²: <b>{format_money(row['avg_meter'])}</b>\n"
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
            conclusion = "Рынок по выбранному фильтру показывает рост: средний чек и цена за м² выше предыдущего аналогичного периода."
        elif price_change < 0 and meter_change < 0:
            conclusion = "Рынок по выбранному фильтру просел: средний чек и цена за м² ниже предыдущего аналогичного периода. Это может давать окно для переговоров."
        elif price_change > 0 and meter_change < 0:
            conclusion = "Средний чек вырос, но цена за м² снизилась. Вероятно, в текущем периоде было больше крупных или нестандартных сделок."
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
        f"📐 Цена за м²: <b>{format_money(current['avg_meter'])}</b>\n\n"
        f"<b>Предыдущий аналогичный период</b>\n"
        f"📊 Сделок: <b>{format_int(previous.get('deals'))}</b>\n"
        f"💰 {value_name}: <b>{format_money(previous['avg_price'])}</b>\n"
        f"📐 Цена за м²: <b>{format_money(previous['avg_meter'])}</b>\n\n"
        f"<b>Динамика</b>\n"
        f"{arrow(deals_change)} Сделки: <b>{format_pct(deals_change)}</b>\n"
        f"{arrow(price_change)} {value_name}: <b>{format_pct(price_change)}</b>\n"
        f"{arrow(meter_change)} Цена за м²: <b>{format_pct(meter_change)}</b>\n\n"
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

RENT_TABLE = "public.dld_rents_full"


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
    # Используем shared safe_date_sql — поддерживает DD-MM-YYYY/DD/MM/YYYY/ISO/epoch.
    try:
        from safe_coerce import safe_date_sql as _sd
        return _sd(qcol(col).strip('"'))
    except Exception:
        # Fallback: ISO + DD-MM-YYYY + DD/MM/YYYY вручную (без shared).
        c = qcol(col)
        return (
            "CASE "
            f"WHEN {c}::text ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}' THEN ({c}::text)::date "
            f"WHEN {c}::text ~ '^\\d{{2}}-\\d{{2}}-\\d{{4}}$' THEN to_date({c}::text, 'DD-MM-YYYY') "
            f"WHEN {c}::text ~ '^\\d{{2}}/\\d{{2}}/\\d{{4}}$' THEN to_date({c}::text, 'DD/MM/YYYY') "
            "ELSE NULL END"
        )


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

    # FIX (RENT_ANNUALIZE B057): приоритет — annual_*-поля. Если их нет, делим
    # contract_amount на длительность (years) если есть start/end даты. Иначе
    # contract_amount as-is. Это решает баг 800K за JVC 1BR (multi-year totals).
    _annual_candidates = ["annual_amount", "annual_rent", "annual_rent_amount", "rent_value"]
    _contract_candidates = ["contract_amount", "contract_value", "ejari_contract_amount",
                            "rent_amount", "amount", "actual_worth"]
    _annual_col = first_existing(cols, _annual_candidates)
    _contract_col = first_existing(cols, _contract_candidates)
    _start_col = first_existing(cols, ["contract_start_date", "start_date", "from_date", "lease_start"])
    _end_col = first_existing(cols, ["contract_end_date", "end_date", "to_date", "lease_end"])

    def _num(c):
        return f"NULLIF(regexp_replace({qcol(c)}::text, '[^0-9.]', '', 'g'), '')::numeric"

    if _annual_col and _contract_col and _start_col and _end_col:
        # COALESCE(annual, contract / max(1, years))
        _years_expr = (
            f"GREATEST(1.0, EXTRACT(EPOCH FROM ("
            f"NULLIF({qcol(_end_col)}::text, '')::timestamp - "
            f"NULLIF({qcol(_start_col)}::text, '')::timestamp"
            f"))/31557600.0)"
        )
        rent_price = f"COALESCE({_num(_annual_col)}, {_num(_contract_col)} / {_years_expr})"
    elif _annual_col:
        rent_price = _num(_annual_col)
    elif _contract_col and _start_col and _end_col:
        _years_expr = (
            f"GREATEST(1.0, EXTRACT(EPOCH FROM ("
            f"NULLIF({qcol(_end_col)}::text, '')::timestamp - "
            f"NULLIF({qcol(_start_col)}::text, '')::timestamp"
            f"))/31557600.0)"
        )
        rent_price = f"({_num(_contract_col)} / {_years_expr})"
    elif _contract_col:
        rent_price = _num(_contract_col)
    else:
        rent_price = "NULL::numeric"
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


_RENT_COLS_CACHE = {}


def _rent_col(field):
    """v52 FIX: schema-aware колонка для rent tables.
    Реальные колонки в dld_rents_full: area_en, project_en (нет building_en/building_name_en!),
    prop_type_en, prop_sub_type_en, rooms_en, annual_amount, contract_amount.
    Раньше hardcoded building_name_en / area_name_en / property_sub_type_en —
    SQL крэшил → user видел "no data".
    """
    candidates = {
        "building": ["building_name_en", "building_en", "building", "project_en", "project_name_en"],
        "area":     ["area_name_en", "area_en", "area"],
        "rooms":    ["rooms_en", "rooms"],
        "prop_type":     ["property_type_en", "prop_type_en"],
        "prop_subtype":  ["property_sub_type_en", "prop_sub_type_en"],
    }
    if field in _RENT_COLS_CACHE:
        return _RENT_COLS_CACHE[field]
    # Try to find which one exists in RENT_TABLE
    try:
        cols = set()
        for t in ("dld_rents_full", "dld_rent_archive"):
            try:
                tcols = table_columns(t)
                cols.update(tcols or [])
            except Exception:
                pass
        lowered = {c.lower(): c for c in cols}
        for cand in candidates.get(field, []):
            if cand.lower() in lowered:
                _RENT_COLS_CACHE[field] = lowered[cand.lower()]
                return _RENT_COLS_CACHE[field]
    except Exception:
        pass
    # Fallback: first candidate name (queries will use it; if missing, fail gracefully)
    _RENT_COLS_CACHE[field] = candidates.get(field, [""])[0]
    return _RENT_COLS_CACHE[field]


def rent_scope_condition(scope, name):
    if not name:
        return "", []
    if scope == "building":
        col = _rent_col("building")
        return f" AND {col} ILIKE %s" if col else "", [f"%{name}%"] if col else []
    if scope == "area":
        col = _rent_col("area")
        return f" AND {col} ILIKE %s" if col else "", [f"%{name}%"] if col else []
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
    """v52 FIX: schema-aware rent columns. property_sub_type_en → prop_sub_type_en in dld_rents_full."""
    if not prop:
        return "", []
    rooms_col = _rent_col("rooms") or "rooms_en"
    pt_col    = _rent_col("prop_type") or "prop_type_en"
    pst_col   = _rent_col("prop_subtype") or "prop_sub_type_en"
    p = str(prop).lower().strip()
    if p == "studio":
        return f"AND ({rooms_col} ILIKE %s OR {pst_col} ILIKE %s)", ["%studio%", "%studio%"]
    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        return (f"AND ({rooms_col} ILIKE %s OR {rooms_col} = %s OR {pst_col} ILIKE %s)",
                [f"%{n}%", n, f"%{n}%"])
    if p == "5 br+":
        return (f"AND ({rooms_col} ILIKE %s OR {rooms_col} ILIKE %s OR {rooms_col} ILIKE %s OR {rooms_col} ILIKE %s OR {rooms_col} ILIKE %s)",
                ["%5%", "%6%", "%7%", "%8%", "%9%"])
    val = f"%{prop}%"
    return f"AND ({pt_col} ILIKE %s OR {pst_col} ILIKE %s)", [val, val]


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
    meter_label = "Аренда за sqft" if rent else "Цена за м²"

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
    meter_label = "Аренда за sqft" if rent else "Цена за м²"

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
    """B045: при возврате назад показываем prompt текущего step c сохранёнными filters.
    Покрывает все wizard'ы (building/area/best_object/format_compare/smart/full_report/ranking/deals)."""
    user_id = message.from_user.id
    step = state.get("step")

    if not step:
        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
        return

    # Single-object search wizards
    if step == "building_query":
        await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
        return
    if step == "area_query":
        await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
        return
    if step == "choose_building":
        suggestions = state.get("suggestions") or [o.get("label") or o.get("name") for o in (state.get("building_options") or [])]
        buttons = [[name] for name in suggestions[:8] if name]
        buttons.append([tr(user_id, "back"), tr(user_id, "main")])
        await message.answer(tr(user_id, "choose_building"), reply_markup=kb(buttons))
        return
    if step == "choose_area":
        suggestions = state.get("suggestions", [])
        buttons = [[name] for name in suggestions[:8]]
        buttons.append([tr(user_id, "back"), tr(user_id, "main")])
        await message.answer(tr(user_id, "choose_area"), reply_markup=kb(buttons))
        return

    # Generic 3-step filter funnel
    if step == "choose_deal_type":
        await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
        return
    if step == "choose_property":
        await message.answer(tr(user_id, "choose_property"), reply_markup=property_menu(user_id))
        return
    if step == "choose_period":
        await message.answer(tr(user_id, "choose_period"), reply_markup=period_menu(user_id))
        return
    if step == "choose_report":
        await message.answer(tr(user_id, "choose_report"), reply_markup=report_menu(user_id))
        return

    # Action menus (after building/area selected)
    if step == "building_action":
        await message.answer(_action_title_v72(state, user_id), reply_markup=building_action_menu(user_id))
        return
    if step == "area_action":
        await message.answer(_action_title_v72(state, user_id), reply_markup=area_action_menu(user_id))
        return
    if step == "dubai_action":
        await message.answer(_action_title_v72(state, user_id), reply_markup=dubai_action_menu(user_id))
        return

    # Deals / Rankings / Full report scopes
    if step == "deals_scope":
        await message.answer(tr(user_id, "deals_dld_header"), reply_markup=deals_scope_menu(user_id))
        return
    if step == "ranking_menu":
        await message.answer(tr(user_id, "rankings_header"), reply_markup=ranking_menu(user_id))
        return
    if step == "full_report_menu":
        await message.answer(tr(user_id, "full_report_header"), reply_markup=full_report_menu(user_id))
        return
    if step == "full_report_area_query":
        await message.answer(tr(user_id, "enter_area_example"), reply_markup=back_menu(user_id))
        return
    if step == "full_report_multi_areas":
        await message.answer(tr(user_id, "enter_areas_csv"), reply_markup=back_menu(user_id))
        return
    if step == "full_report_building_query":
        await message.answer(tr(user_id, "enter_building_short"), reply_markup=back_menu(user_id))
        return

    # Best object wizard
    if step == "best_object_deal_type":
        await message.answer(tr(user_id, "best_object_step1"), reply_markup=best_object_deal_type_menu(user_id))
        return
    if step == "best_object_format":
        await message.answer(tr(user_id, "best_object_step2"), reply_markup=best_object_format_menu(user_id))
        return
    if step == "best_object_budget":
        await message.answer(tr(user_id, "best_object_step3"), reply_markup=best_object_budget_menu(user_id))
        return
    if step == "best_object_rooms":
        await message.answer(tr(user_id, "best_object_step4"), reply_markup=best_object_rooms_menu(user_id))
        return
    if step == "best_object_goal":
        await message.answer(tr(user_id, "best_object_step5"), reply_markup=best_object_goal_menu(user_id))
        return

    # Format comparison wizard
    if step == "format_compare_scope":
        await message.answer(tr(user_id, "format_compare_header"), reply_markup=format_compare_scope_menu(user_id))
        return
    if step == "format_compare_area_query":
        await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
        return
    if step == "format_compare_choose_area":
        suggestions = state.get("area_suggestions", [])
        buttons = [[x] for x in suggestions[:8]]
        buttons.append([tr(user_id, "back"), tr(user_id, "main")])
        await message.answer(tr(user_id, "choose_area"), reply_markup=kb(buttons))
        return
    if step == "format_compare_budget":
        await message.answer(tr(user_id, "format_compare_budget_header"), reply_markup=format_compare_budget_menu(user_id))
        return
    if step == "format_compare_goal":
        await message.answer(tr(user_id, "format_compare_goal_header"), reply_markup=format_compare_goal_menu(user_id))
        return
    if step == "format_compare_period":
        await message.answer(tr(user_id, "format_compare_period_header"), reply_markup=format_compare_period_menu(user_id))
        return

    # Smart investment wizard
    if step == "smart_goal":
        await message.answer(tr(user_id, "smart_pick_intro"), reply_markup=smart_goal_menu(user_id))
        return
    if step == "smart_budget":
        await message.answer(tr(user_id, "smart_budget_header"), reply_markup=smart_budget_menu(user_id))
        return
    if step == "smart_timing":
        await message.answer(tr(user_id, "smart_timing_header"), reply_markup=smart_timing_menu(user_id))
        return
    if step == "smart_risk":
        await message.answer(tr(user_id, "smart_risk_header"), reply_markup=smart_risk_menu(user_id))
        return

    # Result / unknown → fall back to main
    await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))



async def start_building_search_from_text(message, text):
    user_id = message.from_user.id

    try:
        await message.answer(tr(user_id, "searching_buildings"))
        rows = find_buildings(text)
    except Exception as e:
        print("START BUILDING SEARCH ERROR:", repr(e))
        rows = []

    if not rows:
        # B045: остаёмся на step=building_query (history не трогаем, чтобы Back вёл к scope-выбору)
        cur = user_states.get(user_id, {}) or {}
        cur["step"] = "building_query"
        cur["scope"] = "building"
        user_states[user_id] = cur
        await message.answer(
            tr(user_id, "not_found_examples"),
            reply_markup=back_menu(user_id)
        )
        return

    suggestions = []
    for r in rows:
        name = r.get("building_name_en")
        if name and name not in suggestions:
            suggestions.append(name)

    # B045: push текущий building_query на history, чтобы Back из choose_building → ввод
    cur = user_states.get(user_id, {}) or {}
    force_kind = cur.get("force_kind")
    push_state(user_id, {
        "step": "choose_building",
        "scope": "building",
        "suggestions": suggestions,
        **({"force_kind": force_kind} if force_kind else {}),
    })

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


_NO_DATA_BODY = {
    "ru": {
        "no_data_period": "По выбранным фильтрам нет достаточных данных за этот период.",
        "hint_year": "💡 За год по «{name}» в DLD: <b>{n:,} сделок</b>.",
        "hint_2y":   "💡 За 2 года: <b>{n:,} сделок</b>.",
        "hint_all":  "💡 Всего в архиве: <b>{n:,} сделок</b>.",
        "what_to_do": (
            "Что можно сделать:\n"
            "• выбрать «Всё время» — увидите все сделки;\n"
            "• увеличить период до 6 / 12 месяцев;\n"
            "• выбрать «Пропустить» в типе юнита;\n"
            "• попробовать 1 BR / 2 BR / Studio;\n"
            "• проверить другое здание или район."
        ),
    },
    "en": {
        "no_data_period": "Not enough data for the selected filters in this period.",
        "hint_year": "💡 Past year for \"{name}\" in DLD: <b>{n:,} deals</b>.",
        "hint_2y":   "💡 Past 2 years: <b>{n:,} deals</b>.",
        "hint_all":  "💡 Total in archive: <b>{n:,} deals</b>.",
        "what_to_do": (
            "What you can do:\n"
            "• pick \"All time\" — see all deals;\n"
            "• extend period to 6 / 12 months;\n"
            "• press \"Skip\" on unit type;\n"
            "• try 1 BR / 2 BR / Studio;\n"
            "• check another building or area."
        ),
    },
    "ar": {
        "no_data_period": "لا توجد بيانات كافية للفلاتر المحددة في هذه الفترة.",
        "hint_year": "💡 في السنة الماضية لـ «{name}» في DLD: <b>{n:,} صفقة</b>.",
        "hint_2y":   "💡 خلال السنتين الماضيتين: <b>{n:,} صفقة</b>.",
        "hint_all":  "💡 الإجمالي في الأرشيف: <b>{n:,} صفقة</b>.",
        "what_to_do": (
            "ماذا يمكنك أن تفعل:\n"
            "• اختر «كل الفترة» — لرؤية كل الصفقات؛\n"
            "• وسّع الفترة إلى 6 / 12 شهراً؛\n"
            "• اضغط «تخطي» على نوع الوحدة؛\n"
            "• جرب 1 BR / 2 BR / Studio؛\n"
            "• جرب مبنى أو منطقة أخرى."
        ),
    },
}


def no_data_message(title="Аналитика", *, scope=None, name=None,
                     prop=None, period=None, deal_type=None, user_id=None):
    """v52: enrich сообщение реальной подсказкой + log event для мониторинга."""
    # Async-ish DB log so we know what users hit this
    try:
        _log_no_data_event(title, scope, name, prop, period, deal_type, user_id)
    except Exception:
        pass

    ulang = lang(user_id) if user_id is not None else "en"
    if ulang not in _NO_DATA_BODY:
        ulang = "en"
    L = _NO_DATA_BODY[ulang]

    # Smart hint: try wider periods + show counts
    hint_lines = []
    if name and scope in ("area", "building"):
        try:
            cnts = _count_deals_by_period(scope, name)
            if cnts:
                if cnts.get(365):
                    hint_lines.append(L["hint_year"].format(name=name, n=cnts[365]))
                if cnts.get(730):
                    hint_lines.append(L["hint_2y"].format(n=cnts[730]))
                if cnts.get('all'):
                    hint_lines.append(L["hint_all"].format(n=cnts['all']))
        except Exception as _e:
            print(f"[no_data hint err] {_e}")

    msg = f"⚠️ <b>{title}</b>\n\n"
    msg += L["no_data_period"] + "\n\n"
    if hint_lines:
        msg += "\n".join(hint_lines) + "\n\n"
    msg += L["what_to_do"]
    return msg


def _count_deals_by_period(scope, name):
    """Quick count of deals для 365д / 730д / всё время. Без блокировки UI (best-effort)."""
    if not name or scope not in ("area", "building"):
        return None
    out = {}
    try:
        with _archive_conn() as ac, ac.cursor() as cur:
            col = "building_name_en" if scope == "building" else "area_name_en"
            for days_label, where in [
                (365, "CASE WHEN instance_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN instance_date::date ELSE NULL END > CURRENT_DATE - INTERVAL '365 days'"),
                (730, "CASE WHEN instance_date ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN instance_date::date ELSE NULL END > CURRENT_DATE - INTERVAL '730 days'"),
                ('all', "1=1"),
            ]:
                try:
                    cur.execute(f"""
                        SELECT COUNT(*) FROM dld_sale_archive
                         WHERE LOWER(TRIM({col})) = LOWER(TRIM(%s))
                           AND NULLIF(actual_worth,'')::numeric > 100000
                           AND {where}
                    """, (name,))
                    out[days_label] = cur.fetchone()[0]
                except Exception:
                    pass
    except Exception:
        return None
    return out


def _archive_conn():
    """Helper для no_data hint queries. Использует ARCHIVE_DATABASE_URL."""
    import psycopg2
    url = os.environ.get("ARCHIVE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def _log_no_data_event(title, scope, name, prop, period, deal_type, user_id):
    """Логирует в intelligence DB событие 'no_data' чтобы watchdog мог отчитываться о таких UX-fail."""
    intel_url = os.environ.get("INTELLIGENCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not intel_url:
        return
    try:
        import psycopg2
        with psycopg2.connect(intel_url, connect_timeout=3) as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_no_data_events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    user_id BIGINT,
                    title TEXT,
                    scope TEXT,
                    name TEXT,
                    prop TEXT,
                    period TEXT,
                    deal_type TEXT
                )
            """)
            cur.execute("""
                INSERT INTO user_no_data_events (user_id, title, scope, name, prop, period, deal_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, title[:100], scope, (name or '')[:100],
                  (prop or '')[:50], (period or '')[:20], (deal_type or '')[:10]))
            c.commit()
    except Exception as e:
        print(f"[no_data log err] {e}")


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
    # B027 fix: _num() returns None for null inputs; max(None, 0) raises
    # TypeError("'>' not supported between 'int' and 'NoneType'"). Coerce None → 0.
    deals = max(_num(row.get("deals")) or 0, 0)
    avg_price = max(_num(row.get("avg_price")) or 0, 0)
    avg_meter = max(_num(row.get("avg_meter")) or 0, 0)
    min_price = max(_num(row.get("min_price")) or 0, 0)
    max_price = max(_num(row.get("max_price")) or 0, 0)
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


def show_format_best_areas(prop, period=None, budget=None, *, user_id=None):
    rows = format_compare_best_areas(prop, period, budget)
    if not rows:
        return no_data_message("Лучшие районы", user_id=user_id)
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


def show_format_best_buildings(prop, area=None, period=None, *, user_id=None):
    scope = "area" if area else "dubai"
    name = area if area else None
    try:
        rows = get_top_buildings_in_scope(scope, name, period, "sale", limit=8)
    except Exception as e:
        print("FORMAT_BEST_BUILDINGS_ERROR:", repr(e))
        rows = []
    if not rows:
        return no_data_message("Лучшие здания", user_id=user_id)
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

@dp.message(Command("menu"))
async def cmd_menu_global(message: Message):
    """v134 UX: /menu — глобальный возврат в главное меню из любого state
    (включая wizard mid-step: smart_goal, best_object, format_compare, etc.).
    Сбрасывает FSM-state через reset_to_main + transient ✓ pattern (B019)."""
    user_id = message.from_user.id
    reset_to_main(user_id)
    try:
        trans = await message.answer("✓", reply_markup=ReplyKeyboardRemove())
        await trans.delete()
    except Exception as _e:
        print(f"[analytics] /menu reply-kb clear failed: {_e}", flush=True)
    await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))


# ── Layer 11: Causal Reasoning — /why <area or free text> ─────────────────
@dp.message(Command("why"))
async def cmd_why_causal(message: Message):
    """🧠 Объяснить тренд / почему такая цена / ROI / спрос.
    Пример: /why Marina rent down  |  /why почему ROI JVC упал"""
    user_id = message.from_user.id
    lang = user_languages.get(user_id, "en")
    text = (message.text or "").split(maxsplit=1)
    q = text[1] if len(text) > 1 else (
        "Объясни текущий тренд рынка Dubai" if lang == "ru"
        else "Explain current Dubai market trend"
    )
    try:
        import sys as _sys
        if r"C:\Projects" not in _sys.path:
            _sys.path.insert(0, r"C:\Projects")
        from shared.causal_engine import explain, format_chain  # type: ignore
        # crude area detection
        area = None
        for a in ["Marina","Downtown","JVC","Business Bay","Palm","Dubailand","Hills"]:
            if a.lower() in q.lower():
                area = a; break
        chain = explain(q, {"area": area or "Dubai"},
                        user_id=user_id, bot_source="analytics")
        await message.answer(format_chain(chain, lang=lang), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[analytics][why] failed: {e}", flush=True)
        await message.answer("🧠 Анализ временно недоступен." if lang == "ru"
                             else "🧠 Analysis temporarily unavailable.")


@dp.message(Command("cancel"))
async def cmd_cancel_global(message: Message):
    """v134 UX: /cancel — отмена текущего wizard/сценария + главное меню."""
    user_id = message.from_user.id
    reset_to_main(user_id)
    lang = user_languages.get(user_id, "en")
    cancel_txt = {"ru": "❌ Действие отменено.",
                  "en": "❌ Action cancelled.",
                  "ar": "❌ تم الإلغاء."}.get(lang, "❌ Cancelled.")
    try:
        trans = await message.answer("✓", reply_markup=ReplyKeyboardRemove())
        await trans.delete()
    except Exception as _e:
        print(f"[analytics] /cancel reply-kb clear failed: {_e}", flush=True)
    await message.answer(cancel_txt)
    await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))


@dp.message(CommandStart())
async def start_handler(message: Message):
    user_id = message.from_user.id
    user_states[user_id] = {}
    # v52: parse deep-link payload from other bots
    # /start bld-NAME → pre-load building report
    # /start area-NAME → pre-load area report
    # /start from_XXX → just welcome screen
    text = (message.text or "").strip()
    payload = ""
    if " " in text:
        payload = text.split(" ", 1)[1].strip()
    # v55: cross-bot UTM tracking
    try:
        from cross_bot_utm import log_jump_async
        log_jump_async("analytics", user_id, payload)
    except Exception as _e:
        print(f"[analytics] cbj log err: {_e}", flush=True)
    if payload.startswith("bld-") or payload.startswith("bld_"):
        bld = payload[4:].replace("_", " ").strip()
        if bld:
            user_states[user_id] = {"step": "full_report_done", "history": []}
            try:
                await send_full_market_report(message, "building", bld)
                return
            except Exception as e:
                print(f"[deeplink bld] {e}")
    elif payload.startswith("area-") or payload.startswith("area_"):
        area = payload[5:].replace("_", " ").strip()
        if area:
            user_states[user_id] = {"step": "full_report_done", "history": []}
            try:
                await send_full_market_report(message, "area", area)
                return
            except Exception as e:
                print(f"[deeplink area] {e}")
    # default: show welcome / language picker (with logo if available)
    # v108.1: restored welcome text + logo fallback after Layla UX cut
    welcome_text = (
        "🏙 <b>Dubai DLD Analytics</b>\n"
        "<i>Real-time market intelligence · UAE</i>\n\n"
        "🇬🇧 <b>200K+ transactions · 52 areas · Price trends · ROI rankings · Building reports · PDF export</b>\n"
        "Dubai property market analytics powered by official DLD data — find the best ROI, compare areas, track price dynamics, get full building reports.\n\n"
        "🇷🇺 <b>200K+ сделок · 52 района · Динамика цен · Рейтинги ROI · Отчёты по зданиям · PDF-экспорт</b>\n"
        "Аналитика рынка недвижимости Дубая на официальных данных DLD — лучший ROI, сравнение районов, динамика цен, полные отчёты по зданиям.\n\n"
        "🌐 <b>Choose language / Выберите язык / اختر اللغة</b> ⬇️"
    )
    # v132: file_id cache for instant /start after first upload (avoids 15s sendPhoto timeout)
    global _ANALYTICS_LOGO_FILE_ID
    if _ANALYTICS_LOGO_FILE_ID:
        try:
            await message.answer_photo(_ANALYTICS_LOGO_FILE_ID, caption=welcome_text, reply_markup=language_menu())
            return
        except Exception as _e0:
            print(f"[welcome logo cached] {_e0}")
            _ANALYTICS_LOGO_FILE_ID = None  # type: ignore[assignment]
    try:
        from aiogram.types import FSInputFile
        import os as _os
        _logo_candidates = ["analytics_logo.png", "logo.png", "logo.jpg", "AB_logo.png"]
        _here = _os.path.dirname(_os.path.abspath(__file__))
        _logo = None
        for _name in _logo_candidates:
            _p = _os.path.join(_here, _name)
            if _os.path.isfile(_p):
                _logo = _p
                break
        if _logo:
            try:
                _sent = await message.answer_photo(FSInputFile(_logo), caption=welcome_text, reply_markup=language_menu())
                try:
                    if _sent.photo:
                        _ANALYTICS_LOGO_FILE_ID = _sent.photo[-1].file_id  # type: ignore[assignment]
                        print(f"[welcome logo] file_id cached: {_ANALYTICS_LOGO_FILE_ID[:20]}...")
                except Exception:
                    pass
                return
            except Exception as _e2:
                print(f"[welcome logo send] {_e2}")
    except Exception as _e:
        print(f"[welcome logo] {_e}")
    # Fallback: text only (no logo file present)
    await message.answer(welcome_text, reply_markup=language_menu())


@dp.message(lambda m: m.text in ["🇷🇺 Русский", "🇬🇧 English", "🇦🇪 العربية"])
async def language_handler(message: Message):
    if message.text == "🇷🇺 Русский":
        user_languages[message.from_user.id] = "ru"
    elif message.text == "🇬🇧 English":
        user_languages[message.from_user.id] = "en"
    else:
        user_languages[message.from_user.id] = "ar"

    # LANG_FIX: persist the picked language to bot_users so future restarts
    # can hydrate it back. Without this the next process restart silently
    # downgrades the user to EN. Fire-and-forget; failure is non-fatal.
    try:
        from bot_user_tracker import track_user_async
        track_user_async(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            language=user_languages[message.from_user.id],
            action="message",
        )
    except Exception:
        pass

    user_states[message.from_user.id] = {}
    await message.answer(tr(message.from_user.id, "lang_selected"), reply_markup=main_menu(message.from_user.id))



# =========================
# ADAPTIVE PRODUCT MENU v72
# Простая иерархия: объект -> тип отчёта -> фильтры -> результат.
# Главное правило: на каждом экране только те кнопки, которые имеют смысл в текущем шаге.
# =========================

def legacy_main_menu_keyboard(user_id):
    """PHASE BN N1 legacy: предыдущее главное меню (5 рядов с Прогнозом рынка).
    Сохранено для backward compat — некоторые users могли захардкодить эти кнопки
    в saved keyboards. Все handlers по-прежнему работают, callbacks те же.
    Текущий main_menu() ниже — компактный (4 ряда core + ⚡ Pro)."""
    return kb([
        [tr(user_id, "m_smart_pick"), tr(user_id, "m_best_obj")],
        [tr(user_id, "m_area"), tr(user_id, "m_building")],
        [tr(user_id, "m_dubai"), tr(user_id, "m_deals")],
        [tr(user_id, "m_ratings"), tr(user_id, "m_compare")],
        ["🔮 Прогноз рынка"],
    ])


def main_menu(user_id):
    """PHASE BN N1: упрощённое главное меню = только core функции.
    Advanced (forecast, causal, compare-format, ratings) — под ⚡ Pro кнопкой.
    Старые названия (m_compare, m_ratings) остались как handlers в pro_menu,
    callback patterns не сломаны."""
    return kb([
        [tr(user_id, "m_smart_pick"), tr(user_id, "m_best_obj")],
        [tr(user_id, "m_area"), tr(user_id, "m_building")],
        [tr(user_id, "m_dubai"), tr(user_id, "m_deals")],
        ["⚡ Pro / 🌟 Супер-фишки"],
    ])


def pro_menu(user_id):
    """PHASE BN N1: подменю Pro features.
    Advanced инструменты не в главном меню, но в одном клике.
    Все кнопки используют существующие text-handlers (m_ratings,
    m_compare, 🔮 Прогноз рынка, 📊 Причинно-следственный анализ) — никакие
    handlers не дублируются и не ломаются."""
    causal_btn = "📊 Causal analysis" if (user_languages.get(user_id, "ru") == "en") else "📊 Причинно-следственный анализ"
    return kb([
        [tr(user_id, "m_ratings"), tr(user_id, "m_compare")],
        ["🔮 Прогноз рынка"],
        [causal_btn],
        [tr(user_id, "main")],
    ])


def full_report_menu(user_id):
    return kb([
        ["🌆 Отчёт Дубай"],
        ["🏙 Отчёт район", "🏘 Несколько районов"],
        ["🏢 Отчёт здание"],
        [tr(user_id, "main")],
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
    # PDF row hidden when feature off — 2026-06-03 manual disable.
    pdf_row = ["📄 PDF", "💼 Заявка"] if _pdf_enabled() else ["💼 Заявка"]
    return kb([
        pdf_row,
        ["🔁 Новый отчёт", "🔁 Изменить"],
        [tr(user_id, "main")],
    ])


def _action_title_v72(state, user_id=None):
    scope = state.get("scope")
    name = _display_scope_name_v71(state.get("name")) if "_display_scope_name_v71" in globals() else state.get("name")
    uid = user_id if user_id is not None else state.get("_uid")
    if scope == "building":
        return trf(uid, "action_building_title", name=name)
    if scope == "area":
        return trf(uid, "action_area_title", name=name)
    return tr(uid, "action_dubai_title")


def _report_kind_label_v72(kind, user_id=None):
    key = {
        "full": "report_kind_full",
        "deals": "report_kind_deals",
        "period": "report_kind_period",
        "price": "report_kind_price",
        "top_buildings": "report_kind_top_buildings",
    }.get(kind, "report_kind_default")
    return tr(user_id, key)


async def _ask_action_menu_v72(message, state):
    user_id = message.from_user.id
    scope = state.get("scope")
    user_states[user_id] = state
    if scope == "building":
        await message.answer(_action_title_v72(state, user_id), reply_markup=building_action_menu(user_id))
    elif scope == "area":
        await message.answer(_action_title_v72(state, user_id), reply_markup=area_action_menu(user_id))
    else:
        await message.answer(_action_title_v72(state, user_id), reply_markup=dubai_action_menu(user_id))


async def _start_filters_for_report_v72(message, state, kind):
    user_id = message.from_user.id
    state["report_kind"] = kind
    state["step"] = "choose_deal_type"
    user_states[user_id] = state
    await message.answer(
        trf(user_id, "report_step1_deal", kind=_report_kind_label_v72(kind, user_id)),
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
        await send_full_report(message, scope, name, prop, period, deal_type, _report_kind_label_v72(kind, message.from_user.id))


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
    return text in ["🧠 Подбор", "🏢 Здание", "🏆 Лучший объект", "🏙 Район", "📊 Рейтинги", "⚖️ Сравнение", "⚖️ Сравнение форматов", "🧾 Сделки", "🌆 Дубай", "📑 Полный отчёт"]


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
        f"средняя цена за м² <b>{format_money(avg_meter)}</b>.\n\n"
        f"Инвестиционный вывод: <b>{risk}</b>. "
        "Перед покупкой стоит сравнить конкретный юнит с последними сделками, этажом, видом, состоянием, сервисными сборами и реальной арендной ставкой."
    )


async def send_full_report(message, scope, name=None, prop=None, period=None, deal_type=None, title_prefix="Полная аналитика"):
    user_id = message.from_user.id
    await send_processing(message)
    row, used_prop, used_period, used_deal_type = get_stats_smart(scope, name, prop, period, deal_type)
    if not row or not _int(row.get("deals")):
        try:
            if _BT_OK:
                _bt_log(bot_name="analytics", user_id=user_id,
                        query_type="full_report",
                        query_params={"scope": scope, "name": name, "prop": prop,
                                      "period": period, "deal_type": deal_type},
                        outcome="empty", result_count=0)
        except Exception: pass
        await message.answer(no_data_message(title_prefix, scope=scope, name=name, prop=prop, period=period, deal_type=deal_type, user_id=user_id), reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
        return

    title = _human_report_title(scope, name, title_prefix)
    html = show_stats(f"<b>{title}</b>", row, used_prop, used_period, used_deal_type)
    html += _build_360_conclusion(row, scope, name, title_prefix)
    if (used_prop, used_period, used_deal_type) != (prop, period, deal_type):
        html += "\n\nℹ️ По точному фильтру выборка была узкой, поэтому показана ближайшая стабильная DLD-выборка."
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))
    try:
        if _BT_OK:
            _iid = _bt_log(bot_name="analytics", user_id=user_id,
                           query_type="full_report",
                           query_params={"scope": scope, "name": name, "prop": prop,
                                         "period": period, "deal_type": deal_type},
                           result_count=int(_int(row.get("deals")) or 0),
                           outcome="success",
                           bot_response_preview=title[:300] if title else None)
            if _iid and _bt_fb_enabled():
                _kb = _bt_feedback_kb(_iid, lang="ru")
                if _kb:
                    try: await message.answer("Полезен отчёт?", reply_markup=_kb)
                    except Exception: pass
    except Exception: pass


async def send_period_report(message, scope, name=None, prop=None, period=None, deal_type=None):
    user_id = message.from_user.id
    await send_processing(message)
    period = period or "12"
    comparison = get_comparison(scope, name, prop, period, deal_type)
    if not comparison:
        await message.answer(
            no_data_message("Сравнение периодов", scope=scope, name=name,
                             prop=prop, period=period, deal_type=deal_type, user_id=user_id),
            reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id)
        )
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
    # v52 FIX: try/except + централизованный error_logger
    try:
        rows, used_prop, used_period, used_deal_type = get_latest_deals_smart(scope, name, prop, period, deal_type)
    except Exception as e:
        print("SEND_DEALS_REPORT_SQL_ERROR:", repr(e), "scope=", scope, "name=", name, "prop=", prop, "period=", period, "deal_type=", deal_type)
        try:
            import error_logger as _el, traceback as _tb
            _el.log_error("analytics", "send_deals_report", str(e),
                           error_class=type(e).__name__, user_id=user_id,
                           context={"scope": scope, "name": name, "prop": prop,
                                    "period": period, "deal_type": deal_type},
                           tb=_tb.format_exc()[-1500:])
        except Exception:
            pass
        await message.answer(
            no_data_message("Последние сделки", scope=scope, name=name,
                             prop=prop, period=period, deal_type=deal_type, user_id=user_id),
            reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id)
        )
        return
    if not rows:
        await message.answer(
            no_data_message("Последние сделки", scope=scope, name=name,
                             prop=prop, period=period, deal_type=deal_type, user_id=user_id),
            reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id)
        )
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
            f"📐 {format_money(r.get('meter_price'))} за м²\n\n"
        )
    # v149: removed auto-360° from deals report.
    # User can press "Резюме" / "Аналитика" to see it explicitly.
    # Also: warn user about silent sale↔rent fallback so they know filter changed.
    if deal_type and used_deal_type and deal_type != used_deal_type:
        try:
            fallback_label = "аренду" if used_deal_type == "rent" else "продажу"
            html += f"\n⚠️ <i>По выбранному фильтру не нашлось — показаны данные на {fallback_label}.</i>\n"
        except Exception:
            pass
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
        await message.answer(no_data_message("Рейтинг", user_id=user_id), reply_markup=ranking_menu(user_id))
        return
    html = f"<b>{title}</b>\n\n"
    for i, r in enumerate(rows[:10], 1):
        html += (
            f"{i}. 🏢 <b>{r.get('building_name_en') or '-'}</b>\n"
            f"📍 {r.get('area_name_en') or '-'}\n"
            f"📊 Сделки: <b>{format_int(r.get('deals'))}</b>\n"
            f"💰 Средняя цена: <b>{format_money(r.get('avg_price'))}</b>\n"
            f"📐 Цена за м²: <b>{format_money(r.get('avg_meter'))}</b>\n\n"
        )
    html += _build_360_conclusion(rows[0], "dubai", None, "rating")
    set_last_report(user_id, title, html, "dubai")
    await message.answer(html, reply_markup=_final_actions_menu(user_id, "dubai"))


async def start_building_search_from_text(message, text):
    user_id = message.from_user.id
    await message.answer(tr(user_id, "searching_buildings_db"), reply_markup=process_menu(user_id))
    rows = safe_call(find_buildings, text, 10, default=[]) or []
    if not rows:
        user_states[user_id] = {"step": "building_query", "scope": "building", "history": user_states.get(user_id, {}).get("history", [])}
        await message.answer(
            tr(user_id, "not_found_examples"),
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
    await message.answer(tr(user_id, "searching_area_db"), reply_markup=process_menu(user_id))
    rows = safe_call(find_areas, text, 10, default=[]) or []
    if not rows:
        cur = user_states.get(user_id, {}) or {}
        cur["step"] = "area_query"
        cur["scope"] = "area"
        user_states[user_id] = cur
        await message.answer(
            tr(user_id, "not_found_examples"),
            reply_markup=back_menu(user_id),
        )
        return
    suggestions = []
    for r in rows:
        n = r.get("area_name_en")
        if n and n not in suggestions:
            suggestions.append(n)
    # B045: push area_query на history, чтобы Back из choose_area → ввод имени
    cur = user_states.get(user_id, {}) or {}
    force_kind = cur.get("force_kind")
    push_state(user_id, {
        "step": "choose_area", "scope": "area", "suggestions": suggestions,
        **({"force_kind": force_kind} if force_kind else {}),
    })
    buttons = [[name] for name in suggestions[:8]] + [[tr(user_id, "back"), tr(user_id, "main")]]
    html = tr(user_id, "choose_area") + "\n\n"
    for i, r in enumerate(rows[:8], 1):
        html += f"{i}. <b>{r.get('area_name_en')}</b> ({format_int(r.get('deals'))} сделок)\n"
    await message.answer(html, reply_markup=kb(buttons))


@dp.message()
async def main_handler(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    # v51 ANTISPAM: bypass для admin
    try:
        import antispam as _as
        is_adm = user_id in ADMIN_IDS
        if _as.is_spam(user_id, text, is_admin=is_adm):
            return
    except Exception:
        pass
    state = user_states.get(user_id, {}) or {}

    # Phase BL: log interaction (best-effort)
    try:
        if _BT_OK:
            _mtype = "command" if text.startswith("/") else "text"
            _qt = "analytics"
            if text.startswith("/"):
                _qt = text.split()[0].lstrip("/")[:32] or "command"
            _bt_log(
                bot_name="analytics",
                user_id=user_id,
                user_message=text[:500],
                user_message_type=_mtype,
                query_type=_qt,
                query_params={"step": state.get("step")} if state else None,
                language=(message.from_user.language_code or "")[:8],
            )
    except Exception:
        pass

    # Phase BM L9: remember turn (background, fail-open)
    try:
        _bm_safe_record_turn(
            user_id,
            language=(message.from_user.language_code or "ru")[:8],
            last_user_text=text[:500] if text else None,
        )
    except Exception:
        pass

    try:
        # Служебные команды — не засоряют главное меню.
        if text in ["/language", "/settings", "⚙️ Настройки", "⚙️ Язык", "⚙️ Settings", "⚙️ الإعدادات"]:
            user_states[user_id] = {"step": "settings", "history": []}
            await message.answer(tr(user_id, "lang_menu_header"), reply_markup=language_menu())
            return
        if text == "/pdf" or text == "📄 PDF":
            await handle_pdf_request(message)
            return
        if text in ["/admin", "👑 Админ", "👑 Админ-панель"]:
            await handle_admin_dashboard(message)
            return
        # v50/v51: admin trigger for daily_reports (manual regen if cron failed)
        # FIX: was ADMIN_ID undefined → use ADMIN_IDS set
        # v112: admin /stats — cross-bot conversions за последние 7 дней
        if text in ["/stats", "📈 /stats"] and user_id in ADMIN_IDS:
            await _send_cross_bot_stats(message)
            return
        if text in ["/trigger_daily_reports", "/run_reports"] and user_id in ADMIN_IDS:
            await message.answer("⏳ Запускаю daily_reports.run_daily() …")
            try:
                import daily_reports, asyncio as _asyncio
                loop = _asyncio.get_event_loop()
                await loop.run_in_executor(None, daily_reports.run_daily, 150, 300)
                await message.answer("✅ Отчёты обновлены.")
            except Exception as _e:
                await message.answer(f"❌ Ошибка: {_e}")
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
            # B019 fix: wizard reply-keyboard не всегда заменяется новой reply-keyboard
            # (Telegram-баг, особенно iOS). Сначала transient "✓" с remove_keyboard,
            # удаляем — это сбрасывает wizard-клавиатуру на клиенте — потом main menu.
            # Pattern из channel-bot v132.7 (commit fc6b0ee).
            try:
                trans = await message.answer("✓", reply_markup=ReplyKeyboardRemove())
                await trans.delete()
            except Exception as _e:
                print(f"[analytics] B019 main-menu reply-kb clear failed: {_e}", flush=True)
            await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))
            return
        if text == tr(user_id, "back") or text == "⬅️ Назад":
            prev = go_back(user_id)
            await show_current_state_prompt(message, prev)
            return
        if text in ["💼 Заявка", "💼 Консультация"]:
            await handle_consultation_request(message)
            return
        # PHASE BN N1: open Pro submenu
        if text == "⚡ Pro / 🌟 Супер-фишки":
            user_states[user_id] = {"step": "pro_menu", "history": []}
            await message.answer(
                "⚡ <b>Pro features — продвинутая аналитика и AI-инструменты</b>\n\n"
                "• 📊 Рейтинги — топ зданий/районов\n"
                "• ⚖️ Сравнение форматов — apt vs villa и т.п.\n"
                "• 🔮 Прогноз рынка — Market World Model\n"
                "• 📊 Causal analysis — причинно-следственный анализ\n\n"
                "<i>Vadim Realty (RERA BRN 65011)</i>",
                reply_markup=pro_menu(user_id),
            )
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
                # B019 fix: see main-menu handler above.
                try:
                    trans = await message.answer("✓", reply_markup=ReplyKeyboardRemove())
                    await trans.delete()
                except Exception as _e:
                    print(f"[analytics] B019 main-menu reply-kb clear failed: {_e}", flush=True)
                await message.answer(tr(user_id, "pick_new_scenario"), reply_markup=main_menu(user_id))
                return
            # Если человек нажал старую кнопку отчёта из результата — обработаем мягко.
            if text in ["📊 Аналитика", "💼 Резюме", "📊 Обзор 360"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "full"})
                return
            if _is_menu_btn(text, "m_deals") or text in ["🧾 Сделки"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "deals"})
                return
            if text in ["📈 Периоды", "📈 Динамика"]:
                await _execute_selected_report_v72(message, {**state, "report_kind": "period"})
                return

        # Главное меню: 6 понятных сценариев.
        if _is_menu_btn(text, "m_smart_pick"):
            user_states[user_id] = {"step": "smart_goal", "history": []}
            await message.answer(tr(user_id, "smart_pick_intro"), reply_markup=smart_goal_menu(user_id))
            return
        if _is_menu_btn(text, "m_compare") or text == "⚖️ Сравнение форматов":
            user_states[user_id] = {"step": "format_compare_scope", "history": []}
            await message.answer(
                tr(user_id, "format_compare_intro"),
                reply_markup=format_compare_scope_menu(user_id)
            )
            return
        if _is_menu_btn(text, "m_best_obj"):
            user_states[user_id] = {"step": "best_object_deal_type", "history": []}
            await message.answer(
                tr(user_id, "best_object_intro"),
                reply_markup=best_object_deal_type_menu(user_id)
            )
            return
        if _is_menu_btn(text, "m_building"):
            user_states[user_id] = {"step": "building_query", "scope": "building", "history": []}
            await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
            return
        if _is_menu_btn(text, "m_area"):
            user_states[user_id] = {"step": "area_query", "scope": "area", "history": []}
            await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
            return
        if _is_menu_btn(text, "m_deals") and state.get("step") not in ["building_action", "area_action", "dubai_action", "result"]:
            user_states[user_id] = {"step": "deals_scope", "history": []}
            await message.answer(tr(user_id, "deals_dld_header"), reply_markup=deals_scope_menu(user_id))
            return
        if _is_menu_btn(text, "m_ratings"):
            user_states[user_id] = {"step": "ranking_menu", "history": []}
            await message.answer(
                tr(user_id, "rankings_market_pick"),
                reply_markup=ranking_menu(user_id),
            )
            return
        if _is_menu_btn(text, "m_dubai"):
            st = {"step": "dubai_action", "scope": "dubai", "name": None, "history": []}
            await _ask_action_menu_v72(message, st)
            return
        # PHASE BM Layer 13: Market World Model forecast button
        if text == "🔮 Прогноз рынка":
            user_states[user_id] = {"step": "mwm_forecast_query", "history": []}
            await message.answer(
                "🔮 <b>Прогноз рынка Дубая</b>\n\n"
                "Напишите вопрос свободным текстом, например:\n"
                "• <i>Что будет с Marina через 6 месяцев?</i>\n"
                "• <i>Dubai Marina vs Business Bay</i>\n"
                "• <i>Прогноз Downtown 12 мес</i>\n"
                "• <i>Сценарий: Business Bay supply +30%</i>",
                reply_markup=back_menu(user_id),
            )
            return
        if state.get("step") == "mwm_forecast_query":
            try:
                from shared.market_world_model import api as _mwm
                lang = "ru"
                ans = _mwm.ask(text, lang=lang)
                await message.answer(ans, reply_markup=main_menu(user_id))
            except Exception as e:
                await message.answer(
                    f"Не удалось построить прогноз: {e}",
                    reply_markup=main_menu(user_id),
                )
            user_states[user_id] = {"step": None, "history": []}
            return
        if text == "📑 Полный отчёт":
            user_states[user_id] = {"step": "full_report_menu", "history": []}
            await message.answer(
                "📑 <b>Полный аналитический отчёт</b>\n\n"
                "Готовые ежедневные отчёты с медианными ценами по типам квартир, "
                "ROI, динамикой за месяц/год. Выберите масштаб:",
                reply_markup=full_report_menu(user_id),
            )
            return
        # Меню полного отчёта.
        if state.get("step") == "full_report_menu":
            if text == "🌆 Отчёт Дубай":
                await send_full_market_report(message, "dubai", None)
                return
            if text == "🏙 Отчёт район":
                push_state(user_id, {"step": "full_report_area_query"})
                await message.answer(tr(user_id, "enter_area_example"), reply_markup=back_menu(user_id))
                return
            if text == "🏘 Несколько районов":
                push_state(user_id, {"step": "full_report_multi_areas", "areas": []})
                await message.answer(
                    tr(user_id, "enter_areas_csv_long"),
                    reply_markup=back_menu(user_id),
                )
                return
            if text == "🏢 Отчёт здание":
                push_state(user_id, {"step": "full_report_building_query"})
                await message.answer(tr(user_id, "enter_building_short"), reply_markup=back_menu(user_id))
                return
            await message.answer(tr(user_id, "full_report_pick_scope"), reply_markup=full_report_menu(user_id))
            return
        if state.get("step") == "full_report_area_query":
            await send_full_market_report(message, "area", text.strip())
            return
        if state.get("step") == "full_report_multi_areas":
            areas = [a.strip() for a in text.split(",") if a.strip()]
            if not areas:
                await message.answer(tr(user_id, "not_understood_areas"), reply_markup=back_menu(user_id))
                return
            await send_multi_area_report(message, areas)
            return
        if state.get("step") == "full_report_building_query":
            await send_full_market_report(message, "building", text.strip())
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
            await message.answer(tr(user_id, "rankings_pick_button"), reply_markup=ranking_menu(user_id))
            return

        # Сделки: выбор области.
        if state.get("step") == "deals_scope":
            if text == "🏢 По зданию":
                push_state(user_id, {"step": "building_query", "scope": "building", "force_kind": "deals"})
                await message.answer(tr(user_id, "enter_building"), reply_markup=back_menu(user_id))
                return
            if text == "🏙 По району":
                push_state(user_id, {"step": "area_query", "scope": "area", "force_kind": "deals"})
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
                return
            if text == "🌆 По Дубаю":
                push_state(user_id, {"scope": "dubai", "name": None})
                await _start_filters_for_report_v72(message, user_states[user_id], "deals")
                return
            await message.answer(tr(user_id, "deals_pick_button"), reply_markup=deals_scope_menu(user_id))
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
                # B062: Обзор 360 — сразу отчёт с дефолтами (sale + all rooms + 12mo),
                # без 3-step wizard. Юзер: "оптимизировать чтобы было всё просто".
                _quick_state = {
                    **state,
                    "report_kind": "full",
                    "deal_type": state.get("deal_type") or "sale",
                    "property": state.get("property") or None,  # None = all
                    "period": state.get("period") or "12",
                }
                await _execute_selected_report_v72(message, _quick_state)
                return
            if text == "🧾 Сделки":
                await _start_filters_for_report_v72(message, state, "deals")
                return
            if text == "📈 Динамика":
                # B062: Динамика — сразу с дефолтами 12mo sale.
                _quick_state = {
                    **state,
                    "report_kind": "period",
                    "deal_type": state.get("deal_type") or "sale",
                    "property": state.get("property") or None,
                    "period": state.get("period") or "12",
                }
                await _execute_selected_report_v72(message, _quick_state)
                return
            if text == "💰 Цены":
                await _start_filters_for_report_v72(message, state, "price")
                return
            if text == "🏢 Топ зданий":
                await send_ranking_report(message, "active")
                return
            if text == "📊 Рейтинги":
                user_states[user_id] = {"step": "ranking_menu", "history": state.get("history", [])}
                await message.answer(tr(user_id, "rankings_header"), reply_markup=ranking_menu(user_id))
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
            new_state = dict(state)
            new_state["deal_type"] = _skip_to_none_v86(_normalize_deal_type_from_text(user_id, text))
            new_state["step"] = "choose_property"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "wizard_step2_property"), reply_markup=property_menu(user_id))
            return

        if state.get("step") == "choose_property":
            new_state = dict(state)
            new_state["property"] = _skip_to_none_v86(_normalize_property_from_text(user_id, text))
            new_state["step"] = "choose_period"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "wizard_step3_period"), reply_markup=period_menu(user_id))
            return

        if state.get("step") == "choose_period":
            state["period"] = _skip_to_none_v86(_normalize_period_from_text(user_id, text))
            await _execute_selected_report_v72(message, state)
            return

        # Best object funnel — отдельный сценарий, не меняет существующие меню и отчёты.
        if state.get("step") == "best_object_deal_type":
            allowed = ["🏠 Продажа", "🔑 Аренда", "📊 Неважно", tr(user_id, "skip"),
                       tr(user_id, "sale"), tr(user_id, "rent"), tr(user_id, "both")]
            if text not in allowed:
                await message.answer(tr(user_id, "best_object_pick_deal"), reply_markup=best_object_deal_type_menu(user_id))
                return
            new_state = dict(state)
            new_state["deal_type"] = None if text in ["📊 Неважно", tr(user_id, "skip"), tr(user_id, "both")] else text
            new_state["step"] = "best_object_format"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "best_object_step2"), reply_markup=best_object_format_menu(user_id))
            return

        if state.get("step") == "best_object_format":
            allowed = ["🏢 Апартаменты", "🏘 Таунхаус", "🏡 Вилла", "🌍 Plot / Land", "📊 Неважно", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer(tr(user_id, "best_object_pick_format"), reply_markup=best_object_format_menu(user_id))
                return
            new_state = dict(state)
            new_state["object_format"] = None if text in ["📊 Неважно", tr(user_id, "skip")] else text
            new_state["step"] = "best_object_budget"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "best_object_step3"), reply_markup=best_object_budget_menu(user_id))
            return

        if state.get("step") == "best_object_budget":
            allowed = ["до 1M AED", "1–2M AED", "2–3M AED", "3–5M AED", "5M+ AED", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer(tr(user_id, "best_object_pick_budget"), reply_markup=best_object_budget_menu(user_id))
                return
            new_state = dict(state)
            new_state["budget"] = None if text == tr(user_id, "skip") else text
            new_state["step"] = "best_object_rooms"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "best_object_step4"), reply_markup=best_object_rooms_menu(user_id))
            return

        if state.get("step") == "best_object_rooms":
            allowed = ["Studio", "1 BR", "2 BR", "3 BR", "4 BR", "5 BR+", "📊 Неважно", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer(tr(user_id, "best_object_pick_rooms"), reply_markup=best_object_rooms_menu(user_id))
                return
            new_state = dict(state)
            new_state["rooms"] = None if text in ["📊 Неважно", tr(user_id, "skip")] else text
            new_state["step"] = "best_object_goal"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "best_object_step5"), reply_markup=best_object_goal_menu(user_id))
            return

        if state.get("step") == "best_object_goal":
            allowed = ["🏡 Для жизни", "🔑 Для аренды", "📈 Для перепродажи", "💰 Максимальный ROI", "⚖️ Сбалансировано", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer(tr(user_id, "best_object_pick_goal"), reply_markup=best_object_goal_menu(user_id))
                return
            state["goal"] = "⚖️ Сбалансировано" if text == tr(user_id, "skip") else text
            user_states[user_id] = state
            await send_processing(message, tr(user_id, "best_object_loading"))
            try:
                html = build_best_object_report_v95(state, user_id=user_id)
            except Exception as e:
                print("BEST_OBJECT_REPORT_ERROR:", repr(e))
                html = no_data_message("Лучший объект", user_id=user_id)
            user_states[user_id] = {"step": "result", "scope": "dubai", "last_report_title": "Лучший объект", "last_report_html": html, "history": []}
            await message.answer(html, reply_markup=post_result_menu(user_id, "dubai"))
            return

        # Format comparison funnel
        if state.get("step") == "format_compare_scope":
            if text == "🌆 По Дубаю":
                new_state = dict(state)
                new_state.update({"scope": "dubai", "name": None, "step": "format_compare_budget"})
                push_state(user_id, new_state)
                await message.answer(tr(user_id, "format_compare_budget_header"), reply_markup=format_compare_budget_menu(user_id))
                return
            if text == "🏙 По району":
                new_state = dict(state)
                new_state.update({"step": "format_compare_area_query"})
                push_state(user_id, new_state)
                await message.answer(tr(user_id, "enter_area"), reply_markup=back_menu(user_id))
                return
            await message.answer(tr(user_id, "format_compare_pick_scope"), reply_markup=format_compare_scope_menu(user_id))
            return

        if state.get("step") == "format_compare_area_query":
            rows = safe_call(find_areas, text, 8, default=[])
            if not rows:
                # принимаем введённый район как есть, чтобы не ломать сценарий на псевдонимах
                new_state = dict(state)
                new_state.update({"scope": "area", "name": virtual_area_name(text), "step": "format_compare_budget"})
                push_state(user_id, new_state)
                await message.answer(tr(user_id, "format_compare_budget_header"), reply_markup=format_compare_budget_menu(user_id))
                return
            suggestions = []
            for r in rows[:8]:
                area = r.get("area_name_en")
                if area and area not in suggestions:
                    suggestions.append(area)
            new_state = dict(state)
            new_state.update({"area_suggestions": suggestions, "step": "format_compare_choose_area"})
            push_state(user_id, new_state)
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
                await message.answer(tr(user_id, "format_compare_pick_area_list"), reply_markup=back_menu(user_id))
                return
            new_state = dict(state)
            new_state.update({"scope": "area", "name": chosen, "step": "format_compare_budget"})
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "format_compare_budget_header"), reply_markup=format_compare_budget_menu(user_id))
            return

        if state.get("step") == "format_compare_budget":
            allowed = ["до 1M AED", "1–2M AED", "2–3M AED", "3–5M AED", "5M+ AED", tr(user_id, "skip")]
            if text not in allowed:
                await message.answer(tr(user_id, "format_compare_pick_budget"), reply_markup=format_compare_budget_menu(user_id))
                return
            new_state = dict(state)
            new_state["budget"] = None if text == tr(user_id, "skip") else text
            new_state["step"] = "format_compare_goal"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "format_compare_goal_header"), reply_markup=format_compare_goal_menu(user_id))
            return

        if state.get("step") == "format_compare_goal":
            if text not in ["📈 Перепродажа", "🔑 Аренда", "💰 ROI", "⚖️ Сбалансировано"]:
                await message.answer(tr(user_id, "format_compare_pick_goal"), reply_markup=format_compare_goal_menu(user_id))
                return
            new_state = dict(state)
            new_state["goal"] = text
            new_state["step"] = "format_compare_period"
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "format_compare_period_header"), reply_markup=format_compare_period_menu(user_id))
            return

        if state.get("step") == "format_compare_period":
            period_map = {tr(user_id, "p6"): "6", tr(user_id, "p12"): "12", tr(user_id, "p36"): "36", tr(user_id, "all_time"): None}
            if text not in period_map:
                await message.answer(tr(user_id, "format_compare_pick_period"), reply_markup=format_compare_period_menu(user_id))
                return
            state["period"] = period_map[text]
            user_states[user_id] = state
            await message.answer(
                tr(user_id, "format_compare_loading"),
            )
            report, rows = build_format_comparison_report(
                scope=state.get("scope", "dubai"),
                area=state.get("name"),
                budget=state.get("budget"),
                goal=state.get("goal"),
                period=state.get("period"),
            )
            if not report:
                await message.answer(no_data_message("Сравнение форматов", user_id=user_id), reply_markup=format_compare_after_menu(user_id))
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
                await message.answer(show_format_best_areas(best, state.get("period"), state.get("budget"), user_id=user_id), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "🏢 Лучшие здания":
                await message.answer(show_format_best_buildings(best, state.get("name"), state.get("period"), user_id=user_id), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "📄 PDF":
                await message.answer(tr(user_id, "pdf_after_selection"), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "💼 Заявка":
                await message.answer(tr(user_id, "consult_link"), reply_markup=format_compare_after_menu(user_id))
                return
            if text == "🔁 Новый отчёт":
                state.clear()
                state.update({"step": "format_compare_scope"})
                user_states[user_id] = state
                await message.answer(tr(user_id, "format_compare_header"), reply_markup=format_compare_scope_menu(user_id))
                return

        # Smart investment flow — оставлен, но после результата только PDF/Заявка/Изменить.
        if state.get("step") == "smart_goal":
            # ⚡ Quick mode: skip remaining steps using smart defaults
            # (deal=sale + investment + 1-2M + sбалансировано + до 12 мес).
            # Result uses DLD-driven area_rankings via smart_area_universe().
            if text in ("⚡ Быстрый подбор", "⚡ Quick pick", "⚡ اختيار سريع"):
                new_state = dict(state)
                new_state.update({
                    "goal": "💰 Инвестиция / ROI",
                    "budget": "1–2M AED",
                    "timing": "до 12 месяцев",
                    "risk": "сбалансировано",
                    "step": "smart_risk",  # will fall through to result pipeline below
                    "_quick_mode": True,
                })
                user_states[user_id] = new_state
                state = new_state
                text = "сбалансировано"
                # fall through into smart_risk handler below
            else:
                new_state = dict(state)
                new_state.update({"goal": text, "step": "smart_budget"})
                push_state(user_id, new_state)
                await message.answer(tr(user_id, "smart_budget_header"), reply_markup=smart_budget_menu(user_id))
                return
        if state.get("step") == "smart_budget":
            new_state = dict(state)
            new_state.update({"budget": text, "step": "smart_timing"})
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "smart_timing_header"), reply_markup=smart_timing_menu(user_id))
            return
        if state.get("step") == "smart_timing":
            new_state = dict(state)
            new_state.update({"timing": text, "step": "smart_risk"})
            push_state(user_id, new_state)
            await message.answer(tr(user_id, "smart_risk_header"), reply_markup=smart_risk_menu(user_id))
            return
        if state.get("step") == "smart_risk":
            state["risk"] = text
            await send_processing(message, "⌛️ <b>Подбираю инвестиционный сценарий</b>\n\n◇ Сопоставляю бюджет, цель и риск.\n◇ Проверяю DLD-активность и ликвидность.\n◇ Формирую заключение 360°.")
            # v107 antifreeze: весь sync-pipeline (SQL + economic_engine) выносим в thread
            # и ограничиваем суммарным таймаутом, чтобы handler никогда не зависал.
            title = "🧠 Инвестиционный подбор"

            def _v107_smart_invest_pipeline():
                _candidates = safe_call(
                    smart_pick_candidates,
                    state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"),
                    default=[],
                ) or []
                if not _candidates:
                    _candidates = smart_fallback_candidates(
                        state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"),
                    )
                try:
                    _html = show_smart_recommendation(
                        state.get("goal"),
                        state.get("budget"),
                        state.get("timing"),
                        state.get("risk"),
                        _candidates,
                    )
                except Exception as _e:
                    print("SMART_RECOMMENDATION_ENGINE_ERROR:", repr(_e))
                    _best = _candidates[0] if _candidates else {}
                    _html = (
                        "🧠 <b>Инвестиционный подбор</b>\n\n"
                        "🏆 <b>Лучший сценарий</b>\n"
                        f"📍 Район: <b>{_best.get('area') or 'JVC'}</b>\n"
                        f"🏠 Формат: <b>{_best.get('property') or _best.get('unit_segment') or '1 BR'}</b>\n"
                        f"📊 Сделки: <b>{format_int(_best.get('deals'))}</b>\n"
                        f"💰 Средняя цена: <b>{format_money(_best.get('avg_price'))}</b>\n"
                        f"📐 Средняя цена за м²: <b>{format_money(_best.get('avg_meter'))}</b>\n\n"
                        "🧠 <b>Экономическое заключение 360°</b>\n\n"
                        "Недостаточно данных для полного экономического отчёта. Расширьте период или фильтр."
                    )
                return _candidates, _html

            html = None
            try:
                _t0 = time.time()
                # v108.1: increased timeout 45s→90s to allow v107 read-model fast-path
                # to complete for all 8 areas (previously fallback fired prematurely).
                candidates, html = await asyncio.wait_for(
                    asyncio.to_thread(_v107_smart_invest_pipeline),
                    timeout=90,
                )
                print(f"SMART_INVEST_V107_OK: dt={time.time()-_t0:.1f}s goal={state.get('goal')!r} budget={state.get('budget')!r} risk={state.get('risk')!r}")
            except asyncio.TimeoutError:
                print("SMART_INVEST_V107_TIMEOUT: goal=%r budget=%r risk=%r timing=%r" % (
                    state.get('goal'), state.get('budget'), state.get('risk'), state.get('timing')))
                # Graceful static fallback на основе бюджета — без SQL и без LLM.
                try:
                    candidates = smart_fallback_candidates(
                        state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"),
                    ) or []
                except Exception:
                    candidates = []
                best = candidates[0] if candidates else {}
                html = (
                    "🧠 <b>Инвестиционный подбор</b>\n\n"
                    "⚠️ DLD-архив сейчас отвечает медленно, поэтому показываю быстрый профильный сценарий без полной выборки.\n\n"
                    "🏆 <b>Лучший сценарий</b>\n"
                    f"📍 <b>Район:</b> {best.get('area') or 'JVC'}\n"
                    f"🏠 <b>Формат:</b> {best.get('property') or '1 BR'}\n"
                    f"💰 <b>Ориентир цены:</b> {format_money(best.get('avg_price'))}\n"
                    f"📐 <b>Цена за м²:</b> {format_money(best.get('avg_meter'))}\n\n"
                    "🧠 <b>Экономическое заключение 360°</b>\n\n"
                    "Расчёт выполнен по профильной модели бюджет × риск × горизонт. "
                    "Полный DLD-отчёт по этому сценарию можно перезапустить через минуту, когда архив разгрузится."
                )
                try:
                    if _err_logger:
                        _err_logger.log_error(
                            "analytics", "smart_invest_pipeline", "timeout 90s",
                            error_class="TimeoutError", user_id=user_id,
                            context={
                                "goal": state.get("goal"), "budget": state.get("budget"),
                                "risk": state.get("risk"), "timing": state.get("timing"),
                            },
                        )
                except Exception:
                    pass
            except Exception as _e:
                print("SMART_INVEST_V107_ERROR:", repr(_e))
                try:
                    candidates = smart_fallback_candidates(
                        state.get("goal"), state.get("budget"), state.get("risk"), state.get("timing"),
                    ) or []
                except Exception:
                    candidates = []
                best = candidates[0] if candidates else {}
                html = (
                    "🧠 <b>Инвестиционный подбор</b>\n\n"
                    "⚠️ Подбор временно работает в упрощённом режиме — попробуйте полный отчёт через минуту.\n\n"
                    f"📍 <b>Район:</b> {best.get('area') or 'JVC'}\n"
                    f"🏠 <b>Формат:</b> {best.get('property') or '1 BR'}\n"
                    f"💰 <b>Ориентир цены:</b> {format_money(best.get('avg_price'))}\n"
                )

            user_states[user_id] = {"step": "result", "scope": "dubai", "last_report_title": title, "last_report_html": html, "history": []}
            await message.answer(html, reply_markup=post_result_menu(user_id, "dubai"))
            return

        await message.answer(tr(user_id, "main_menu"), reply_markup=main_menu(user_id))

    except Exception as e:
        print("MAIN_ROUTER_V72_ERROR:", repr(e))
        # v52 LOGGING: пишем в bot_error_events чтобы watchdog мог alert'нуть
        try:
            import error_logger as _el, traceback as _tb
            _el.log_error("analytics", "main_handler", str(e),
                           error_class=type(e).__name__, user_id=user_id,
                           context={"text": text[:200] if text else "", "state_step": state.get("step")},
                           tb=_tb.format_exc()[-1500:])
        except Exception:
            pass
        await message.answer(tr(user_id, "tech_error"), reply_markup=main_menu(user_id))


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

RENT_TABLE = "public.dld_rents_full"


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
    # FIX 2026-06-03 (METER_PRICE_PER_SOURCE — agent rev):
    # Verified directly: dld_transactions_full.meter_sale_price is stored as AED/m²
    # (p50 = 11_726 AED/m² across 1.69M rows). Multiplying by SQFT_TO_M2 inflated
    # values 10×. Drop the multiplier on the primary expression.
    # Fallback path (price / size) keeps SQFT_TO_M2 because actual_area is in sqft
    # and actual_worth is total AED → (AED / sqft) × 10.7639 = AED/m².
    meter_expr = f"""
        COALESCE(
            ({m['meter']}),
            CASE WHEN ({m['size']}) IS NOT NULL AND ({m['size']}) > 0 AND ({m['price']}) IS NOT NULL
                 THEN (({m['price']}) / NULLIF(({m['size']}), 0)) * {SQFT_TO_M2}
                 ELSE NULL::numeric END
        )
    """
    # ВНИМАНИЕ: используем `_norm` суффикс для derived колонок чтобы избежать
    # AmbiguousColumn — реальная таблица содержит колонки `rooms_en/building_name_en/
    # area_name_en/property_type_en/property_sub_type_en/procedure_name_en`, и
    # `SELECT *, ... AS rooms_en` создавало 2 колонки с одинаковым именем →
    # любая ссылка на rooms_en во внешнем SELECT падала с AmbiguousColumn.
    return f"""
        FROM (
            SELECT
                *,
                {m['date']} AS safe_date,
                {m['building']} AS building_name_norm,
                {m['area']} AS area_name_norm,
                {m['rooms']} AS rooms_norm,
                {m['ptype']} AS property_type_norm,
                {m['subtype']} AS property_sub_type_norm,
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
# Используем `_norm` derived колонки чтобы избежать AmbiguousColumn:
# реальная DB имеет rooms_en/building_name_en/area_name_en, и SELECT * приносит
# их же — поэтому ссылка на `rooms_en` без префикса падает в UNION-контексте.
PRICE = "actual_worth_norm"
METER_PRICE = "meter_sale_price_norm"
BUILDING_NAME = "building_name_norm"
AREA_TXT = "COALESCE(area_name_norm::text, '')"
BUILDING_TXT = "COALESCE(building_name_norm::text, '')"
ROOMS_TXT = "COALESCE(rooms_norm::text, '')"
PROPERTY_TYPE_TXT = "COALESCE(property_type_norm::text, '')"
PROPERTY_SUB_TYPE_TXT = "COALESCE(property_sub_type_en::text, '')"
PROCEDURE_TXT = "COALESCE(procedure_name_en::text, procedure_name_norm::text, '')"


def building_search_expression():
    return "LOWER(COALESCE(search_text::text, '') || ' ' || COALESCE(building_name_en::text, '') || ' ' || COALESCE(area_name_en::text, ''))"


def building_aliases(name):
    q = normalize_search_text(name)
    # FIX 2026-05-29: marketing-suffixed names (e.g. "Grande Signature Residences",
    # "Address Residences Dubai Opera") are stored in DLD under their SHORT
    # registry name ("Grande", "Address Opera"). Returning multi-token aliases
    # forces AND-search which yields zero results. Map to the short name only.
    aliases = {
        'grande': ['grande'],
        'grande signature': ['grande'],
        'grande signature residences': ['grande'],
        'opera grande': ['opera', 'grande'],
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
    # FIX 2026-06-03 (DLD_SQL_SWEEP): default 12mo date filter for find_areas v44 path.
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
                      AND safe_date >= CURRENT_DATE - INTERVAL '12 months'
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


# Circuit breaker around main DB — opens after repeated connect failures
# so handlers can return degraded responses instead of timing out.
try:
    from stability import get_breaker as _get_breaker_db
    _db_breaker = _get_breaker_db("db_main")
except Exception:
    _db_breaker = None


def db():
    """Совместимая db() функция: старый код продолжает вызывать db(),
    но фактически подключается к активной базе archive/live.
    v107: жёсткие таймауты, чтобы запросы к dld_transactions_full не висели.
    Wrapped by CircuitBreaker — when OPEN, raises so callers fall to degraded
    paths (cached / "Data updating…" placeholders) instead of hanging.
    """
    if _db_breaker is not None:
        return _db_breaker.call(
            psycopg2.connect,
            _ACTIVE_DATABASE_URL,
            cursor_factory=RealDictCursor,
            connect_timeout=8,
            options="-c statement_timeout=15000 -c idle_in_transaction_session_timeout=20000",
        )
    return psycopg2.connect(
        _ACTIVE_DATABASE_URL,
        cursor_factory=RealDictCursor,
        connect_timeout=8,
        options="-c statement_timeout=15000 -c idle_in_transaction_session_timeout=20000",
    )


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

    # PDF feature manually disabled 2026-06-03 — short-circuit before any work.
    if not _pdf_enabled():
        try:
            await message.answer("📄 PDF-отчёт временно отключён.\n\nVadim Realty · RERA BRN 65011")
        except Exception:
            pass
        return

    # Feature flag: FF_PDF_REPORT_ENABLED=0 → degrade with friendly message.
    try:
        from stability import ff as _ff, degrade_msg as _dmsg
        if not _ff("PDF_REPORT"):
            try:
                lang = state.get("lang") or "ru"
                await message.answer(_dmsg("feature_unavailable", lang))
            except Exception:
                pass
            return
    except Exception:
        pass

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

    # v_pdf2: используем общий vadim_pdf модуль (10 страниц, бренд Vadim Realty).
    pdf_path = None
    try:
        from vadim_pdf import generate_pdf_report
        scope = state.get("scope", "dubai")
        row = state.get("last_row") or {}
        # Map scope → report_type
        report_type = {"area": "area", "building": "building",
                       "project": "project", "dubai": "area"}.get(scope, "area")
        payload = {
            "name": state.get("name") or title,
            "title": title,
            "avg_price": row.get("avg_price"),
            "median_price": row.get("median_price"),
            "price_per_m2": row.get("price_per_m2") or row.get("avg_price_m2"),
            "deals": row.get("deals"),
            "yield": row.get("yield"),
            "growth_yoy": row.get("growth_yoy") or row.get("growth"),
            "liquidity": row.get("liquidity"),
            "summary": _html_to_plain(content)[:2500] if content else None,
        }
        # try add dynamics if available in state
        if state.get("dynamics_series"):
            payload["dynamics_series"] = state.get("dynamics_series")
        pdf_path = generate_pdf_report(report_type, payload, lang="ru")
    except Exception as _e:
        import logging as _lg
        _lg.warning(f"vadim_pdf failed → fallback to legacy PDF: {_e}")

    if pdf_path:
        from aiogram.types import FSInputFile
        await message.answer_document(
            FSInputFile(pdf_path, filename="investment_report.pdf"),
            caption="📄 Инвестиционный отчёт готов.",
            reply_markup=result_menu(user_id, state.get("scope")),
        )
        return

    # ── legacy fallback ──
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
        await message.answer(tr(user_id, "lead_rate_limited"), reply_markup=result_menu(user_id))
        return
    LAST_LEAD_TS[user_id] = now
    await message.answer(trf(user_id, "lead_consult", url=LEAD_BOT_URL), reply_markup=result_menu(user_id))


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


# v112: admin /stats — cross-bot conversions table (last 7 days)
async def _send_cross_bot_stats(message):
    """Показывает таблицу cross_bot_jumps (from_bot × to_bot) за 7 дней.
    Источник: RESALE_DATABASE_URL (общая аналитическая БД)."""
    user_id = message.from_user.id
    url = os.environ.get("RESALE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        await message.answer("⚠️ RESALE_DATABASE_URL не настроен — нет источника cross_bot_jumps.",
                             reply_markup=main_menu(user_id))
        return
    try:
        import psycopg2 as _pg
        conn = _pg.connect(url, connect_timeout=8, application_name="analytics-bot[/stats]")
        try:
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = 8000")
                cur.execute("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_name='cross_bot_jumps'
                """)
                if not cur.fetchone()[0]:
                    await message.answer("ℹ️ Таблица cross_bot_jumps ещё пустая — переходов между ботами не было.",
                                         reply_markup=main_menu(user_id))
                    return
                cur.execute("""
                    SELECT COALESCE(from_bot, 'unknown') AS f,
                           COALESCE(to_bot,   'unknown') AS t,
                           COUNT(*)                       AS cnt,
                           COUNT(DISTINCT user_id)        AS uniq
                    FROM cross_bot_jumps
                    WHERE jumped_at >= NOW() - INTERVAL '7 days'
                    GROUP BY 1, 2
                    ORDER BY cnt DESC
                    LIMIT 40
                """)
                rows = cur.fetchall()
                cur.execute("""
                    SELECT COUNT(*), COUNT(DISTINCT user_id)
                    FROM cross_bot_jumps
                    WHERE jumped_at >= NOW() - INTERVAL '7 days'
                """)
                total, uniq_total = cur.fetchone()
                cur.execute("""
                    SELECT COALESCE(utm_source, '∅'),
                           COUNT(*) AS c
                    FROM cross_bot_jumps
                    WHERE jumped_at >= NOW() - INTERVAL '7 days'
                    GROUP BY 1
                    ORDER BY c DESC
                    LIMIT 8
                """)
                utm_rows = cur.fetchall()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        await message.answer(f"⚠️ Ошибка чтения cross_bot_jumps:\n<code>{str(e)[:500]}</code>",
                             reply_markup=main_menu(user_id))
        return

    if not rows:
        await message.answer(
            "📈 <b>Cross-bot conversions · 7d</b>\n\n"
            "За последние 7 дней переходов между ботами не зафиксировано.",
            reply_markup=main_menu(user_id),
        )
        return

    lines = ["📈 <b>Cross-bot conversions · 7d</b>"]
    lines.append(f"Всего переходов: <b>{int(total or 0)}</b> · уникальных user: <b>{int(uniq_total or 0)}</b>\n")
    lines.append("<b>from → to · count · uniq</b>")
    lines.append("<pre>")
    for f, t, cnt, uniq in rows:
        f_s = (f or "?")[:10].ljust(10)
        t_s = (t or "?")[:10].ljust(10)
        lines.append(f"{f_s} {t_s} {int(cnt):>5} {int(uniq):>5}")
    lines.append("</pre>")
    if utm_rows:
        lines.append("\n<b>Top utm_source · 7d</b>")
        lines.append("<pre>")
        for src, c in utm_rows:
            lines.append(f"{(src or '∅')[:18].ljust(18)} {int(c):>5}")
        lines.append("</pre>")
    await message.answer("\n".join(lines), reply_markup=main_menu(user_id))


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
        f"📐 <b>Средняя цена за м²:</b> {format_money(best.get('avg_meter'))}\n"
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
        f"📐 <b>Средняя цена за м²:</b> {format_money(avg_meter)}\n"
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
    # FIX 2026-06-03 (DLD_SQL_SWEEP): default 12mo date filter on deals counter.
    # Was showing all-history (566k for JVC); user expects 12mo (~24k).
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
        date_expr = _date_expr_v67(cols) if '_date_expr_v67' in globals() else "NULL::date"
        date_filter = f"AND ({date_expr}) >= CURRENT_DATE - INTERVAL '12 months'" if date_expr != "NULL::date" else ""
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
                          {date_filter}
                        GROUP BY NULLIF({building}, ''), NULLIF({area}, '')
                        ORDER BY deals DESC
                        LIMIT %s
                    """, params + [limit])
                    rows.extend(cur.fetchall())
        except Exception as e:
            print("SOURCE_FIND_BUILDINGS_V66_ERROR:", table, repr(e))
    return _merge_group_rows(rows, ["building_name_en", "area_name_en"], limit=limit, sort_field="deals")


def _source_find_areas_v66(query, limit=10):
    # FIX 2026-06-03 (DLD_SQL_SWEEP): default 12mo date filter on deals counter.
    # Was returning sum of full-history sub-area counts (e.g., JVC 566,386).
    # Real 12mo: ~20k sales + ~4k rents = ~24k.
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
        date_expr = _date_expr_v67(cols) if '_date_expr_v67' in globals() else "NULL::date"
        date_filter = f"AND ({date_expr}) >= CURRENT_DATE - INTERVAL '12 months'" if date_expr != "NULL::date" else ""
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
                          {date_filter}
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
    # FIX 2026-05-30 (Grande regression): support both ISO (YYYY-MM-DD) and
    # DLD legacy (DD-MM-YYYY) formats. Previously only ISO matched → all rows
    # got safe_date=NULL when archive shipped DD-MM-YYYY → period filter
    # `safe_date >= CURRENT_DATE - 12 months` killed every match.
    for c in ["transaction_date", "instance_date", "contract_start_date", "contract_end_date", "load_timestamp", "created_at", "date"]:
        if c in cols:
            qc = qcol(c) if 'qcol' in globals() else '"' + c + '"'
            return (
                f"CASE "
                f"WHEN {qc}::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' "
                f"THEN ({qc}::text)::date "
                f"WHEN {qc}::text ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}' "
                f"THEN to_date({qc}::text, 'DD-MM-YYYY') "
                f"WHEN {qc}::text ~ '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}' "
                f"THEN to_date({qc}::text, 'DD/MM/YYYY') "
                f"ELSE NULL END"
            )
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
                rows = cur.fetchall()
                # FIX 2026-05-30 (Grande regression diag): log empty results so we can
                # tell broken-SQL from genuine no-data in production logs.
                if not rows and os.getenv("V67_DIAG", "").strip() in ("1", "true", "True"):
                    print(
                        "RUN_SOURCE_SQL_V67_EMPTY:",
                        source, table,
                        "params=", repr(params)[:200],
                    )
                return rows
    except Exception as e:
        print("RUN_SOURCE_SQL_V67_ERROR:", source, table, repr(e), "params=", repr(params)[:200])
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
            html += f"📐 Средняя цена за м²: <b>{format_money(r.get('avg_meter'))}</b>\n"
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

def build_best_object_report_v95(state, *, user_id=None):
    areas, buildings, notes = _v95_top_areas_and_buildings(state)
    if not areas and not buildings:
        return no_data_message("Лучший объект", user_id=user_id)
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


async def _mv_offplan_refresher_loop():
    """v133: Daily REFRESH MATERIALIZED VIEW CONCURRENTLY mv_offplan_summary.
    Делается background-task'ом при старте сервиса, повторяется каждые 24h.
    """
    import subprocess
    while True:
        try:
            r = subprocess.run(
                ["python", "mv_refresher.py"],
                capture_output=True, text=True, timeout=300
            )
            print(f"[mv_refresher] exit={r.returncode} stdout={r.stdout[:200]} stderr={r.stderr[:200]}")
        except Exception as e:
            print(f"[mv_refresher] err: {e}")
        await asyncio.sleep(24 * 3600)


try:
    from aiogram import BaseMiddleware as _BaseMiddleware
    class _StaleButtonMiddleware(_BaseMiddleware):
        """FSST: catch stale-button errors from ALL callback handlers."""
        async def __call__(self, handler, event, data):
            try:
                return await handler(event, data)
            except Exception as e:
                if is_stale_button_error(e):
                    await answer_stale(event, "en")
                else:
                    raise
    dp.callback_query.middleware(_StaleButtonMiddleware())
    print("[FSST] stale-button middleware registered (class-based)")
except Exception as _mw_err:
    print(f"[FSST] stale middleware skip: {_mw_err}")


# Phase BM L10 — opt-out callback handler (outer middleware on callback_query)
try:
    from aiogram import BaseMiddleware as _BM_BaseMiddleware
    from aiogram.types import CallbackQuery as _BM_CallbackQuery

    class _BMOptOutMiddleware(_BM_BaseMiddleware):
        async def __call__(self, handler, event, data):
            try:
                if isinstance(event, _BM_CallbackQuery) and \
                        _bm_is_opt_out_callback(event.data or ""):
                    uid = event.from_user.id if event.from_user else None
                    ok, msg = _bm_handle_opt_out(event.data or "", uid)
                    try:
                        await event.answer((msg or "OK")[:200], show_alert=False)
                    except Exception:
                        pass
                    return  # short-circuit
            except Exception as _e:
                print(f"[phase_bm] opt-out mw err: {_e}")
            return await handler(event, data)

    dp.callback_query.outer_middleware(_BMOptOutMiddleware())
    print("[phase_bm] opt-out callback middleware registered")
except Exception as _bm_mw_err:
    print(f"[phase_bm] opt-out middleware skip: {_bm_mw_err}")


def _analytics_db_ping() -> bool:
    """Lightweight DB liveness check for /health endpoint."""
    if not DATABASE_URL:
        return True
    try:
        import psycopg2
        with psycopg2.connect(DATABASE_URL, connect_timeout=2) as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


async def main():
    # Resilience: smoke test + watchdog + crash-loop counter (opt-out via envs)
    try:
        from resilience import start_resilience as _start_resilience
        if not _start_resilience("analytics"):
            print("[resilience] smoke test FAILED — refusing to start polling", flush=True)
            import sys as _sys
            _sys.exit(1)
    except SystemExit:
        raise
    except Exception as _re:
        print(f"[resilience] init error (continuing): {_re}", flush=True)
    # FSST: HTTP health endpoint with DB ping
    if _fsst_ok:
        start_health_server(
            bot_name="analytics-bot",
            bot_username=os.environ.get("BOT_USERNAME", "analytics_bot"),
            db_ping=_analytics_db_ping,
        )
    try:
        p = start_metrics_server()
        if p:
            print(f"[metrics] exposed on :{p}")
    except Exception as _me:
        print(f"[metrics] failed: {_me}")
    # Contract validator (background, non-blocking). См. shared/contract_validator.py.
    # STRICT_CONTRACTS=0 by default → алертим, но не падаем.
    try:
        from contract_boot_hook import async_contract_check
        try:
            from admin_notify import admin_notify as _adm_notify
        except Exception:
            _adm_notify = None
        asyncio.create_task(async_contract_check(
            bot_name="analytics",
            dsns={
                "live": LIVE_DATABASE_URL,
                "archive": ARCHIVE_DATABASE_URL,
                "resale": os.getenv("RESALE_DATABASE_URL") or "",
            },
            contracts_filter=["dld_*", "users", "leads", "area_price_benchmark", "listings_v2"],
            admin_notify=_adm_notify,
        ))
        print("[contract] boot check scheduled", flush=True)
    except Exception as _cce:
        print(f"[contract] boot check skipped: {_cce!r}", flush=True)
    # PHASE BM Layer 12: agent_bus install
    try:
        from agent_bus.boot_hook import install_agent_bus
        install_agent_bus(
            bot_name="analytics",
            subscribes_to=["listing.priced_low", "market.shift_detected", "user.handoff_requested"],
        )
        print("[agent_bus] installed for analytics", flush=True)
    except Exception as _abe:
        print(f"[agent_bus] install skipped: {_abe!r}", flush=True)
    print("Dubai DLD Analytics Bot started")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        print("Telegram webhook cleared before polling")
    except Exception as e:
        print("WEBHOOK_CLEAR_ERROR", repr(e))
    # B051 v5: задержка 180с — Railway rolling deploy, старый контейнер жив ~30-60с.
    # Ждём 3 мин чтобы гарантированно убить его до начала polling.
    print("[B051] Startup delay 180s — waiting for old Railway container to die…", flush=True)
    await asyncio.sleep(180)
    print("[B051] Startup delay done, polling starts now.", flush=True)
    # v133: background MV refresher (mv_offplan_summary daily)
    asyncio.create_task(_mv_offplan_refresher_loop())

    # PHASE BM Layer 18/20/22: multimodal + tours + background-think
    try:
        from phase_bm_bootstrap import wire_phase_bm
        wire_phase_bm(dp)
    except Exception as _e:
        try:
            logger.warning(f"PHASE BM wire failed: {_e}")
        except Exception:
            print(f"[phase_bm] wire failed: {_e!r}", flush=True)

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
    lines.append(f"• Средняя цена за м²: <b>{_econ_money_v78(avg_meter)}</b>.")
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
        f"📐 <b>Средняя цена за м²:</b> {_econ_money_v78(avg_meter)}\n\n"
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
            await message.answer(no_data_message(title_prefix, scope=scope, name=name, prop=prop, period=period, deal_type=deal_type, user_id=user_id), reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))
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
        await message.answer(no_data_message(title_prefix, scope=scope, name=name, prop=prop, period=period, deal_type=deal_type, user_id=user_id), reply_markup=no_data_menu(user_id) if scope in ["building", "area"] else main_menu(user_id))

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
    """v52 FIX: убран property_usage_en — этой колонки нет в текущей DLD схеме,
    из-за чего UndefinedColumn ломал все деал-запросы (user видел 'no data')."""
    return "LOWER(" \
        "COALESCE(rooms_en::text, '') || ' ' || " \
        "COALESCE(property_type_en::text, '') || ' ' || " \
        "COALESCE(property_sub_type_en::text, '')" \
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
        f"📐 <b>Средняя цена за м²:</b> {format_money(best.get('avg_meter'))}\n\n"
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
                # v92 fix: AmbiguousColumn ("rooms_en is ambiguous") fired in scope='area'
                # because base_from() subquery exposed physical `rooms_en` via `SELECT *`
                # AND outer SELECT created `... AS rooms_en` alias. property_condition()
                # plain `rooms_en` reference in WHERE then was ambiguous in some PG paths.
                # Fix: wrap result in outer SELECT — WHERE goes on inner scope where only
                # subquery's physical rooms_en exists (no outer alias conflict).
                cur.execute(f"""
                    SELECT
                        safe_date,
                        procedure_name_en,
                        rooms_en,
                        property_type_en,
                        property_sub_type_en,
                        price,
                        area_size,
                        meter_price,
                        building_name_en,
                        area_name_en
                    FROM (
                        SELECT
                            safe_date,
                            COALESCE(procedure_name_norm::text, '') AS procedure_name_en,
                            {ROOMS_TXT} AS rooms_en_norm,
                            rooms_en AS rooms_en,
                            COALESCE(property_type_norm::text, '') AS property_type_en,
                            COALESCE(property_sub_type_norm::text, '') AS property_sub_type_en,
                            {value_expr} AS price,
                            {area_expr} AS area_size,
                            {METER_PRICE} AS meter_price,
                            {BUILDING_TXT} AS building_name_en,
                            {AREA_TXT} AS area_name_en
                        {base_from()}
                          {scope_sql}
                          AND {value_expr} IS NOT NULL
                          {prop_sql}
                          {deal_sql}
                          {p_sql}
                          {unit_sql}
                        ORDER BY safe_date DESC NULLS LAST
                        LIMIT %s
                    ) v91_inner
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
            f"Средняя цена: <b>{format_money(avg_price)}</b>, средняя цена за м²: <b>{format_money(avg_meter)}</b>. "
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
        f"📐 <b>Средняя цена за м²:</b> {format_money(best.get('avg_meter'))}\n\n"
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
        f"📐 <b>Средняя цена за м²:</b> {format_money(best.get('avg_meter'))}\n"
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
        "language": state.get("language", "en"),
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
        await send_full_report(message, scope, name, prop, period, deal_type, _report_kind_label_v72(kind, message.from_user.id))


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

def build_best_object_report_v95(state, *, user_id=None):
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
            html = no_data_message("Лучший объект", user_id=user_id)
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


def _smart_human_compact_money(value):
    """Compact AED format: 1.4 млн, 850 тыс, 12.5 млн."""
    try:
        v = float(value or 0)
    except Exception:
        return "—"
    if v <= 0:
        return "—"
    if v >= 1_000_000:
        return f"{v/1_000_000:.1f} млн AED"
    if v >= 1_000:
        return f"{int(round(v/1_000))} тыс AED"
    return f"{int(round(v))} AED"


def _smart_fetch_area_rent_12m(area_display):
    """FIX (RENT_YIELD): fetch realistic 12-month rent + apartment-segment sale price
    for the same area, so yield = rent / price reflects ACTUAL apartments (not all-property mix).
    Returns dict {avg_rent, sale_avg_price_apt, deals_rent} or None.
    Uses mv_area_12m_summary directly via READ_MODEL connection.
    """
    if not (_READ_MODEL_OK and _read_model):
        return None
    try:
        # Find area_keys for this display label via existing area_universe helper.
        try:
            au = _v109_area_universe_safe(area_display) or []
        except Exception:
            au = []
        keys = []
        for disp, reals in au:
            if str(disp).strip().lower() == str(area_display).strip().lower():
                for r in (reals or []):
                    k = str(r).strip().lower()
                    if k:
                        keys.append(k)
                break
        # Fallback: try area_display itself as key
        if not keys:
            keys = [str(area_display).strip().lower()]
        import psycopg2.extras as _pe
        with _read_model._conn().cursor(cursor_factory=_pe.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  SUM(CASE WHEN deal_type='rent' AND property_type='apartment' AND rooms='all'
                           THEN avg_price * deals ELSE 0 END)::numeric
                    / NULLIF(SUM(CASE WHEN deal_type='rent' AND property_type='apartment' AND rooms='all' THEN deals ELSE 0 END), 0) AS avg_rent_apt,
                  SUM(CASE WHEN deal_type='rent' AND property_type='apartment' AND rooms='all' THEN deals ELSE 0 END) AS deals_rent_apt,
                  SUM(CASE WHEN deal_type='sale' AND property_type='apartment' AND rooms='all'
                           THEN avg_price * deals ELSE 0 END)::numeric
                    / NULLIF(SUM(CASE WHEN deal_type='sale' AND property_type='apartment' AND rooms='all' THEN deals ELSE 0 END), 0) AS sale_avg_price_apt,
                  SUM(CASE WHEN deal_type='sale' AND property_type='apartment' AND rooms='all' THEN deals ELSE 0 END) AS deals_sale_apt
                FROM mv_area_12m_summary
                WHERE area_key = ANY(%s)
                """,
                (keys,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "avg_rent": float(row.get("avg_rent_apt") or 0) or None,
            "deals_rent": int(row.get("deals_rent_apt") or 0),
            "sale_avg_price_apt": float(row.get("sale_avg_price_apt") or 0) or None,
            "deals_sale_apt": int(row.get("deals_sale_apt") or 0),
        }
    except Exception as _e:
        print("SMART_FETCH_AREA_RENT_12M_ERROR:", repr(_e))
        return None


def _smart_human_liquidity(deals):
    try:
        d = int(deals or 0)
    except Exception:
        return ""
    if d >= 5000:
        return "очень высокая"
    if d >= 1500:
        return "высокая"
    if d >= 500:
        return "средняя"
    if d >= 100:
        return "ограниченная"
    return "низкая"


def _smart_human_why(area, prop, deals, avg_price, yield_top):
    """Short, human, actionable explanation."""
    a = (area or "").lower()
    p = (prop or "").lower()
    base_areas = {
        "jvc": "JVC — один из самых ликвидных районов для входа с бюджетом 1–2М. Studio и 1BR здесь продаются быстрее всего: молодая аудитория, экспаты, низкий ценник, реальная арендная отдача.",
        "business bay": "Business Bay — премиальная локация рядом с Даунтауном. Платишь премию +20-30% за статус и lifestyle, но получаешь ликвидность и сильный спрос на аренду.",
        "dubai marina": "Marina — туристический магнит, короткие аренды, yield 6-7%. Service charge выше среднего, но и спрос постоянный.",
        "palm jumeirah": "Palm Jumeirah — премиум-сегмент с видом на море. Меньше сделок, выше чек, но статус и капитализация ощутимо растут.",
        "downtown dubai": "Downtown — Burj Khalifa, Dubai Mall, премиум-класс. Высокие цены, но и максимальная узнаваемость для аренды.",
        "jvt": "JVT — соседствует с JVC, тише и зеленее. Хорошо для life-style покупателя, ликвидность чуть ниже.",
        "jlt": "JLT — рабочая зона рядом с Marina, цены ниже Marina на 15-20%, такая же транспортная доступность.",
    }
    for k, v in base_areas.items():
        if k in a:
            return v
    return f"{area} — устойчивый район по DLD-выборке за последний год: {format_int(deals)} сделок, средний чек {_smart_human_compact_money(avg_price)}."


def show_smart_recommendation(goal, budget, timing, risk, rows):
    """SMART_PICK_HUMAN: humanized 'Лучший сценарий' report.
    Сохраняет все ключевые цифры (deals, yield, comfort range), но переформулирует
    их в actionable советы, не в академическую сводку.
    """
    if not rows:
        return "❌ По этим параметрам пока не нашёл сильных вариантов.\n\nПопробуй расширить бюджет или выбрать другой риск-профиль."

    best = dict(rows[0])
    area = best.get('area') or '—'
    prop = best.get('property') or best.get('format') or '—'
    good_low, good_high = _v101_entry_range(best, budget)

    deals = best.get('deals') or 0
    avg_price = best.get('avg_price') or 0
    avg_meter = best.get('avg_meter') or 0
    # FIX (REALISTIC_YIELD): AVG, не TOP — top это аномалии (17-18%) от пиковых сделок.
    yield_top = best.get('avg_rental_yield_pct') or 0

    # FIX (RENT_YIELD): avg_rent теперь читается напрямую из mv_area_12m_summary
    # (apartment rent 12mo), а не как avg_price × yield_top (что давало завышенную
    # цифру 121K из-за yield от смешанного all-property сегмента).
    # yield_pct = real_rent / avg_price → реалистичный для apartments.
    avg_rent = 0
    try:
        _rent_info = _smart_fetch_area_rent_12m(area)
    except Exception:
        _rent_info = None
    if _rent_info and _rent_info.get('avg_rent'):
        avg_rent = float(_rent_info['avg_rent'])
        # Пересчитываем yield от реальной аренды и средней цены apartment-сегмента.
        _apt_price = _rent_info.get('sale_avg_price_apt') or avg_price
        if _apt_price:
            yield_top = round(avg_rent / float(_apt_price) * 100.0, 1)
    elif not avg_rent and avg_price and yield_top:
        # Legacy fallback only when MV rent data unavailable.
        avg_rent = avg_price * float(yield_top) / 100.0

    text = (
        f"🏆 <b>Лучший вариант под твой бюджет</b>\n\n"
        f"📍 <b>{area}</b> · {prop}\n"
        f"💰 Средняя цена: <b>{_smart_human_compact_money(avg_price)}</b>\n"
        f"📐 За квадратный метр: <b>~{_smart_human_compact_money(avg_meter)}</b>\n"
        f"📊 Активность рынка: <b>{format_int(deals)} сделок за год</b> ({_smart_human_liquidity(deals)})\n\n"
        f"💡 <b>Почему это интересно:</b>\n"
        f"{_smart_human_why(area, prop, deals, avg_price, yield_top)}\n\n"
        f"✅ <b>Комфортная зона входа:</b> {_smart_human_compact_money(good_low)} — {_smart_human_compact_money(good_high)}\n"
        f"   (всё что выше — нужно обосновать видом, этажом или ремонтом)\n"
    )

    if avg_rent and yield_top:
        text += (
            f"\n🏠 <b>Аренда:</b> ~{_smart_human_compact_money(avg_rent)}/год → доходность <b>~{float(yield_top):.1f}%</b>\n"
            f"   (типичный yield для района)\n"
        )

    text += (
        f"\n⚠️ <b>На что обратить внимание:</b>\n"
        f"• Конкуренция в одном здании — проверь последние 5 сделок в твоём building\n"
        f"• Service charge: смотри 16–20 AED/sqft (выше = плохо)\n"
        f"• Желательно ready unit или handover в текущем году\n"
    )

    try:
        compare_rows, compare_notes = _v90_collect_format_comparison(scope='area', area=area, budget=budget, goal=goal, period='12')
        if not compare_rows:
            compare_rows, compare_notes = _v90_collect_format_comparison(scope='dubai', area=None, budget=budget, goal=goal, period='12')
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

    # SMART_PICK_HUMAN: Keep adaptive filter notes but in a friendly tone
    if compare_notes:
        text += "\n📌 <b>Что мы убрали из выборки:</b>\n"
        text += "У некоторых форматов мало DLD-данных под твой бюджет — показываем только те, где есть устойчивая выборка.\n"

    # SMART_PICK_HUMAN: Skip the academic 360° economic report.
    # All key numbers (price, yield, comfort range, deals) are already shown above
    # in actionable form. Long economic_engine output adds noise, not value.

    if len(rows) > 1:
        text += "\n\n🥈 <b>Альтернативы:</b>\n"
        _max_yield_pick = None  # B060: "купить дешевле и зарабатывать больше %"
        for i, r in enumerate(rows[1:4], 2):  # max 3 alternatives
            r_avg = r.get('avg_price') or 0
            # FIX (REALISTIC_YIELD): берём AVG yield, не TOP — top это нереалистичный
            # пик (17-18%) от выбросов. Если есть apt rent MV — пересчитываем напрямую.
            r_yield = r.get('avg_rental_yield_pct') or 0
            try:
                _r_rent_info = _smart_fetch_area_rent_12m(r.get('area'))
                if _r_rent_info and _r_rent_info.get('avg_rent'):
                    _r_apt_price = _r_rent_info.get('sale_avg_price_apt') or r_avg
                    if _r_apt_price:
                        r_yield = round(float(_r_rent_info['avg_rent']) / float(_r_apt_price) * 100.0, 1)
            except Exception:
                pass
            extra = ""
            try:
                if avg_price and r_avg and r_avg > avg_price * 1.05:
                    extra = f", премия за локацию +{int((r_avg/avg_price-1)*100)}%"
                elif avg_price and r_avg and r_avg < avg_price * 0.95:
                    extra = f", дешевле на ~{int((1-r_avg/avg_price)*100)}%"
                # Realistic apartment yield: 4-9% — выше скрываем как недостоверное.
                if r_yield and 3.0 <= float(r_yield) <= 12.0:
                    extra += f", yield ~{float(r_yield):.1f}%"
            except Exception:
                pass
            text += f"{i}. <b>{r.get('area')}</b> — {_smart_human_compact_money(r_avg)}{extra}\n"

            # B060: запоминаем альтернативу, которая дешевле + yield выше топа.
            try:
                if (avg_price and r_avg and float(r_avg) <= float(avg_price) * 0.85
                        and r_yield and yield_top
                        and float(r_yield) >= float(yield_top) + 1.0
                        and 3.0 <= float(r_yield) <= 12.0):
                    _cur_score = (float(yield_top or 0) - float(r_yield)) * 0
                    _candidate = {
                        'area': r.get('area'),
                        'avg': float(r_avg),
                        'yield': float(r_yield),
                        'save_pct': int((1 - float(r_avg) / float(avg_price)) * 100),
                        'yield_diff': float(r_yield) - float(yield_top),
                    }
                    if (not _max_yield_pick) or _candidate['yield'] > _max_yield_pick['yield']:
                        _max_yield_pick = _candidate
            except Exception:
                pass

        # B060: callout — "можно купить дешевле и зарабатывать в % больше".
        if _max_yield_pick:
            try:
                _alt_income = _max_yield_pick['avg'] * _max_yield_pick['yield'] / 100.0
                _top_income = float(avg_price or 0) * float(yield_top or 0) / 100.0
                _income_hint = ""
                if _top_income and _alt_income:
                    if _alt_income >= _top_income * 0.95:
                        _income_hint = " — годовой доход примерно тот же, но вложений меньше"
                    elif _alt_income >= _top_income * 0.80:
                        _income_hint = " — доход чуть ниже, но % на капитал выше"
                text += (
                    f"\n💎 <b>Хочешь максимальный yield?</b> "
                    f"<b>{_max_yield_pick['area']}</b> дешевле на ~{_max_yield_pick['save_pct']}% "
                    f"и yield выше на ~{_max_yield_pick['yield_diff']:.1f} п.п. "
                    f"(~{_max_yield_pick['yield']:.1f}% против ~{float(yield_top):.1f}%)"
                    f"{_income_hint}.\n"
                )
            except Exception:
                pass

    # SMART_PICK_HUMAN: Compact footer.
    try:
        from datetime import datetime as _dt
        _today = _dt.utcnow().strftime("%d.%m.%Y")
    except Exception:
        _today = ""
    text += f"\n📡 <i>Источник: DLD-аналитика (последние 12 мес)</i>"
    if _today:
        text += f" <i>· обновлено {_today}</i>"
    text += "\n<i>Vadim Realty · RERA BRN 65011</i>"

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


def _unified_period_html_block(scope, name, deal_type=None):
    """v104/v50: добавляет блок «совокупная динамика archive + live» к отчёту по периодам.
    Источник — unified_analytics: month-over-month, quarter-over-quarter, year-ago.
    Для rent показывает уведомление вместо тихого пропуска.
    """
    if scope not in ('building', 'area') or not name:
        return ''
    # Rent: показываем report_api данные если есть (отдельный fallback вместо silent skip)
    dt = str(deal_type or '').lower()
    is_rent = any(x in dt for x in ('аренд', 'rent', 'rental'))
    if is_rent:
        # Try to pull rent data from daily_market_reports — там есть rent_365d за район
        try:
            import report_api
            rep = report_api.get_area_report(name) if scope == 'area' else report_api.get_building_report(name)
            if rep:
                breakdown = rep.get('bedroom_breakdown') or {}
                rows = []
                for bucket in ['Studio', '1BR', '2BR', '3BR', '4BR+']:
                    bk = breakdown.get(bucket) or {}
                    r365 = bk.get('rent_365d') or {}
                    n = r365.get('n') or 0
                    med = r365.get('median_rent') or r365.get('avg_rent')
                    if n and med:
                        rows.append(f"  • <b>{bucket}</b>: {n} контрактов · медиана AED {int(med):,}/год")
                if rows:
                    lines = ["\n\n<b>🔑 Аренда — DLD контракты за 365 дней</b>"]
                    lines.extend(rows)
                    lines.append("<i>(совокупная динамика по продажам недоступна для деал-типа «аренда»; "
                                 "переключите фильтр на «Продажа» для полного отчёта)</i>")
                    return "\n".join(lines)
        except Exception as _e:
            print(f"[unified] rent fallback err: {_e}")
        return ("\n\n<i>📌 Совокупная динамика рынка доступна только для продаж. "
                "Переключите тип сделки на «🏠 Продажа» для полного отчёта.</i>")
    try:
        import unified_analytics as ua
    except Exception as e:
        print(f"[unified] import skip: {e}")
        return ''
    try:
        if scope == 'area':
            summary = ua.area_period_summary(name)
        else:
            summary = ua.building_period_summary(name)
    except Exception as e:
        print(f"[unified] summary err: {e}")
        return ''
    if not summary:
        return ''

    cm = summary.get('current_month') or {}
    n_cur = cm.get('n') or 0

    def _arrow(v):
        if v is None:
            return '—'
        if v > 0:
            return f"📈 +{v}%"
        if v < 0:
            return f"📉 {v}%"
        return f"➖ 0%"

    vm = summary.get('vs_prev_month') or {}
    vq = summary.get('vs_prev_quarter') or {}
    vy = summary.get('vs_year_ago') or {}

    lines = ["\n\n<b>📊 Совокупная динамика (archive + live)</b>"]
    if cm.get('median_price'):
        lines.append(f"• Медиана цены (тек. месяц): <b>AED {int(cm['median_price']):,}</b>")
    if cm.get('median_psf'):
        lines.append(f"• Медиана /sqft: <b>AED {int(cm['median_psf']):,}</b>")
    lines.append(f"• Сделок в этом месяце: <b>{n_cur}</b>")
    lines.append("")
    lines.append("<b>vs предыдущий месяц</b>")
    lines.append(f"  Сделки: {_arrow(vm.get('tx_change_pct'))}  •  Медиана: {_arrow(vm.get('median_change_pct'))}")
    lines.append("<b>vs предыдущий квартал</b>")
    lines.append(f"  Сделки: {_arrow(vq.get('tx_change_pct'))}  •  Медиана: {_arrow(vq.get('median_change_pct'))}")
    lines.append("<b>vs год назад (тот же месяц)</b>")
    lines.append(f"  Сделки: {_arrow(vy.get('tx_change_pct'))}  •  Медиана: {_arrow(vy.get('median_change_pct'))}")

    try:
        ai = ua.ai_period_insight(summary, lang='ru')
        if ai:
            lines.append("")
            lines.append(f"<i>🤖 {ai.strip()}</i>")
    except Exception as e:
        print(f"[unified] ai err: {e}")

    return "\n".join(lines)


async def send_period_report(message, scope, name=None, prop=None, period=None, deal_type=None):
    """v104+v52: v103 + UnifiedAnalytics period block + try/except защита от SQL crash."""
    user_id = message.from_user.id
    await send_processing(message)
    period = period or '12'
    try:
        comparison = get_comparison(scope, name, prop, period, deal_type)
    except Exception as e:
        print("SEND_PERIOD_REPORT_SQL_ERROR:", repr(e), "scope=", scope, "name=", name, "prop=", prop, "period=", period, "deal_type=", deal_type)
        await message.answer(
            no_data_message('Сравнение периодов', scope=scope, name=name,
                             prop=prop, period=period, deal_type=deal_type, user_id=user_id),
            reply_markup=no_data_menu(user_id) if scope in ['building', 'area'] else main_menu(user_id)
        )
        return
    if not comparison:
        await message.answer(
            no_data_message('Сравнение периодов', scope=scope, name=name,
                             prop=prop, period=period, deal_type=deal_type, user_id=user_id),
            reply_markup=no_data_menu(user_id) if scope in ['building', 'area'] else main_menu(user_id)
        )
        return
    current, previous = comparison
    if not current or not previous or not _int(current.get('deals')) or not _int(previous.get('deals')):
        await message.answer(no_data_message('Сравнение периодов', scope=scope, name=name, prop=prop, period=period, deal_type=deal_type, user_id=user_id), reply_markup=no_data_menu(user_id) if scope in ['building', 'area'] else main_menu(user_id))
        return
    title = _human_report_title(scope, name, 'Сравнение периодов')
    html = show_comparison(f"<b>{title}</b>", current, previous, period, deal_type)
    html += _build_360_conclusion(current, scope, name, 'period')
    try:
        html += _unified_period_html_block(scope, name, deal_type=deal_type)
    except Exception as e:
        print(f"[unified] block err: {e}")
    set_last_report(user_id, title, html, scope)
    await message.answer(html, reply_markup=_final_actions_menu(user_id, scope))


# =========================
# v104 GRACEFUL BEST-OBJECT FALLBACK FIX
# Purpose: best-object flow must not hard-fail when one exact slice is narrow.
# It must degrade gracefully by using the same market logic, preserving goal/deal type,
# and only relaxing filters step-by-step. This fixes the "По выбранным фильтрам..." nonsense.
# =========================

def _v104_goal_requires_sale(goal):
    g = str(goal or '').lower()
    return any(x in g for x in ['перепрод', 'resale', 'flip', 'рост капитала', 'capital'])


def _v104_goal_requires_rent(goal):
    g = str(goal or '').lower()
    return any(x in g for x in ['аренд', 'roi', 'доход', 'rent', 'yield'])


def _v104_best_deal_type(state):
    # For resale/capital growth the market basis must be sales even if user selected "неважно".
    if _v104_goal_requires_sale((state or {}).get('goal')):
        return '🏠 Продажа'
    dt = (state or {}).get('deal_type')
    if not dt or str(dt).lower() in ['none', 'неважно', '📊 неважно', 'skip', 'пропустить']:
        return '🏠 Продажа'
    return dt


def _v104_clean_best_object_state(state):
    st = dict(state or {})
    st['deal_type'] = _v104_best_deal_type(st)
    fmt = st.get('object_format')
    # Land/commercial cannot carry bedroom filters from previous state.
    if fmt and any(x in str(fmt).lower() for x in ['plot', 'land', 'зем', 'плот', 'office', 'shop', 'commercial', 'retail']):
        st['rooms'] = None
    return st


def _v104_query_top(kind, state, relaxed_budget=False, relaxed_rooms=False, relaxed_format=False, limit=3, min_count=1):
    """More tolerant version of _v95_query_top.
    Keeps the same architecture/table helpers but avoids hard failure on narrow slices.
    """
    st = _v104_clean_best_object_state(state)
    deal_type = st.get('deal_type') or '🏠 Продажа'
    src = _v95_value_source(deal_type)
    value_expr = src['value']
    meter_expr = src['meter']
    fmt_sql, fmt_args = ("", []) if relaxed_format else _v95_format_clause(st.get('object_format'), _v95_is_rent(deal_type))
    room_sql, room_args = ("", []) if relaxed_rooms else _v95_rooms_clause(st.get('rooms'))
    budget_sql, budget_args = ("", []) if relaxed_budget else _v95_budget_clause(value_expr, st.get('budget'))
    group_col = 'area_name_en' if kind == 'area' else 'building_name_en'
    extra_select = 'COUNT(DISTINCT building_name_en) AS buildings,' if kind == 'area' else 'MAX(area_name_en) AS area_name_en,'
    not_empty = f"AND NULLIF({group_col}::text, '') IS NOT NULL"

    # For best-object search, current 36-month filter can be too harsh in a sparse slice.
    # We first keep it, then caller will retry with date relaxed by passing relaxed_period=True through state.
    date_filter = "" if st.get('_v104_relaxed_period') else src['date_filter']

    params = fmt_args + room_args + budget_args + [min_count, limit]
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
          {date_filter}
          {not_empty}
          AND {value_expr} IS NOT NULL
          AND {value_expr} > 0
        GROUP BY {group_col}
        HAVING COUNT(*) >= %s
        ORDER BY deals DESC, avg_price ASC NULLS LAST
        LIMIT %s
    """
    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        cleaned = []
        for r in rows or []:
            avg = _v95_num(r.get('avg_price'), None)
            if avg is None or avg <= 0:
                continue
            # Guard dirty values in best-object output.
            r['min_price'] = _v101_clean_min_price(r.get('min_price'), avg, st.get('budget'))
            r['score'] = _v95_score(r, st.get('goal'))
            cleaned.append(r)
        return sorted(cleaned, key=lambda r: (_v95_num(r.get('score')), _v95_num(r.get('deals'))), reverse=True)[:limit]
    except Exception as e:
        print('V104_TOP_QUERY_ERROR', kind, repr(e))
        return []


def _v104_top_areas_and_buildings(state):
    """Graceful degradation for best-object only.
    Order is intentional:
    1) exact;
    2) remove period limit;
    3) widen budget;
    4) remove rooms;
    5) remove rooms + widen budget;
    6) only then remove format as Dubai benchmark.
    """
    base = _v104_clean_best_object_state(state)
    attempts = [
        ({}, 'точный фильтр'),
        ({'_v104_relaxed_period': True}, 'расширил период анализа'),
        ({'_v104_relaxed_period': True, '_relaxed_budget': True}, 'расширил период и бюджетный коридор'),
        ({'_v104_relaxed_period': True, '_relaxed_rooms': True}, 'расширил период и снял комнатность'),
        ({'_v104_relaxed_period': True, '_relaxed_budget': True, '_relaxed_rooms': True}, 'расширил период, бюджет и снял комнатность'),
        ({'_v104_relaxed_period': True, '_relaxed_budget': True, '_relaxed_rooms': True, '_relaxed_format': True}, 'использовал рыночный benchmark без формата только как последний ориентир'),
    ]
    tried_notes = []
    for flags, label in attempts:
        st = dict(base)
        st.update(flags)
        areas = _v104_query_top('area', st, relaxed_budget=bool(flags.get('_relaxed_budget')), relaxed_rooms=bool(flags.get('_relaxed_rooms')), relaxed_format=bool(flags.get('_relaxed_format')), limit=3, min_count=1)
        buildings = _v104_query_top('building', st, relaxed_budget=bool(flags.get('_relaxed_budget')), relaxed_rooms=bool(flags.get('_relaxed_rooms')), relaxed_format=bool(flags.get('_relaxed_format')), limit=3, min_count=1)
        if areas or buildings:
            notes = []
            if label != 'точный фильтр':
                notes.append('Точная выборка была узкой, поэтому я не остановил сценарий, а ' + label + '.')
            if flags.get('_relaxed_format'):
                notes.append('Формат снят только как benchmark; финальное решение по объекту нужно подтверждать по конкретному формату.')
            return areas, buildings, notes
        tried_notes.append(label)
    return [], [], tried_notes


def _v104_best_object_limited_report(state):
    st = _v104_clean_best_object_state(state)
    return (
        '⚠️ <b>Лучший объект</b>\n\n'
        'По выбранной комбинации фильтров не нашёл даже расширенной DLD-выборки, достаточной для честного ранжирования.\n\n'
        f'📊 <b>Сделка:</b> {st.get("deal_type") or "продажа"}\n'
        f'🏠 <b>Формат:</b> {st.get("object_format") or "любой формат"}\n'
        f'💰 <b>Бюджет:</b> {st.get("budget") or "не указан"}\n'
        f'🛏 <b>Комнаты:</b> {st.get("rooms") or "неважно"}\n'
        f'🎯 <b>Цель:</b> {st.get("goal") or "сбалансированно"}\n\n'
        '📌 <b>Что это значит:</b> это не рекомендация отказаться от покупки. Это значит, что для автоматического рейтинга мало сопоставимых DLD-сделок. '\
        'Следующий правильный шаг — проверить конкретный объект вручную: последние сделки в здании/районе, площадь, этаж, вид, сервисные платежи и срочность продавца.'
    )


def build_best_object_report_v95(state, *, user_id=None):
    """v104 override: graceful degradation instead of hard no-data failure."""
    payload = _ir_v96_prepare(state, 'best object')
    normalized = _v104_clean_best_object_state(state or {})
    if payload:
        req = payload.request
        fmt_map = {
            'apartment': 'Apartment', 'townhouse': 'Townhouse', 'villa': 'Villa',
            'land': 'Plot', 'plot': 'Plot', 'office': 'Office', 'shop': 'Shop',
            'retail': 'Shop', 'commercial': 'Commercial', 'penthouse': 'Penthouse', 'duplex': 'Duplex',
        }
        normalized['deal_type'] = _v104_best_deal_type(normalized)
        if req.property_format:
            normalized['object_format'] = fmt_map.get(req.property_format, normalized.get('object_format'))
        if req.property_format in {'land', 'plot', 'office', 'shop', 'retail', 'commercial', 'warehouse', 'full_building'}:
            normalized['rooms'] = None
        elif req.bedrooms:
            normalized['rooms'] = req.bedrooms.title().replace(' Br', ' BR')
        normalized['goal'] = _v96_goal_text(payload, normalized)

    areas, buildings, notes = _v104_top_areas_and_buildings(normalized)
    if not areas and not buildings:
        return _v104_best_object_limited_report(normalized)

    best_area = areas[0] if areas else None
    best_building = buildings[0] if buildings else None
    chosen = best_building or best_area or {}

    deal_type = normalized.get('deal_type') or '🏠 Продажа'
    obj_format = normalized.get('object_format') or 'любой формат'
    budget = normalized.get('budget') or 'не указан'
    rooms = normalized.get('rooms') or 'неважно'
    goal = normalized.get('goal') or 'сбалансированная стратегия'

    avg_price = _v95_num(chosen.get('avg_price'), None)
    min_price = _v101_clean_min_price(chosen.get('min_price'), avg_price, budget)
    entry_low, entry_high = _v101_entry_range(chosen, budget)

    html = '🏆 <b>Лучший объект</b>\n\n'
    html += _ir_v96_notes_html(payload)
    html += (
        f'📊 <b>Сделка:</b> {deal_type}\n'
        f'🏠 <b>Формат:</b> {obj_format}\n'
        f'💰 <b>Бюджет:</b> {budget}\n'
        f'🛏 <b>Комнаты:</b> {rooms}\n'
        f'🎯 <b>Цель:</b> {goal}\n\n'
    )

    if notes:
        html += '📌 <b>Адаптивная логика</b>\n' + '\n'.join([f'• {n}' for n in notes]) + '\n\n'

    if best_area:
        html += f'🥇 <b>Лучший район:</b> {best_area.get("name")}\n'
    if best_building:
        html += f'🥇 <b>Лучший объект / здание:</b> {best_building.get("name")}'
        if best_building.get('area_name_en'):
            html += f' — {best_building.get("area_name_en")} '
        html += '\n'

    html += (
        f'💰 <b>Средний ориентир:</b> {format_money(avg_price)}\n'
        f'✅ <b>Комфортная зона входа:</b> {format_money(entry_low)} — {format_money(entry_high)}\n'
        f'🔥 <b>Сильная точка входа:</b> ниже среднего DLD или ближе к нижним подтверждённым сделкам {format_money(min_price)}\n\n'
    )

    if areas:
        html += '🏙 <b>Топ-3 района под цель</b>\n\n'
        for i, r in enumerate(areas[:3], 1):
            html += _v95_line(i, r, 'area') + '\n'
    if buildings:
        html += '🏢 <b>Топ-3 объекта / здания</b>\n\n'
        for i, r in enumerate(buildings[:3], 1):
            html += _v95_line(i, r, 'building') + '\n'

    # Goal-aware conclusion. Do not require rent model for resale.
    g = str(goal or '').lower()
    if _v104_goal_requires_sale(goal):
        strategy = (
            'Для перепродажи главный критерий — ликвидность и цена входа ниже DLD-средней. '
            'Система ранжирует варианты по количеству сделок, среднему уровню цены, диапазону min/avg/max и глубине рынка. '
            'Арендные данные здесь вторичны: их отсутствие не должно останавливать анализ перепродажи.'
        )
    elif _v104_goal_requires_rent(goal):
        strategy = (
            'Для ROI важны две проверки: цена покупки по sale-DLD и арендный потенциал по rent-DLD. '
            'Если rent-выборка узкая, решение не блокируется, но confidence по доходности снижается и объект нужно проверить вручную.'
        )
    else:
        strategy = (
            'Для сбалансированной стратегии важны ликвидность района, понятный формат, справедливая цена входа и возможность выхода без сильного дисконта.'
        )

    html += (
        '🧠 <b>Экономическое заключение 360°</b>\n\n'
        f'{strategy}\n\n'
        '📌 <b>Практический вывод:</b> используйте топ как short-list. Перед депозитом проверьте конкретный unit: площадь, этаж, вид, состояние, сервисные платежи, последние DLD-сделки и мотивацию продавца.'
    )
    return html

print('Loaded v104 graceful best-object fallback fix')


print('Loaded v103 building format and strict comparison fix')


# =========================================================================
# v105 DAILY MARKET REPORTS UI
# =========================================================================
# Wires daily_reports + report_api into the bot UI. Pulls precomputed reports
# from intelligence DB — no on-the-fly aggregation.

def _fmt_aed(v):
    if v is None: return "—"
    try: return f"AED {int(v):,}"
    except Exception: return str(v)


def _fmt_pct(v):
    if v is None: return "—"
    if v > 0: return f"📈 +{v}%"
    if v < 0: return f"📉 {v}%"
    return "➖ 0%"


def _format_market_report(report: dict) -> str:
    """Форматирует полный отчёт в HTML для Telegram."""
    if not report:
        return ("📑 <b>Отчёт ещё не сгенерирован</b>\n\n"
                "Запустите daily_reports.run_daily() или подождите ночного крона.")

    scope = report.get("scope") or "?"
    entity = report.get("entity") or "Дубай"
    meta = report.get("_meta") or {}
    rpt_date = meta.get("report_date") or report.get("report_date") or "—"
    stale_mark = " ⚠️ устарел" if meta.get("stale") else ""

    totals = report.get("totals") or {}
    dyn = report.get("dynamics") or {}
    vsm = dyn.get("vs_prev_month") or {}
    vsy = dyn.get("vs_year_ago") or {}
    breakdown = report.get("bedroom_breakdown") or {}

    scope_emoji = {"dubai": "🌆", "area": "🏙", "building": "🏢"}.get(scope, "📑")
    lines = [f"{scope_emoji} <b>Полный отчёт — {entity}</b>"]
    lines.append(f"<i>Дата отчёта: {rpt_date}{stale_mark}</i>")
    lines.append("")
    lines.append("<b>📊 Ключевые метрики (30 дней)</b>")
    lines.append(f"• Сделок:          <b>{totals.get('deals_30d') or 0}</b>")
    lines.append(f"• Медиана цены:    <b>{_fmt_aed(totals.get('median_price_30d'))}</b>")
    lines.append(f"• Медиана /sqft:   <b>{_fmt_aed(totals.get('median_psf_30d'))}</b>")
    lines.append(f"• Сделок за 90д:   <b>{totals.get('deals_90d') or 0}</b>")
    lines.append(f"• Сделок за 365д:  <b>{totals.get('deals_365d') or 0}</b>")
    lines.append("")
    lines.append("<b>📈 Динамика</b>")
    lines.append(f"vs предыдущий месяц:  сделки {_fmt_pct(vsm.get('deals_change_pct'))}  •  медиана {_fmt_pct(vsm.get('median_change_pct'))}")
    lines.append(f"vs год назад:         сделки {_fmt_pct(vsy.get('deals_change_pct'))}  •  медиана {_fmt_pct(vsy.get('median_change_pct'))}")
    lines.append("")
    lines.append("<b>🛏 По типам квартир (медианы за 30д сделок + ROI)</b>")

    for bucket in ["Studio", "1BR", "2BR", "3BR", "4BR+"]:
        bk = breakdown.get(bucket) or {}
        s30 = bk.get("sales_30d") or {}
        s365 = bk.get("sales_365d") or {}
        r365 = bk.get("rent_365d") or {}
        n = s30.get("n") or s365.get("n") or 0
        price = s30.get("median_price") or s365.get("median_price")
        psf = s30.get("median_psf") or s365.get("median_psf")
        rent = r365.get("median_rent") or r365.get("avg_rent")
        roi = bk.get("roi_pct_365d")
        if n == 0 and not price and not rent:
            continue
        parts = [f"<b>{bucket}</b> ({n} сд.)"]
        if price: parts.append(f"цена {_fmt_aed(price)}")
        if psf:   parts.append(f"{_fmt_aed(psf)}/sqft")
        if rent:  parts.append(f"аренда {_fmt_aed(rent)}/год")
        if roi:   parts.append(f"ROI <b>{roi}%</b>")
        lines.append("  • " + "  •  ".join(parts))

    return "\n".join(lines)


async def send_full_market_report(message, scope: str, name=None):
    """Достаёт отчёт из intelligence DB и отправляет пользователю."""
    user_id = message.from_user.id
    await send_processing(message)
    try:
        import report_api
    except Exception as e:
        await message.answer(f"❌ report_api не доступен: {e}", reply_markup=main_menu(user_id))
        return

    rep = None
    try:
        if scope == "dubai":
            rep = report_api.get_dubai_report()
        elif scope == "area":
            rep = report_api.get_area_report(name)
        elif scope == "building":
            rep = report_api.get_building_report(name)
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения отчёта: {e}", reply_markup=main_menu(user_id))
        return

    if not rep:
        # Fallback: попытаемся посчитать на лету (медленно, но что-то покажем)
        try:
            from daily_reports import build_report
            built, n, med, psf = build_report(scope, name)
            built["_meta"] = {"stale": False, "report_date": built.get("report_date")}
            rep = built
        except Exception as e:
            await message.answer(
                f"📑 По запросу <b>{name or 'Дубай'}</b> отчёт не найден.\n"
                f"Возможно, район/здание написано иначе, либо отчёт ещё не сгенерирован.\n"
                f"(err: {e})",
                reply_markup=main_menu(user_id),
            )
            return

    html = _format_market_report(rep)

    # AI-нарратив (best effort, опциональный)
    try:
        from llm_chain import llm_call
        totals = rep.get("totals") or {}
        dyn = rep.get("dynamics") or {}
        prompt = (
            f"Ты — старший аналитик рынка Дубая. На основе сухих метрик ниже напиши КОРОТКОЕ "
            f"(2-3 предложения, до 350 символов) экспертное мнение по-русски. Будь конкретен.\n\n"
            f"Объект: {scope}/{name or 'Дубай'}\n"
            f"Сделок за 30д: {totals.get('deals_30d')}\n"
            f"Медиана цены: {totals.get('median_price_30d')}\n"
            f"Медиана /sqft: {totals.get('median_psf_30d')}\n"
            f"Динамика vs пред. месяц: {dyn.get('vs_prev_month')}\n"
            f"Динамика vs год назад: {dyn.get('vs_year_ago')}\n\n"
            f"Вердикт (только проза):"
        )
        ai = llm_call(prompt, max_tokens=200, timeout=10)
        if ai:
            html += f"\n\n<i>🤖 {ai.strip()}</i>"
    except Exception as e:
        print(f"[full_report ai] {e}")

    # v47/v49 ECOSYSTEM cross-nav (inline urls — рядом с отчётом)
    # Telegram /start payload только [A-Za-z0-9_] (64 char max). Очищаем Cyrillic/&/+/?
    def _safe_payload(s):
        import re as _re
        if not s:
            return ""
        s2 = s.replace(" ", "_")
        # Оставляем только ASCII alphanumeric и _ (Telegram req)
        s2 = _re.sub(r"[^A-Za-z0-9_]", "", s2)
        return s2[:30] or "x"

    cross_nav_html = "\n\n<b>🔗 Связанные сервисы:</b>\n"
    if scope == "area" and name:
        nm_enc = _safe_payload(name)
        cross_nav_html += (
            f"🏘 <a href=\"https://t.me/dubai_resale_fpr_bot?start=area_{nm_enc}\">Готовое жильё в районе</a>\n"
            f"🏗 <a href=\"https://t.me/dubai_projects_monitor_bot?start=area_{nm_enc}\">Новостройки в районе</a>\n"
            f"📊 <a href=\"https://t.me/dubai_roi_fpr_bot?start=area_{nm_enc}\">Рассчитать ROI</a>\n"
        )
    elif scope == "building" and name:
        nm_enc = _safe_payload(name)
        cross_nav_html += (
            f"🏘 <a href=\"https://t.me/dubai_resale_fpr_bot?start=bld_{nm_enc}\">Готовое жильё в здании</a>\n"
            f"📊 <a href=\"https://t.me/dubai_roi_fpr_bot?start=from_analytics\">ROI калькулятор</a>\n"
        )
    else:
        cross_nav_html += (
            f"🏘 <a href=\"https://t.me/dubai_resale_fpr_bot?start=from_analytics\">Готовое жильё</a>  •  "
            f"🏗 <a href=\"https://t.me/dubai_projects_monitor_bot?start=from_analytics\">Новостройки</a>  •  "
            f"📊 <a href=\"https://t.me/dubai_roi_fpr_bot?start=from_analytics\">ROI</a>\n"
        )
    # Lead-bot deep-link: префикс area-/bld- + sanitized name
    lead_payload = (
        f"area-{_safe_payload(name)}" if scope == "area" and name
        else f"bld-{_safe_payload(name)}" if scope == "building" and name
        else "analytics"
    )
    cross_nav_html += (
        f"💼 <a href=\"https://t.me/dubai_fpr_lead_bot?start={lead_payload}\">"
        f"Оставить заявку агенту</a>"
    )
    html += cross_nav_html

    await message.answer(html, reply_markup=main_menu(user_id))


async def send_multi_area_report(message, area_names):
    """Объединённый отчёт по нескольким районам."""
    user_id = message.from_user.id
    await send_processing(message)
    try:
        import report_api
        combined = report_api.get_multi_area_report(area_names)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=main_menu(user_id))
        return

    if not combined or not combined.get("per_area"):
        await message.answer(
            f"📑 По районам [{', '.join(area_names)}] отчёты не найдены.\n"
            f"Возможно названия указаны иначе или отчёты ещё не сгенерированы.",
            reply_markup=main_menu(user_id),
        )
        return

    totals = combined.get("totals") or {}
    per = combined.get("per_area") or {}
    lines = [f"📑 <b>Объединённый отчёт по районам</b>"]
    lines.append(f"<i>{', '.join(area_names)}</i>")
    lines.append("")
    lines.append("<b>📊 Сводные метрики (30д, взвешено по сделкам)</b>")
    lines.append(f"• Сделок:        <b>{totals.get('deals_30d') or 0}</b>")
    lines.append(f"• Сделок 90д:    <b>{totals.get('deals_90d') or 0}</b>")
    lines.append(f"• Сделок 365д:   <b>{totals.get('deals_365d') or 0}</b>")
    lines.append(f"• Медиана цены:  <b>{_fmt_aed(totals.get('median_price_30d'))}</b>")
    lines.append(f"• Медиана /sqft: <b>{_fmt_aed(totals.get('median_psf_30d'))}</b>")
    lines.append("")
    lines.append("<b>📍 По районам</b>")
    # компактный rank по сделкам
    sorted_areas = sorted(
        per.items(),
        key=lambda kv: -((kv[1].get("totals") or {}).get("deals_30d") or 0),
    )
    for a, rep in sorted_areas:
        t = rep.get("totals") or {}
        dyn = (rep.get("dynamics") or {}).get("vs_year_ago") or {}
        lines.append(
            f"• <b>{a}</b>: {t.get('deals_30d') or 0} сд.  •  "
            f"медиана {_fmt_aed(t.get('median_price_30d'))}  •  "
            f"YoY {_fmt_pct(dyn.get('median_change_pct'))}"
        )

    await message.answer("\n".join(lines), reply_markup=main_menu(user_id))


print('Loaded v105 daily market reports UI')


# =========================
# v106 USER-REPORTED BUG FIXES
# Scope:
#   Bug 1: "Аналитика" + "6 месяцев" падает в "нет стабильной выборки" даже когда
#          в DLD за тот же период данные есть. Причина: get_stats_smart attempts
#          могут получать SQL exceptions (AmbiguousColumn / UndefinedTable),
#          which are swallowed inside _run_source_sql_v67. Если ВСЕ attempts
#          молча вернули 0 — мы показываем no_data, но реально это не "нет данных",
#          а "выборки за период нет; нужно расширить". Решение: добавляем явный
#          retry на period=None ВНУТРИ send_full_report когда SMART вернул None;
#          + лог об этом через error_logger.
#
#   Bug 2: Wizard "Сделки DLD" застрял на Шаге 1. После показа "Шаг 1 из 3" wizard
#          у пользователя теряется state (race / новый сессионный poll), и нажатие
#          на "🏠 Продажа" / "🔑 Аренда" попадает в финальный fallback, который
#          шлёт main_menu. Решение: ловим эти 4 кнопки раньше финального fallback
#          и форсим step=choose_deal_type если state пустой/потерян.
#
#   Bug 3: "Address Opera" не находится. Причина: _query_aliases_v66 не разворачивает
#          такие алиасы как "the address residences dubai opera" / "opera grande".
#          Решение: подменяем _query_aliases_v66 — добавляем алиасы для известных
#          зданий + AND-токенный поиск как fallback (для запросов из 2+ слов).
# =========================

import re as _re_v106


def _v106_log(scenario, err, **ctx):
    try:
        import error_logger as _el, traceback as _tb
        _el.log_error("analytics", scenario, str(err),
                       error_class=type(err).__name__ if isinstance(err, BaseException) else "InfoEvent",
                       context=ctx,
                       tb=_tb.format_exc()[-1500:])
    except Exception:
        pass


# ---- Bug 3: расширенные алиасы поиска зданий ----------------------------------

_V106_BUILDING_ALIASES = {
    # Канонические building_name_en в архиве: 'The Address Residences Dubai Opera T1',
    # 'The Address Residences Dubai Opera T2', 'Opera Grand', 'THE ADDRESS DUBAI OPERA'.
    "address opera": [
        "address opera",
        "the address opera",
        "address dubai opera",
        "the address dubai opera",
        "address residences dubai opera",
        "the address residences dubai opera",
        "The Address Residences Dubai Opera T1",
        "The Address Residences Dubai Opera T2",
        "THE ADDRESS DUBAI OPERA",
        "Opera Grand",
        "address residences",
        "residences dubai opera",
    ],
    "the address opera": [
        "address opera",
        "the address opera",
        "address residences dubai opera",
        "the address residences dubai opera",
        "address dubai opera",
        "The Address Residences Dubai Opera T1",
        "The Address Residences Dubai Opera T2",
        "THE ADDRESS DUBAI OPERA",
    ],
    "address residences dubai opera": [
        "address residences dubai opera",
        "the address residences dubai opera",
        "The Address Residences Dubai Opera T1",
        "The Address Residences Dubai Opera T2",
        "THE ADDRESS DUBAI OPERA",
        "address opera",
    ],
    "the address residences dubai opera": [
        "address residences dubai opera",
        "the address residences dubai opera",
        "The Address Residences Dubai Opera T1",
        "The Address Residences Dubai Opera T2",
        "THE ADDRESS DUBAI OPERA",
        "address opera",
    ],
    "opera grand": [
        "opera grand",
        "Opera Grand",
        "il primo",
    ],
    "opera grande": ["opera grande", "il primo", "the address residences dubai opera", "opera grand"],
    "grande burj khalifa": ["grande signature", "grande burj khalifa", "the grande", "grande residence"],
    "grande": ["grande signature", "the grande", "grande burj khalifa", "grande residence"],
    "burj khalifa": ["burj khalifa", "burj views", "the residences", "armani residence"],
    "binghatti corner": ["binghatti corner"],
    "corner": ["binghatti corner", "marina corner"],
    "marina gate": ["marina gate"],
    "burj vista": ["burj vista"],
    "address downtown": ["address downtown", "the address downtown"],
    "address residences": ["address residences"],
    "sobha hartland": ["sobha hartland"],
}


def _v106_query_aliases(q):
    try:
        cleaned = clean_query(q) if 'clean_query' in globals() else (str(q or "").strip())
    except Exception:
        cleaned = str(q or "").strip()
    if not cleaned:
        return []

    low = cleaned.lower().strip()
    aliases = [cleaned]

    # Существующие area aliases (не теряем поведение v66).
    try:
        for k, vals in _AREA_ALIASES_V66.items():
            if low == k or low in [v.lower() for v in vals]:
                aliases.extend(vals)
    except Exception:
        pass
    if low == "jvc":
        aliases.append("Jumeirah Village Circle")

    # Новые building aliases — точные подставки для известных зданий.
    if low in _V106_BUILDING_ALIASES:
        aliases.extend(_V106_BUILDING_ALIASES[low])
    else:
        # Частичный матч по ключам (например "address opera tower 2" → "address opera")
        for k, vals in _V106_BUILDING_ALIASES.items():
            if k in low or low in k:
                aliases.extend(vals)
                break

    # dedup, preserve order
    out = []
    seen = set()
    for a in aliases:
        if not a:
            continue
        key = a.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


# Override v66's alias resolver. Все downstream search функции v66 будут
# использовать расширенный список алиасов.
_query_aliases_v66 = _v106_query_aliases  # noqa: F811


# Дополнительный robust find_buildings: если стандартный v66 search не дал
# ничего по запросу из 2+ слов, делаем AND-токенный fallback на live таблицах.
try:
    _v106_original_find_buildings = find_buildings  # capture v66
except NameError:
    _v106_original_find_buildings = None


def _v106_token_fallback_find(query, limit=10):
    """AND-tokenized fallback search. v106.1: запускается ВСЕГДА и мёрджится с v66,
    чтобы пользовательский ввод вида "Address Opera" (а в архиве канонические имена
    содержат лишние слова "Residences Dubai") всё равно матчил T1/T2.
    v111: сначала бьём по mv_building_12m_summary (trigram) — мгновенно.
    Только если read-model молчит — откатываемся в raw dld_sale_archive ILIKE.
    """
    import time as _t
    _t0 = _t.perf_counter()
    q = str(query or "").strip()
    if not q:
        return []
    # v106.1: ослабили требование — допускаем 1 токен >=3 букв (для "opera"),
    # т.к. при коротком single-token запросе v66 может не найти из-за фильтра минимального деалов.
    tokens = [t for t in _re_v106.split(r"\s+", q.lower()) if len(t) >= 3]
    if not tokens:
        return []
    # ── v111 fast path: read-model trigram lookup ─────────────────────────
    if _READ_MODEL_OK and _read_model:
        try:
            sql = """
                SELECT building_name_display AS building_name_en,
                       area_name AS area_name_en,
                       deals::bigint AS deals
                FROM mv_building_12m_summary
                WHERE deal_type='sale' AND rooms='all' AND deals > 0
                  AND lower(building_name_display) ILIKE ALL(%s)
                ORDER BY deals DESC
                LIMIT %s
            """
            patterns = [f"%{t}%" for t in tokens]
            with _read_model._conn().cursor() as cur:
                cur.execute(sql, (patterns, limit))
                fast_rows = [
                    {"building_name_en": r[0], "area_name_en": r[1], "deals": int(r[2] or 0)}
                    for r in cur.fetchall()
                ]
            dt_ms = (_t.perf_counter() - _t0) * 1000
            print(f"[LAT] v111_token_fallback_mv: {dt_ms:.0f}ms tokens={len(tokens)} hits={len(fast_rows)}")
            if fast_rows:
                return fast_rows
        except Exception as _e:
            print(f"[v111_token_fb_mv_err] {_e}")
    # ── raw DLD fallback (медленный) ──────────────────────────────────────
    rows = []
    try:
        for source in _active_sources():
            old_src = globals().get("_ACTIVE_SOURCE", "live")
            try:
                _set_data_source(source)
                # Перебираем оба stable plan-source таблицы.
                for _src, table in _v67_table_plan(None):
                    if _src != source:
                        continue
                    try:
                        cols = _cols_v66(table)
                    except Exception:
                        cols = set()
                    if not cols:
                        continue
                    building = _txt_expr_v66(cols, [
                        "building_name_en", "building_name", "building", "project_name_en", "project_name", "project_en", "project",
                        "master_project_en", "master_project", "property_name_en", "property_name",
                    ])
                    area = _txt_expr_v66(cols, ["area_name_en", "area_en", "area_name", "area", "procedure_area"])
                    if building == "''" and area == "''":
                        continue
                    haystack = f"({building} || ' ' || {area})"
                    where_parts = [f"{haystack} ILIKE %s" for _ in tokens]
                    params = [f"%{t}%" for t in tokens]
                    sql = f"""
                        SELECT
                            NULLIF({building}, '') AS building_name_en,
                            NULLIF({area}, '') AS area_name_en,
                            COUNT(*)::bigint AS deals
                        FROM {table}
                        WHERE ({' AND '.join(where_parts)})
                          AND NULLIF({building}, '') IS NOT NULL
                        GROUP BY NULLIF({building}, ''), NULLIF({area}, '')
                        ORDER BY deals DESC
                        LIMIT %s
                    """
                    try:
                        with db() as conn:
                            with conn.cursor() as cur:
                                cur.execute(sql, params + [limit])
                                rows.extend(cur.fetchall())
                    except Exception as e:
                        _v106_log("find_buildings_token_fallback", e, table=table, source=source, q=q)
            finally:
                try:
                    _set_data_source(old_src)
                except Exception:
                    pass
    except Exception as e:
        _v106_log("find_buildings_token_fallback_outer", e, q=q)
    # dedup + sort
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: -_int(r.get("deals"))):
        key = (str(r.get("building_name_en") or "").lower().strip(),
               str(r.get("area_name_en") or "").lower().strip())
        if key in seen or not key[0]:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    dt_ms = (_t.perf_counter() - _t0) * 1000
    if dt_ms > 500:
        print(f"[LAT_SLOW] _v106_token_fallback_find: {dt_ms:.0f}ms raw_path tokens={len(tokens)}")
    return out


def find_buildings(query, limit=10):  # noqa: F811
    """v106.1: всегда мёрджим v66 + token-fallback. Раньше fallback бежал только
    при пустом v66 — но для "Address Opera" v66 находил "THE ADDRESS DUBAI OPERA"
    с малым кол-вом сделок, а T1/T2 (где основной массив) терялись.

    v_unified: если оба способа пустые — fallback в канонический trigram-поиск
    из building_search (опечатки + алиасы + master-zone link). Так бот
    НИКОГДА не возвращает пустоту: показывает похожие здания.
    """
    rows = []
    if _v106_original_find_buildings is not None:
        try:
            rows = _v106_original_find_buildings(query, limit) or []
        except Exception as e:
            _v106_log("find_buildings_primary", e, q=query)
            rows = []
    try:
        fb = _v106_token_fallback_find(query, limit) or []
    except Exception as e:
        _v106_log("find_buildings_fallback_outer", e, q=query)
        fb = []
    # ── v_unified: trigram suggest ──
    suggest = []
    if not rows and not fb:
        try:
            from building_search import search_building_canonical as _bs
            cands = _bs(query, limit=limit, include_popular_fallback=False) or []
            for c in cands:
                suggest.append({
                    "building_name_en": c["name"],
                    "area_name_en":     c.get("master_zone") or c.get("area_name"),
                    "deals":            c.get("deals_12m") or 0,
                    "_kind":            c.get("kind"),
                    "_master_zone":     c.get("master_zone"),
                })
        except Exception as e:
            _v106_log("find_buildings_trgm_suggest", e, q=query)
    if not rows and not fb and not suggest:
        return []
    # Мёрдж: dedup по (building.lower, area.lower), сортировка по deals desc.
    seen, merged = set(), []
    for r in list(rows) + list(fb) + list(suggest):
        try:
            key = (
                str((r.get("building_name_en") if hasattr(r, "get") else r["building_name_en"]) or "").lower().strip(),
                str((r.get("area_name_en") if hasattr(r, "get") else r["area_name_en"]) or "").lower().strip(),
            )
        except Exception:
            continue
        if not key[0] or key in seen:
            continue
        seen.add(key)
        merged.append(r)
    try:
        merged.sort(key=lambda r: -_int(r.get("deals") if hasattr(r, "get") else r["deals"]))
    except Exception:
        pass
    return merged[:limit]


# ---- Bug 1: безопасный fallback для "Аналитика + 6 месяцев" -------------------

try:
    _v106_original_get_stats_smart = get_stats_smart  # v83/v67/etc
except NameError:
    _v106_original_get_stats_smart = None


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v106: оборачиваем существующий get_stats_smart, но если он вернул None
    при заданном периоде — пробуем явно ещё раз с period=None и логируем событие.
    Это исключает "ложно-пустую" выборку, когда DLD-данные в архиве идут не до
    сегодняшней даты и узкий 6-месячный период падает в 0.
    """
    if _v106_original_get_stats_smart is None:
        return None, prop, period, deal_type
    try:
        row, used_prop, used_period, used_deal_type = _v106_original_get_stats_smart(
            scope, name, prop, period, deal_type
        )
    except Exception as e:
        _v106_log("get_stats_smart_v106_outer", e,
                  scope=scope, name=str(name)[:120] if name else None,
                  prop=prop, period=period, deal_type=deal_type)
        row, used_prop, used_period, used_deal_type = None, prop, period, deal_type

    if row and _int(row.get("deals")) > 0:
        return row, used_prop, used_period, used_deal_type

    # v106.1: если v83 проглотил exception и вернул пусто — пробуем все периоды
    # через прямой get_stats (минуя v83-обёртку), чтобы исключить тихие сбои.
    direct_attempts = []
    if period:
        direct_attempts.append((prop, None, deal_type))     # тот же prop, без периода
    direct_attempts.append((None, period, deal_type))       # без prop, заданный период
    direct_attempts.append((None, None, deal_type))         # без всего, по deal_type
    if deal_type:
        direct_attempts.append((prop, period, None))        # снять deal_type фильтр
        direct_attempts.append((None, None, None))          # полностью без фильтров (последний шанс)

    for p_try, per_try, dt_try in direct_attempts:
        try:
            r2 = get_stats(scope, name, p_try, per_try, dt_try)
            if r2 and _int(r2.get("deals")) > 0:
                _v106_log("stats_smart_direct_fallback", "v83_returned_empty",
                          scope=scope, name=str(name)[:120] if name else None,
                          orig=(prop, period, deal_type),
                          used=(p_try, per_try, dt_try))
                return r2, p_try, per_try, dt_try
        except Exception as e:
            _v106_log("get_stats_smart_v106_direct", e,
                      scope=scope, name=str(name)[:120] if name else None,
                      attempt=(p_try, per_try, dt_try))
            continue

    return None, prop, period, deal_type


# ---- Bug 1b: безопасный fallback для "Последние сделки" -----------------------

try:
    _v106_original_get_latest_deals_smart = get_latest_deals_smart  # v83
except NameError:
    _v106_original_get_latest_deals_smart = None


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    """v106: оборачиваем v83 get_latest_deals_smart по образцу get_stats_smart.
    Если v83 вернул [] при заданном period (например 6 месяцев) — пробуем явно
    повторно через прямой get_latest_deals с расширением периода до None/all-time."""
    if _v106_original_get_latest_deals_smart is None:
        return [], prop, period, deal_type
    try:
        rows, used_prop, used_period, used_deal_type = _v106_original_get_latest_deals_smart(
            scope, name, prop, period, deal_type, limit=limit, unit_query=unit_query
        )
    except Exception as e:
        _v106_log("get_latest_deals_smart_v106_outer", e,
                  scope=scope, name=str(name)[:120] if name else None,
                  prop=prop, period=period, deal_type=deal_type)
        rows, used_prop, used_period, used_deal_type = [], prop, period, deal_type

    if rows:
        return rows, used_prop, used_period, used_deal_type

    direct_attempts = []
    if period:
        direct_attempts.append((prop, None, deal_type))
    direct_attempts.append((None, period, deal_type))
    direct_attempts.append((None, None, deal_type))
    if deal_type:
        direct_attempts.append((prop, period, None))
        direct_attempts.append((None, None, None))

    for p_try, per_try, dt_try in direct_attempts:
        try:
            r2 = get_latest_deals(scope, name, p_try, per_try, dt_try, limit=limit, unit_query=unit_query)
            if r2:
                _v106_log("latest_deals_direct_fallback", "v83_returned_empty",
                          scope=scope, name=str(name)[:120] if name else None,
                          orig=(prop, period, deal_type),
                          used=(p_try, per_try, dt_try))
                return r2, p_try, per_try, dt_try
        except Exception as e:
            _v106_log("get_latest_deals_smart_v106_direct", e,
                      scope=scope, name=str(name)[:120] if name else None,
                      attempt=(p_try, per_try, dt_try))
            continue

    return [], prop, period, deal_type


# ---- Bug 1c: soft-period fallback в _period_where_v67 ------------------------
# Оборачиваем get_stats / get_latest_deals чтобы при пустом результате с заданным
# периодом автоматически расширять до 12 мес / all-time прозрачно для caller'а.
# Это страховка на случай, когда smart-wrappers по какой-то причине обошли период.

try:
    _v106_original_get_stats_raw = get_stats
except NameError:
    _v106_original_get_stats_raw = None


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v110 READ-MODEL ONLY: get_stats теперь всегда идёт через read-model.
    Raw DLD больше НЕ опрашивается (вызывало зависания). Если данных в
    read-model нет — возвращаем None и пусть caller сам решит.
    """
    # v110: read-model fast-path
    try:
        # Используем уже определённый READ_MODEL глобал из v107
        if _READ_MODEL_OK and _read_model:
            months = _v107_period_to_months(period)
            scope_l = (scope or "").lower()
            if not name and scope_l in ("dubai", "city", "market", ""):
                mo = _read_model.try_market_overview(months)
                if mo and mo.get("deals"):
                    return {
                        "deals": int(mo["deals"]),
                        "avg": float(mo["avg_price"]) if mo.get("avg_price") else None,
                        "avg_price": float(mo["avg_price"]) if mo.get("avg_price") else None,
                        "median": float(mo["median_price"]) if mo.get("median_price") else None,
                        "median_price": float(mo["median_price"]) if mo.get("median_price") else None,
                        "top_quartile": float(mo["top_quartile_price"]) if mo.get("top_quartile_price") else None,
                        "top_quartile_price": float(mo["top_quartile_price"]) if mo.get("top_quartile_price") else None,
                        "yoy_growth_pct": float(mo["yoy_growth_pct"]) if mo.get("yoy_growth_pct") is not None else None,
                        "yoy_growth_top_pct": float(mo["yoy_growth_top_pct"]) if mo.get("yoy_growth_top_pct") is not None else None,
                        "sum": float(mo["total_volume"]) if mo.get("total_volume") else None,
                        "total_volume": float(mo["total_volume"]) if mo.get("total_volume") else None,
                        "_source": "read_model_market_overview",
                    }
                return None
            if name and isinstance(name, str):
                row = None
                kind = "area"
                if scope_l in ("area", "areas", "district"):
                    row = _read_model.try_area_stats(name, months, prop=prop, rooms=None, deal_type=deal_type)
                elif scope_l in ("building", "buildings", "project", "tower"):
                    row = _read_model.try_building_stats(name, months, rooms=None, deal_type=deal_type)
                    kind = "building"
                else:
                    row = _read_model.try_area_stats(name, months, prop=prop, rooms=None, deal_type=deal_type)
                    if not row:
                        row = _read_model.try_building_stats(name, months, rooms=None, deal_type=deal_type)
                        kind = "building"
                shaped = _v107_row_for_smart(row, kind)
                if shaped and shaped.get("deals", 0) > 0:
                    return shaped
    except Exception as e:
        try:
            _v106_log("get_stats_v110_read_model_err", e,
                      scope=scope, name=str(name)[:120] if name else None,
                      prop=prop, period=period, deal_type=deal_type)
        except Exception:
            pass
    # v110: НЕ откатываемся в raw — это причина зависаний. None означает «нет данных».
    return None


# ---- Bug 2: лечим потерю state у wizard "Сделки DLD" --------------------------

_V106_DEAL_TYPE_BUTTONS = {"🏠 Продажа", "🔑 Аренда", "📊 Всё", "⏭ Пропустить"}
_V106_PROPERTY_BUTTONS = {"Studio", "1 BR", "2 BR", "3 BR", "4 BR", "5 BR+",
                          "Apartment", "Villa", "Townhouse", "Penthouse",
                          "Office", "Shop"}
_V106_PERIOD_BUTTONS = {"3 месяца", "6 месяцев", "1 год", "3 года", "📅 Всё время"}


# Patch main_handler через middleware: вставляем pre-check перед общим dispatch.
# Невозможно без переписать main_handler, поэтому используем aiogram outer middleware.

try:
    from aiogram import BaseMiddleware as _BaseMiddleware_v106
    from aiogram.types import Message as _Message_v106

    class _V106WizardRecoveryMiddleware(_BaseMiddleware_v106):
        async def __call__(self, handler, event, data):
            try:
                if isinstance(event, _Message_v106):
                    text = (event.text or "").strip()
                    uid = event.from_user.id if event.from_user else None
                    if uid is not None and text in _V106_DEAL_TYPE_BUTTONS:
                        st = user_states.get(uid, {}) or {}
                        step = st.get("step")
                        # B058 FIX: в smart_goal/format_compare_goal "🔑 Аренда" это GOAL
                        # (rental yield), НЕ deal_type. Middleware ломал invest wizard,
                        # принудительно отправляя в "Последние сделки".
                        _allowed_deal_steps = (
                            "choose_deal_type", "best_object_deal_type",
                            "smart_goal", "smart_budget", "smart_timing", "smart_risk",
                            "format_compare_goal", "format_compare_scope",
                            "format_compare_area_query", "format_compare_choose_area",
                            "format_compare_budget", "format_compare_period",
                        )
                        # Если step не expects deal type выбор, но user всё равно
                        # тыкает в кнопку из этого набора — восстанавливаем wizard.
                        if step not in _allowed_deal_steps:
                            # v106.1: НЕ перезаписываем state целиком — это уничтожало
                            # report_kind / scope / name выбранные пользователем раньше.
                            # Только in-place обновляем step и доустанавливаем недостающие
                            # поля (scope/name/report_kind). Все прочие ключи сохраняются.
                            scope = st.get("scope") or "dubai"
                            name = st.get("name")
                            report_kind = st.get("report_kind") or "deals"
                            st["step"] = "choose_deal_type"
                            st.setdefault("scope", scope)
                            st.setdefault("name", name)
                            st.setdefault("report_kind", report_kind)
                            st.setdefault("history", st.get("history", []))
                            user_states[uid] = st
                            _v106_log("wizard_state_recovered", "deal_type_buttons_outside_wizard",
                                      uid=uid, text=text, prev_step=step,
                                      scope=scope, name=str(name)[:120] if name else None)
            except Exception as e:
                _v106_log("wizard_recovery_mw", e)
            return await handler(event, data)

    try:
        dp.message.outer_middleware(_V106WizardRecoveryMiddleware())
        print("Loaded v106 wizard recovery middleware")
    except Exception as e:
        _v106_log("wizard_recovery_register", e)
except Exception as e:
    _v106_log("wizard_recovery_import", e)


print("Loaded v106 user-reported bug fixes (analytics period + wizard recovery + address opera search)")


# ==================================================================
# v107: READ-MODEL FAST PATH
# ==================================================================
# Цель: ответы аналитики < 500 мс из готовых агрегатов area_stats /
# building_stats / market_overview, которые ежедневно перестраивает
# сервис dxb-stats-builder. При отсутствии данных в read-model — тихий
# fallback на v106 (raw DLD-таблицы), поведение пользователя не меняется.
# ==================================================================

try:
    import read_model as _read_model
    _READ_MODEL_OK = _read_model.is_available()
    print(f"Loaded v107 read-model fast path (available={_READ_MODEL_OK})")
except Exception as _e:  # noqa: BLE001
    _read_model = None
    _READ_MODEL_OK = False
    print(f"Loaded v107 read-model fast path (DISABLED: {_e})")


def _v107_period_to_months(period):
    """Конвертирует period-токен бота ('6m'/'1y'/'3y'/'all' и пр.) в число месяцев."""
    if period is None:
        return 36  # ≈ 3 года истории = весь bootstrap
    s = str(period).lower().strip()
    table = {
        "3m": 3, "3 m": 3, "3 months": 3, "3 месяца": 3,
        "6m": 6, "6 m": 6, "6 months": 6, "6 месяцев": 6,
        "1y": 12, "12m": 12, "1 year": 12, "1 год": 12,
        "2y": 24, "24m": 24, "2 years": 24, "2 года": 24,
        "3y": 36, "36m": 36, "3 years": 36, "3 года": 36,
        "all": 36, "all_time": 36, "all-time": 36, "📅 всё время": 36,
    }
    if s in table:
        return table[s]
    # Попробуем выдрать число + суффикс
    import re as _re
    m = _re.match(r"(\d+)\s*([myг])", s)
    if m:
        n = int(m.group(1))
        suf = m.group(2)
        if suf in ("m",):
            return min(36, max(1, n))
        if suf in ("y", "г"):
            return min(36, max(1, n * 12))
    return 12


def _v107_row_for_smart(row, area_or_building):
    """Конвертирует dict из read_model.try_area_stats / try_building_stats
    в формат, который ожидает caller get_stats_smart: dict с ключами
    deals, sum, avg, min, max, median, top_quartile, и т.п."""
    if not row:
        return None
    deals = int(row.get("deals") or 0)
    if deals <= 0:
        return None
    out = {
        "deals": deals,
        "avg": float(row["avg_price"]) if row.get("avg_price") is not None else None,
        "median": float(row["median_price"]) if row.get("median_price") is not None else None,
        "top_quartile": float(row["top_quartile_price"]) if row.get("top_quartile_price") is not None else None,
        "avg_psf": float(row["avg_price_psf"]) if row.get("avg_price_psf") is not None else None,
        "top_quartile_psf": float(row["top_quartile_psf"]) if row.get("top_quartile_psf") is not None else None,
        # обратно совместимые алиасы
        "avg_price": float(row["avg_price"]) if row.get("avg_price") is not None else None,
        "median_price": float(row["median_price"]) if row.get("median_price") is not None else None,
        "top_quartile_price": float(row["top_quartile_price"]) if row.get("top_quartile_price") is not None else None,
        # маркетинговая подача — "до X"
        "yoy_growth_pct": float(row["yoy_growth_pct"]) if row.get("yoy_growth_pct") is not None else None,
        "yoy_growth_top_pct": float(row["yoy_growth_top_pct"]) if row.get("yoy_growth_top_pct") is not None else None,
        "avg_rental_yield_pct": float(row["avg_rental_yield_pct"]) if row.get("avg_rental_yield_pct") is not None else None,
        "top_rental_yield_pct": float(row["top_rental_yield_pct"]) if row.get("top_rental_yield_pct") is not None else None,
        "_source": "read_model",
        "_area_or_building": area_or_building,
        "_name_display": row.get("area_name") or row.get("building_name"),
    }
    # sum / total_volume — для отображения объёма
    if row.get("avg_price") and deals:
        try:
            out["sum"] = float(row["avg_price"]) * deals
            out["total_volume"] = out["sum"]
        except Exception:
            pass
    return out


try:
    _v107_orig_get_stats_smart = get_stats_smart  # v106 wrapper
except NameError:
    _v107_orig_get_stats_smart = None


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v110 READ-MODEL ONLY: вся аналитика идёт только из агрегатов
    (area_stats / building_stats / market_overview). Raw DLD больше НЕ
    используется в этом пути — он зависает и таймаутит. Если данных
    в read-model нет — отдаём (None, ...), UI покажет "нет данных"
    или вышестоящий код возьмёт смежный fallback (market overview).
    """
    import time as __t
    __t0 = __t.perf_counter()
    # Read-model: scope=dubai → market_overview, иначе area/building
    if _READ_MODEL_OK and _read_model:
        try:
            months = _v107_period_to_months(period)
            scope_l = (scope or "").lower()
            row = None
            kind = "area"

            if not name and scope_l in ("dubai", "city", "market", ""):
                # Общий обзор Дубая — берём из market_overview
                mo = _read_model.try_market_overview(months)
                if mo and mo.get("deals"):
                    shaped = {
                        "deals": int(mo["deals"]),
                        "avg": float(mo["avg_price"]) if mo.get("avg_price") else None,
                        "avg_price": float(mo["avg_price"]) if mo.get("avg_price") else None,
                        "median": float(mo["median_price"]) if mo.get("median_price") else None,
                        "median_price": float(mo["median_price"]) if mo.get("median_price") else None,
                        "top_quartile": float(mo["top_quartile_price"]) if mo.get("top_quartile_price") else None,
                        "top_quartile_price": float(mo["top_quartile_price"]) if mo.get("top_quartile_price") else None,
                        "yoy_growth_pct": float(mo["yoy_growth_pct"]) if mo.get("yoy_growth_pct") is not None else None,
                        "yoy_growth_top_pct": float(mo["yoy_growth_top_pct"]) if mo.get("yoy_growth_top_pct") is not None else None,
                        "sum": float(mo["total_volume"]) if mo.get("total_volume") else None,
                        "total_volume": float(mo["total_volume"]) if mo.get("total_volume") else None,
                        "_source": "read_model_market_overview",
                    }
                    try:
                        _v106_log("read_model_hit", "market_overview", deals=shaped["deals"])
                    except Exception: pass
                    return shaped, prop, period, deal_type
                return None, prop, period, deal_type

            if name and isinstance(name, str):
                if scope_l in ("area", "areas", "district"):
                    row = _read_model.try_area_stats(name, months, prop=prop, rooms=None, deal_type=deal_type)
                    kind = "area"
                elif scope_l in ("building", "buildings", "project", "tower"):
                    row = _read_model.try_building_stats(name, months, rooms=None, deal_type=deal_type)
                    kind = "building"
                else:
                    # auto: сначала area, потом building
                    row = _read_model.try_area_stats(name, months, prop=prop, rooms=None, deal_type=deal_type)
                    kind = "area"
                    if not row:
                        row = _read_model.try_building_stats(name, months, rooms=None, deal_type=deal_type)
                        kind = "building"

                shaped = _v107_row_for_smart(row, kind)
                if shaped and shaped.get("deals", 0) > 0:
                    try:
                        _v106_log("read_model_hit", "served_from_aggregates",
                                  scope=scope, name=str(name)[:120],
                                  prop=prop, period=period, deal_type=deal_type,
                                  kind=kind, deals=shaped["deals"])
                    except Exception:
                        pass
                    return shaped, prop, period, deal_type
        except Exception as e:
            try:
                _v106_log("read_model_fastpath_err", e,
                          scope=scope, name=str(name)[:120] if name else None,
                          prop=prop, period=period, deal_type=deal_type)
            except Exception:
                pass

    # v110: НЕ откатываемся в raw DLD (это причина зависаний). Возвращаем None.
    try:
        _v106_log("read_model_miss_no_raw_fallback", "no_data",
                  scope=scope, name=str(name)[:120] if name else None,
                  prop=prop, period=period, deal_type=deal_type)
    except Exception:
        pass
    __dt = (__t.perf_counter() - __t0) * 1000
    if __dt > 500:
        print(f"[LAT_SLOW] get_stats_smart_miss: {__dt:.0f}ms scope={scope} name={str(name)[:60]!r}")
    return None, prop, period, deal_type


# ---- v107 marketing helpers (для UI/каротки) ---------------------
def v107_marketing_overview(scope="dubai", name=None, months=12, deal_type="sale"):
    """Готовая статистика для маркетингового overview-блока:
    {'top_yield','top_growth','top_price','source'}. None если read-model недоступен.
    """
    if not (_READ_MODEL_OK and _read_model):
        return None
    try:
        if name:
            row = _read_model.try_area_stats(name, months, deal_type=deal_type)
            if not row:
                row = _read_model.try_building_stats(name, months, deal_type=deal_type)
        else:
            row = _read_model.try_market_overview(months)
        if not row:
            return None
        return {
            "top_quartile_price": float(row["top_quartile_price"]) if row.get("top_quartile_price") else None,
            "top_quartile_psf": float(row["top_quartile_psf"]) if row.get("top_quartile_psf") else None,
            "top_growth_pct": float(row["yoy_growth_top_pct"]) if row.get("yoy_growth_top_pct") else None,
            "top_yield_pct": float(row["top_rental_yield_pct"]) if row.get("top_rental_yield_pct") else None,
            "deals": int(row.get("deals") or 0),
            "source": "read_model",
        }
    except Exception as e:
        try:
            _v106_log("v107_marketing_overview_err", e, scope=scope, name=str(name)[:120] if name else None)
        except Exception:
            pass
        return None


print("Loaded v107 read-model fast path wrappers")


# ============================================================
# v108 MARKETING REWRITE — sales-oriented copy, top-quartile cifry
# ----------------------------------------------------------------
# Идея: render-функции (show_stats, show_comparison, send_full_report,
# top buildings) переписываются в продающем стиле «до X / достигает X»
# с верхней планкой (top_quartile, yoy_growth_top, top_yield).
#
# Технические правила:
#  - старые функции (_v108_orig_*) сохраняются как fallback;
#  - если top_quartile_price/psf отсутствуют в dict — fallback на
#    max_price / avg_meter с пометкой источника данных;
#  - подписочный CTA «/subscribe» добавляется в подвал каждого блока.
# ============================================================

# ----- helpers ----------------------------------------------------

def _v108_num(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _v108_top_price(row):
    """Верхняя планка цены: top_quartile_price → max_price → avg_price."""
    if not row:
        return None, "нет данных"
    tq = _v108_num(row.get("top_quartile_price")) or _v108_num(row.get("top_quartile"))
    if tq:
        return tq, "топ-25% сделок DLD"
    mx = _v108_num(row.get("max_price"))
    if mx:
        return mx, "макс. цена по DLD"
    av = _v108_num(row.get("avg_price")) or _v108_num(row.get("avg"))
    if av:
        return av, "средняя по DLD"
    return None, "нет данных"


def _v108_top_psf(row):
    """Верхняя планка цены за sqft."""
    if not row:
        return None, "нет данных"
    tq = _v108_num(row.get("top_quartile_psf"))
    if tq:
        return tq, "топ-25% по sqft"
    av = _v108_num(row.get("avg_psf")) or _v108_num(row.get("avg_meter"))
    if av:
        return av, "средняя по sqft"
    return None, "нет данных"


def _v108_top_growth(row):
    """Top decile YoY роста."""
    if not row:
        return None, None
    tg = _v108_num(row.get("yoy_growth_top_pct")) or _v108_num(row.get("top_growth_pct"))
    if tg is not None:
        return tg, "топ-10% роста"
    g = _v108_num(row.get("yoy_growth_pct"))
    if g is not None:
        return g, "средний рост"
    return None, None


def _v108_top_yield(row):
    """Top decile доходности аренды."""
    if not row:
        return None, None
    ty = _v108_num(row.get("top_rental_yield_pct")) or _v108_num(row.get("top_yield_pct"))
    if ty is not None:
        return ty, "топ-10% доходности"
    y = _v108_num(row.get("avg_rental_yield_pct"))
    if y is not None:
        return y, "средняя доходность"
    return None, None


def _v108_subscribe_cta(lang="ru"):
    if lang == "en":
        return "\n💡 <i>Get daily market insights → /subscribe</i>"
    if lang == "ar":
        return "\n💡 <i>احصل على رؤى يومية للسوق → /subscribe</i>"
    return "\n💡 <i>Хотите получать такие данные ежедневно? → /subscribe</i>"


def _v108_smart_pick_cta(lang="ru"):
    if lang == "en":
        return "🎯 <b>Pick under your budget</b> → /smart_pick"
    if lang == "ar":
        return "🎯 <b>اختر حسب ميزانيتك</b> → /smart_pick"
    return "🎯 <b>Подобрать объект под бюджет</b> → /smart_pick"


# ----- show_stats v108 --------------------------------------------

try:
    _v108_orig_show_stats = show_stats  # сохраняем оригинал v106/v107
except NameError:
    _v108_orig_show_stats = None


def show_stats(title, row, prop=None, period=None, deal_type=None):  # noqa: F811
    """v108 marketing rewrite — sales-oriented copy с топ-цифрами.

    Fallback: если ключевых полей нет (top_quartile_*) — отдаём
    управление оригинальной show_stats, чтобы не потерять никакой
    сценарий.
    """
    if not row or not row.get("deals"):
        return "❌ Нет данных по выбранным фильтрам."

    # Если top_quartile_* полностью отсутствует и нет даже max_price —
    # отдаём управление старой версии (полный fallback на её формат).
    tq, _ = _v108_top_price(row)
    if tq is None and _v108_orig_show_stats is not None:
        try:
            return _v108_orig_show_stats(title, row, prop, period, deal_type)
        except Exception:
            pass

    try:
        rent = is_rent_deal(deal_type)
    except Exception:
        rent = False

    deals = int(row.get("deals") or 0)
    top_price, top_price_src = _v108_top_price(row)
    top_psf, top_psf_src = _v108_top_psf(row)
    growth, growth_src = _v108_top_growth(row)
    yld, yld_src = _v108_top_yield(row)

    # Hook — сильная цифра первой фразой
    if rent:
        hook = (
            f"🔥 <b>Премиальная аренда — до {format_money(top_price)} в год</b>\n"
            f"<i>({top_price_src})</i>\n\n"
        )
    else:
        hook = (
            f"🔥 <b>Топ-цены достигают {format_money(top_price)}</b>\n"
            f"<i>({top_price_src})</i>\n\n"
        )

    # Контекст — район/период/источник
    period_txt = period_label(period)
    prop_txt = prop or "все типы"
    context = (
        f"📍 <b>{title}</b>\n"
        f"📅 Период: {period_txt} · 🏠 {prop_txt}\n"
        f"📊 Источник: DLD ({format_int(deals)} сделок)\n\n"
    )

    # Доказательство — 2–3 числа максимум
    proof_lines = []
    if top_psf:
        psf_label = "Аренда" if rent else "Цена"
        proof_lines.append(f"💰 {psf_label} до <b>{format_money(top_psf)}/sqft</b> ({top_psf_src})")
    if growth is not None and not rent:
        sign = "+" if growth > 0 else ""
        proof_lines.append(f"📈 Рост до <b>{sign}{growth:.1f}% YoY</b> ({growth_src})")
    if yld is not None and rent:
        proof_lines.append(f"💎 Доходность до <b>{yld:.1f}% годовых</b> ({yld_src})")

    # Если аренда — добавим объём ликвидности
    proof_lines.append(f"⚡ <b>{format_int(deals)}</b> сделок за период — высокая ликвидность")
    proof = "\n".join(proof_lines) + "\n\n"

    # CTA
    cta_block = _v108_smart_pick_cta("ru") + "\n"
    cta_block += _v108_subscribe_cta("ru")

    text = hook + context + proof + cta_block
    return text


# ----- show_comparison v108 --------------------------------------

try:
    _v108_orig_show_comparison = show_comparison
except NameError:
    _v108_orig_show_comparison = None


def show_comparison(title, current, previous, period=None, deal_type=None):  # noqa: F811
    """v108 marketing comparison — акцент на топ-рост."""
    if not current or not previous or not current.get("deals") or not previous.get("deals"):
        return "❌ Недостаточно данных для сравнения."

    cur_top, cur_src = _v108_top_price(current)
    prev_top, _prev_src = _v108_top_price(previous)
    if cur_top is None and _v108_orig_show_comparison is not None:
        try:
            return _v108_orig_show_comparison(title, current, previous, period, deal_type)
        except Exception:
            pass

    try:
        rent = is_rent_deal(deal_type)
    except Exception:
        rent = False

    # Top-quartile dynamic — топовый сегмент рынка вырос на сколько
    top_change = None
    if cur_top and prev_top:
        try:
            top_change = (cur_top - prev_top) / prev_top * 100.0
        except Exception:
            top_change = None

    deals_change = pct_change(current.get("deals"), previous.get("deals"))
    growth, growth_src = _v108_top_growth(current)

    # Hook
    if top_change is not None and top_change > 5:
        hook = (
            f"🚀 <b>Топ-сегмент рынка вырос на {format_pct(top_change)}</b>\n"
            f"<i>({cur_src})</i>\n\n"
        )
    elif top_change is not None and top_change < -5:
        hook = (
            f"📉 <b>Топ-сегмент скорректировался на {format_pct(top_change)}</b>\n"
            f"<i>момент для входа ниже рынка</i>\n\n"
        )
    elif growth is not None:
        sign = "+" if growth > 0 else ""
        hook = (
            f"📈 <b>Рост до {sign}{growth:.1f}% YoY ({growth_src})</b>\n\n"
        )
    else:
        hook = "📊 <b>Сравнение периодов</b>\n\n"

    context = (
        f"📍 <b>{title}</b>\n"
        f"📅 Текущий период: {period_label(period)} vs аналогичный предыдущий\n"
        f"📊 Источник: DLD\n\n"
    )

    proof_lines = [
        f"💎 Топ-цена сейчас: <b>{format_money(cur_top)}</b>",
        f"💎 Топ-цена ранее: <b>{format_money(prev_top)}</b>",
        f"⚡ Активность: <b>{format_pct(deals_change)}</b> сделок",
    ]
    proof = "\n".join(proof_lines) + "\n\n"

    if rent:
        verdict = "Сильный рынок аренды — момент для апсейла действующих контрактов."
    elif top_change is not None and top_change > 5:
        verdict = "Окно входа сужается — премиальные объекты дорожают быстрее среднего."
    elif top_change is not None and top_change < -5:
        verdict = "Появилось окно для входа ниже исторических максимумов."
    else:
        verdict = "Рынок стабилен — решение по конкретному юниту важнее тайминга."

    cta = (
        f"🧠 <b>Вывод:</b> {verdict}\n\n"
        f"{_v108_smart_pick_cta('ru')}\n"
        f"{_v108_subscribe_cta('ru')}"
    )

    return hook + context + proof + cta


# ----- v108 helper: top buildings card --------------------------

def v108_format_top_building(name, area, row):
    """Карточка одного здания в продающем стиле.

    Пример вывода:
      🏢 HQ by ROVE — Business Bay
      Премиальные сделки от AED 2.8M (топ-quartile)
      Активность: 12 сделок за период
      Тренд: цены растут до +12.1% YoY
    """
    if not row:
        return f"🏢 <b>{name}</b> — {area or ''}\n<i>нет данных DLD</i>\n"
    deals = int(row.get("deals") or 0)
    top_price, src = _v108_top_price(row)
    growth, _ = _v108_top_growth(row)
    lines = [f"🏢 <b>{name}</b> — {area or ''}"]
    if top_price:
        lines.append(f"   💎 Топ-сделки от <b>{format_money(top_price)}</b> ({src})")
    if deals:
        lines.append(f"   ⚡ Активность: <b>{format_int(deals)}</b> сделок за период")
    if growth is not None and growth != 0:
        sign = "+" if growth > 0 else ""
        lines.append(f"   📈 Тренд: до <b>{sign}{growth:.1f}% YoY</b>")
    return "\n".join(lines) + "\n"


print("Loaded v108 marketing rewrite (top-quartile cifry + sales copy)")


# ==================================================================
# v109 SMART-INVEST READ-MODEL FAST PATH
# ------------------------------------------------------------------
# Проблема v108: smart_pick_candidates вызывал get_stats_smart(name=display_area)
# где display_area = 'JVC' / 'Downtown Dubai' / 'Sobha Hartland' / 'JLT'.
# Эти имена НЕ совпадают с area_key в read-model area_stats (там DLD-имена:
# 'al barsha south fourth', 'burj khalifa', 'jumeirah lakes towers' и т.п.).
# Read-model промахивался для 4 из 8 areas, бот падал в raw-fallback,
# raw-fallback таймаутил, юзер получал static "DLD-архив медленно" сообщение.
#
# Фикс: smart_pick_candidates тянет данные напрямую из read_model по real_areas
# (DLD-именам), агрегирует по display_area и формирует кандидата за < 3 сек.
# ==================================================================

_V109_AREA_REAL_MAP = {
    "JVC": ["Al Barsha South Fourth", "Al Barsha South Fifth", "Al Hebiah First"],
    "Dubai Marina": ["Marsa Dubai"],
    "Business Bay": ["Business Bay"],
    "Downtown Dubai": ["Burj Khalifa"],
    "Palm Jumeirah": ["Palm Jumeirah"],
    "JLT": ["Jumeirah Lakes Towers"],
    "Sobha Hartland": ["Nadd Hessa"],  # ближайшая по DLD-зоне
    "Dubai Sports City": ["Al Hebiah Fourth"],
    "Arjan": ["Al Barsha South Second"],
    "Damac Hills": ["Al Hebiah Third"],
    "Dubai Hills": ["Hadaeq Sheikh Mohammed Bin Rashid"],
    "MBR City": ["Hadaeq Sheikh Mohammed Bin Rashid"],
    "Jumeirah Village Triangle": ["Al Barsha South Third"],
    "Discovery Gardens": ["Jabal Ali First"],
    "Dubai Investment Park": ["Dubai Investment Park First"],
    "Dubai South": ["Madinat Al Mataar"],
    "DIFC": ["Zaabeel Second", "Burj Khalifa"],
    "Mirdif": ["Mirdif"],
    "Dubai Production City": ["Me'Aisem First"],
}


def _v109_area_universe_safe(goal):
    """Безопасная вселенная: только display_area, у которых есть DLD-данные.

    FIX (B055, 2026-06-03): использовать canonical area_keys MV
    (mv_area_*_summary), а НЕ raw DLD sector area_names. Прошлая версия
    маппила «JVC» → 3 sector_names (Al Barsha South Fourth, Al Hebiah
    First, Al Barsha South Fifth) которые в DLD охватывают огромную
    территорию (JVC + Sports City + Production City + Motor City + JVT),
    давая 109K «сделок JVC за 12mo» вместо реальных ~20K. Те же
    sector_names ломали все смежные карточки. Canonical area_keys в MV
    уже корректно агрегированы парсером по реальному JVC/Palm/Business Bay.
    """
    g = str(goal or "")
    if "жизн" in g.lower() or "Для жизни" in g:
        return [
            ("Downtown Dubai", ["burj khalifa"]),
            ("Dubai Marina", ["dubai marina"]),
            ("JVC", ["jvc"]),
            ("Business Bay", ["business bay"]),
            ("Palm Jumeirah", ["palm jumeirah"]),
        ]
    if "Аренд" in g or "аренд" in g.lower():
        return [
            ("Dubai Marina", ["dubai marina"]),
            ("JVC", ["jvc"]),
            ("Business Bay", ["business bay"]),
            ("Downtown Dubai", ["burj khalifa"]),
            ("JLT", ["jumeirah lakes towers"]),
        ]
    # Инвестиция / ROI / Перепродажа
    return [
        ("JVC", ["jvc"]),
        ("Business Bay", ["business bay"]),
        ("Dubai Marina", ["dubai marina"]),
        ("Downtown Dubai", ["burj khalifa"]),
        ("Dubai Sports City", ["dubai sports city"]),
        ("JLT", ["jumeirah lakes towers"]),
        ("Dubai Hills", ["dubai hills"]),
        ("Palm Jumeirah", ["palm jumeirah"]),
    ]


def _v109_rooms_filter_for_budget(bmax):
    """B055: вернуть (property_type, rooms_list) для совпадения с prop_label.
    None → не фильтровать (берём agg-row property_type='all' rooms='all').
    """
    try:
        bmax = float(bmax or 0)
    except Exception:
        bmax = 0
    if bmax <= 0 or bmax > 5_000_000:
        return (None, None)  # all/all
    if bmax <= 1_000_000:
        return ("apartment", ["studio"])
    if bmax <= 2_000_000:
        return ("apartment", ["studio", "1br"])
    if bmax <= 3_000_000:
        return ("apartment", ["1br", "2br"])
    # bmax <= 5_000_000
    return ("apartment", ["2br", "3br"])


def _v109_fetch_area_aggregate(real_areas, months=24, deal_type="sale"):
    """Один SQL по N real_areas → агрегат для display_area.
    Возвращает dict с deals/avg_price/avg_meter/yoy/top_yield или None."""
    if not (_READ_MODEL_OK and _read_model):
        return None
    try:
        keys = [str(a).strip().lower() for a in (real_areas or []) if a]
        if not keys:
            return None
        start = _read_model._period_start(months)
        sql = """
            SELECT
                SUM(deals_count)::int                                          AS deals,
                (SUM(avg_price_aed * deals_count) / NULLIF(SUM(deals_count),0)) AS avg_price,
                AVG(avg_price_psf)                                              AS avg_meter,
                MAX(top_quartile_price_aed)                                     AS top_quartile_price,
                MAX(top_quartile_psf)                                           AS top_quartile_psf,
                AVG(yoy_growth_pct)                                             AS yoy_growth_pct,
                MAX(yoy_growth_top_pct)                                         AS yoy_growth_top_pct,
                AVG(avg_rental_yield_pct)                                       AS avg_rental_yield_pct,
                MAX(top_rental_yield_pct)                                       AS top_rental_yield_pct
            FROM area_stats
            WHERE area_key = ANY(%s)
              AND property_type = 'all'
              AND rooms = 'all'
              AND deal_type = %s
              AND period_month >= %s
        """
        import psycopg2.extras as _pe
        with _read_model._conn().cursor(cursor_factory=_pe.RealDictCursor) as cur:
            cur.execute(sql, (keys, deal_type, start))
            row = cur.fetchone()
        if not row or not row.get("deals"):
            return None
        return dict(row)
    except Exception as _e:
        print("V109_FETCH_AGG_ERROR:", repr(_e), "keys=", real_areas)
        return None


# v111 BATCH FAST PATH: один SQL по N display_area сразу.
def _v111_batch_area_aggregates(area_universe, months=24, deal_type="sale",
                                prop_type=None, rooms_filter=None):
    """Один SQL по списку (display_area, [real_areas]) → dict display_area→agg.
    Заменяет цикл из N round-trip на 1 round-trip.

    B055 (2026-06-03): добавлены параметры prop_type и rooms_filter, чтобы
    avg_price/yield соответствовали prop_label по бюджету. Когда они
    заданы — берём rows по соответствующим (property_type, rooms IN ...)
    и weighted-average по deals. Иначе — старое поведение (all/all).
    """
    import time as _t
    if not (_READ_MODEL_OK and _read_model):
        return {}
    if not area_universe:
        return {}
    t0 = _t.perf_counter()
    # Собираем плоский список (area_key → display_area)
    key_to_display = {}
    all_keys = []
    for display, reals in area_universe:
        for r in (reals or []):
            k = str(r).strip().lower()
            if not k:
                continue
            key_to_display[k] = display
            all_keys.append(k)
    if not all_keys:
        return {}
    # FIX (DEALS_12M): months=12 should read mv_area_12m_summary, NOT 24m.
    # Old code hard-coded mv_area_24m_summary and ignored months parameter,
    # causing JVC to show 192K deals "за год" (actually 24mo sum).
    _mv_name = "mv_area_12m_summary" if int(months or 12) <= 12 else "mv_area_24m_summary"
    use_room_filter = bool(prop_type) and bool(rooms_filter)
    if use_room_filter:
        sql = f"""
            SELECT area_key,
                   deals,
                   avg_price,
                   avg_price_psf AS avg_meter,
                   top_quartile_price,
                   top_quartile_psf,
                   yoy_growth_pct,
                   yoy_growth_top_pct,
                   avg_rental_yield_pct,
                   top_rental_yield_pct
            FROM {_mv_name}
            WHERE area_key = ANY(%s)
              AND property_type=%s AND rooms = ANY(%s) AND deal_type=%s
        """
        sql_params = (all_keys, prop_type, list(rooms_filter), deal_type)
    else:
        sql = f"""
            SELECT area_key,
                   deals,
                   avg_price,
                   avg_price_psf AS avg_meter,
                   top_quartile_price,
                   top_quartile_psf,
                   yoy_growth_pct,
                   yoy_growth_top_pct,
                   avg_rental_yield_pct,
                   top_rental_yield_pct
            FROM {_mv_name}
            WHERE area_key = ANY(%s)
              AND property_type='all' AND rooms='all' AND deal_type=%s
        """
        sql_params = (all_keys, deal_type)
    try:
        import psycopg2.extras as _pe
        with _read_model._conn().cursor(cursor_factory=_pe.RealDictCursor) as cur:
            cur.execute(sql, sql_params)
            rows = cur.fetchall()
    except Exception as _e:
        print("V111_BATCH_AGG_ERROR:", repr(_e))
        return {}
    # Aggregate per display_area (sum over real_areas)
    out = {}
    for r in rows:
        dk = r.get("area_key")
        disp = key_to_display.get(dk)
        if not disp:
            continue
        cur = out.setdefault(disp, {
            "deals": 0, "_sum_price_x_deals": 0.0, "_psf_sum": 0.0, "_psf_n": 0,
            "top_quartile_price": 0.0, "top_quartile_psf": 0.0,
            "_yoy_sum": 0.0, "_yoy_n": 0, "yoy_growth_top_pct": 0.0,
            "_yld_sum": 0.0, "_yld_n": 0, "top_rental_yield_pct": 0.0,
        })
        d = int(r.get("deals") or 0)
        cur["deals"] += d
        if r.get("avg_price"):
            cur["_sum_price_x_deals"] += float(r["avg_price"]) * d
        if r.get("avg_meter"):
            cur["_psf_sum"] += float(r["avg_meter"]); cur["_psf_n"] += 1
        if r.get("top_quartile_price") and float(r["top_quartile_price"]) > cur["top_quartile_price"]:
            cur["top_quartile_price"] = float(r["top_quartile_price"])
        if r.get("top_quartile_psf") and float(r["top_quartile_psf"]) > cur["top_quartile_psf"]:
            cur["top_quartile_psf"] = float(r["top_quartile_psf"])
        if r.get("yoy_growth_pct") is not None:
            cur["_yoy_sum"] += float(r["yoy_growth_pct"]); cur["_yoy_n"] += 1
        if r.get("yoy_growth_top_pct") and float(r["yoy_growth_top_pct"]) > cur["yoy_growth_top_pct"]:
            cur["yoy_growth_top_pct"] = float(r["yoy_growth_top_pct"])
        if r.get("avg_rental_yield_pct") is not None:
            cur["_yld_sum"] += float(r["avg_rental_yield_pct"]); cur["_yld_n"] += 1
        if r.get("top_rental_yield_pct") and float(r["top_rental_yield_pct"]) > cur["top_rental_yield_pct"]:
            cur["top_rental_yield_pct"] = float(r["top_rental_yield_pct"])
    # finalize
    final = {}
    for disp, c in out.items():
        if c["deals"] <= 0:
            continue
        final[disp] = {
            "deals": c["deals"],
            "avg_price": (c["_sum_price_x_deals"] / c["deals"]) if c["deals"] else 0,
            "avg_meter": (c["_psf_sum"] / c["_psf_n"]) if c["_psf_n"] else 0,
            "top_quartile_price": c["top_quartile_price"] or None,
            "top_quartile_psf": c["top_quartile_psf"] or None,
            "yoy_growth_pct": (c["_yoy_sum"] / c["_yoy_n"]) if c["_yoy_n"] else 0,
            "yoy_growth_top_pct": c["yoy_growth_top_pct"] or 0,
            "avg_rental_yield_pct": (c["_yld_sum"] / c["_yld_n"]) if c["_yld_n"] else 0,
            "top_rental_yield_pct": c["top_rental_yield_pct"] or 0,
        }
    dt_ms = (_t.perf_counter() - t0) * 1000
    print(f"[LAT] V111_BATCH_AGG: {dt_ms:.0f}ms areas={len(area_universe)} hit={len(final)} rows={len(rows)}")
    if dt_ms > 500:
        print(f"[LAT_SLOW] V111_BATCH_AGG: {dt_ms:.0f}ms exceeds 500ms budget")
    return final


def smart_pick_candidates(goal, budget_text, risk, timing):  # noqa: F811
    """v109: быстрый read-model путь по display_area → real_areas.
    Гарантия < 5 сек на запрос (~8 SQL × ~300мс = 2.5с).
    """
    import time as _time
    t0 = _time.time()
    try:
        bmin, bmax = parse_budget_range(budget_text)
    except Exception:
        bmin, bmax = (0, 0)

    # B059 FIX: goal=Аренда означает "купить под сдачу", а не "арендовать".
    # Юзеру нужна цена ПОКУПКИ + ожидаемая аренда + yield. Раньше при rent goal
    # avg_price был annual rent (~264K) что бессмысленно. Теперь всегда sale.
    deal_type = "sale"
    _is_rent_goal = bool(goal and ("Аренд" in goal or "аренд" in str(goal).lower()))
    areas = _v109_area_universe_safe(goal)

    risk_text = str(risk or "").lower()
    goal_text = str(goal or "").lower()

    # v111: batch fetch — 1 SQL вместо N round-trip
    # FIX (SMART_PICK_HUMAN): было months=24 -> ~192K сделок для JVC (нереалистично много).
    # 12 месяцев = honest year. Также batch_agg возвращает avg_meter в AED/sqft
    # (колонка mv.avg_price_psf), нужно конвертировать -> AED/m^2.
    # B055: применяем room-filter по бюджету, чтобы avg_price/yield
    # соответствовали prop_label (например Studio/1BR → apartment+studio+1br).
    _b055_pt, _b055_rooms = _v109_rooms_filter_for_budget(bmax)
    batch_agg = _v111_batch_area_aggregates(
        areas, months=12, deal_type=deal_type,
        prop_type=_b055_pt, rooms_filter=_b055_rooms,
    )
    try:
        for _disp, _row in (batch_agg or {}).items():
            if _row and _row.get("avg_meter"):
                _row["avg_meter"] = float(_row["avg_meter"]) * SQFT_TO_M2
            if _row and _row.get("top_quartile_psf"):
                _row["top_quartile_psf"] = float(_row["top_quartile_psf"]) * SQFT_TO_M2
    except Exception:
        pass

    results = []
    for display_area, real_areas in areas:
        row = batch_agg.get(display_area)
        if not row:
            # graceful fallback на старый путь, если batch промахнулся (12mo per SMART_PICK_HUMAN)
            row = _v109_fetch_area_aggregate(real_areas, months=12, deal_type=deal_type)
            if row and row.get("avg_meter"):
                try:
                    row["avg_meter"] = float(row["avg_meter"]) * SQFT_TO_M2
                except Exception:
                    pass
        if not row or not row.get("deals"):
            continue

        deals = int(row.get("deals") or 0)
        avg_price = float(row.get("avg_price") or 0)
        avg_meter = float(row.get("avg_meter") or 0)
        top_quartile_price = float(row.get("top_quartile_price") or 0) or None
        yoy = float(row.get("yoy_growth_pct") or 0)
        yoy_top = float(row.get("yoy_growth_top_pct") or 0)
        yield_avg = float(row.get("avg_rental_yield_pct") or 0)
        yield_top = float(row.get("top_rental_yield_pct") or 0)

        # бюджет-affordability: насколько средняя цена близка к бюджету
        budget_mid = ((bmin or 0) + (bmax or avg_price or 0)) / 2 if (bmin or bmax) else avg_price
        if budget_mid and avg_price:
            affordability = 100 - min(100, abs(avg_price - budget_mid) / max(budget_mid, 1) * 100)
        else:
            affordability = 45
        liquidity = min(100, deals / 5000 * 100)  # 5000 deals = 100% liquidity (big areas like business bay)

        score = liquidity * 0.45 + affordability * 0.35
        if yield_top:
            score += min(20, yield_top * 1.5)
        if yoy_top:
            score += min(15, max(0, yoy_top * 0.5))

        if "низ" in risk_text and deals >= 5000:
            score += 14
        elif "сбал" in risk_text:
            score += 10
        elif "агр" in risk_text:
            score += 8

        # B059 FIX: для rental-goal ВЕС yield доминирующий. Раньше +12 (если ≥7%)
        # был слишком слаб — Downtown с большим liquidity побеждал JVC с реально
        # высоким yield. Теперь yield_avg * 5 (clamp 0-40) делает yield главным.
        if _is_rent_goal:
            yield_for_score = yield_avg if (yield_avg and 3 <= yield_avg <= 12) else min(yield_top or 0, 10)
            score += min(40, yield_for_score * 5)
        elif any(x in goal_text for x in ["roi", "инвест"]):
            if yield_top and yield_top >= 7:
                score += 12
        if "жизн" in goal_text and avg_price and bmax and abs(avg_price - bmax) / max(bmax, 1) < 0.4:
            score += 10
        if "перепрод" in goal_text and yoy_top and yoy_top > 0:
            score += 10

        # Выбор формата по бюджету
        if bmax <= 1_000_000:
            prop_label = "Studio"
        elif bmax <= 2_000_000:
            prop_label = "Studio / 1 BR"
        elif bmax <= 3_000_000:
            prop_label = "1 BR / 2 BR"
        elif bmax <= 5_000_000:
            prop_label = "2 BR / 3 BR"
        else:
            prop_label = "3 BR / Penthouse / Villa"

        results.append({
            "area": display_area,
            "property": prop_label,
            "deals": deals,
            "buildings": 0,
            "avg_price": avg_price,
            "min_price": avg_price * 0.85,
            "max_price": top_quartile_price or avg_price * 1.2,
            "avg_meter": avg_meter,
            "score": score,
            "yoy_growth_pct": yoy,
            "yoy_growth_top_pct": yoy_top,
            "avg_rental_yield_pct": yield_avg,
            "top_rental_yield_pct": yield_top,
            "top_quartile_price": top_quartile_price,
            "_source": "v109_read_model",
        })

    dt_ms = (_time.time() - t0) * 1000
    try:
        _safe_goal = str(goal).encode("ascii", "replace").decode("ascii") if goal else ""
        _safe_budget = str(budget_text).encode("ascii", "replace").decode("ascii") if budget_text else ""
        _safe_risk = str(risk).encode("ascii", "replace").decode("ascii") if risk else ""
        print(f"V109_SMART_PICK: areas_with_data={len(results)} dt={dt_ms:.0f}ms goal={_safe_goal!r} budget={_safe_budget!r} risk={_safe_risk!r}")
    except Exception:
        print(f"V109_SMART_PICK: areas_with_data={len(results)} dt={dt_ms:.0f}ms")

    if not results:
        return smart_fallback_candidates(goal, budget_text, risk, timing)

    return sorted(results, key=lambda x: (x.get("score") or 0, x.get("deals") or 0), reverse=True)[:5]


print("Loaded v109 smart-invest read-model fast path (real area_key mapping)")


# =========================================================================
# v110 MASTER-ZONE CANONICAL SEARCH (override v66 raw-DLD find_areas)
# =========================================================================
# Fixes: поиск «JVC» возвращал 5 DLD sub-areas (Al Barsha South Fourth,
# Al Hebiah First, …) вместо ОДНОЙ сгруппированной master-zone.
# Правило: боты ОБЯЗАНЫ ходить в master_project_zones / area_stats,
# а не в raw dld_transactions_full для поиска района.
try:
    from area_search import search_area_canonical as _v110_search_canonical

    def _v110_find_areas(query, limit=10):
        rows = _v110_search_canonical(query, limit=limit) or []
        out = []
        for r in rows:
            out.append({
                "area_name_en": r.get("display_en") or r.get("name"),
                "name":         r.get("display_en") or r.get("name"),
                "master_zone":  r.get("master_zone"),
                "area_key":     r.get("area_key"),
                "display_ru":   r.get("display_ru"),
                "display_en":   r.get("display_en"),
                "kind":         r.get("kind"),
                "deals":        r.get("deals_12m") or 0,
                "deals_12m":    r.get("deals_12m") or 0,
                "yield_pct":    r.get("yield_avg"),
                "buildings":    0,  # для совместимости со старым форматом
                "hint":         r.get("hint"),
                "_source":      "v110_master_zone",
            })
        return out

    find_areas = _v110_find_areas  # type: ignore
    print("Loaded v110 master-zone canonical search (overrides v66 find_areas)")
except Exception as _v110_err:
    print(f"V110 master-zone search NOT loaded: {_v110_err!r}")


# =========================================================================
# v140 GRANDE/BURJ KHALIFA REGRESSION FIX (2026-05-30)
# =========================================================================
# Root cause (analytics-bot regression after task #76):
#   `_v67_table_plan` reads sales from `public.dld_transactions_full` (LIVE
#   view) and `public.dld_sale_archive` (ARCHIVE table). `_date_expr_v67`
#   only recognised ISO-formatted dates `YYYY-MM-DD`. If `instance_date`
#   in either source ships in DLD legacy format `DD-MM-YYYY`, every
#   `safe_date` is NULL and the `>= CURRENT_DATE - INTERVAL '12 months'`
#   filter eliminates every row — including 3 BR Grande/Burj Khalifa
#   deals that DO exist in the archive.
#
# Fix applied above (`_date_expr_v67`): accept both formats.
#
# This block adds:
#   1. /diag_grande   — admin-only diagnostic running the same checks the
#                       offline script would.
#   2. boot-time VIEW recreate fallback — tries to (re-)create
#      `dld_transactions_full` in LIVE DB if it disappeared (regression
#      vector reported by user).
# =========================================================================
_V140_ADMIN_IDS = {353806371}  # Вадим


def _v140_recreate_dld_view():
    """Ensure `public.dld_transactions_full` exists in LIVE. Best-effort,
    never blocks startup. Mirrors the safe-date contract task #76 added."""
    try:
        old = globals().get("_ACTIVE_SOURCE", "live")
        try:
            _set_data_source("live")
            with db() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.dld_transactions_full')")
                    r = cur.fetchone()
                    val = (r.get("to_regclass") if isinstance(r, dict) else (r[0] if r else None))
                    if val:
                        print("[v140] dld_transactions_full present:", val)
                        return
                    # Try to find a sales-like table to back it with.
                    cur.execute("""
                        SELECT table_name FROM information_schema.tables
                        WHERE table_schema='public'
                          AND table_name IN ('dld_sales_unified','dld_transactions','dld_sales','dld_sales_raw')
                        LIMIT 1
                    """)
                    src = cur.fetchone()
                    src_name = (src.get("table_name") if isinstance(src, dict) else (src[0] if src else None))
                    if not src_name:
                        print("[v140] no source table found to back dld_transactions_full")
                        return
                    cur.execute(f"CREATE OR REPLACE VIEW public.dld_transactions_full AS SELECT * FROM public.{src_name}")
                    print(f"[v140] recreated public.dld_transactions_full -> public.{src_name}")
        finally:
            try:
                _set_data_source(old)
            except Exception:
                pass
    except Exception as _e:
        print(f"[v140] dld_transactions_full ensure failed: {_e!r}")


try:
    _v140_recreate_dld_view()
except Exception as _v140_boot_err:
    print(f"[v140] boot ensure failed: {_v140_boot_err!r}")


@dp.message(Command("diag_grande"))
async def _v140_diag_grande(message):
    uid = message.from_user.id
    if uid not in _V140_ADMIN_IDS:
        return
    lines = [f"<b>DIAG Grande/Burj Khalifa</b> uid={uid}"]
    checks = [
        ("ARCHIVE dld_sale_archive total Grande",
         "archive",
         "SELECT COUNT(*) AS n FROM public.dld_sale_archive WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%'"),
        ("ARCHIVE Grande+Burj total",
         "archive",
         "SELECT COUNT(*) AS n FROM public.dld_sale_archive WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%' AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%'"),
        ("ARCHIVE Grande+Burj 12m (safe_date)",
         "archive",
         """SELECT COUNT(*) AS n FROM public.dld_sale_archive
             WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%'
               AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%'
               AND (CASE
                      WHEN instance_date::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN (instance_date::text)::date
                      WHEN instance_date::text ~ '^[0-9]{2}-[0-9]{2}-[0-9]{4}' THEN to_date(instance_date::text,'DD-MM-YYYY')
                      WHEN instance_date::text ~ '^[0-9]{2}/[0-9]{2}/[0-9]{4}' THEN to_date(instance_date::text,'DD/MM/YYYY')
                      ELSE NULL END) >= CURRENT_DATE - INTERVAL '12 months'"""),
        ("ARCHIVE Grande+Burj 12m 3BR",
         "archive",
         """SELECT COUNT(*) AS n FROM public.dld_sale_archive
             WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%'
               AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%'
               AND (LOWER(COALESCE(rooms_en,'')) LIKE '%3 b/r%' OR LOWER(COALESCE(rooms_en,'')) LIKE '%3 br%' OR LOWER(COALESCE(rooms_en,'')) LIKE '%three%')
               AND (CASE
                      WHEN instance_date::text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN (instance_date::text)::date
                      WHEN instance_date::text ~ '^[0-9]{2}-[0-9]{2}-[0-9]{4}' THEN to_date(instance_date::text,'DD-MM-YYYY')
                      ELSE NULL END) >= CURRENT_DATE - INTERVAL '12 months'"""),
        ("LIVE dld_transactions_full exists?",
         "live",
         "SELECT to_regclass('public.dld_transactions_full')::text AS n"),
        ("LIVE Grande+Burj total",
         "live",
         "SELECT COUNT(*) AS n FROM public.dld_transactions_full WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%' AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%'"),
        ("ARCHIVE rooms_en formats (Grande+Burj)",
         "archive",
         """SELECT string_agg(DISTINCT rooms_en, ' | ') AS n FROM public.dld_sale_archive
             WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%'
               AND LOWER(COALESCE(area_name_en,'')) LIKE '%burj%khalifa%'"""),
        ("ARCHIVE sample instance_date formats",
         "archive",
         "SELECT string_agg(DISTINCT substring(instance_date::text, 1, 10), ' | ') AS n FROM (SELECT instance_date FROM public.dld_sale_archive WHERE LOWER(COALESCE(building_name_en,'')) LIKE '%grande%' LIMIT 5) s"),
    ]
    for label, src, sql in checks:
        old = globals().get("_ACTIVE_SOURCE", "live")
        try:
            _set_data_source(src)
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    r = cur.fetchone()
                    v = (r.get("n") if isinstance(r, dict) else (r[0] if r else None))
                    lines.append(f"• <b>{label}</b>: <code>{v}</code>")
        except Exception as e:
            lines.append(f"• <b>{label}</b>: ERR <code>{repr(e)[:120]}</code>")
        finally:
            try:
                _set_data_source(old)
            except Exception:
                pass
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Unhandled-exception alert hook (added 2026-05-29) ─────────────────────
import sys as _sys, traceback as _tb
def _unhandled_exception_hook(exc_type, exc_value, exc_traceback):
    try:
        from admin_notify import admin_notify
        tb = "".join(_tb.format_exception(exc_type, exc_value, exc_traceback))
        service = __name__
        admin_notify(
            f"🚨 <b>Unhandled exception</b> in <code>{service}</code>\n"
            f"<code>{exc_type.__name__}: {exc_value}</code>\n\n"
            f"<pre>{tb[-1500:]}</pre>"
        )
    except Exception:
        pass
    _sys.__excepthook__(exc_type, exc_value, exc_traceback)
_sys.excepthook = _unhandled_exception_hook


# ── Empty-result sanity guards (v111, 2026-05-30) ─────────────────────────
# Wraps the final v110/v106 versions of get_stats / get_latest_deals so that
# a successful-but-empty result is audited and, if the base table is big,
# escalates to admin_notify. Throttled to 1 alert/hour per (label, args sig).
#
# Implementation lives in C:\Projects\shared\empty_guard.py. We add the
# /shared path to sys.path so Railway prod (where /shared ships as a path)
# can import it.
try:
    import sys as _eg_sys, os as _eg_os
    for _eg_path in ("/app/shared", r"C:\Projects\shared", "../shared"):
        if _eg_os.path.isdir(_eg_path) and _eg_path not in _eg_sys.path:
            _eg_sys.path.insert(0, _eg_path)
    from empty_guard import guarded_query as _guarded_query
    _EG_BASE_SQL_DLD = "SELECT COUNT(*) FROM public.dld_transactions_full"

    # Save the v110/v106 originals and rebind through the guard. We use the
    # try/except NameError pattern so this stays safe if any wrapped function
    # is renamed or removed during a refactor.
    try:
        _eg_orig_get_stats = get_stats
        get_stats = _guarded_query(  # type: ignore[misc]
            label="analytics.get_stats",
            base_count_sql=_EG_BASE_SQL_DLD,
            dsn_env="LIVE_DATABASE_URL",
            bot="analytics",
            base_min=10_000,
            alert_threshold=100_000,
            is_empty=lambda r: r is None or (isinstance(r, dict) and not r.get("deals")),
        )(_eg_orig_get_stats)
    except NameError:
        pass

    try:
        _eg_orig_get_stats_smart = get_stats_smart
        def _eg_smart_empty(r):
            try:
                return r is None or r[0] is None or (
                    isinstance(r[0], dict) and not r[0].get("deals"))
            except Exception:
                return False
        get_stats_smart = _guarded_query(  # type: ignore[misc]
            label="analytics.get_stats_smart",
            base_count_sql=_EG_BASE_SQL_DLD,
            dsn_env="LIVE_DATABASE_URL",
            bot="analytics",
            base_min=10_000,
            alert_threshold=100_000,
            is_empty=_eg_smart_empty,
        )(_eg_orig_get_stats_smart)
    except NameError:
        pass

    try:
        _eg_orig_get_latest_deals = get_latest_deals
        get_latest_deals = _guarded_query(  # type: ignore[misc]
            label="analytics.get_latest_deals",
            base_count_sql=_EG_BASE_SQL_DLD,
            dsn_env="LIVE_DATABASE_URL",
            bot="analytics",
            base_min=10_000,
            alert_threshold=100_000,
        )(_eg_orig_get_latest_deals)
    except NameError:
        pass

    try:
        _eg_orig_get_latest_deals_smart = get_latest_deals_smart
        def _eg_smart_list_empty(r):
            try:
                return not r or not r[0]
            except Exception:
                return False
        get_latest_deals_smart = _guarded_query(  # type: ignore[misc]
            label="analytics.get_latest_deals_smart",
            base_count_sql=_EG_BASE_SQL_DLD,
            dsn_env="LIVE_DATABASE_URL",
            bot="analytics",
            base_min=10_000,
            alert_threshold=100_000,
            is_empty=_eg_smart_list_empty,
        )(_eg_orig_get_latest_deals_smart)
    except NameError:
        pass

    print("[empty_guard] v111 wrappers installed on get_stats/get_latest_deals "
          "(+ smart variants)")
except Exception as _eg_err:
    print(f"[empty_guard] init failed (non-fatal): {_eg_err}")


# ============================================================
# v141 GRANDE FIX — strip "|||area" suffix before read-model lookup
# ----------------------------------------------------------------
# Root cause: _state_for_selected_building_v72 stores
#   name = "Grande|||Burj Khalifa"
# (building + area encoded with triple-pipe). Downstream v110
# get_stats / get_stats_smart pass this raw name to
# read_model.try_building_stats(name), which does
#   building_key = name.strip().lower()
# and runs EXACT match: WHERE building_key = %s.
# Result: "grande|||burj khalifa" never matches any row in
# building_stats (where building_key = clean "grande"), so v110
# returns None and bot says "no data".
#
# Fix: split "|||" before read-model call. Also add a building-
# in-area fallback: if exact-building miss, try area lookup
# (so user still gets useful aggregate even if specific building
# wasn't pre-aggregated). This is fail-soft — only adds data paths.
# ============================================================

def _v141_split_building_area(name):
    """Split state-encoded 'Building|||Area' into (building, area)."""
    if not name:
        return None, None
    raw = str(name)
    if "|||" in raw:
        b, a = raw.split("|||", 1)
        return b.strip() or None, a.strip() or None
    return raw.strip() or None, None


# v144: When DLD canonical area name differs from UI label, try synonyms.
# Example: UI passes "Burj Khalifa" (DLD community code) but pre-aggregated
# area_stats may be keyed by the sector label "Downtown Dubai". The list is
# best-effort — empty means no synonym known, downstream handles None.
_V144_AREA_SYNONYMS = {
    "burj khalifa": ["Downtown Dubai", "Downtown", "Burj Khalifa Community"],
    "downtown dubai": ["Burj Khalifa", "Downtown"],
    "downtown": ["Downtown Dubai", "Burj Khalifa"],
    "marsa dubai": ["Dubai Marina", "Marina"],
    "dubai marina": ["Marsa Dubai", "Marina"],
    "al barsha south fourth": ["JVC", "Jumeirah Village Circle"],
    "al barsha south fifth": ["JVC", "Jumeirah Village Circle"],
    "al hebiah first": ["JVC", "Jumeirah Village Circle"],
    "jvc": ["Jumeirah Village Circle", "Al Barsha South Fourth"],
    "jumeirah village circle": ["JVC", "Al Barsha South Fourth"],
    "jlt": ["Jumeirah Lakes Towers"],
    "jumeirah lakes towers": ["JLT"],
}


def _v144_area_synonyms(area):
    if not area:
        return []
    key = area.strip().lower()
    return _V144_AREA_SYNONYMS.get(key, [])


try:
    _v141_orig_get_stats_smart = get_stats_smart
except NameError:
    _v141_orig_get_stats_smart = None

try:
    _v141_orig_get_stats = get_stats
except NameError:
    _v141_orig_get_stats = None


def get_stats_smart(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v141 wrapper: parse '|||' before read-model + area fallback for buildings."""
    if _v141_orig_get_stats_smart is None:
        return None, prop, period, deal_type
    # 1) try original with as-is name
    try:
        row, up, upe, udt = _v141_orig_get_stats_smart(scope, name, prop, period, deal_type)
        if row and (row.get("deals") or 0) > 0:
            return row, up, upe, udt
    except Exception as e:
        try:
            _v106_log("v141_get_stats_smart_pass1_err", e, scope=scope, name=str(name)[:120])
        except Exception:
            pass
        row = None

    # 2) if name has '|||', retry with clean building name only
    bld, area = _v141_split_building_area(name)
    if bld and bld != (name or ""):
        try:
            row2, up2, upe2, udt2 = _v141_orig_get_stats_smart(scope, bld, prop, period, deal_type)
            if row2 and (row2.get("deals") or 0) > 0:
                try:
                    _v106_log("v141_pipe_strip_hit", "get_stats_smart",
                              raw_name=str(name)[:120], clean=bld, kind=scope)
                except Exception:
                    pass
                return row2, up2, upe2, udt2
        except Exception as e:
            try:
                _v106_log("v141_get_stats_smart_clean_err", e, name=str(name)[:120])
            except Exception:
                pass

    # 3) building-scope fallback: query the area for context data.
    #    Try original area first, then v144 synonyms (Burj Khalifa ↔ Downtown).
    if scope in ("building", "buildings", "project", "tower") and area:
        area_candidates = [area] + _v144_area_synonyms(area)
        for cand in area_candidates:
            try:
                row3, up3, upe3, udt3 = _v141_orig_get_stats_smart("area", cand, prop, period, deal_type)
                if row3 and (row3.get("deals") or 0) > 0:
                    try:
                        _v106_log("v141_area_fallback_hit", "get_stats_smart",
                                  raw_name=str(name)[:120], area=cand,
                                  via_synonym=(cand != area))
                    except Exception:
                        pass
                    return row3, up3, upe3, udt3
            except Exception as e:
                try:
                    _v106_log("v141_get_stats_smart_area_fallback_err", e,
                              name=str(name)[:120], cand=cand)
                except Exception:
                    pass

    return None, prop, period, deal_type


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v141 wrapper: parse '|||' before read-model + area fallback for buildings."""
    if _v141_orig_get_stats is None:
        return None
    try:
        row = _v141_orig_get_stats(scope, name, prop, period, deal_type)
        if row and (row.get("deals") or 0) > 0:
            return row
    except Exception as e:
        try:
            _v106_log("v141_get_stats_pass1_err", e, scope=scope, name=str(name)[:120])
        except Exception:
            pass
        row = None

    bld, area = _v141_split_building_area(name)
    if bld and bld != (name or ""):
        try:
            row2 = _v141_orig_get_stats(scope, bld, prop, period, deal_type)
            if row2 and (row2.get("deals") or 0) > 0:
                return row2
        except Exception:
            pass

    if scope in ("building", "buildings", "project", "tower") and area:
        for cand in [area] + _v144_area_synonyms(area):
            try:
                row3 = _v141_orig_get_stats("area", cand, prop, period, deal_type)
                if row3 and (row3.get("deals") or 0) > 0:
                    return row3
            except Exception:
                pass

    return None


# Also patch get_latest_deals_smart to strip '|||' for raw DLD path
try:
    _v141_orig_get_latest_deals_smart = get_latest_deals_smart
except NameError:
    _v141_orig_get_latest_deals_smart = None


def get_latest_deals_smart(scope, name, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    """v141: try original first, then retry with clean building name (no '|||area')."""
    if _v141_orig_get_latest_deals_smart is None:
        return [], prop, period, deal_type
    try:
        rows, up, upe, udt = _v141_orig_get_latest_deals_smart(
            scope, name, prop, period, deal_type, limit=limit, unit_query=unit_query
        )
        if rows:
            return rows, up, upe, udt
    except Exception as e:
        try:
            _v106_log("v141_latest_deals_smart_pass1_err", e, scope=scope, name=str(name)[:120])
        except Exception:
            pass

    bld, area = _v141_split_building_area(name)
    if bld and bld != (name or ""):
        try:
            rows2, up2, upe2, udt2 = _v141_orig_get_latest_deals_smart(
                scope, bld, prop, period, deal_type, limit=limit, unit_query=unit_query
            )
            if rows2:
                try:
                    _v106_log("v141_pipe_strip_hit", "latest_deals_smart",
                              raw_name=str(name)[:120], clean=bld)
                except Exception:
                    pass
                return rows2, up2, upe2, udt2
        except Exception:
            pass

    # If still empty and scope=building with area context, try just-area lookup
    # (also iterate v144 area synonyms for Burj Khalifa ↔ Downtown Dubai).
    if scope in ("building", "buildings", "project", "tower") and area:
        for cand in [area] + _v144_area_synonyms(area):
            try:
                rows3, up3, upe3, udt3 = _v141_orig_get_latest_deals_smart(
                    "area", cand, prop, period, deal_type, limit=limit, unit_query=unit_query
                )
                if rows3:
                    try:
                        _v106_log("v141_area_fallback_hit", "latest_deals_smart",
                                  raw_name=str(name)[:120], area=cand,
                                  via_synonym=(cand != area))
                    except Exception:
                        pass
                    return rows3, up3, upe3, udt3
            except Exception:
                pass

    return [], prop, period, deal_type


print("Loaded v143 Grande honest-combo hint: building+area mismatch shows real DLD coverage")
print("Loaded v144 area-synonyms fallback: Burj Khalifa <-> Downtown Dubai, JVC subdistricts, etc.")


# ============================================================
# v146: wrap RAW get_latest_deals so v106 fallback in get_latest_deals_smart
# also strips '|||' from name. Root cause of remaining Grande "no data":
# v106 (line 12316) tries direct get_latest_deals(name=...) bypassing v141
# smart wrap → raw v67 SQL sees 'Grande|||Burj Khalifa' → 0 rows.
# ARCHIVE has 5476 Grande / 1629 in Burj Khalifa / 213 3BR / 615 1BR.
# ============================================================
try:
    _v146_orig_get_latest_deals = get_latest_deals
except NameError:
    _v146_orig_get_latest_deals = None


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    """v146: split '|||' before raw v67 query so name='Grande' (not 'Grande|||Burj Khalifa')."""
    if _v146_orig_get_latest_deals is None:
        return []
    # 1) try as-is (cheap when state already clean)
    try:
        rows = _v146_orig_get_latest_deals(scope, name, prop, period, deal_type, limit=limit, unit_query=unit_query)
        if rows:
            return rows
    except Exception:
        pass

    # 2) strip '|||area' suffix and retry
    try:
        bld, area = _v141_split_building_area(name)
    except Exception:
        bld, area = (str(name).split("|||", 1)[0].strip() if name and "|||" in str(name) else (name, None))
    if bld and bld != (name or ""):
        try:
            rows2 = _v146_orig_get_latest_deals(scope, bld, prop, period, deal_type, limit=limit, unit_query=unit_query)
            if rows2:
                try:
                    _v106_log("v146_pipe_strip_hit", "raw_latest_deals",
                              raw_name=str(name)[:120], clean=bld, scope=scope)
                except Exception:
                    pass
                return rows2
        except Exception:
            pass

    # 3) area-only fallback for building scope
    if scope in ("building", "buildings", "project", "tower") and area:
        try:
            rows3 = _v146_orig_get_latest_deals("area", area, prop, period, deal_type, limit=limit, unit_query=unit_query)
            if rows3:
                try:
                    _v106_log("v146_area_fallback_hit", "raw_latest_deals",
                              raw_name=str(name)[:120], area=area)
                except Exception:
                    pass
                return rows3
        except Exception:
            pass

    return []


# Same wrap for raw get_stats so direct fallbacks also strip '|||'
try:
    _v146_orig_get_stats_raw = get_stats
except NameError:
    _v146_orig_get_stats_raw = None


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v146: strip '|||' before raw v67 SQL query."""
    if _v146_orig_get_stats_raw is None:
        return None
    try:
        r = _v146_orig_get_stats_raw(scope, name, prop, period, deal_type)
        if r and (r.get("deals") or 0) > 0:
            return r
    except Exception:
        pass

    try:
        bld, area = _v141_split_building_area(name)
    except Exception:
        bld, area = (str(name).split("|||", 1)[0].strip() if name and "|||" in str(name) else (name, None))

    if bld and bld != (name or ""):
        try:
            r2 = _v146_orig_get_stats_raw(scope, bld, prop, period, deal_type)
            if r2 and (r2.get("deals") or 0) > 0:
                return r2
        except Exception:
            pass

    if scope in ("building", "buildings", "project", "tower") and area:
        try:
            r3 = _v146_orig_get_stats_raw("area", area, prop, period, deal_type)
            if r3 and (r3.get("deals") or 0) > 0:
                return r3
        except Exception:
            pass

    return None


print("Loaded v146 raw-layer ||| strip: get_latest_deals + get_stats (Grande Burj Khalifa fix)")


# ============================================================
# v147 TRACER — log every call to get_latest_deals + get_stats for Grande.
# Reveals exact path that returns empty.
# ============================================================
_v146_after_get_latest_deals = get_latest_deals
_v146_after_get_stats = get_stats


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    rows = _v146_after_get_latest_deals(scope, name, prop, period, deal_type, limit=limit, unit_query=unit_query)
    try:
        if name and "grande" in str(name).lower():
            print(f"[V147_TRACE] get_latest_deals scope={scope!r} name={name!r} prop={prop!r} period={period!r} dt={deal_type!r} → rows={len(rows) if rows else 0}", flush=True)
    except Exception:
        pass
    return rows


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    r = _v146_after_get_stats(scope, name, prop, period, deal_type)
    try:
        if name and "grande" in str(name).lower():
            deals = (r or {}).get("deals", 0) if isinstance(r, dict) else "non-dict"
            print(f"[V147_TRACE] get_stats scope={scope!r} name={name!r} prop={prop!r} period={period!r} dt={deal_type!r} → deals={deals}", flush=True)
    except Exception:
        pass
    return r


print("Loaded v147 Grande call tracer")


# ============================================================
# v148: Expand BUILDING_ALIASES in get_latest_deals + get_stats.
# Root cause discovered via v147 trace:
#   Bot stores building name as 'grande signature residences' (full marketing
#   name), but DLD ARCHIVE stores building_name_en = 'Grande' (short DLD
#   registry name). SQL exact-match + ILIKE both fail.
#   BUILDING_ALIASES dict at line 1103 maps:
#       'grande signature residences' -> ['grande']
#   but _query_aliases_v66 (line 7571) only checks AREA aliases, not building.
# Fix: v148 calls the underlying function with each alias and aggregates rows.
# ============================================================
_v148_orig_get_latest_deals = get_latest_deals
_v148_orig_get_stats = get_stats


def _v148_expand_building(name):
    """Return list of names to try for building scope, using BUILDING_ALIASES."""
    if not name:
        return [None]
    raw = str(name).strip()
    out = [raw]
    try:
        low = raw.lower().strip()
        for k, vals in BUILDING_ALIASES.items():
            if low == k.lower():
                for v in vals:
                    if v and v not in out:
                        out.append(v)
                break
            if low in [v.lower() for v in vals]:
                for v in vals:
                    if v and v not in out:
                        out.append(v)
                if k not in out:
                    out.append(k)
                break
    except Exception:
        pass
    return out


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    """v148: try each BUILDING_ALIASES variant; return first non-empty result."""
    if _v148_orig_get_latest_deals is None:
        return []
    candidates = _v148_expand_building(name) if scope in ("building", "buildings", "project", "tower") else [name]
    last = []
    for cand in candidates:
        try:
            rows = _v148_orig_get_latest_deals(scope, cand, prop, period, deal_type, limit=limit, unit_query=unit_query)
            if rows:
                if cand != (name or ""):
                    try:
                        print(f"[V148_HIT] get_latest_deals alias={cand!r} (was {name!r}) → rows={len(rows)}", flush=True)
                    except Exception:
                        pass
                return rows
            last = rows
        except Exception:
            continue
    return last


def get_stats(scope="dubai", name=None, prop=None, period=None, deal_type=None):  # noqa: F811
    """v148: try each BUILDING_ALIASES variant; return first non-empty result."""
    if _v148_orig_get_stats is None:
        return None
    candidates = _v148_expand_building(name) if scope in ("building", "buildings", "project", "tower") else [name]
    last = None
    for cand in candidates:
        try:
            r = _v148_orig_get_stats(scope, cand, prop, period, deal_type)
            if r and isinstance(r, dict) and (r.get("deals") or 0) > 0:
                if cand != (name or ""):
                    try:
                        print(f"[V148_HIT] get_stats alias={cand!r} (was {name!r}) → deals={r.get('deals')}", flush=True)
                    except Exception:
                        pass
                return r
            last = r
        except Exception:
            continue
    return last


print("Loaded v148 BUILDING_ALIASES expansion: grande signature residences -> Grande, etc.")


# ============================================================
# v151 PER-SOURCE TRACE — log every _run_source_sql_v67 call for Grande.
# ============================================================
try:
    _v151_orig_run_source = _run_source_sql_v67
except NameError:
    _v151_orig_run_source = None


_V153_COUNTER = [0]


def _run_source_sql_v67(source, table, sql, params):  # noqa: F811
    rows = _v151_orig_run_source(source, table, sql, params) if _v151_orig_run_source else []
    # v153: UNCONDITIONAL log first 50 calls + grande-specific
    _V153_COUNTER[0] += 1
    try:
        params_str = repr(params)[:300] if params else "[]"
        is_grande = "grande" in params_str.lower() or "grande" in (sql or "").lower()
        if _V153_COUNTER[0] <= 50 or is_grande:
            print(f"[V153_SQL #{_V153_COUNTER[0]}] grande={is_grande} src={source} tbl={table} → rows={len(rows) if rows else 0} params={params_str}", flush=True)
            if is_grande:
                print(f"[V153_SQL]   SQL={(sql or '')[:600]}", flush=True)
    except Exception as e:
        try:
            print(f"[V153_SQL] trace_error: {e!r}", flush=True)
        except Exception:
            pass
    return rows


print("Loaded v153 unconditional SQL trace")


# ============================================================
# v154 ARCHIVE FORCE-MERGE: latest get_latest_deals (v91, line 9912)
# uses single DB (LIVE only). Result: Grande shows 1 deal instead of 25.
# v154 queries ARCHIVE directly via psycopg2 and merges with existing rows.
# ============================================================
import psycopg2 as _v154_psycopg2
import psycopg2.extras as _v154_extras

_v154_after_get_latest_deals = get_latest_deals


def _v154_query_archive(scope, name, prop, period, deal_type, limit=7):
    """Direct ARCHIVE query bypassing v91 fastpath."""
    try:
        if not name or scope != "building":
            return []
        if not ARCHIVE_DATABASE_URL or ARCHIVE_DATABASE_URL == LIVE_DATABASE_URL:
            return []
        is_sale = is_sale_deal(deal_type) if 'is_sale_deal' in globals() else True
        is_rent = is_rent_deal(deal_type) if 'is_rent_deal' in globals() else False
        if not is_sale and not is_rent:
            is_sale = True
        table = "public.dld_sale_archive" if is_sale else "public.dld_rent_archive"
        price_col = "actual_worth" if is_sale else "annual_amount"
        # rooms filter
        import re as _re
        prop_low = (str(prop or "")).lower().strip()
        m = _re.search(r"(\d+)\s*br", prop_low)
        rooms_clause = ""
        rooms_params = []
        if "studio" in prop_low:
            rooms_clause = "AND LOWER(rooms_en) LIKE %s"
            rooms_params = ["%studio%"]
        elif m:
            n = m.group(1)
            rooms_clause = "AND (LOWER(rooms_en) LIKE %s OR LOWER(rooms_en) LIKE %s)"
            rooms_params = [f"%{n} b/r%", f"%{n} br%"]
        # period filter
        months = period_months(period) if 'period_months' in globals() else None
        period_clause = ""
        if months:
            period_clause = f"""AND (
                CASE
                  WHEN instance_date::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN (instance_date::text)::date
                  WHEN instance_date::text ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}' THEN to_date(instance_date::text, 'DD-MM-YYYY')
                  ELSE NULL
                END) >= CURRENT_DATE - INTERVAL '{int(months)} months'"""
        # building aliases (BUILDING_ALIASES + name)
        # v154.2: for SHORT aliases (single word, no marketing suffix) use EXACT
        # match to avoid pollution (e.g. 'grande' matching 'Sobha Creek Vistas
        # Grande', 'Beverly Grande', 'Crest Grande'). Long aliases stay fuzzy.
        candidates = _v148_expand_building(name)
        building_or = []
        building_params = []
        for c in candidates:
            cl = c.strip().lower()
            if " " not in cl and len(cl) <= 12:
                building_or.append("LOWER(building_name_en) = %s")
                building_params.append(cl)
            else:
                building_or.append("LOWER(building_name_en) ILIKE %s")
                building_params.append(f"%{cl}%")
        building_where = "AND (" + " OR ".join(building_or) + ")"
        # discover available columns for archive table
        with _v154_psycopg2.connect(ARCHIVE_DATABASE_URL, connect_timeout=10) as _pc:
            with _pc.cursor() as _pcur:
                _pcur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table.split(".")[-1],))
                _arch_cols = set(r[0] for r in _pcur.fetchall())
        def _pick(*opts):
            for o in opts:
                if o in _arch_cols:
                    return o
            return None
        size_col = _pick("procedure_area", "actual_area", "size_sqft", "property_size", "area_size_sqft", "area_size")
        ptype_col = _pick("property_type_en", "property_type")
        subtype_col = _pick("property_sub_type_en", "property_sub_type", "unit_type")
        size_expr = f"NULLIF({size_col}::text, '')::numeric" if size_col else "NULL::numeric"
        ptype_expr = ptype_col if ptype_col else "''"
        subtype_expr = subtype_col if subtype_col else "''"
        sql = f"""
            SELECT
                CASE
                  WHEN instance_date::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN (instance_date::text)::date
                  WHEN instance_date::text ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}' THEN to_date(instance_date::text, 'DD-MM-YYYY')
                  ELSE NULL
                END AS safe_date,
                building_name_en,
                area_name_en,
                rooms_en,
                {ptype_expr} AS property_type_en,
                {subtype_expr} AS property_sub_type_en,
                NULLIF({price_col}::text, '')::numeric AS price,
                {size_expr} AS area_size,
                NULL::numeric AS meter_price
            FROM {table}
            WHERE {price_col} IS NOT NULL AND {price_col}::text != ''
              {building_where}
              {rooms_clause}
              {period_clause}
            ORDER BY safe_date DESC NULLS LAST
            LIMIT %s
        """
        params = building_params + rooms_params + [limit]
        conn = _v154_psycopg2.connect(ARCHIVE_DATABASE_URL, connect_timeout=10)
        try:
            with conn.cursor(cursor_factory=_v154_extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            conn.close()
    except Exception as e:
        try:
            print(f"[V154_ARCHIVE_ERR] {e!r}", flush=True)
        except Exception:
            pass
        return []


def get_latest_deals(scope="building", name=None, prop=None, period=None, deal_type=None, limit=7, unit_query=None):  # noqa: F811
    """v154: merge LIVE (via existing path) + ARCHIVE (direct query)."""
    live_rows = []
    try:
        live_rows = _v154_after_get_latest_deals(scope, name, prop, period, deal_type, limit=limit, unit_query=unit_query) or []
    except Exception:
        pass
    archive_rows = _v154_query_archive(scope, name, prop, period, deal_type, limit=max(limit, 10))
    try:
        if name and "grande" in str(name).lower():
            print(f"[V154_MERGE] name={name!r} live={len(live_rows)} archive={len(archive_rows)}", flush=True)
    except Exception:
        pass
    # Merge + dedup
    seen = set()
    merged = []
    for r in list(live_rows) + list(archive_rows):
        try:
            key = (str(r.get("safe_date")), str(r.get("building_name_en")), str(r.get("price")))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
        except Exception:
            merged.append(r)
    try:
        merged.sort(key=lambda r: str(r.get("safe_date") or ""), reverse=True)
    except Exception:
        pass
    return merged[:limit]


print("Loaded v154 ARCHIVE force-merge: direct psycopg2 + dedupe")



# ============================================================
# v150 BOOT — count Grande+Burj 3BR last 12m in BOTH LIVE and ARCHIVE.
# Will be REMOVED after data collected.
# ============================================================
def _v150_count_grande_3br(label, table, date_col):
    qs = [
        ("01_total_grande_burj",
         f"SELECT COUNT(*) FROM public.{table} WHERE LOWER(building_name_en) LIKE '%grande%' AND LOWER(area_name_en) LIKE '%burj%'"),
        ("02_3br_grande_burj_all_time",
         f"SELECT COUNT(*) FROM public.{table} WHERE LOWER(building_name_en) LIKE '%grande%' AND LOWER(area_name_en) LIKE '%burj%' AND (LOWER(rooms_en) LIKE '%3 b/r%' OR LOWER(rooms_en) LIKE '%3 br%')"),
        ("03_3br_grande_burj_12m_safe",
         f"""SELECT COUNT(*) FROM public.{table} WHERE LOWER(building_name_en) LIKE '%grande%' AND LOWER(area_name_en) LIKE '%burj%' AND (LOWER(rooms_en) LIKE '%3 b/r%' OR LOWER(rooms_en) LIKE '%3 br%') AND (
            CASE
              WHEN {date_col}::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN ({date_col}::text)::date
              WHEN {date_col}::text ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}' THEN to_date({date_col}::text, 'DD-MM-YYYY')
              WHEN {date_col}::text ~ '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}' THEN to_date({date_col}::text, 'DD/MM/YYYY')
              ELSE NULL
            END) >= CURRENT_DATE - INTERVAL '12 months'"""),
        ("04_3br_grande_burj_24m",
         f"""SELECT COUNT(*) FROM public.{table} WHERE LOWER(building_name_en) LIKE '%grande%' AND LOWER(area_name_en) LIKE '%burj%' AND (LOWER(rooms_en) LIKE '%3 b/r%' OR LOWER(rooms_en) LIKE '%3 br%') AND (
            CASE
              WHEN {date_col}::text ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN ({date_col}::text)::date
              WHEN {date_col}::text ~ '^[0-9]{{2}}-[0-9]{{2}}-[0-9]{{4}}' THEN to_date({date_col}::text, 'DD-MM-YYYY')
              WHEN {date_col}::text ~ '^[0-9]{{2}}/[0-9]{{2}}/[0-9]{{4}}' THEN to_date({date_col}::text, 'DD/MM/YYYY')
              ELSE NULL
            END) >= CURRENT_DATE - INTERVAL '24 months'"""),
        ("05_3br_grande_burj_recent_5",
         f"""SELECT building_name_en, rooms_en, {date_col}, COALESCE(actual_worth, procedure_value, transaction_value, sale_price, price, amount) AS price FROM public.{table} WHERE LOWER(building_name_en) LIKE '%grande%' AND LOWER(area_name_en) LIKE '%burj%' AND (LOWER(rooms_en) LIKE '%3 b/r%' OR LOWER(rooms_en) LIKE '%3 br%') ORDER BY {date_col} DESC LIMIT 5"""),
    ]
    try:
        with db() as conn:
            with conn.cursor() as cur:
                for name, sql in qs:
                    try:
                        cur.execute(sql)
                        rows = cur.fetchall()
                        print(f"[V150_{label}] {name}: {rows[:5]}", flush=True)
                    except Exception as e:
                        print(f"[V150_{label}] {name} ERR: {str(e)[:160]}", flush=True)
    except Exception as e:
        print(f"[V150_{label}] outer ERR: {str(e)[:160]}", flush=True)


try:
    _v150_orig = globals().get("_ACTIVE_SOURCE", "live")
    try:
        _set_data_source("archive")
        _v150_count_grande_3br("ARCHIVE_SALE", "dld_sale_archive", "instance_date")
    except Exception as e:
        print(f"[V150_ARCHIVE_SALE] setup ERR: {e!r}", flush=True)
    try:
        _set_data_source("live")
        _v150_count_grande_3br("LIVE_SALE", "dld_transactions_full", "transaction_date")
    except Exception as e:
        print(f"[V150_LIVE_SALE] setup ERR: {e!r}", flush=True)
    try:
        _set_data_source(_v150_orig)
    except Exception:
        pass
    print("[V150] grande 3br diagnostic complete", flush=True)
except Exception as e:
    print(f"[V150] outer-most ERR: {e!r}", flush=True)


# ─────────────────────────────────────────────────────────────────────────
# Layer 17: Causal Inference Engine (DoWhy + EconML)
# Adds /causal_analysis command + inline button. Pure read-model — pulls
# pre-fit ATE rows from `causal_studies`; never refits inside hot path.
# See: shared/causal_inference/  + memory/agents/PHASE_BM_K.md
# ─────────────────────────────────────────────────────────────────────────
try:
    import sys as _ci_sys, os as _ci_os
    _ci_shared = _ci_os.path.join(_ci_os.path.dirname(__file__), "..", "shared")
    _ci_shared = _ci_os.path.abspath(_ci_shared)
    if _ci_shared not in _ci_sys.path:
        _ci_sys.path.insert(0, _ci_os.path.dirname(_ci_shared))
    from shared.causal_inference import load_study, format_effect_ru, REGISTRY as _CI_REGISTRY  # noqa: E402
    _CAUSAL_AVAILABLE = True
except Exception as _ci_imp_err:
    print(f"[Layer17] causal_inference unavailable: {_ci_imp_err!r}", flush=True)
    _CAUSAL_AVAILABLE = False
    _CI_REGISTRY = {}


def _ci_render_all() -> str:
    """Render a single message with all 5 pre-fit causal studies."""
    if not _CAUSAL_AVAILABLE:
        return ("📊 <b>Причинно-следственный анализ</b>\n\n"
                "Модуль временно недоступен. Попробуйте позже.")
    lines = ["📊 <b>Причинно-следственный анализ</b>",
             "Математические causal-эффекты на исторических данных DLD",
             "(метод: DoWhy backdoor + EconML DoubleML).\n"]
    found = 0
    for name, spec in _CI_REGISTRY.items():
        try:
            row = load_study(name)
            if not row:
                continue
            found += 1
            label = spec.get("label_ru", name)
            baseline = None
            try:
                baseline = row["model"]["frame_means"].get(row["outcome_var"])
            except Exception:
                baseline = None
            sentence = format_effect_ru(row, baseline=baseline)
            nl = row.get("natural_language")
            block = f"<b>{found}. {label}</b>\n{sentence}"
            if nl:
                block += f"\n<i>{nl}</i>"
            lines.append(block)
        except Exception as _err:
            lines.append(f"<b>{name}</b>: ошибка — {_err}")
    if found == 0:
        lines.append("Ни одно исследование ещё не посчитано. Запустите cron monthly_refresh.")
    lines.append("\n— Vadim Realty · RERA BRN 65011")
    return "\n\n".join(lines)


@dp.message(Command("causal_analysis"))
async def _causal_analysis_cmd(message):
    try:
        text = _ci_render_all()
    except Exception as _err:
        text = f"📊 Причинно-следственный анализ временно недоступен: {_err}"
    await message.answer(text[:4000])


# Inline button shortcut: "📊 Причинно-следственный анализ"
@dp.message(lambda m: (m.text or "").strip() in (
    "📊 Причинно-следственный анализ",
    "📊 Causal analysis",
))
async def _causal_analysis_btn(message):
    try:
        text = _ci_render_all()
    except Exception as _err:
        text = f"📊 Причинно-следственный анализ временно недоступен: {_err}"
    await message.answer(text[:4000])


# =========================================================================
# B061: COMPACT HUMAN 360-SUMMARY (override of _build_360_conclusion)
# =========================================================================
# Раньше выдавал ~10 абзацев академического текста с "нет данных" в
# пустых полях. Юзер хочет 6-8 строк: сколько сделок / самая выгодная
# категория / средняя цена / рост / yield / цена входа.
try:
    _B061_ORIG_BUILD_360 = _build_360_conclusion
except NameError:
    _B061_ORIG_BUILD_360 = None


def _b061_money(v):
    try:
        v = float(v or 0)
    except Exception:
        return None
    if not v:
        return None
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f} млн AED".replace('.', ',')
    if v >= 1_000:
        return f"{int(round(v/1_000))} тыс AED"
    return f"{int(round(v))} AED"


def _b061_collect_comparison(scope, name):
    try:
        rows, _notes = _v90_collect_format_comparison(
            scope=scope or 'dubai', area=name if scope == 'area' else None,
            budget=None, goal=None, period='12',
        )
        return rows or []
    except Exception:
        return []


def _build_360_conclusion_compact(row, scope=None, name=None, report_kind=None):
    """Короткое человечное резюме (5-8 строк) вместо длинного академического."""
    if not row:
        return ""
    try:
        deals = int(row.get("deals") or 0)
    except Exception:
        deals = 0
    if not deals:
        return ""

    try:
        avg_price = float(row.get("avg_price") or 0)
    except Exception:
        avg_price = 0
    try:
        avg_meter = float(row.get("avg_meter") or 0)
    except Exception:
        avg_meter = 0
    # B061: fallback на top_quartile_psf × SQFT_TO_M2 если avg_meter пустой
    if not avg_meter:
        try:
            psf = float(row.get("top_quartile_psf") or row.get("avg_price_psf") or 0)
            if psf:
                avg_meter = psf * float(globals().get("SQFT_TO_M2", 10.7639))
        except Exception:
            pass

    try:
        yoy = float(row.get("yoy_growth_top_pct") or row.get("yoy_growth_pct") or 0)
    except Exception:
        yoy = 0
    try:
        yld_avg = float(row.get("avg_rental_yield_pct") or 0)
    except Exception:
        yld_avg = 0

    # Самая выгодная категория из comparison
    cmp_rows = _b061_collect_comparison(scope, name)
    best_format = None
    villa = None
    for r in (cmp_rows or []):
        fmt = (r.get('format') or '').strip()
        try:
            _r_deals = int(r.get('deals') or 0)
        except Exception:
            _r_deals = 0
        if _r_deals < 50:
            continue
        try:
            _r_score = float(r.get('score') or 0)
        except Exception:
            _r_score = 0
        if 'апарт' in fmt.lower() or 'apart' in fmt.lower():
            if not best_format or _r_score > float(best_format.get('score') or 0):
                best_format = r
        if 'вилл' in fmt.lower() or 'villa' in fmt.lower():
            villa = r

    liquidity = "очень высокая" if deals >= 5000 else ("высокая" if deals >= 1000 else ("средняя" if deals >= 200 else "низкая"))

    parts = ["\n\n🧠 <b>Что важно знать</b>\n"]
    parts.append(f"📊 Сделок за период: <b>{format_int(deals)}</b>")
    if avg_price:
        parts.append(f"💰 Средняя цена: <b>{_b061_money(avg_price) or '—'}</b>")
    if avg_meter:
        parts.append(f"📐 За м²: <b>~{_b061_money(avg_meter) or '—'}</b>")
    if yoy:
        sign = "+" if yoy > 0 else ""
        parts.append(f"📈 Динамика: <b>{sign}{yoy:.1f}%</b> YoY")
    parts.append(f"⚡ Ликвидность: <b>{liquidity}</b>")

    if best_format:
        try:
            _bf_deals = int(best_format.get('deals') or 0)
            _bf_price = _b061_money(best_format.get('avg_price'))
            _bf_yield = best_format.get('yield_pct') or best_format.get('rental_yield_pct') or 0
            _line = f"\n🥇 <b>Самая выгодная категория:</b> Апартаменты"
            _details = []
            _details.append(f"{format_int(_bf_deals)} сделок")
            if _bf_price:
                _details.append(f"avg {_bf_price}")
            try:
                _y = float(_bf_yield or 0)
                if 3 <= _y <= 12:
                    _details.append(f"доходность {_y:.1f}%")
            except Exception:
                pass
            if _details:
                _line += f"\n   {' · '.join(_details)}"
            parts.append(_line)
        except Exception:
            pass

    if villa:
        try:
            _v_deals = int(villa.get('deals') or 0)
            _v_price = _b061_money(villa.get('avg_price'))
            if _v_deals and _v_price:
                parts.append(f"🏡 Виллы: {format_int(_v_deals)} сделок · avg {_v_price}")
        except Exception:
            pass

    # Цена входа
    try:
        if avg_price:
            _low = avg_price * 0.90
            _high = avg_price * 0.95
            parts.append(
                f"\n✅ <b>Цена входа:</b> {_b061_money(_low)} — {_b061_money(_high)}"
                f"\n   <i>выше — нужно обоснование (вид, этаж, ремонт)</i>"
            )
    except Exception:
        pass

    parts.append("\n<i>Vadim Realty · RERA BRN 65011</i>")
    return "\n".join(parts)


# Override the function used by send_full_report / send_period_report / etc.
def _build_360_conclusion(row, scope=None, name=None, report_kind=None):  # noqa: F811
    try:
        return _build_360_conclusion_compact(row, scope, name, report_kind)
    except Exception as _e:
        print("BUILD_360_COMPACT_ERROR:", repr(_e))
        if _B061_ORIG_BUILD_360:
            try:
                return _B061_ORIG_BUILD_360(row, scope, name, report_kind)
            except Exception:
                pass
        return ""


print("Loaded B061 compact 360-summary override")


if __name__ == "__main__":
    # Boot-time ecosystem contract verification (fail-soft unless STRICT_CONTRACTS=1).
    try:
        from contracts_registry import verify_my_contracts as _vmc
        _vmc(bot_name="analytics", role="both", notify=True)
    except Exception as _ce:
        try:
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "contracts_registry verify failed (non-fatal): %s", _ce)
        except Exception:
            pass
    try:
        asyncio.run(main())
    except Exception:
        import traceback as _crash_tb
        try:
            from admin_notify import admin_notify as _crash_notify
            _crash_notify(f"🚨 Bot CRASH (dld-analytics):\n<pre>{_crash_tb.format_exc()[-1500:]}</pre>")
        except Exception:
            pass
        raise
