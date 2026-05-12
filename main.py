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
        "period_selected": "📅 Период выбран: <b>{period}</b>\n\nФильтрация по периоду будет следующим этапом.",
        "area_prompt": "🏙 Введите название района.\n\nНапример:\n• Business Bay\n• Downtown Dubai\n• Dubai Marina",
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
        keyboard=[[KeyboardButton(text="🇷🇺 Русский")]],
        resize_keyboard=True
    )


def main_menu(user_id):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text
