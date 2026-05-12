from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message
from aiogram.filters import CommandStart
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


@dp.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "🏙 Dubai DLD Analytics Bot is running successfully."
    )


async def main():
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
