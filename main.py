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


def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


TEXTS = {
    "ru": {
        "start": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nВыберите язык:",
        "lang_selected": "✅ Язык выбран: <b>Русский</b>\n\nГлавное меню:",
        "main_menu": "🏠 Главное меню.\n\nВыберите раздел:",
        "view_deals": "📊 Смотреть сделки",
        "area_stats": "🏙 Статистика района",
        "dubai_stats": "🌆 Статистика по Дубаю",
        "top_growing": "🚀 Топ активных зданий",
        "top_roi_btn": "💰 Топ по средней цене",
        "building_search": "🏢 Поиск здания",
        "settings_btn": "⚙️ Настройки",
        "sale": "🏠 Продажа",
        "rent": "🔑 Аренда",
        "main_button": "🏠 Главное меню",
        "back_button": "⬅️ Назад",
        "six_months": "6 месяцев",
        "one_year": "1 год",
        "three_years": "3 года",
        "deals": "📊 Выберите тип сделки:",
        "deal_selected": "Вы выбрали: <b>{deal_type}</b>\n\nТеперь выберите период:",
        "period_selected": "📅 Период выбран: <b>{period}</b>\n\nСэр, фильтрация по периоду будет следующим этапом.",
        "area_prompt": "🏙 Введите название района.\n\nНапример:\n• Business Bay\n• Downtown Dubai\n• Dubai Marina",
        "dubai_title": "🌆 <b>Аналитика рынка Дубая</b>",
        "building_prompt": "🏢 Введите название здания.\n\nМожно полностью или частично:\n• Creek\n• Marina\n• Downtown\n• Sobha",
        "settings": "⚙️ Настройки.\n\nВыберите язык:",
        "not_found": "❌ Ничего не найдено.\n\nПопробуйте другое название здания или района.",
        "analytics_loaded": "✅ Данные из DLD PostgreSQL загружены",
        "back": "⬅️ Возвращаю в главное меню."
    }
}


def get_lang(user_id):
    return user_languages.get(user_id, "ru")


def t(user_id, key):
    return TEXTS[get_lang(user_id)][key]


def language_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇷🇺 Русский")]
        ],
        resize_keyboard=True
    )


def main_menu(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(user_id, "view_deals"))],
            [
                KeyboardButton(text=t(user_id, "area_stats")),
                KeyboardButton(text=t(user_id, "dubai_stats"))
            ],
            [
                KeyboardButton(text=t(user_id, "top_growing")),
                KeyboardButton(text=t(user_id, "top_roi_btn"))
            ],
            [
                KeyboardButton(text=t(user_id, "building_search")),
                KeyboardButton(text=t(user_id, "settings_btn"))
            ]
        ],
        resize_keyboard=True
    )


def deal_type_menu(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(user_id, "sale")), KeyboardButton(text=t(user_id, "rent"))],
            [KeyboardButton(text=t(user_id, "back_button")), KeyboardButton(text=t(user_id, "main_button"))]
        ],
        resize_keyboard=True
    )


def period_menu(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t(user_id, "six_months")),
                KeyboardButton(text=t(user_id, "one_year")),
                KeyboardButton(text=t(user_id, "three_years"))
            ],
            [KeyboardButton(text=t(user_id, "back_button")), KeyboardButton(text=t(user_id, "main_button"))]
        ],
        resize_keyboard=True
    )


def format_aed(value):
    if value is None:
        return "нет данных"
    return f"{float(value):,.0f} AED".replace(",", " ")


def get_dubai_stats():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total_transactions,
                    COUNT(DISTINCT building_name_en) AS total_buildings,
                    COUNT(DISTINCT area_name_en) AS total_areas,
                    AVG(actual_worth) AS avg_price,
                    AVG(meter_sale_price) AS avg_meter_price
                FROM public.dld_transactions_full
                WHERE actual_worth IS NOT NULL
            """)
            return cur.fetchone()


def get_top_active_buildings(limit=10):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG(actual_worth) AS avg_price,
                    AVG(meter_sale_price) AS avg_meter_price
                FROM public.dld_transactions_full
                WHERE building_name_en IS NOT NULL
                  AND building_name_en <> ''
                  AND actual_worth IS NOT NULL
                GROUP BY building_name_en, area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def get_top_expensive_buildings(limit=10):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG(actual_worth) AS avg_price,
                    AVG(meter_sale_price) AS avg_meter_price
                FROM public.dld_transactions_full
                WHERE building_name_en IS NOT NULL
                  AND building_name_en <> ''
                  AND actual_worth IS NOT NULL
                GROUP BY building_name_en, area_name_en
                HAVING COUNT(*) >= 5
                ORDER BY avg_price DESC
                LIMIT %s
            """, (limit,))
            return cur.fetchall()


def search_building(query, limit=10):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    building_name_en,
                    area_name_en,
                    COUNT(*) AS deals,
                    AVG(actual_worth) AS avg_price,
                    AVG(meter_sale_price) AS avg_meter_price,
                    MIN(instance_date) AS first_deal,
                    MAX(instance_date) AS last_deal
                FROM public.dld_transactions_full
                WHERE building_name_en ILIKE %s
                   OR area_name_en ILIKE %s
                GROUP BY building_name_en, area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", limit))
            return cur.fetchall()


