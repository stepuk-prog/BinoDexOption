import os

from dotenv import load_dotenv
from database import Database
from pyrogram import Client
from classes.Option_class import Option
from logs import init_logger
from settings._bootstrap import bootstrap_fetch
from settings.env import parse_bool, req_int, opt_int, req_str  # единые безопасные парсеры env
from settings.logger_config import file_suffix          # единый суффикс {tf}_{bin|otc}

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm

# init_logger (а не голый getLogger): иначе сообщения config (override'ы, ошибка создания Client)
# уходили бы в неподключённый root и терялись (нет файловых/TG-хендлеров).
logger = init_logger(__name__)
# Единый async-интерфейс к БД (asyncpg, пулы program+binodex). Пулы поднимает
# main.py::bot через `await database.connect()`; здесь только создаём объект.
# Конфиг/креды/cookies на старте читаются синхронно через bootstrap_fetch (пулов
# ещё нет), рантайм-запросы — через методы database.* внутри event loop.
database = Database()


timeframe = os.getenv("TIMEFRAME")
binary = parse_bool(os.getenv("BINARY", "0"))
# Ключ программы — фильтр своих строк в общей settings.option_setting.
prog_key = os.getenv("PROG_KEY")

# Пути к файлам с суффиксом экземпляра (file_suffix — из logger_config, единый источник)
shot_path = f"pictures/shot_{file_suffix}.png"
screenshot_path = f"pictures/screenshot_{file_suffix}.png"
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
overlap = opt_int("OVERLAP", 0)
program_id = option['program_id']
overlap_random = opt_int("OVERLAP_RANDOM", 0)
overlap_random = max(0, min(overlap_random, overlap))  # инвариант 0 <= overlap_random <= overlap
# Пауза между циклами main — в .env (§7), дефолты = историческому хардкоду (для OTC +30
# добавляет time_sleep). Тайминги retry/backoff остаются константами в коде.
main_cycle_pause_min = opt_int("MAIN_CYCLE_PAUSE_MIN", 100)
main_cycle_pause_max = opt_int("MAIN_CYCLE_PAUSE_MAX", 120)
if test:
    # Тест (§2): файловая session под files/ (TEST_SESSION_FILE), креды/канал — из .env.
    channel_id = req_int("TEST_CHANNEL")
    api_id = req_int("TEST_API_ID")        # явная ошибка вместо Client(api_id=None) позже
    api_hash = req_str("TEST_API_HASH")
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
    # Прод (§2): только session_string из БД (in_memory, без .session на диске). Пустой
    # session_string → явная ошибка на старте (нужна переавторизация и заливка строки в БД).
    session_string = creds['session_string']
    if not session_string:
        raise ValueError(f"Пустой session_string для юзербота id_telegram={option['user_bot']} "
                         f"в telegram.telegram — нужна переавторизация и заливка строки в БД")
    session_file = None  # прод не использует файловую session (только TEST=1)
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

# user_id владельцев кук — для рантайм-перечитывания из БД на каждом init (Survive §4.3:
# пересоздание браузера после отвала cookies подхватывает свежий refresh без рестарта).
cookies_tv_id = option['cookies_tv']
cookies_pocket_id = int(cook_otc_override) if (cook_otc_override and not binary) else option['cookies_pocket']

# Авто-восстановление OTC-кук: релогин INLINE в основном браузере (apps/otc_login.py, из
# otc_app.init_otc) — без подпроцесса. Имя владельца кук — для текста алертов (§4.2).
cook_name_otc = '?'
if not binary:
    cook_name_otc = bootstrap_fetch(
        'program', 'SELECT name FROM telegram.telegram WHERE id_telegram = $1',
        cookies_pocket_id, fetch_mode='val') or '?'

# Test override: переадресация основных сигналов в другой канал через env SIGNAL_CHANNEL
signal_channel_override = os.getenv("SIGNAL_CHANNEL")
if signal_channel_override:
    channel_id = int(signal_channel_override)
    logger.info("SIGNAL_CHANNEL override: channel_id=%s", channel_id)
option_data = Option(tf=timeframe, dogon=option['dogon'])
# translocation — пара [start_random, end_random] из jsonb БД. Проверяем явно, иначе
# NULL/короткий массив дал бы TypeError/IndexError на импорте (краш до подъёма логгера).
translocation = option['translocation']
if not isinstance(translocation, (list, tuple)) or len(translocation) < 2:
    raise ValueError(f"Некорректный option_setting.translocation={translocation!r} — "
                     f"ожидается массив [start_random, end_random]")
# Нормализуем порядок: перевёрнутая пара (start > end) дала бы неверные уровни ПС в Option.levels().
_start_random, _end_random = sorted((translocation[0], translocation[1]))
option_data.start_random = _start_random
option_data.binary = binary
option_data.end_random = _end_random
option_data.timeframe = timeframe

# Ленивая инициализация Pyrogram Client (создаётся при первом вызове get_app())
_app: Client | None = None


def get_app() -> Client:
    """Получить Pyrogram Client (создаётся лениво внутри event loop). §2: прод —
    session_string из БД (in_memory, без файла); тест (TEST=1) — файловая session
    (TEST_SESSION_FILE). Прод с пустым session_string уже отсечён на старте (config)."""
    global _app
    if _app is None:
        try:
            if test:
                # Тест: файловая session files/*.session (TEST_SESSION_FILE).
                _app = Client(name=session_file, api_id=api_id, api_hash=api_hash)
            else:
                # Прод: сессия из БД — без файла на диске.
                _app = Client(name=prog_name, api_id=api_id, api_hash=api_hash,
                              session_string=session_string, in_memory=True)
        except (Exception,) as e:
            logger.error(f"Ошибка создания Pyrogram Client: {e}")
            raise
    return _app
