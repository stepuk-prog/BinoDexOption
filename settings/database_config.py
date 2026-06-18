import json
import os

import asyncpg
from dotenv import load_dotenv

from settings.env import req_str, opt_int

load_dotenv(override=False)  # override=False — не перетирать per-instance env (TIMEFRAME/BINARY от systemd)


pg_name = req_str("DATABASE")
pg_user = req_str("PG_USER")
pg_password = req_str("PG_PASSWORD")
pg_host = req_str("PG_HOST")
pg_port = opt_int("PG_PORT", 5432)  # дефолт стандартного порта PG — не падать на старте, если env не задан
# Отдельная база с данными опционов (сигналы FIN/OTC). Те же host/port/логин,
# отличается только именем базы. Настройки браузера и cookies остаются в pg_name.
pg_name_fin = os.getenv("DATABASE_FIN", "binodex")

# Ключи пулов → имена БД. 'program' — Program, 'binodex' — база данных опционов.
# Общий словарь и codec для database.py (рантайм-пулы) и _bootstrap.py (старт) —
# чтобы не расходились две копии.
DB_NAMES = {'program': pg_name, 'binodex': pg_name_fin}


async def init_json_codec(conn: asyncpg.Connection):
    """json/jsonb codec — иначе asyncpg отдаёт их как str (psycopg2 парсил сам)."""
    for t in ('json', 'jsonb'):
        await conn.set_type_codec(
            t, encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
        )