def search_area(query, limit=10):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    area_name_en,
                    COUNT(*) AS deals,
                    COUNT(DISTINCT building_name_en) AS buildings,
                    AVG(actual_worth) AS avg_price,
                    AVG(meter_sale_price) AS avg_meter_price
                FROM public.dld_transactions_full
                WHERE area_name_en ILIKE %s
                  AND actual_worth IS NOT NULL
                GROUP BY area_name_en
                ORDER BY deals DESC
                LIMIT %s
            """, (f"%{query}%", limit))
            return cur.fetchall()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(TEXTS["ru"]["start"], reply_markup=language_menu())


@dp.message(lambda message: message.text == "🇷🇺 Русский")
async def language_handler(message: Message):
    user_languages[message.from_user.id] = "ru"
    await message.answer(
        t(message.from_user.id, "lang_selected"),
        reply_markup=main_menu(message.from_user.id)
    )


@dp.message()
async def main_handler(message: Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if text == t(user_id, "main_button"):
        await message.answer(t(user_id, "main_menu"), reply_markup=main_menu(user_id))
        return

    if text == t(user_id, "back_button"):
        await message.answer(t(user_id, "back"), reply_markup=main_menu(user_id))
        return

    if text == t(user_id, "view_deals"):
        await message.answer(t(user_id, "deals"), reply_markup=deal_type_menu(user_id))
        return

    if text in [t(user_id, "sale"), t(user_id, "rent")]:
        await message.answer(
            t(user_id, "deal_selected").format(deal_type=text),
            reply_markup=period_menu(user_id)
        )
        return

    if text in [t(user_id, "six_months"), t(user_id, "one_year"), t(user_id, "three_years")]:
        await message.answer(t(user_id, "period_selected").format(period=text))
        return

    if text == t(user_id, "dubai_stats"):
        stats = get_dubai_stats()

        await message.answer(
            f"{t(user_id, 'dubai_title')}\n\n"
            f"📊 Сделок в базе: <b>{stats['total_transactions']:,}</b>\n"
            f"🏢 Зданий: <b>{stats['total_buildings']:,}</b>\n"
            f"📍 Районов: <b>{stats['total_areas']:,}</b>\n"
            f"💰 Средняя цена сделки: <b>{format_aed(stats['avg_price'])}</b>\n"
            f"📐 Средняя цена за метр: <b>{format_aed(stats['avg_meter_price'])}</b>\n\n"
            f"{t(user_id, 'analytics_loaded')}"
        )
        return

    if text == t(user_id, "top_growing"):
        rows = get_top_active_buildings()

        response = "🚀 <b>Топ активных зданий по количеству сделок</b>\n\n"

        for i, row in enumerate(rows, start=1):
            response += (
                f"{i}. 🏢 <b>{row['building_name_en']}</b>\n"
                f"📍 Район: {row['area_name_en']}\n"
                f"📊 Сделок: {row['deals']}\n"
                f"💰 Средняя цена: {format_aed(row['avg_price'])}\n"
                f"📐 Цена за метр: {format_aed(row['avg_meter_price'])}\n\n"
            )

        await message.answer(response)
        return

    if text == t(user_id, "top_roi_btn"):
        rows = get_top_expensive_buildings()

        response = "💰 <b>Топ зданий по средней цене сделки</b>\n\n"

        for i, row in enumerate(rows, start=1):
            response += (
                f"{i}. 🏢 <b>{row['building_name_en']}</b>\n"
                f"📍 Район: {row['area_name_en']}\n"
                f"📊 Сделок: {row['deals']}\n"
                f"💰 Средняя цена: {format_aed(row['avg_price'])}\n"
                f"📐 Цена за метр: {format_aed(row['avg_meter_price'])}\n\n"
            )

        await message.answer(response)
        return

    if text == t(user_id, "area_stats"):
        await message.answer(t(user_id, "area_prompt"))
        return

    if text == t(user_id, "building_search"):
        await message.answer(t(user_id, "building_prompt"))
        return

    if text == t(user_id, "settings_btn"):
        await message.answer(t(user_id, "settings"), reply_markup=language_menu())
        return

    rows = search_building(text)

    if not rows:
        rows = search_area(text)

        if not rows:
            await message.answer(t(user_id, "not_found"))
            return

        response = f"🏙 <b>Статистика района по запросу:</b> {text}\n\n"

        for row in rows:
            response += (
                f"📍 <b>{row['area_name_en']}</b>\n"
                f"🏢 Зданий: {row['buildings']}\n"
                f"📊 Сделок: {row['deals']}\n"
                f"💰 Средняя цена: {format_aed(row['avg_price'])}\n"
                f"📐 Цена за метр: {format_aed(row['avg_meter_price'])}\n\n"
            )

        await message.answer(response)
        return

    response = f"🔎 <b>Результаты поиска:</b> {text}\n\n"

    for row in rows:
        response += (
            f"🏢 <b>{row['building_name_en']}</b>\n"
            f"📍 Район: {row['area_name_en']}\n"
            f"📊 Сделок: {row['deals']}\n"
            f"💰 Средняя цена: {format_aed(row['avg_price'])}\n"
            f"📐 Цена за метр: {format_aed(row['avg_meter_price'])}\n"
            f"🗓 Первая сделка: {row['first_deal']}\n"
            f"🗓 Последняя сделка: {row['last_deal']}\n\n"
        )

    await message.answer(response)


async def main():
    print("Bot started with PostgreSQL DLD database")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
