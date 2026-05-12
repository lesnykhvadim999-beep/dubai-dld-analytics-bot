from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart

from dotenv import load_dotenv

import asyncio
import os
import sqlite3

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()


# =========================
# DATABASE
# =========================

conn = sqlite3.connect("dld.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    area TEXT,
    avg_price TEXT,
    roi REAL,
    sqft TEXT,
    growth REAL,
    deals INTEGER
)
""")

conn.commit()

cursor.execute("SELECT COUNT(*) FROM buildings")
existing = cursor.fetchone()[0]

if existing == 0:
    buildings_data = [
        ("Creek Vista Heights", "Sobha Hartland", "2.4M AED", 7.2, "2,150 AED", 14, 148),
        ("Bayz 101", "Business Bay", "1.9M AED", 8.4, "2,050 AED", 21, 203),
        ("Sobha Hartland Waves", "Sobha Hartland", "1.6M AED", 6.8, "1,950 AED", 11, 97),
        ("Sobha One", "Ras Al Khor", "2.8M AED", 7.9, "2,430 AED", 18, 121),
        ("Burj Crown", "Downtown Dubai", "3.7M AED", 5.9, "3,100 AED", 9, 82)
    ]

    cursor.executemany("""
    INSERT INTO buildings
    (name, area, avg_price, roi, sqft, growth, deals)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, buildings_data)

    conn.commit()


# =========================
# MENUS
# =========================

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Смотреть сделки")],
        [
            KeyboardButton(text="🏙 Статистика района"),
            KeyboardButton(text="🌆 Статистика по Дубаю")
        ],
        [
            KeyboardButton(text="🚀 Топ растущих районов"),
            KeyboardButton(text="💰 Топ ROI")
        ],
        [
            KeyboardButton(text="🏢 Поиск здания"),
            KeyboardButton(text="⚙️ Настройки")
        ]
    ],
    resize_keyboard=True
)

deal_type_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏠 Продажа"), KeyboardButton(text="🔑 Аренда")],
        [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="🏠 Главное меню")]
    ],
    resize_keyboard=True
)

period_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="6 месяцев"), KeyboardButton(text="1 год"), KeyboardButton(text="3 года")],
        [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="🏠 Главное меню")]
    ],
    resize_keyboard=True
)

language_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🇷🇺 Русский"), KeyboardButton(text="🇬🇧 English"), KeyboardButton(text="🇦🇪 العربية")],
        [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="🏠 Главное меню")]
    ],
    resize_keyboard=True
)


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏙 <b>Dubai DLD Analytics Bot</b>\n\n"
        "Система аналитики недвижимости Дубая активна.\n\n"
        "Введите название здания или выберите раздел:",
        reply_markup=main_menu
    )


# =========================
# NAVIGATION
# =========================

@dp.message(lambda message: message.text == "🏠 Главное меню")
async def main_menu_handler(message: Message):
    await message.answer(
        "🏠 Главное меню.\n\n"
        "Выберите раздел:",
        reply_markup=main_menu
    )


@dp.message(lambda message: message.text == "⬅️ Назад")
async def back_handler(message: Message):
    await message.answer(
        "⬅️ Возвращаю в главное меню.",
        reply_markup=main_menu
    )


# =========================
# BUTTONS
# =========================

@dp.message(lambda message: message.text == "📊 Смотреть сделки")
async def deals_handler(message: Message):
    await message.answer(
        "📊 Выберите тип сделки:",
        reply_markup=deal_type_menu
    )


@dp.message(lambda message: message.text in ["🏠 Продажа", "🔑 Аренда"])
async def deal_type_handler(message: Message):
    deal_type = "Продажа" if message.text == "🏠 Продажа" else "Аренда"

    await message.answer(
        f"Вы выбрали: <b>{deal_type}</b>\n\n"
        "Теперь выберите период:",
        reply_markup=period_menu
    )


@dp.message(lambda message: message.text in ["6 месяцев", "1 год", "3 года"])
async def period_handler(message: Message):
    await message.answer(
        f"📅 Период выбран: <b>{message.text}</b>\n\n"
        "Следующий шаг: подключим реальные DLD сделки и фильтрацию по району / зданию / юниту."
    )


@dp.message(lambda message: message.text == "🏙 Статистика района")
async def area_handler(message: Message):
    await message.answer(
        "🏙 Введите название района.\n\n"
        "Например:\n"
        "• Business Bay\n"
        "• Downtown Dubai\n"
        "• Sobha Hartland"
    )


