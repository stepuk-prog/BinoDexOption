import logging
import os

from dotenv import load_dotenv
from database import Database
from pyrogram import Client
from classes.Option_class import Option
from settings._bootstrap import bootstrap_fetch
from settings.logger_config import parse_bool  # единый безопасный парсер (без падения на None)

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm

logger = logging.getLogger(__name__)
# Единый async-интерфейс к БД (asyncpg, пулы program+binodex). Пулы поднимает
# main.py::bot через `await database.connect()`; здесь только создаём объект.
# Конфиг/креды/cookies на старте читаются синхронно через bootstrap_fetch (пулов
# ещё нет), рантайм-запросы — через методы database.* внутри event loop.
database = Database()


def _env_int(name: str, default: str | None = None) -> int:
    """int из env с дефолтом; при отсутствии и без дефолта — понятная ошибка вместо TypeError."""
    value = os.getenv(name, default)
    if value is None:
        raise ValueError(f"Не задана обязательная переменная окружения {name}")
    return int(value)


timeframe = os.getenv("TIMEFRAME")
binary = parse_bool(os.getenv("BINARY", "0"))
# Ключ программы — фильтр своих строк в общей settings.option_setting.
prog_key = os.getenv("PROG_KEY")

# Суффикс для файлов (для разделения между экземплярами)
file_suffix = f"{timeframe}_{'bin' if binary else 'otc'}"

# Пути к файлам с суффиксом
shot_path = f"pictures/shot_{file_suffix}.png"
screenshot_path = f"pictures/screenshot_{file_suffix}.png"
log_path = f"logs/option_{file_suffix}.log"
# Базовые настройки экземпляра — из базы данных опционов (binodex).
# settings.option_setting общая для нескольких программ → отбираем свои по program.
option = bootstrap_fetch(
    'binodex',
    'SELECT * FROM settings.option_setting '
    'WHERE timeframe = $1 AND "binary" = $2 AND program = $3',
    timeframe, binary, prog_key, fetch_mode='row')
if option is None:
    raise ValueError(f"Не найдены настройки в БД для TIMEFRAME={timeframe}, BINARY={binary}")
test = parse_bool(os.getenv("TEST", "0"))
overlap = _env_int("OVERLAP", "0")
program_id = option['program_id']
overlap_random = _env_int("OVERLAP_RANDOM", "0")
if test:
    channel_id = _env_int("CHANNEL")
    api_id = os.getenv("TEST_API_ID")
    api_hash = os.getenv("TEST_API_HASH")
    session_file = os.getenv("TEST_SESSION_FILE")
    session_string = None
else:
    channel_id = option['channel_id']
    # Креды юзербота — из Program (telegram.telegram), таблицу не переносим.
    creds = bootstrap_fetch(
        'program',
        'SELECT api_id, api_hash, session_string FROM telegram.telegram '
        'WHERE id_telegram = $1',
        option['user_bot'], fetch_mode='row')
    if not creds:
        raise ValueError(f"Не найден юзербот id_telegram={option['user_bot']} в telegram.telegram")
    api_id = creds['api_id']
    api_hash = creds['api_hash']
    # Сессия из БД (Pyrogram session string). Если пусто — fallback на файл files/*.session.
    session_string = creds['session_string']
    session_file = f'files/{option["session_file"]}'
prog_name = option['prog_name']
# Куки. FIN (TV) — плоский list[dict] из Program.cookies.tv_cookies (add_cookies).
# OTC (binodex) — storage_state {cookies, origins} из binodex.cookies.binodex_cookies
# (Privy держит сессию в localStorage, одних cookies мало → new_context(storage_state=...)).
if binary:
    cookies = bootstrap_fetch(
        'program', 'SELECT cookies FROM cookies.tv_cookies WHERE user_id = $1',
        option['cookies_tv'], fetch_mode='val')
else:
    cookies = bootstrap_fetch(
        'binodex', 'SELECT cookies FROM cookies.binodex_cookies WHERE user_id = $1',
        option['cookies_pocket'], fetch_mode='val')

# Test override: подмена OTC storage_state через env COOK_OTC (user_id в binodex_cookies), без правки БД
cook_otc_override = os.getenv("COOK_OTC")
if cook_otc_override and not binary:
    cookies = bootstrap_fetch(
        'binodex', 'SELECT cookies FROM cookies.binodex_cookies WHERE user_id = $1',
        int(cook_otc_override), fetch_mode='val')
    if not cookies:
        raise ValueError(f"COOK_OTC={cook_otc_override}: storage_state не найден в cookies.binodex_cookies")
    logger.info("COOK_OTC override: загружены куки для user_id=%s", cook_otc_override)

# Test override: переадресация основных сигналов в другой канал через env SIGNAL_CHANNEL
signal_channel_override = os.getenv("SIGNAL_CHANNEL")
if signal_channel_override:
    channel_id = int(signal_channel_override)
    logger.info("SIGNAL_CHANNEL override: channel_id=%s", channel_id)
option_data = Option(tf=timeframe, dogon=option['dogon'])
option_data.start_random = option['translocation'][0]
option_data.binary = binary
option_data.end_random = option['translocation'][1]
option_data.timeframe = timeframe

# Ленивая инициализация Pyrogram Client (создаётся при первом вызове get_app())
_app: Client | None = None


def get_app() -> Client:
    """Получить Pyrogram Client (создаётся лениво внутри event loop)"""
    global _app
    if _app is None:
        try:
            if session_string:
                # Сессия из БД — без файла на диске.
                _app = Client(name=prog_name, api_id=api_id, api_hash=api_hash,
                              session_string=session_string, in_memory=True)
            else:
                # Fallback: файловая сессия files/*.session.
                _app = Client(name=session_file, api_id=api_id, api_hash=api_hash)
        except (Exception,) as e:
            logger.error(f"Ошибка создания Pyrogram Client: {e}")
            raise
    return _app
