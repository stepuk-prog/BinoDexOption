import json
import os

import asyncpg
from dotenv import load_dotenv

load_dotenv()
pg_name = os.getenv("DATABASE")
pg_user = os.getenv("PG_USER")
pg_password = os.getenv("PG_PASSWORD")
pg_host = os.getenv("PG_HOST")
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
