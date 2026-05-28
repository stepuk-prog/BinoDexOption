import logging
import os

from dotenv import load_dotenv
from database import Database
from pyrogram import Client
from settings.Option_class import Option

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm

logger = logging.getLogger(__name__)
database = Database()


def parse_bool(value: str) -> bool:
    """Парсинг bool из строки (поддержка 1/0, true/false, yes/no)"""
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


timeframe = os.getenv("TIMEFRAME")
binary = parse_bool(os.getenv("BINARY", "0"))

# Суффикс для файлов (для разделения между экземплярами)
file_suffix = f"{timeframe}_{'bin' if binary else 'otc'}"

# Пути к файлам с суффиксом
shot_path = f"pictures/shot_{file_suffix}.png"
screenshot_path = f"pictures/screenshot_{file_suffix}.png"
log_path = f"logs/option_{file_suffix}.log"
option = database.option_setting(timeframe=timeframe, binary=binary)
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
else:
    channel_id = option['channel_id']
    api_id = option['api_id']
    api_hash = option['api_hash']
    session_file = f'files/{option["session_file"]}'
prog_name = option['prog_name']
if binary:
    cookies = option['cook_tv']
else:
    cookies = option['cookies']

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
            _app = Client(name=session_file, api_id=api_id, api_hash=api_hash)
        except (Exception,) as e:
            logger.error(f"Ошибка создания Pyrogram Client: {e}")
            raise
    return _app
