import logging
import os

from dotenv import load_dotenv
from database import Database
from pyrogram import Client
from settings.Option_class import Option
from settings.database_config import pg_name_fin

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm

logger = logging.getLogger(__name__)
database = Database()  # Program: настройки браузера, cookies, telegram, программа
database_fin = Database(db_name=pg_name_fin)  # binodex: данные опционов и option_setting


def parse_bool(value: str) -> bool:
    """Парсинг bool из строки (поддержка 1/0, true/false, yes/no)"""
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


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
option = database_fin.option_setting_base(timeframe=timeframe, binary=binary, program=prog_key)
if option is None:
    raise ValueError(f"Не найдены настройки в БД для TIMEFRAME={timeframe}, BINARY={binary}")
test = parse_bool(os.getenv("TEST", "0"))
overlap = int(os.getenv("OVERLAP"))
program_id = option['program_id']
overlap_random = int(os.getenv("OVERLAP_RANDOM"))
if test:
    channel_id = int(os.getenv("CHANNEL"))
    api_id = os.getenv("TEST_API_ID")
    api_hash = os.getenv("TEST_API_HASH")
    session_file = os.getenv("TEST_SESSION_FILE")
    session_string = None
else:
    channel_id = option['channel_id']
    # Креды юзербота — из Program (telegram.telegram), таблицу не переносим.
    creds = database.telegram_creds(id_telegram=option['user_bot'])
    if not creds:
        raise ValueError(f"Не найден юзербот id_telegram={option['user_bot']} в telegram.telegram")
    api_id = creds['api_id']
    api_hash = creds['api_hash']
    # Сессия из БД (Pyrogram session string). Если пусто — fallback на файл files/*.session.
    session_string = creds['session_string']
    session_file = f'files/{option["session_file"]}'
prog_name = option['prog_name']
# Куки — из Program (cookies.*), не переносим.
if binary:
    cookies = database.tv_cookies(user_id=option['cookies_tv'])
else:
    cookies = database.get_pocket_cookies(user_id=option['cookies_pocket'])

# Test override: подмена OTC-кук через env COOK_OTC (user_id в cookies.pocket_cookies), без правки БД
cook_otc_override = os.getenv("COOK_OTC")
if cook_otc_override and not binary:
    cookies = database.get_pocket_cookies(user_id=int(cook_otc_override))
    if not cookies:
        raise ValueError(f"COOK_OTC={cook_otc_override}: куки не найдены в cookies.pocket_cookies")
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
