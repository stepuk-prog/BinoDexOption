import time

import psycopg2
import psycopg2.extras
from psycopg2 import OperationalError, InterfaceError
from psycopg2.extras import RealDictCursor
from logs import init_logger
from settings.database_config import pg_name, pg_user, pg_password, pg_host, pg_port

logger = init_logger(__name__)


class Database:
    def __init__(self, max_retries=5, retry_delay=2):
        """
        Инициализация соединения с БД через PgBouncer.
        :param max_retries: Количество попыток подключения
        :param retry_delay: задержка между попытками
        """
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.connection = self.connect_to_db()

    def connect_to_db(self):
        """Подключение к БД с повторными попытками"""
        retries = 0
        while retries < self.max_retries:
            try:
                conn = psycopg2.connect(
                    database=pg_name,
                    user=pg_user,
                    password=pg_password,
                    host=pg_host,
                    port=pg_port
                )
                logger.info("✅ Подключение к БД через PgBouncer установлено.")
                return conn
            except OperationalError as e:
                logger.warning(f"⚠️ Ошибка подключения к БД: {e}")
                retries += 1
                if retries < self.max_retries:
                    time.sleep(self.retry_delay)
                else:
                    logger.error("❌ Не удалось подключиться к БД после всех попыток.")
                    raise
        return None

    def execute_query_with_retries(self, sql, values=None, retries=3, delay=2, commit=False, fetch_mode="all"):
        for attempt in range(1, retries + 1):
            try:
                # 🛠 Переподключение, если соединение закрыто
                if self.connection is None or self.connection.closed != 0:
                    logger.warning("🔄 Соединение с БД закрыто. Переподключаемся...")
                    self.connection = self.connect_to_db()

                with self.connection.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, values)

                    if fetch_mode == "row":
                        return cur.fetchone()
                    elif fetch_mode == "val":
                        result = cur.fetchone()
                        if commit:
                            self.connection.commit()
                        return result if result else None
                    elif fetch_mode == "all":
                        return cur.fetchall()
                    elif fetch_mode == "execute":
                        if commit:
                            self.connection.commit()
                        return True
                    else:
                        logger.error(f"Неверный режим fetch_mode: {fetch_mode}")
                        return None

            except (InterfaceError, OperationalError) as e:
                logger.warning(f"🔁 Ошибка соединения с БД: {e}. Попытка переподключения...")
                recoverable = [
                    "got result for unknown protocol state",
                    "client_login_timeout",
                    "server closed the connection unexpectedly",
                    "terminating connection due to administrator command",
                    "Software caused connection abort"
                ]
                if any(msg in str(e) for msg in recoverable):
                    logger.warning(f"🔁 Ошибка с PgBouncer: {e}. Переподключаемся...")
                    try:
                        self.connection = self.connect_to_db()
                    except (Exception,) as conn_err:
                        logger.error(f"❌ Не удалось переподключиться после ошибки PgBouncer: {conn_err}")
                        return False
                    if attempt < retries:
                        time.sleep(delay * attempt)
                        continue
                    else:
                        logger.error("❌ Не удалось восстановить соединение после всех попыток.")
                        return False
                else:
                    logger.error(f"❌ Ошибка при выполнении запроса: {e}")
                    self._safe_rollback()
                    return False

            except (Exception,) as e:
                logger.error(f"❌ Ошибка при выполнении запроса: {e}")
                self._safe_rollback()
                return False
        return None

    def _safe_rollback(self):
        try:
            if self.connection and self.connection.closed == 0:
                self.connection.rollback()
        except (Exception,) as rollback_error:
            logger.warning(f"⚠️ Ошибка при rollback: {rollback_error}")

    def option_data_pocket(self, tf, exclude_ids: list):
        """
        Загрузка данных из otc_data с фильтром по timeframe и исключёнными val_id
        """
        sql = '''
            SELECT * FROM option_data.otc_data_view 
            WHERE timeframe = %s AND val_id != ALL(%s)
            ORDER BY otc_percent DESC, itog_stat_up DESC
        '''
        return self.execute_query_with_retries(
            sql=sql,
            values=(tf, exclude_ids),
            fetch_mode='all'
        )

    def option_data_tv(self, tf, exclude_ids):
        """
        # поиск данных для опциона
        :param tf: текущий таймфрейм
        :param exclude_ids: список использованных ранее пар
        :return:
        """
        sql = '''
                    SELECT * FROM option_data.binary_data_view 
                    WHERE timeframe = %s AND val_id != ALL(%s)
                    ORDER BY strong DESC, binary_percent DESC, itog_stat_up DESC
                '''
        return self.execute_query_with_retries(
            sql=sql,
            values=(tf, exclude_ids),
            fetch_mode='all'
        )

    def check_otc(self, val_id):
        """
        # проверка, является ли текущая пара рабочей
        :param val_id: id валютной пары OTC
        :return:
        """
        sql = f"SELECT pocket_real FROM vocabulary.otc_valute WHERE val_id = %s"
        return self.execute_query_with_retries(sql, (val_id,), fetch_mode='val')

    # счетчики
    def plus_counter(self, tf, otc):
        """
        Обновление и получение данных счетчика плюсов
        :param tf:
        :param otc:
        :return:
        """
        sql = (f"UPDATE option_data.counter "
               f"SET plus = plus +1, minus = 0 "
               f"WHERE timeframe = %s AND otc = %s "
               f"RETURNING plus")
        return self.execute_query_with_retries(sql, (tf, otc), fetch_mode='val', commit=True)

    def minus_counter(self, tf, otc):
        """
        Обновление и получение данных счетчика плюсов
        :param tf:
        :param otc:
        :return:
        """
        # получить значение счетчика для минусов
        sql = (f"UPDATE option_data.counter "
               f"SET plus = 0, minus = minus + 1 "
               f"WHERE timeframe = %s AND otc = %s "
               f"RETURNING minus")
        return self.execute_query_with_retries(sql, (tf, otc), fetch_mode='val', commit=True)

    def tv_setting(self):
        """
        загрузка поиск настроек для браузера TradingView
        :return:
        """
        sql = "SELECT * FROM settings.tv_settings"
        return self.execute_query_with_retries(sql, fetch_mode='all')

    def otc_setting(self):
        """
        загрузка поиск настроек для браузера Pocket
        :return:
        """
        sql = "SELECT * FROM settings.pocket_settings"
        return self.execute_query_with_retries(sql, fetch_mode='all')

    def option_setting(self, timeframe: str, binary=False):
        """
        # поиск настроек для опционов
        :param timeframe: таймфрейм
        :param binary: True - если опцион на обычных валютных парах
        :return: либо результат, если не найден - перезагрузка программы
        """
        sql = (f"SELECT * FROM settings.option_setting_view os "
               f"WHERE os.timeframe = %s AND os.binary = %s")
        return self.execute_query_with_retries(sql, (timeframe, binary,), fetch_mode='row')

    def pages_setting(self, timeframe: str):
        """
        # поиск страниц для браузера TradingView
        :param timeframe: таймфрейм
        :return: либо результат, если не найден - перезагрузка программы
          """
        sql = ("SELECT cp.* FROM cookies.cook_page cp "
               "JOIN settings.option_setting os ON cp.user_id  = os.cookies_tv "
               "WHERE os.timeframe = %s AND os.binary = true "
               "ORDER BY cp.page_id")
        return self.execute_query_with_retries(sql, (timeframe,), fetch_mode='all')

    def close_program(self, program_id: int):
        """
        Установка выключения программы для корректной работы с диспетчером
        :param program_id: id программы
        :return:
        """
        sql = 'UPDATE program.programdata SET status = false WHERE program_id = %s'
        return self.execute_query_with_retries(sql, (program_id,), fetch_mode='execute', commit=True)

    def get_pocket_cookies(self, user_id: int):
        """
        Загрузка OTC-кук по user_id из cookies.pocket_cookies.
        Используется для тестового override через env COOK_OTC — без правки БД.
        :param user_id: id пользователя в cookies.pocket_cookies
        :return: jsonb кук (list[dict]) либо None если не нашли
        """
        sql = "SELECT cookies FROM cookies.pocket_cookies WHERE user_id = %s"
        row = self.execute_query_with_retries(sql, (user_id,), fetch_mode='val')
        return row['cookies'] if row else None