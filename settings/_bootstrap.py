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

import asyncpg

from settings.database_config import (DB_NAMES, init_json_codec, pg_host,
                                      pg_password, pg_port, pg_user)


async def _connect(db: str):
    conn = await asyncpg.connect(
        user=pg_user, password=pg_password, host=pg_host, port=pg_port,
        database=DB_NAMES[db], statement_cache_size=0,
        timeout=10,           # таймаут установки соединения
        command_timeout=15,   # таймаут самого запроса — не зависнуть на старте навсегда
    )
    await init_json_codec(conn)
    return conn


async def _run_one(conn, sql: str, args, fetch_mode: str):
    if fetch_mode == 'row':
        return await conn.fetchrow(sql, *args)
    if fetch_mode == 'val':
        return await conn.fetchval(sql, *args)
    return await conn.fetch(sql, *args)


async def _fetch_many(db: str, queries):
    conn = await _connect(db)
    try:
        return [await _run_one(conn, sql, args, mode) for sql, args, mode in queries]
    finally:
        await conn.close()


def bootstrap_fetch_many(db: str, queries):
    """Несколько стартовых запросов к ОДНОЙ базе за ОДИН коннект.

    `queries` — последовательность (sql, args, fetch_mode); результаты возвращаются в том же
    порядке. Каждый bootstrap_fetch — отдельный asyncpg.connect через PgBouncer плюс свой
    event loop. Зависимые запросы (сначала узнать id, потом строку по нему) по-прежнему идут
    отдельными вызовами — объединяется только то, что известно разом.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_fetch_many(db, list(queries)))
    finally:
        loop.close()


def bootstrap_fetch(db: str, sql: str, *args, fetch_mode: str = 'all'):
    """Синхронно (блокирующе) выполнить ОДИН запрос на старте. db — 'program' | 'binodex'.
    fetch_mode — 'row' | 'val' | 'all'."""
    return bootstrap_fetch_many(db, [(sql, args, fetch_mode)])[0]