@dp.message(lambda message: message.text == "🌆 Статистика по Дубаю")
async def dubai_handler(message: Message):
    cursor.execute("SELECT COUNT(*) FROM buildings")
    total_buildings = cursor.fetchone()[0]

    cursor.execute("SELECT AVG(roi) FROM buildings")
    avg_roi = round(cursor.fetchone()[0], 2)

    cursor.execute("SELECT AVG(growth) FROM buildings")
    avg_growth = round(cursor.fetchone()[0], 2)

    await message.answer(
        f"🌆 <b>Dubai Market Analytics</b>\n\n"
        f"🏢 Buildings tracked: {total_buildings}\n"
        f"📈 Average ROI: {avg_roi}%\n"
        f"🚀 Average Growth: +{avg_growth}%\n"
        f"💰 Market Status: Active\n\n"
        f"✅ Analytics engine active"
    )


@dp.message(lambda message: message.text == "🚀 Топ растущих районов")
async def growth_handler(message: Message):
    cursor.execute("""
    SELECT name, area, growth
    FROM buildings
    ORDER BY growth DESC
    LIMIT 5
    """)

    results = cursor.fetchall()
    response = "🚀 <b>Top Growing Buildings</b>\n\n"

    for name, area, growth in results:
        response += (
            f"🏢 <b>{name}</b>\n"
            f"📍 Area: {area}\n"
            f"📈 Growth: +{growth}%\n\n"
        )

    await message.answer(response)


@dp.message(lambda message: message.text == "💰 Топ ROI")
async def roi_handler(message: Message):
    cursor.execute("""
    SELECT name, area, roi
    FROM buildings
    ORDER BY roi DESC
    LIMIT 5
    """)

    results = cursor.fetchall()
    response = "💰 <b>Top ROI Buildings</b>\n\n"

    for name, area, roi in results:
        response += (
            f"🏢 <b>{name}</b>\n"
            f"📍 Area: {area}\n"
            f"📈 ROI: {roi}%\n\n"
        )

    await message.answer(response)


@dp.message(lambda message: message.text == "🏢 Поиск здания")
async def building_handler(message: Message):
    await message.answer(
        "🏢 Введите название здания.\n\n"
        "Можно полностью или частично:\n"
        "• Sobha\n"
        "• Bayz\n"
        "• Creek\n"
        "• Crown"
    )


@dp.message(lambda message: message.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    await message.answer(
        "⚙️ Настройки.\n\n"
        "Выберите язык:",
        reply_markup=language_menu
    )


@dp.message(lambda message: message.text in ["🇷🇺 Русский", "🇬🇧 English", "🇦🇪 العربية"])
async def language_handler(message: Message):
    await message.answer(
        f"✅ Язык выбран: <b>{message.text}</b>\n\n"
        "Позже мы привяжем этот выбор к профилю пользователя.",
        reply_markup=main_menu
    )


# =========================
# SEARCH
# =========================

@dp.message()
async def search_handler(message: Message):
    text = message.text.strip().lower()

    cursor.execute("""
    SELECT name, area, avg_price, roi, sqft, growth, deals
    FROM buildings
    WHERE lower(name) LIKE ?
       OR lower(area) LIKE ?
    """, ("%" + text + "%", "%" + text + "%"))

    results = cursor.fetchall()

    if not results:
        await message.answer(
            "❌ Ничего не найдено.\n\n"
            "Попробуйте:\n"
            "• Sobha\n"
            "• Business Bay\n"
            "• Downtown Dubai\n"
            "• Bayz\n"
            "• Creek"
        )
        return

    if len(results) == 1:
        name, area, avg_price, roi, sqft, growth, deals = results[0]

        await message.answer(
            f"🏢 <b>{name}</b>\n\n"
            f"📍 Area: {area}\n"
            f"💰 Average Price: {avg_price}\n"
            f"📈 ROI: {roi}%\n"
            f"📐 Price per sqft: {sqft}\n"
            f"🚀 Growth: +{growth}%\n"
            f"📊 Transactions: {deals}\n\n"
            f"✅ Analytics loaded"
        )
        return

    response = "🔎 <b>Найдено несколько объектов:</b>\n\n"

    for name, area, avg_price, roi, sqft, growth, deals in results:
        response += (
            f"🏢 <b>{name}</b>\n"
            f"📍 Area: {area}\n"
            f"💰 {avg_price}\n"
            f"📈 ROI: {roi}%\n"
            f"🚀 Growth: +{growth}%\n\n"
        )

    await message.answer(response)


# =========================
# MAIN
# =========================

async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
