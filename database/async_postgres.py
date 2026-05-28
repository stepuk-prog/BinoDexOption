"""
Асинхронное подключение к PostgreSQL через asyncpg с пулом соединений.
Поддержка PgBouncer, автоматическое переподключение.
"""

import asyncio
from typing import Any

import asyncpg
from asyncpg import Pool
from asyncpg.exceptions import InterfaceError, CannotConnectNowError, ConnectionDoesNotExistError

from logs import init_logger
from settings.database_config import pg_name, pg_name_fin, pg_user, pg_password, pg_host, pg_port

logger = init_logger(__name__)


class AsyncDatabase:
    """Асинхронный класс для работы с PostgreSQL через asyncpg с пулом соединений."""

    def __init__(self, min_size: int = 2, max_size: int = 10):
        """
        Инициализация пула соединений.
        :param min_size: Минимальное количество соединений в пуле.
        :param max_size: Максимальное количество соединений в пуле.
        """
        self.min_size = min_size
        self.max_size = max_size
        # Основной пул (pg_name) — настройки, счётчики, cookies, программа.
        self.pool: Pool | None = None
        # Пул базы с данными опционов (pg_name_fin) — сигналы FIN/OTC.
        self.data_pool: Pool | None = None

    async def _create_pool(self, database: str, retries: int, delay: float) -> Pool:
        """Создание одного пула соединений с повторными попытками."""
        for attempt in range(1, retries + 1):
            try:
                pool = await asyncpg.create_pool(  # type: ignore[misc]
                    user=pg_user,
                    password=pg_password,
                    host=pg_host,
                    port=pg_port,
                    database=database,
                    min_size=self.min_size,
                    max_size=self.max_size,
                    statement_cache_size=0  # Для PgBouncer
                )
                logger.info(f"✅ Пул соединений '{database}' создан (min={self.min_size}, max={self.max_size}).")
                return pool
            except (asyncpg.CannotConnectNowError, ConnectionRefusedError, OSError) as error:
                logger.warning(f"⚠️ Попытка {attempt}/{retries} ('{database}') не удалась: {error}")
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)
                else:
                    logger.error(f"❌ Не удалось создать пул соединений '{database}' после всех попыток.")
                    raise

    async def connect(self, retries: int = 5, delay: float = 2.0):
        """
        Создание пулов соединений с повторными попытками.
        :param retries: Количество попыток подключения.
        :param delay: Задержка между попытками (секунды).
        """
        self.pool = await self._create_pool(pg_name, retries, delay)
        self.data_pool = await self._create_pool(pg_name_fin, retries, delay)

    async def close(self):
        """Закрытие пулов соединений."""
        for pool in (self.pool, self.data_pool):
            if pool:
                await pool.close()
        self.pool = None
        self.data_pool = None
        logger.info("Пулы соединений закрыты.")

    async def _ensure_pool(self):
        """Убедиться, что оба пула соединений существуют."""
        if self.pool is None or self.data_pool is None:
            logger.warning("⚠️ Пул отсутствует, пересоздаём...")
            await self.close()
            await self.connect()

    async def execute_query(
        self,
        sql: str,
        *args,
        retries: int = 3,
        delay: float = 2.0,
        fetch_mode: str = "all",
        func: str = "unknown",
        use_data_pool: bool = False
    ) -> Any:
        """
        Универсальный метод для выполнения SQL-запросов.
        :param sql: Строка SQL-запроса (используйте $1, $2 для параметров).
        :param args: Аргументы для подстановки в запрос.
        :param retries: Количество попыток выполнения.
        :param delay: Задержка между попытками (секунды).
        :param fetch_mode: Режим выполнения запроса:
            - "row" → fetchrow() (одна строка)
            - "all" → fetch() (все строки)
            - "val" → fetchval() (одно значение)
            - "execute" → execute() (без возврата данных, для UPDATE/INSERT)
        :param func: Название функции для логирования.
        :param use_data_pool: True — выполнять в базе с данными опционов (pg_name_fin).
        :return: Результат выполнения запроса или False при ошибке.
        """
        await self._ensure_pool()

        for attempt in range(1, retries + 1):
            try:
                # Получаем соединение из нужного пула
                pool = self.data_pool if use_data_pool else self.pool
                async with pool.acquire() as connection:
                    if fetch_mode == "row":
                        return await connection.fetchrow(sql, *args)
                    elif fetch_mode == "val":
                        return await connection.fetchval(sql, *args)
                    elif fetch_mode == "all":
                        return await connection.fetch(sql, *args)
                    elif fetch_mode == "execute":
                        await connection.execute(sql, *args)
                        return True
                    else:
                        logger.error(f"Некорректный fetch_mode: {fetch_mode}")
                        return False

            except (InterfaceError, CannotConnectNowError, ConnectionDoesNotExistError) as error:
                logger.warning(
                    f"⚠️ Ошибка соединения при обработке {func} "
                    f"(попытка {attempt}/{retries}): {error}"
                )
                if attempt < retries:
                    logger.info(f"🔄 Пересоздаём пул (попытка {attempt}/{retries})...")
                    await self.close()
                    await asyncio.sleep(delay * attempt)
                    await self.connect()
                else:
                    logger.error("❌ Не удалось восстановить пул после всех попыток.")
                    return False

            except (Exception,) as e:
                error_str = str(e)
                # Ошибки PgBouncer, которые можно восстановить
                pgbouncer_recoverable_errors = [
                    "got result for unknown protocol state",
                    "client_login_timeout",
                    "server closed the connection unexpectedly",
                    "terminating connection due to administrator command"
                ]
                if any(msg in error_str for msg in pgbouncer_recoverable_errors):
                    logger.warning(f"🔁 PgBouncer: {error_str} — пересоздаём пул...")
                    try:
                        await self.close()
                        await self.connect()
                        logger.info("✅ Пул пересоздан успешно.")
                    except (Exception,) as conn_err:
                        logger.error(f"❌ Ошибка при пересоздании пула: {conn_err}")
                        return False
                    if attempt < retries:
                        await asyncio.sleep(delay * attempt)
                        continue
                    else:
                        logger.error("❌ Не удалось восстановить пул после PgBouncer-ошибки.")
                        return False

                # Любая другая ошибка
                logger.error(f"🚨 Непредвиденная ошибка при выполнении {func}: {error_str}")
                return False

        return None

    # Методы для работы с опционами _______________________________________________________________

    async def option_data_pocket(self, tf: str, exclude_ids: list) -> list | bool:
        """
        Загрузка данных OTC с фильтром по timeframe и исключёнными val_id.
        :param tf: Таймфрейм
        :param exclude_ids: Список исключённых val_id
        :return: Список записей или False
        """
        sql = '''
            SELECT * FROM option_data.otc_data_view
            WHERE timeframe = $1 AND val_id != ALL($2)
            ORDER BY otc_percent DESC, itog_stat_up DESC
        '''
        return await self.execute_query(sql, tf, exclude_ids, fetch_mode='all', func='option_data_pocket',
                                        use_data_pool=True)

    async def option_data_tv(self, tf: str, exclude_ids: list) -> list | bool:
        """
        Поиск данных для опциона Binary.
        :param tf: Таймфрейм
        :param exclude_ids: Список использованных ранее пар
        :return: Список записей или False
        """
        sql = '''
            SELECT * FROM option_data.binary_data_view
            WHERE timeframe = $1 AND val_id != ALL($2)
            ORDER BY strong DESC, binary_percent DESC, itog_stat_up DESC
        '''
        return await self.execute_query(sql, tf, exclude_ids, fetch_mode='all', func='option_data_tv',
                                        use_data_pool=True)

    # Счётчики ____________________________________________________________________________________

    async def plus_counter(self, program_id: int) -> dict | bool | None:
        """
        Инкремент счётчика плюсов экземпляра в binodex.option_data.counter.
        Ключ — program_id (своя строка программы), сброс серии минусов.
        :param program_id: id программы (option_setting.program_id)
        :return: Результат с полем 'plus' или False/None
        """
        sql = '''
            UPDATE option_data.counter
            SET plus = plus + 1, minus = 0
            WHERE program_id = $1
            RETURNING plus
        '''
        return await self.execute_query(sql, program_id, fetch_mode='row', func='plus_counter',
                                        use_data_pool=True)

    async def minus_counter(self, program_id: int) -> dict | bool | None:
        """
        Инкремент счётчика минусов экземпляра в binodex.option_data.counter.
        Ключ — program_id (своя строка программы), сброс серии плюсов.
        :param program_id: id программы (option_setting.program_id)
        :return: Результат с полем 'minus' или False/None
        """
        sql = '''
            UPDATE option_data.counter
            SET plus = 0, minus = minus + 1
            WHERE program_id = $1
            RETURNING minus
        '''
        return await self.execute_query(sql, program_id, fetch_mode='row', func='minus_counter',
                                        use_data_pool=True)

    # Настройки ___________________________________________________________________________________

    async def tv_setting(self) -> list | bool:
        """
        Загрузка настроек для браузера TradingView.
        :return: Список настроек или False
        """
        sql = "SELECT * FROM settings.tv_settings"
        return await self.execute_query(sql, fetch_mode='all', func='tv_setting')

    async def otc_setting(self) -> list | bool:
        """
        Загрузка настроек для браузера Pocket.
        :return: Список настроек или False
        """
        sql = "SELECT * FROM settings.pocket_settings"
        return await self.execute_query(sql, fetch_mode='all', func='otc_setting')

    async def option_setting(self, timeframe: str, binary: bool, program: str) -> dict | None:
        """
        Оркестрация настроек экземпляра поверх двух физических БД.
        База берётся из binodex (option_setting_base), креды юзербота — из Program
        (telegram_creds), затем склеиваются в один dict. Заменяет кросс-БД вью
        settings.option_setting_view, который между двумя БД не работает.
        :param timeframe: Таймфрейм
        :param binary: True для обычных валютных пар
        :param program: ключ программы (PROG_KEY) — фильтр своих строк
        :return: dict с настройками и api_id/api_hash, либо None
        """
        option = await self.option_setting_base(timeframe, binary, program)
        if not option:
            logger.error(f"Не найдены настройки option_setting для tf={timeframe}, binary={binary}, program={program}")
            return None

        creds = await self.telegram_creds(id_telegram=option['user_bot'])
        if not creds:
            logger.error(f"Не найден юзербот id_telegram={option['user_bot']} в telegram.telegram")
            return None

        merged = dict(option)
        merged['api_id'] = creds['api_id']
        merged['api_hash'] = creds['api_hash']
        return merged

    async def option_setting_base(self, timeframe: str, binary: bool, program: str) -> dict | bool | None:
        """
        Базовые настройки экземпляра из базы данных опционов (pg_name_fin).
        Без джойнов на cookies/telegram — те данные добираются отдельно из Program
        (см. telegram_creds). Кросс-БД JOIN между binodex и Program невозможен.
        settings.option_setting общая для нескольких программ → отбираем свои по program.
        :param timeframe: таймфрейм
        :param binary: True — опционы на обычных валютных парах
        :param program: ключ программы (PROG_KEY) — фильтр своих строк
        :return: строка settings.option_setting либо None
        """
        sql = ('SELECT * FROM settings.option_setting '
               'WHERE timeframe = $1 AND "binary" = $2 AND program = $3')
        return await self.execute_query(sql, timeframe, binary, program, fetch_mode='row',
                                        func='option_setting_base', use_data_pool=True)

    async def telegram_creds(self, id_telegram: int) -> dict | bool | None:
        """
        Креды юзербота из Program — telegram.telegram не переносим.
        :param id_telegram: id юзербота (option_setting.user_bot)
        :return: строка с api_id, api_hash, session_string либо None
        """
        sql = "SELECT api_id, api_hash, session_string FROM telegram.telegram WHERE id_telegram = $1"
        return await self.execute_query(sql, id_telegram, fetch_mode='row', func='telegram_creds')

    async def pages_setting(self, timeframe: str) -> list | bool:
        """
        Поиск страниц для браузера TradingView.
        :param timeframe: Таймфрейм
        :return: Список страниц или False
        """
        sql = '''
            SELECT cp.* FROM cookies.cook_page cp
            JOIN settings.option_setting os ON cp.user_id = os.cookies_tv
            WHERE os.timeframe = $1 AND os.binary = true
            ORDER BY cp.page_id
        '''
        return await self.execute_query(sql, timeframe, fetch_mode='all', func='pages_setting')

    async def close_program(self, program_id: int) -> bool:
        """
        Установка выключения программы для корректной работы с диспетчером.
        :param program_id: ID программы
        :return: True при успехе, False при ошибке
        """
        sql = "UPDATE program.programdata SET status = false WHERE program_id = $1"
        return await self.execute_query(sql, program_id, fetch_mode='execute', func='close_program')
