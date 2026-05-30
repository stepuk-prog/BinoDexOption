"""Блокирующее чтение из БД на import-time.

Пулы asyncpg ещё не созданы (их поднимает main.py в asyncio.run), поэтому здесь —
одноразовые `asyncpg.connect()` со `statement_cache_size=0` (обязательно для
PgBouncer transaction mode) и json/jsonb codec (cookies/translocation хранятся
как jsonb — иначе asyncpg вернул бы str, а не list/dict). Через собственный
`new_event_loop()`, а НЕ `asyncio.run()`: последний на выходе делает
`set_event_loop(None)`, после чего логгеры с `get_event_loop()` падают.

Используется settings/config.py (option_setting + telegram + cookies) и
settings/browser_config.py (tv_settings/pocket_settings) — общий хелпер вместо
дублей. Намеренно НЕ импортирует logs/config — иначе циклический импорт.
"""
import asyncio
import json

import asyncpg

from settings.database_config import (pg_host, pg_name, pg_name_fin,
                                      pg_password, pg_port, pg_user)

_DB_NAMES = {'program': pg_name, 'binodex': pg_name_fin}


async def _init_connection(conn: asyncpg.Connection):
    for t in ('json', 'jsonb'):
        await conn.set_type_codec(
            t, encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
        )


async def _fetch(db: str, sql: str, args, fetch_mode: str):
    conn = await asyncpg.connect(
        user=pg_user, password=pg_password, host=pg_host, port=pg_port,
        database=_DB_NAMES[db], statement_cache_size=0, timeout=10,
    )
    await _init_connection(conn)
    try:
        if fetch_mode == 'row':
            return await conn.fetchrow(sql, *args)
        if fetch_mode == 'val':
            return await conn.fetchval(sql, *args)
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def bootstrap_fetch(db: str, sql: str, *args, fetch_mode: str = 'all'):
    """Синхронно (блокирующие) выполнить запрос на старте. db — 'program' | 'binodex'.
    fetch_mode — 'row' | 'val' | 'all'."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch(db, sql, args, fetch_mode))
    finally:
        loop.close()
