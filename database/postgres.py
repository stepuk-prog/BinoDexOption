"""
Единый асинхронный интерфейс к PostgreSQL через asyncpg.

Два пула на одном PG-инстансе (общий PgBouncer, отличается только имя БД):
  'program' (pg_name)      — program.programdata (статус диспетчеру).
  'binodex' (pg_name_fin)  — данные опционов (option_data.*), счётчики,
                             cookies.pages.
Настройки/креды/cookies, нужные на старте, читает settings/_bootstrap.py
(одноразовые коннекты ДО создания этих пулов).
"""
import asyncio
from typing import Awaitable, cast

import asyncpg
from asyncpg.exceptions import (CannotConnectNowError, ConnectionDoesNotExistError,
                                InterfaceError)

from logs import init_logger
from settings.database_config import (DB_NAMES, init_json_codec, pg_user,
                                      pg_password, pg_host, pg_port)

logger = init_logger(__name__)

_PGBOUNCER_RECOVERABLE = (
    "got result for unknown protocol state",
    "client_login_timeout",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "canceling statement due to",
)

# Таймаут ожидания свободного соединения из пула (правило: не зависать).
_ACQUIRE_TIMEOUT = 30


class Database:
    """Асинхронный класс для работы с PostgreSQL через asyncpg с пулами соединений."""

    def __init__(self, min_size: int = 2, max_size: int = 10):
        self.min_size = min_size
        self.max_size = max_size
        # Per-pool state. Ключи — 'program' / 'binodex'.
        self._pools: dict[str, asyncpg.Pool | None] = {'program': None, 'binodex': None}
        self._pool_locks: dict[str, asyncio.Lock] = {
            'program': asyncio.Lock(), 'binodex': asyncio.Lock(),
        }

    async def _connect_pool(self, name: str, retries: int = 5, delay: float = 2.0):
        db_name = DB_NAMES[name]
        for attempt in range(1, retries + 1):
            try:
                pool_factory = cast(Awaitable[asyncpg.Pool], asyncpg.create_pool(
                    user=pg_user, password=pg_password, host=pg_host, port=pg_port,
                    database=db_name,
                    min_size=self.min_size, max_size=self.max_size,
                    statement_cache_size=0,   # обязательно для PgBouncer transaction mode
                    timeout=10,
                    command_timeout=30,       # не зависать на мёртвом соединении
                    init=init_json_codec,
                ))
                self._pools[name] = await pool_factory
                logger.info(f"✅ Пул '{name}' (→ {db_name}) создан "
                            f"(min={self.min_size}, max={self.max_size})")
                return
            except (CannotConnectNowError, ConnectionRefusedError, OSError,
                    TimeoutError, asyncio.TimeoutError) as error:
                logger.warning(f"⚠️ Попытка {attempt}/{retries} пула '{name}': {error}")
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                else:
                    logger.error(f"❌ Не удалось создать пул '{name}' после всех попыток")
                    raise

    async def connect(self, retries: int = 5, delay: float = 2.0):
        """Поднимает оба пула. Идемпотентно (уже поднятый пул не пересоздаём — иначе
        утечка). При частичном сбое (один пул поднялся, второй упал) закрываем всё
        перед пробросом — не оставляем висящий пул/соединения к PgBouncer."""
        try:
            for name in ('program', 'binodex'):
                if self._pools[name] is not None:
                    continue
                await self._connect_pool(name, retries=retries, delay=delay)
        except (Exception,):
            await self.close()
            raise

    async def close(self):
        for name, pool in list(self._pools.items()):
            if pool is not None:
                try:
                    await pool.close()
                    logger.info(f"Пул '{name}' закрыт")
                except (Exception,) as error:
                    logger.warning(f"Ошибка закрытия пула '{name}': {error}")
                self._pools[name] = None

    async def _recreate_pool(self, name: str):
        async with self._pool_locks[name]:
            pool = self._pools[name]
            if pool is not None:
                try:
                    async with pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                    return
                except (Exception,):
                    pass
            try:
                if pool is not None:
                    await pool.close()
            except (Exception,):
                pass
            self._pools[name] = None
            logger.warning(f"Пересоздаю пул '{name}'")
            await self._connect_pool(name)

    async def execute_query(self, sql: str, *args, retries: int = 3, delay: float = 2.0,
                            fetch_mode: str = "all", func: str = "",
                            db: str = "program"):
        """Контракт: при ОШИБКЕ → False (нет пула / retry exhaust / неизвестный
        fetch_mode / непредвиденная). При успехе — результат: list ('all'),
        Record|None ('row'), значение|None ('val'), True ('execute'). None из
        'row'/'val' = «строки нет», False = «сбой» (их можно различать).
        `db` выбирает пул: 'program' (по умолчанию) или 'binodex'."""
        pool = self._pools.get(db)
        if pool is None:
            logger.error(f"Пул '{db}' не создан — {func} невозможен")
            return False
        for attempt in range(1, retries + 1):
            try:
                async with pool.acquire(timeout=_ACQUIRE_TIMEOUT) as conn:
                    if fetch_mode == "row":
                        return await conn.fetchrow(sql, *args)
                    if fetch_mode == "val":
                        return await conn.fetchval(sql, *args)
                    if fetch_mode == "all":
                        return await conn.fetch(sql, *args)
                    if fetch_mode == "execute":
                        await conn.execute(sql, *args)
                        return True
                    logger.error(f"Некорректный fetch_mode: {fetch_mode}")
                    return False
            except (InterfaceError, CannotConnectNowError, ConnectionDoesNotExistError,
                    TimeoutError, asyncio.TimeoutError) as error:
                logger.warning(f"Соединение пула '{db}' разорвано в {func} ({attempt}/{retries}): {error}")
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                    continue
                logger.error(f"{func}: пересоздаю пул '{db}'")
                try:
                    await self._recreate_pool(db)
                except (Exception,) as pool_error:
                    logger.error(f"Не удалось пересоздать пул '{db}': {pool_error}")
                return False
            except (Exception,) as error:
                msg = str(error)
                if any(m in msg for m in _PGBOUNCER_RECOVERABLE):
                    logger.warning(f"PgBouncer ({db}): {msg} — retry {attempt}/{retries} ({func})")
                    if attempt < retries:
                        await asyncio.sleep(delay * attempt)
                        continue
                    try:
                        await self._recreate_pool(db)
                    except (Exception,) as pool_error:
                        logger.error(f"Не удалось пересоздать пул '{db}': {pool_error}")
                    return False
                logger.error(f"Непредвиденная SQL-ошибка в {func} (пул '{db}'): {msg}")
                return False
        return False

    # -------------------- SQL API --------------------
    # Данные опционов, счётчики, cookies.pages — в БД binodex (db='binodex').

    async def option_data_pocket(self, tf: str, exclude_ids: list):
        """Данные OTC с фильтром по timeframe и исключёнными val_id."""
        sql = '''
            SELECT * FROM option_data.otc_data_view
            WHERE timeframe = $1 AND val_id != ALL($2)
            ORDER BY otc_percent DESC, itog_stat_up DESC
        '''
        return await self.execute_query(sql, tf, exclude_ids, fetch_mode='all',
                                        func='option_data_pocket', db='binodex')

    async def option_data_tv(self, tf: str, exclude_ids: list):
        """Данные Binary (TradingView) с фильтром по timeframe и исключёнными val_id."""
        sql = '''
            SELECT * FROM option_data.binary_data_view
            WHERE timeframe = $1 AND val_id != ALL($2)
            ORDER BY strong DESC, binary_percent DESC, itog_stat_up DESC
        '''
        return await self.execute_query(sql, tf, exclude_ids, fetch_mode='all',
                                        func='option_data_tv', db='binodex')

    async def plus_counter(self, program_id: int):
        """Инкремент счётчика плюсов экземпляра в binodex.option_data.counter.
        Ключ — program_id (своя строка программы), сброс серии минусов."""
        sql = '''
            UPDATE option_data.counter
            SET plus = plus + 1, minus = 0
            WHERE program_id = $1
            RETURNING plus
        '''
        return await self.execute_query(sql, program_id, fetch_mode='row',
                                        func='plus_counter', db='binodex')

    async def minus_counter(self, program_id: int):
        """Инкремент счётчика минусов экземпляра в binodex.option_data.counter.
        Ключ — program_id (своя строка программы), сброс серии плюсов."""
        sql = '''
            UPDATE option_data.counter
            SET plus = 0, minus = minus + 1
            WHERE program_id = $1
            RETURNING minus
        '''
        return await self.execute_query(sql, program_id, fetch_mode='row',
                                        func='minus_counter', db='binodex')

    async def pages(self, program: str, mode: str):
        """Страницы браузера из общей binodex.cookies.pages по (program, mode),
        ORDER BY order_idx (description='main' — первой, idx 0)."""
        sql = ("SELECT * FROM cookies.pages "
               "WHERE program = $1 AND mode = $2 ORDER BY order_idx")
        return await self.execute_query(sql, program, mode, fetch_mode='all',
                                        func='pages', db='binodex')

    async def close_program(self, program_id: int):
        """status=false в program.programdata (Program) — сигнал диспетчеру, что
        программа штатно остановлена и не должна перезапускаться до вмешательства."""
        sql = "UPDATE program.programdata SET status = false WHERE program_id = $1"
        return await self.execute_query(sql, program_id, fetch_mode='execute',
                                        func='close_program', db='program')
