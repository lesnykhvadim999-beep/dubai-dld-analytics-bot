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
user_languages = {}

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
if cursor.fetchone()[0] == 0:
    cursor.executemany("""
    INSERT INTO buildings
    (name, area, avg_price, roi, sqft, growth, deals)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, [
        ("Creek Vista Heights", "Sobha Hartland", "2.4M AED", 7.2, "2,150 AED", 14, 148),
        ("Bayz 101", "Business Bay", "1.9M AED", 8.4, "2,050 AED", 21, 203),
        ("Sobha Hartland Waves", "Sobha Hartland", "1.6M AED", 6.8, "1,950 AED", 11, 97),
        ("Sobha One", "Ras Al Khor", "2.8M AED", 7.9, "2,430 AED", 18, 121),
        ("Burj Crown", "Downtown Dubai", "3.7M AED", 5.9, "3,100 AED", 9, 82)
    ])
    conn.commit()


TEXTS = {
    "ru": {
        "start": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nВыберите язык:",
        "lang_selected": "✅ Язык выбран: <b>Русский</b>\n\nГлавное меню:",
        "main_menu": "🏠 Главное меню.\n\nВыберите раздел:",
        "view_deals": "📊 Смотреть сделки",
        "area_stats": "🏙 Статистика района",
        "dubai_stats": "🌆 Статистика по Дубаю",
        "top_growing": "🚀 Топ растущих районов",
        "top_roi_btn": "💰 Топ ROI",
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
        "period_selected": "📅 Период выбран: <b>{period}</b>\n\nСледующий шаг: подключим реальные DLD сделки и фильтрацию по району / зданию / юниту.",
        "area_prompt": "🏙 Введите название района.\n\nНапример:\n• Business Bay\n• Downtown Dubai\n• Sobha Hartland",
        "dubai_title": "🌆 <b>Аналитика рынка Дубая</b>",
        "buildings_tracked": "🏢 Зданий в базе",
        "average_roi": "📈 Средний ROI",
        "average_growth": "🚀 Средний рост",
        "market_status": "💰 Статус рынка: активный",
        "engine_active": "✅ Аналитика активна",
        "top_growth_title": "🚀 <b>Топ растущих зданий</b>\n\n",
        "top_roi_title": "💰 <b>Топ зданий по ROI</b>\n\n",
        "building_prompt": "🏢 Введите название здания.\n\nМожно полностью или частично:\n• Sobha\n• Bayz\n• Creek\n• Crown",
        "settings": "⚙️ Настройки.\n\nВыберите язык:",
        "not_found": "❌ Ничего не найдено.\n\nПопробуйте:\n• Sobha\n• Business Bay\n• Downtown Dubai\n• Bayz\n• Creek",
        "multiple_found": "🔎 <b>Найдено несколько объектов:</b>\n\n",
        "analytics_loaded": "✅ Аналитика загружена",
        "back": "⬅️ Возвращаю в главное меню.",
        "area_label": "📍 Район",
        "growth_label": "📈 Рост",
        "avg_price_label": "💰 Средняя цена",
        "roi_label": "📈 ROI",
        "sqft_label": "📐 Цена за sqft",
        "transactions_label": "📊 Сделки"
    },
    "en": {
        "start": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nChoose language:",
        "lang_selected": "✅ Language selected: <b>English</b>\n\nMain menu:",
        "main_menu": "🏠 Main menu.\n\nChoose a section:",
        "view_deals": "📊 View deals",
        "area_stats": "🏙 Area statistics",
        "dubai_stats": "🌆 Dubai statistics",
        "top_growing": "🚀 Top growing areas",
        "top_roi_btn": "💰 Top ROI",
        "building_search": "🏢 Building search",
        "settings_btn": "⚙️ Settings",
        "sale": "🏠 Sale",
        "rent": "🔑 Rent",
        "main_button": "🏠 Main menu",
        "back_button": "⬅️ Back",
        "six_months": "6 months",
        "one_year": "1 year",
        "three_years": "3 years",
        "deals": "📊 Choose deal type:",
        "deal_selected": "You selected: <b>{deal_type}</b>\n\nNow choose period:",
        "period_selected": "📅 Period selected: <b>{period}</b>\n\nNext step: we will connect real DLD transactions and filters by area / building / unit.",
        "area_prompt": "🏙 Enter area name.\n\nFor example:\n• Business Bay\n• Downtown Dubai\n• Sobha Hartland",
        "dubai_title": "🌆 <b>Dubai Market Analytics</b>",
        "buildings_tracked": "🏢 Buildings tracked",
        "average_roi": "📈 Average ROI",
        "average_growth": "🚀 Average growth",
        "market_status": "💰 Market status: active",
        "engine_active": "✅ Analytics engine active",
        "top_growth_title": "🚀 <b>Top Growing Buildings</b>\n\n",
        "top_roi_title": "💰 <b>Top ROI Buildings</b>\n\n",
        "building_prompt": "🏢 Enter building name.\n\nYou can type full or partial name:\n• Sobha\n• Bayz\n• Creek\n• Crown",
        "settings": "⚙️ Settings.\n\nChoose language:",
        "not_found": "❌ Nothing found.\n\nTry:\n• Sobha\n• Business Bay\n• Downtown Dubai\n• Bayz\n• Creek",
        "multiple_found": "🔎 <b>Several objects found:</b>\n\n",
        "analytics_loaded": "✅ Analytics loaded",
        "back": "⬅️ Back to main menu.",
        "area_label": "📍 Area",
        "growth_label": "📈 Growth",
        "avg_price_label": "💰 Average price",
        "roi_label": "📈 ROI",
        "sqft_label": "📐 Price per sqft",
        "transactions_label": "📊 Transactions"
    },
    "ar": {
        "start": "🏙 <b>Dubai DLD Analytics Bot</b>\n\nاختر اللغة:",
        "lang_selected": "✅ تم اختيار اللغة: <b>العربية</b>\n\nالقائمة الرئيسية:",
        "main_menu": "🏠 القائمة الرئيسية.\n\nاختر القسم:",
        "view_deals": "📊 عرض الصفقات",
        "area_stats": "🏙 إحصائيات المنطقة",
        "dubai_stats": "🌆 إحصائيات دبي",
        "top_growing": "🚀 المناطق الأسرع نمواً",
        "top_roi_btn": "💰 أعلى ROI",
        "building_search": "🏢 بحث عن مبنى",
        "settings_btn": "⚙️ الإعدادات",
        "sale": "🏠 بيع",
        "rent": "🔑 إيجار",
        "main_button": "🏠 القائمة الرئيسية",
        "back_button": "⬅️ رجوع",
        "six_months": "6 أشهر",
        "one_year": "سنة واحدة",
        "three_years": "3 سنوات",
        "deals": "📊 اختر نوع الصفقة:",
        "deal_selected": "لقد اخترت: <b>{deal_type}</b>\n\nاختر الفترة:",
        "period_selected": "📅 تم اختيار الفترة: <b>{period}</b>\n\nالخطوة التالية: سنقوم بربط صفقات DLD الحقيقية والفلاتر حسب المنطقة / المبنى / الوحدة.",
        "area_prompt": "🏙 اكتب اسم المنطقة.\n\nمثال:\n• Business Bay\n• Downtown Dubai\n• Sobha Hartland",
        "dubai_title": "🌆 <b>تحليل سوق دبي</b>",
        "buildings_tracked": "🏢 عدد المباني",
        "average_roi": "📈 متوسط العائد",
        "average_growth": "🚀 متوسط النمو",
        "market_status": "💰 حالة السوق: نشط",
        "engine_active": "✅ التحليلات مفعلة",
        "top_growth_title": "🚀 <b>أعلى المباني نمواً</b>\n\n",
        "top_roi_title": "💰 <b>أعلى المباني من حيث العائد</b>\n\n",
        "building_prompt": "🏢 اكتب اسم المبنى.\n\nيمكنك كتابة الاسم كاملاً أو جزئياً:\n• Sobha\n• Bayz\n• Creek\n• Crown",
        "settings": "⚙️ الإعدادات.\n\nاختر اللغة:",
        "not_found": "❌ لم يتم العثور على نتائج.\n\nجرب:\n• Sobha\n• Business Bay\n• Downtown Dubai\n• Bayz\n• Creek",
        "multiple_found": "🔎 <b>تم العثور على عدة نتائج:</b>\n\n",
        "analytics_loaded": "✅ تم تحميل التحليلات",
        "back": "⬅️ العودة إلى القائمة الرئيسية.",
        "area_label": "📍 المنطقة",
        "growth_label": "📈 النمو",
        "avg_price_label": "💰 متوسط السعر",
        "roi_label": "📈 العائد",
        "sqft_label": "📐 السعر لكل قدم مربع",
        "transactions_label": "📊 الصفقات"
    }
}


def get_lang(user_id):
    return user_languages.get(user_id, "ru")


def t(user_id, key):
    return TEXTS[get_lang(user_id)][key]


def language_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇷🇺 Русский")],
            [KeyboardButton(text="🇬🇧 English")],
            [KeyboardButton(text="🇦🇪 العربية")]
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


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(TEXTS["ru"]["start"], reply_markup=language_menu())


@dp.message(lambda message: message.text in ["🇷🇺 Русский", "🇬🇧 English", "🇦🇪 العربية"])
async def language_handler(message: Message):
    if message.text == "🇷🇺 Русский":
        user_languages[message.from_user.id] = "ru"
    elif message.text == "🇬🇧 English":
        user_languages[message.from_user.id] = "en"
    elif message.text == "🇦🇪 العربية":
        user_languages[message.from_user.id] = "ar"

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

    if text == t(user_id, "area_stats"):
        await message.answer(t(user_id, "area_prompt"))
        return

    if text == t(user_id, "dubai_stats"):
        cursor.execute("SELECT COUNT(*) FROM buildings")
        total_buildings = cursor.fetchone()[0]

        cursor.execute("SELECT AVG(roi) FROM buildings")
        avg_roi = round(cursor.fetchone()[0], 2)

        cursor.execute("SELECT AVG(growth) FROM buildings")
        avg_growth = round(cursor.fetchone()[0], 2)

        await message.answer(
            f"{t(user_id, 'dubai_title')}\n\n"
            f"{t(user_id, 'buildings_tracked')}: {total_buildings}\n"
            f"{t(user_id, 'average_roi')}: {avg_roi}%\n"
            f"{t(user_id, 'average_growth')}: +{avg_growth}%\n"
            f"{t(user_id, 'market_status')}\n\n"
            f"{t(user_id, 'engine_active')}"
        )
        return

    if text == t(user_id, "top_growing"):
        cursor.execute("""
        SELECT name, area, growth
        FROM buildings
        ORDER BY growth DESC
        LIMIT 5
        """)

        response = t(user_id, "top_growth_title")

        for name, area, growth in cursor.fetchall():
            response += (
                f"🏢 <b>{name}</b>\n"
                f"{t(user_id, 'area_label')}: {area}\n"
                f"{t(user_id, 'growth_label')}: +{growth}%\n\n"
            )

        await message.answer(response)
        return

    if text == t(user_id, "top_roi_btn"):
        cursor.execute("""
        SELECT name, area, roi
        FROM buildings
        ORDER BY roi DESC
        LIMIT 5
        """)

        response = t(user_id, "top_roi_title")

        for name, area, roi in cursor.fetchall():
            response += (
                f"🏢 <b>{name}</b>\n"
                f"{t(user_id, 'area_label')}: {area}\n"
                f"{t(user_id, 'roi_label')}: {roi}%\n\n"
            )

        await message.answer(response)
        return

    if text == t(user_id, "building_search"):
        await message.answer(t(user_id, "building_prompt"))
        return

    if text == t(user_id, "settings_btn"):
        await message.answer(t(user_id, "settings"), reply_markup=language_menu())
        return

    search_text = text.lower()

    cursor.execute("""
    SELECT name, area, avg_price, roi, sqft, growth, deals
    FROM buildings
    WHERE lower(name) LIKE ?
       OR lower(area) LIKE ?
    """, ("%" + search_text + "%", "%" + search_text + "%"))

    results = cursor.fetchall()

    if not results:
        await message.answer(t(user_id, "not_found"))
        return

    if len(results) == 1:
        name, area, avg_price, roi, sqft, growth, deals = results[0]

        await message.answer(
            f"🏢 <b>{name}</b>\n\n"
            f"{t(user_id, 'area_label')}: {area}\n"
            f"{t(user_id, 'avg_price_label')}: {avg_price}\n"
            f"{t(user_id, 'roi_label')}: {roi}%\n"
            f"{t(user_id, 'sqft_label')}: {sqft}\n"
            f"{t(user_id, 'growth_label')}: +{growth}%\n"
            f"{t(user_id, 'transactions_label')}: {deals}\n\n"
            f"{t(user_id, 'analytics_loaded')}"
        )
        return

    response = t(user_id, "multiple_found")

    for name, area, avg_price, roi, sqft, growth, deals in results:
        response += (
            f"🏢 <b>{name}</b>\n"
            f"{t(user_id, 'area_label')}: {area}\n"
            f"{t(user_id, 'avg_price_label')}: {avg_price}\n"
            f"{t(user_id, 'roi_label')}: {roi}%\n"
            f"{t(user_id, 'growth_label')}: +{growth}%\n\n"
        )

    await message.answer(response)


async def main():
    print("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
