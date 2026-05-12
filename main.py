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


conn = sqlite3.connect("dld.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    avg_price TEXT,
    roi TEXT,
    sqft TEXT,
    growth TEXT,
    deals TEXT
)
""")

conn.commit()


cursor.execute("SELECT * FROM buildings")
existing = cursor.fetchall()

if not existing:
    buildings_data = [
        ("Creek Vista Heights", "2.4M AED", "7.2%", "2,150 AED", "+14%", "148 deals"),
        ("Bayz 101", "1.9M AED", "8.4%", "2,050 AED", "+21%", "203 deals"),
        ("Sobha Hartland Waves", "1.6M AED", "6.8%", "1,950 AED", "+11%", "97 deals")
    ]

    cursor.executemany("""
    INSERT INTO buildings
    (name, avg_price, roi, sqft, growth, deals)
    VALUES (?, ?, ?, ?, ?, ?)
    """, buildings_data)

    conn.commit()


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


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏙 <b>Dubai DLD Analytics Bot</b>\n\n"
        "Бот запущен успешно.\n\n"
        "Введите название здания или выберите раздел:",
        reply_markup=main_menu
    )


@dp.message(lambda message: message.text == "📊 Смотреть сделки")
async def deals_handler(message: Message):
    await message.answer("📊 Live DLD сделки скоро будут подключены.")


@dp.message(lambda message: message.text == "🏙 Статистика района")
async def area_handler(message: Message):
    await message.answer("🏙 Аналитика районов скоро будет доступна.")


@dp.message(lambda message: message.text == "🌆 Статистика по Дубаю")
async def dubai_handler(message: Message):
    await message.answer("🌆 Общая аналитика Дубая скоро будет доступна.")


@dp.message(lambda message: message.text == "🚀 Топ растущих районов")
async def growth_handler(message: Message):
    await message.answer("🚀 Топ растущих районов скоро будет доступен.")


@dp.message(lambda message: message.text == "💰 Топ ROI")
async def roi_handler(message: Message):
    await message.answer("💰 ROI аналитика скоро будет доступна.")


@dp.message(lambda message: message.text == "🏢 Поиск здания")
async def building_handler(message: Message):
    await message.answer(
        "🏢 Введите название здания.\n\n"
        "Можно ввести полностью или частично:\n"
        "• Sobha\n"
        "• Waves\n"
        "• Bayz\n"
        "• Creek"
    )


@dp.message(lambda message: message.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    await message.answer("⚙️ Настройки скоро будут доступны.")


@dp.message()
async def search_building(message: Message):
    text = message.text.strip().lower()

    cursor.execute("""
    SELECT
        name,
        avg_price,
        roi,
        sqft,
        growth,
        deals
    FROM buildings
    WHERE lower(name) LIKE ?
    """, ("%" + text + "%",))

    results = cursor.fetchall()

    if not results:
        await message.answer(
            "❌ Здание не найдено.\n\n"
            "Попробуйте:\n"
            "• Creek\n"
            "• Bayz\n"
            "• Sobha\n"
            "• Waves"
        )
        return

    if len(results) == 1:
        name, avg_price, roi, sqft, growth, deals = results[0]

        await message.answer(
            f"🏢 <b>{name}</b>\n\n"
            f"💰 Average Price: {avg_price}\n"
            f"📈 ROI: {roi}\n"
            f"📐 Price per sqft: {sqft}\n"
            f"🚀 Growth: {growth}\n"
            f"📊 Transactions: {deals}\n\n"
            f"✅ Live analytics loaded"
        )
        return

    response = "🔎 Найдено несколько объектов:\n\n"

    for item in results:
        name, avg_price, roi, sqft, growth, deals = item
        response += (
            f"🏢 <b>{name}</b>\n"
            f"💰 Average Price: {avg_price}\n"
            f"📈 ROI: {roi}\n"
            f"📐 Price per sqft: {sqft}\n"
            f"🚀 Growth: {growth}\n"
            f"📊 Transactions: {deals}\n\n"
        )

    await message.answer(response)


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
