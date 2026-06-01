import json
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    """Обязательная str-переменная окружения — понятная ошибка на старте вместо
    криптичного падения asyncpg.connect(None) где-то в глубине."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Не задана обязательная переменная окружения {name}")
    return value


pg_name = _require("DATABASE")
pg_user = _require("PG_USER")
pg_password = _require("PG_PASSWORD")
pg_host = _require("PG_HOST")
pg_port = int(os.getenv("PG_PORT", "5432"))  # дефолт стандартного порта PG — не падать на старте, если env не задан
# Отдельная база с данными опционов (сигналы FIN/OTC). Те же host/port/логин,
# отличается только именем базы. Настройки браузера и cookies остаются в pg_name.
pg_name_fin = os.getenv("DATABASE_FIN", "binodex")

# Ключи пулов → имена БД. 'program' — Program, 'binodex' — база данных опционов.
# Общий словарь и codec для postgres.py (рантайм-пулы) и _bootstrap.py (старт) —
# чтобы не расходились две копии.
DB_NAMES = {'program': pg_name, 'binodex': pg_name_fin}


async def init_json_codec(conn: asyncpg.Connection):
    """json/jsonb codec — иначе asyncpg отдаёт их как str (psycopg2 парсил сам)."""
    for t in ('json', 'jsonb'):
        await conn.set_type_codec(
            t, encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
        )
