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
import sys
from typing import NoReturn

from binocore.db import CONNECT_RETRYABLE, connect_with_retry

from settings.database_config import (DB_NAMES, init_json_codec, pg_host,
                                      pg_password, pg_port, pg_user)


def _retry_note(attempt: int, retries: int, error: Exception) -> None:
    """Сообщить о повторе стартового коннекта.

    print, а не logger: сюда попадают ДО настройки логирования (bootstrap зовётся с импорта
    settings), поэтому в logs/ такая строка не легла бы вовсе. stdout юнита читает journald —
    там её и видно. Реестр BinoCore: bootstrap-connect-retry.
    """
    print(f'bootstrap: БД недоступна, попытка {attempt}/{retries} ({error}) — повтор',
          file=sys.stderr)


async def _connect(db: str):
    conn = await connect_with_retry(
        user=pg_user, password=pg_password, host=pg_host, port=pg_port,
        database=DB_NAMES[db], statement_cache_size=0,
        timeout=10,           # таймаут установки соединения
        command_timeout=15,   # таймаут самого запроса — не зависнуть на старте навсегда
        on_retry=_retry_note,   # повтор виден в journald: логгера тут ещё нет
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


def _fatal_bootstrap(db: str, error: Exception) -> NoReturn:
    """Отказ СТАРТОВОГО чтения конфига: сообщить оператору и выйти осмысленным кодом.

    Зачем здесь, а не на местах вызова: чтений на импорте несколько (селекторы TV, селекторы
    binodex, инстанс-настройки, креды бота), и обёртка по одной на вызов себя уже показала —
    ревизия 16-09-2026 нашла обёрнутым ОДНО чтение из четырёх, а остальные падали голым
    traceback мимо settings/fatal.py: код 1, диспетчер поднимает, падает снова, и ни одной
    строки оператору. Здесь точка одна на все чтения, сколько бы их ни добавили потом.

    Код выхода разный, потому что отказы разной природы:
      • БД недоступна (CONNECT_RETRYABLE — тот же список, по которому коннект уже отретраил
        пять раз): PgBouncer флапнул, Patroni переключил лидера. Это рассосётся само, поэтому
        код 1 «краш, рестартани меня» — так же, как рантайм отвечает на ту же беду
        (ProgramRestart), и диспетчер поднимет процесс заново;
      • подключились, но запрос не прошёл (нет таблицы/колонки, синтаксис, права) — само не
        поправится: fatal_exit → EXIT_SETUP (12) «нужен человек», диспетчер ждать не станет.

    Импорт fatal ЛОКАЛЬНЫЙ, а не модульный: нас зовут из середины импорта settings, и
    settings.fatal тянет settings.constant — модульный импорт замкнул бы круг.
    """
    from settings.fatal import fatal_exit, notify_fatal
    where = f'стартовое чтение конфига из БД «{db}»'
    if isinstance(error, CONNECT_RETRYABLE):
        notify_fatal(f'{where}: БД недоступна после повторов — {type(error).__name__}: {error}. '
                     f'Выхожу кодом 1: отказ транзиентный, диспетчер поднимет заново')
        raise SystemExit(1)
    fatal_exit(f'{where} не удалось — {type(error).__name__}: {error}')


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
    except (Exception,) as error:
        _fatal_bootstrap(db, error)   # не возвращается: SystemExit 1 либо 12
    finally:
        loop.close()


def bootstrap_fetch(db: str, sql: str, *args, fetch_mode: str = 'all'):
    """Синхронно (блокирующе) выполнить ОДИН запрос на старте. db — 'program' | 'binodex'.
    fetch_mode — 'row' | 'val' | 'all'."""
    return bootstrap_fetch_many(db, [(sql, args, fetch_mode)])[0]
