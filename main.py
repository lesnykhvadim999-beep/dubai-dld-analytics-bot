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


_COLUMN_CACHE = None

def table_columns():
    global _COLUMN_CACHE
    if _COLUMN_CACHE is not None:
        return _COLUMN_CACHE
    try:
        schema, table = TABLE.split('.', 1) if '.' in TABLE else ('public', TABLE)
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                """, [schema, table])
                _COLUMN_CACHE = {r['column_name'] for r in cur.fetchall()}
    except Exception as e:
        print('TABLE_COLUMNS_ERROR:', repr(e))
        _COLUMN_CACHE = set()
    return _COLUMN_CACHE

def has_col(name):
    return name in table_columns()

def num_sql_dynamic(column):
    return f"NULLIF(regexp_replace(COALESCE({column}::text, ''), '[^0-9.]', '', 'g'), '')::numeric"

def first_existing_col(candidates):
    for col in candidates:
        if has_col(col):
            return col
    return None

def coalesce_text_expr(candidates):
    cols = [c for c in candidates if has_col(c)]
    if not cols:
        return "''"
    return "LOWER(COALESCE(" + ", ' ', ".join([f"{c}::text" for c in cols]) + ", ''))"

def deal_text_expr():
    return coalesce_text_expr([
        'procedure_name_en', 'procedure_name_ar', 'procedure_type_en', 'procedure_type_ar',
        'transaction_type_en', 'transaction_type_ar', 'transaction_type', 'deal_type',
        'reg_type_en', 'reg_type_ar', 'contract_type_en', 'contract_type_ar'
    ])

def rent_value_column():
    # 1) специальные арендные поля, если они есть в базе
    col = first_existing_col([
        'rent_value', 'annual_amount', 'annual_rent', 'rent_amount',
        'lease_amount', 'contract_amount', 'contract_value', 'ejari_value',
        'annual_value', 'amount'
    ])
    if col:
        return col
    # 2) во многих DLD выгрузках аренда лежит в actual_worth,
    # но использовать его можно ТОЛЬКО вместе со строгим rent/lease фильтром по процедуре.
    if has_col('actual_worth'):
        return 'actual_worth'
    return None

def is_rent_deal(deal_type):
    d = str(deal_type or '').lower()
    return ('rent' in d) or ('lease' in d) or ('аренд' in d) or ('🔑' in d)

def is_sale_deal(deal_type):
    d = str(deal_type or '').lower()
    return ('sale' in d) or ('sell' in d) or ('прод' in d) or ('🏠' in d)

def value_expr(deal_type=None):
    if is_rent_deal(deal_type):
        col = rent_value_column()
        if col:
            return num_sql_dynamic(col)
        return 'NULL::numeric'
    return PRICE

def value_label(deal_type=None):
    return 'аренда' if is_rent_deal(deal_type) else 'цена'

def meter_expr(deal_type=None):
    if is_rent_deal(deal_type):
        for col in ['meter_rent_price', 'rent_meter_price', 'meter_lease_price', 'annual_rent_per_meter', 'rent_per_meter']:
            if has_col(col):
                return num_sql_dynamic(col)
        size = None
        for col in ['procedure_area', 'actual_area', 'property_size', 'area_sqft', 'size_sqft', 'rooms_area']:
            if has_col(col):
                size = num_sql_dynamic(col)
                break
        val = value_expr(deal_type)
        if size:
            return f"CASE WHEN {size} > 0 THEN {val} / {size} ELSE NULL END"
        return 'NULL::numeric'
    return METER_PRICE

def value_not_null_sql(deal_type=None):
    return f"AND {value_expr(deal_type)} IS NOT NULL"


def make_deal_type_condition(deal_type):
    if not deal_type:
        return "", []

    txt = deal_text_expr()

    if is_sale_deal(deal_type):
        # Продажа: исключаем все строки rent/lease/ejari, чтобы аренда не смешивалась с продажей.
        return f"""
        AND {PRICE} IS NOT NULL
        AND NOT (
            {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s
        )
        """, ["%rent%", "%lease%", "%rental%", "%ejari%", "%tenancy%"]

    if is_rent_deal(deal_type):
        # Аренда: строго ищем только rent/lease/ejari/tenancy процедуры.
        # Если в базе нет таких строк, лучше показать пустой результат, чем продажу под видом аренды.
        rent_val = value_expr(deal_type)
        return f"""
        AND {rent_val} IS NOT NULL
        AND (
            {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s OR {txt} ILIKE %s
        )
        """, ["%rent%", "%lease%", "%rental%", "%ejari%", "%tenancy%"]

    return "", []

def property_condition(prop):
    if not prop:
        return "", []

    p = (prop or "").lower().strip()

    if p == "studio":
        return """
        AND (
            rooms_en ILIKE %s
            OR property_type_en ILIKE %s
            OR property_sub_type_en ILIKE %s
        )
        """, ["%studio%", "%studio%", "%studio%"]

    if p in ["1 br", "2 br", "3 br", "4 br"]:
        n = p.split()[0]
        return """
        AND (
            rooms_en ILIKE %s
            OR rooms_en ILIKE %s
            OR property_type_en ILIKE %s
            OR property_sub_type_en ILIKE %s
        )
        """, [f"%{n}%", f"%{n} B/R%", f"%{n}%", f"%{n}%"]

    if p == "5 br+":
        return """
        AND (
            rooms_en ILIKE %s OR rooms_en ILIKE %s OR rooms_en ILIKE %s
            OR rooms_en ILIKE %s OR rooms_en ILIKE %s
            OR property_type_en ILIKE %s OR property_sub_type_en ILIKE %s
        )
        """, ["%5%", "%6%", "%7%", "%8%", "%9%", "%5%", "%5%"]

    if p == "villa":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%villa%", "%villa%"]

    if p == "townhouse":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%town%", "%town%"]

    if p == "penthouse":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%penthouse%", "%penthouse%"]

    if p == "apartment":
        return "AND (property_type_en ILIKE %s OR property_sub_type_en ILIKE %s OR property_sub_type_en ILIKE %s)", ["%apartment%", "%apartment%", "%flat%"]

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
    aliases = {
        "grande": ["Grande", "Grande Signature Residences"],
        "grande signature": ["Grande", "Grande Signature Residences"],
        "grande signature residences": ["Grande", "Grande Signature Residences"],
        "address opera": ["Address", "Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "the address opera": ["Address", "Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "address residences dubai opera": ["Address", "Address Residences Dubai Opera", "The Address Residences Dubai Opera"],
        "corner": ["Corner", "Binghatti Corner"],
        "binghatti corner": ["Corner", "Binghatti Corner"],
    }
    return aliases.get(q, [name])

def building_exact_condition_for_name(name):
    name = (name or "").strip()
    aliases = building_aliases(name)

    conditions = []
    params = []

    for alias in aliases:
        conditions.append("LOWER(building_name_en) = LOWER(%s)")
        params.append(alias)

    conditions.append("building_name_en ILIKE %s")
    params.append(f"%{name}%")

    return "AND (" + " OR ".join(conditions) + ")", params


def find_buildings(query, limit=10):
    q = normalize_search_text(query)
    aliases = building_aliases(query)

    words = []
    for a in aliases:
        words.extend([w for w in normalize_search_text(a).split() if len(w) >= 2])
    words.extend([w for w in q.split() if len(w) >= 2])
    words = list(dict.fromkeys(words))

    if not words:
        return []

    expr = "LOWER(COALESCE(building_name_en, '') || ' ' || COALESCE(area_name_en, ''))"
    conditions = " OR ".join([f"{expr} ILIKE %s" for _ in words])
    params = [f"%{w}%" for w in words] + [limit]

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        building_name_en,
                        area_name_en,
                        COUNT(*) AS deals
                    {base_from()}
                      AND building_name_en IS NOT NULL
                      AND building_name_en <> ''
                      AND ({conditions})
                    GROUP BY building_name_en, area_name_en
                    ORDER BY deals DESC
                    LIMIT %s
                """, params)
                return cur.fetchall()
    except Exception as e:
        print("FIND_BUILDINGS_SQL_ERROR:", repr(e))
        return []


def find_areas(query, limit=10):
    q = normalize_search_text(query)
    aliases = {
        "jvc": ["jumeirah village circle", "al hebiah", "al barsha south"],
        "downtown": ["downtown", "burj khalifa"],
        "downtown dubai": ["downtown", "burj khalifa"],
        "business bay": ["business bay"],
        "marina": ["marina", "marsa dubai"],
        "dubai marina": ["dubai marina", "marsa dubai"],
    }
    words = aliases.get(q, [q])
    expr = "LOWER(COALESCE(area_name_en, ''))"
    conditions = " OR ".join([f"{expr} ILIKE %s" for _ in words])
    params = [f"%{w}%" for w in words] + [limit]

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        area_name_en,
                        COUNT(*) AS deals,
                        COUNT(DISTINCT building_name_en) AS buildings
                    {base_from()}
                      AND area_name_en IS NOT NULL
                      AND area_name_en <> ''
                      AND ({conditions})
                    GROUP BY area_name_en
                    ORDER BY deals DESC
                    LIMIT %s
                """, params)
                return cur.fetchall()
    except Exception as e:
        print("FIND_AREAS_SQL_ERROR:", repr(e))
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

    params += prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        COUNT(DISTINCT {BUILDING_NAME}) AS buildings,
                        COUNT(DISTINCT area_name_en) AS areas,
                        AVG({value_expr(deal_type)}) AS avg_price,
                        MIN({value_expr(deal_type)}) AS min_price,
                        MAX({value_expr(deal_type)}) AS max_price,
                        AVG({meter_expr(deal_type)}) AS avg_meter,
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
                      {value_not_null_sql(deal_type)}
                """, params)
                return cur.fetchone()
    except Exception as e:
        print("GET_STATS_ERROR:", repr(e))
        return None


