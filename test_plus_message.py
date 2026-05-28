"""
Тестовая отправка одного из plus_N_message в «Избранное» (Saved Messages).
Использует Pyrogram (TEST=true → аккаунт Fat из .env). БД нужна только для импорта
settings/config.py — пишущих запросов скрипт не делает.

Запуск:
    .venv/bin/python test_plus_message.py        # plus5_message по умолчанию
    .venv/bin/python test_plus_message.py 15     # plus15_message
    .venv/bin/python test_plus_message.py 50     # plus50_message
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()

from messages.message import (
    plus5_message, plus10_message, plus15_message, plus20_message, plus25_message,
    plus30_message, plus35_message, plus40_message, plus45_message, plus50_message,
)

PLUS_FUNCS = {
    5: plus5_message, 10: plus10_message, 15: plus15_message, 20: plus20_message,
    25: plus25_message, 30: plus30_message, 35: plus35_message, 40: plus40_message,
    45: plus45_message, 50: plus50_message,
}


async def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if count not in PLUS_FUNCS:
        print(f"❌ Допустимые значения: {sorted(PLUS_FUNCS)}")
        sys.exit(1)

    api_id = int(os.getenv("TEST_API_ID"))
    api_hash = os.getenv("TEST_API_HASH")
    session_file = os.getenv("TEST_SESSION_FILE")

    app = Client(name=session_file, api_id=api_id, api_hash=api_hash)
    async with app:
        text = PLUS_FUNCS[count]()
        await app.send_message(chat_id="me", text=text)
        me = await app.get_me()
        print(f"✅ Отправлено plus{count}_message в Saved Messages аккаунта "
              f"{me.first_name} (@{me.username or me.id})")


if __name__ == "__main__":
    asyncio.run(main())
