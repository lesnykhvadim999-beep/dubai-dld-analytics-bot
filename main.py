from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

import asyncio
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Смотреть сделки")],
        [KeyboardButton(text="🏙 Статистика района"), KeyboardButton(text="🌆 Статистика по Дубаю")],
        [KeyboardButton(text="🚀 Топ растущих районов"), KeyboardButton(text="💰 Топ ROI")],
        [KeyboardButton(text="🏢 Поиск здания"), KeyboardButton(text="⚙️ Настройки")]
    ],
    resize_keyboard=True
)


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏙 <b>Dubai DLD Analytics Bot</b>\n\n"
        "Бот запущен успешно.\n\n"
        "Выберите раздел в меню ниже:",
        reply_markup=main_menu
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "📌 <b>Команды бота:</b>\n\n"
        "/start — запустить бота\n"
        "/help — список команд\n\n"
        "Основные разделы:\n"
        "📊 Смотреть сделки\n"
        "🏙 Статистика района\n"
        "🌆 Статистика по Дубаю\n"
        "🚀 Топ растущих районов\n"
        "💰 Топ ROI\n"
        "🏢 Поиск здания"
    )


@dp.message(lambda message: message.text == "📊 Смотреть сделки")
async def deals_handler(message: Message):
    await message.answer(
        "Вы выбрали раздел <b>Смотреть сделки</b>.\n\n"
        "Следующим шагом здесь будет выбор:\n"
        "1. Аренда или продажа\n"
        "2. Район\n"
        "3. Здание\n"
        "4. Тип юнита\n"
        "5. Период"
    )


@dp.message(lambda message: message.text == "🏙 Статистика района")
async def area_stats_handler(message: Message):
    await message.answer(
        "Вы выбрали <b>Статистика района</b>.\n\n"
        "Следующим шагом здесь будет выбор района и периода."
    )


@dp.message(lambda message: message.text == "🌆 Статистика по Дубаю")
async def dubai_stats_handler(message: Message):
    await message.answer(
        "Вы выбрали <b>Статистика по Дубаю</b>.\n\n"
        "Здесь будет общая аналитика по всему рынку Дубая."
    )


@dp.message(lambda message: message.text == "🚀 Топ растущих районов")
async def growth_handler(message: Message):
    await message.answer(
        "Вы выбрали <b>Топ растущих районов</b>.\n\n"
        "Здесь будут районы с самым сильным ростом."
    )


@dp.message(lambda message: message.text == "💰 Топ ROI")
async def roi_handler(message: Message):
    await message.answer(
        "Вы выбрали <b>Топ ROI</b>.\n\n"
        "Здесь будут районы с лучшей доходностью."
    )


@dp.message(lambda message: message.text == "🏢 Поиск здания")
async def building_search_handler(message: Message):
    await message.answer(
        "Вы выбрали <b>Поиск здания</b>.\n\n"
        "Позже здесь можно будет ввести название здания и получить аналитику."
    )


@dp.message(lambda message: message.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        "Позже здесь будет выбор языка:\n"
        "Русский / English / العربية"
    )


async def main():
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