def get_unit_summary(scope="building", name=None, prop=None, period=None, deal_type=None):
    where, params = scope_condition(scope, name, original_query=name)
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)
    params += prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr(deal_type)}) AS avg_price,
                        MIN({value_expr(deal_type)}) AS min_price,
                        MAX({value_expr(deal_type)}) AS max_price,
                        AVG({meter_expr(deal_type)}) AS avg_meter,
                        percentile_cont(0.25) WITHIN GROUP (ORDER BY {value_expr(deal_type)}) AS p25_price,
                        percentile_cont(0.50) WITHIN GROUP (ORDER BY {value_expr(deal_type)}) AS median_price,
                        percentile_cont(0.75) WITHIN GROUP (ORDER BY {value_expr(deal_type)}) AS p75_price
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_condition(period)}
                      {value_not_null_sql(deal_type)}
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

    params_current = list(base_params) + prop_args + deal_args
    params_previous = list(base_params) + prop_args + deal_args

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr(deal_type)}) AS avg_price,
                        AVG({meter_expr(deal_type)}) AS avg_meter
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_condition(period)}
                      {value_not_null_sql(deal_type)}
                """, params_current)
                current = cur.fetchone()

                cur.execute(f"""
                    SELECT
                        COUNT(*) AS deals,
                        AVG({value_expr(deal_type)}) AS avg_price,
                        AVG({meter_expr(deal_type)}) AS avg_meter
                    {base_from()}
                      {where}
                      {prop_sql}
                      {deal_sql}
                      {period_previous_condition(period)}
                      {value_not_null_sql(deal_type)}
                """, params_previous)
                previous = cur.fetchone()

        return current, previous
    except Exception as e:
        print("GET_COMPARISON_ERROR:", repr(e))
        return None


def get_latest_deals(scope, name, prop=None, period=None, deal_type=None, limit=5):
    prop_sql, prop_args = property_condition(prop)
    deal_sql, deal_args = make_deal_type_condition(deal_type)

    if scope == "area":
        scope_sql, scope_args = make_area_exact_condition(name)
    elif scope == "building":
        scope_sql, scope_args = building_exact_condition_for_name(name)
    else:
        scope_sql, scope_args = "", []

    def run_query(p_sql, p_args=None):
        p_args = p_args or []
        params = scope_args + prop_args + deal_args + p_args + [limit]
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        safe_date,
                        procedure_name_en,
                        rooms_en,
                        property_type_en,
                        property_sub_type_en,
                        {value_expr(deal_type)} AS price,
                        {meter_expr(deal_type)} AS meter_price,
                        building_name_en,
                        area_name_en
                    {base_from()}
                      {scope_sql}
                      {prop_sql}
                      {deal_sql}
                      {p_sql}
                      {value_not_null_sql(deal_type)}
                    ORDER BY safe_date DESC NULLS LAST
                    LIMIT %s
                """, params)
                return cur.fetchall()

    try:
        # 1) точный выбранный период
        rows = run_query(period_condition(period))
        if rows:
            return rows
        # 2) для кнопки «Последние сделки» лучше показать последние доступные сделки
        # по тем же фильтрам, но без ограничения периода. Важно: тип сделки сохраняется,
        # аренда не заменяется продажей.
        if period:
            rows = run_query("")
        return rows
    except Exception as e:
        print("GET_LATEST_DEALS_ERROR:", repr(e))
        return []


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

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        {BUILDING_NAME} AS building_name_en,
                        area_name_en,
                        COUNT(*) AS deals,
                        AVG({value_expr(deal_type)}) AS avg_price,
                        AVG({meter_expr(deal_type)}) AS avg_meter
                    {base_from()}
                      {where}
                      {deal_sql}
                      {period_condition(period)}
                      AND {BUILDING_NAME} IS NOT NULL
                      {value_not_null_sql(deal_type)}
                    GROUP BY {BUILDING_NAME}, area_name_en
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
                    area_conditions = " OR ".join(["area_name_en ILIKE %s"] * len(real_areas))
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
                                    AVG({value_expr(deal_type)}) AS avg_price,
                                    MIN({value_expr(deal_type)}) AS min_price,
                                    MAX({value_expr(deal_type)}) AS max_price,
                                    AVG({meter_expr(deal_type)}) AS avg_meter,
                                    MIN(safe_date) AS first_deal,
                                    MAX(safe_date) AS last_deal
                                {base_from()}
                                  AND ({area_conditions})
                                  {prop_sql}
                                  {value_not_null_sql(deal_type)}
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
            push_state(user_id, {"step": "choose_deal_type", "scope": "dubai", "name": None, "force_report": "last"})
            await message.answer(tr(user_id, "choose_deal_type"), reply_markup=deal_type_menu(user_id))
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
        current_state = user_states.get(user_id, {})
        if current_state.get("step") in ["building_query", "choose_building"]:
            await message.answer(
                "⚠️ Поиск временно не отработал. Попробуйте ввести название иначе, например: Grande или Address Opera.",
                reply_markup=back_menu(user_id)
            )
        else:
            await message.answer("⚠️ Не удалось выполнить расчёт. Попробуйте другой запрос или вернитесь в главное меню.", reply_markup=main_menu(user_id))


async def main():
    print("Dubai DLD Analytics Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
