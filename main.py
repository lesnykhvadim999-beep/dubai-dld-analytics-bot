from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.filters import CommandStart, Command

from dotenv import load_dotenv

import asyncio
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()


# =========================
# MENU
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


# =========================
# START
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message):

    await message.answer(
        "🏙 <b>Dubai DLD Analytics Bot</b>\n\n"
        "Бот запущен успешно.\n\n"
        "Выберите раздел в меню ниже:",
        reply_markup=main_menu
    )


# =========================
# HELP
# =========================

@dp.message(Command("help"))
async def help_handler(message: Message):

    await message.answer(
        "📌 <b>Команды бота:</b>\n\n"
        "/start — запуск\n"
        "/help — помощь\n\n"
        "Также можно просто написать название здания.\n\n"
        "Например:\n"
        "Creek Vista Heights"
    )


# =========================
# BUTTONS
# =========================

@dp.message(lambda message: message.text == "📊 Смотреть сделки")
async def deals_handler(message: Message):

    await message.answer(
        "📊 Раздел сделок.\n\n"
        "Скоро здесь будет:\n"
        "• Продажи\n"
        "• Аренда\n"
        "• Фильтры\n"
        "• DLD данные"
    )


@dp.message(lambda message: message.text == "🏙 Статистика района")
async def area_handler(message: Message):

    await message.answer(
        "🏙 Статистика района.\n\n"
        "Скоро здесь появится аналитика районов."
    )


@dp.message(lambda message: message.text == "🌆 Статистика по Дубаю")
async def dubai_handler(message: Message):

    await message.answer(
        "🌆 Общая статистика Дубая.\n\n"
        "Скоро здесь будет:\n"
        "• Объём рынка\n"
        "• Средние цены\n"
        "• Рост рынка\n"
        "• Тренды"
    )


@dp.message(lambda message: message.text == "🚀 Топ растущих районов")
async def growth_handler(message: Message):

    await message.answer(
        "🚀 Топ растущих районов.\n\n"
        "Скоро здесь будет live аналитика роста."
    )


@dp.message(lambda message: message.text == "💰 Топ ROI")
async def roi_handler(message: Message):

    await message.answer(
        "💰 Топ ROI районов.\n\n"
        "Скоро здесь будет реальная доходность."
    )


@dp.message(lambda message: message.text == "🏢 Поиск здания")
async def building_handler(message: Message):

    await message.answer(
        "🏢 Напишите название здания.\n\n"
        "Например:\n"
        "Creek Vista Heights"
    )


@dp.message(lambda message: message.text == "⚙️ Настройки")
async def settings_handler(message: Message):

    await message.answer(
        "⚙️ Настройки.\n\n"
        "Скоро здесь появится:\n"
        "• Смена языка\n"
        "• Валюта\n"
        "• Избранное"
    )


# =========================
# BUILDING SEARCH
# =========================

@dp.message()
async def universal_search(message: Message):

    text = message.text.lower()

    buildings = {

        "creek vista heights": {
            "price": "2.4M AED",
            "roi": "7.2%",
            "sqft": "2,150 AED",
            "growth": "+14%",
            "deals": "148 deals"
        },

        "bayz 101": {
            "price": "1.9M AED",
            "roi": "8.4%",
            "sqft": "2,050 AED",
            "growth": "+21%",
            "deals": "203 deals"
        }

    }

    if text in buildings:

        data = buildings[text]

        await message.answer(
            f"🏢 <b>{message.text}</b>\n\n"

            f"💰 Average Price: {data['price']}\n"
            f"📈 ROI: {data['roi']}\n"
            f"📐 Price per sqft: {data['sqft']}\n"
            f"🚀 Growth: {data['growth']}\n"
            f"📊 Transactions: {data['deals']}\n\n"

            f"✅ Building analytics loaded"
        )

    else:

        await message.answer(
            "❌ Building not found.\n\n"
            "Try:\n"
            "- Creek Vista Heights\n"
            "- Bayz 101"
        )


# =========================
# MAIN
# =========================

async def main():

    print("Bot started...")

    await dp.start_polling(bot)


if __name__ == "__main__":

    asyncio.run(main())
