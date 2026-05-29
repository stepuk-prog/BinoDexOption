import asyncio
import logging
import os
from logging import Handler, LogRecord, handlers

from aiogram import Bot
from settings.logger_config import error_channel, token, frame, message_channel, cookies_channel


def _get_log_dir() -> str:
    """Папка логов экземпляра: logs/option_{TIMEFRAME}_{bin|otc}/ (создаётся при отсутствии)"""
    timeframe = os.getenv("TIMEFRAME", "unknown")
    binary_str = os.getenv("BINARY", "0")
    is_binary = binary_str.lower() in ('1', 'true', 'yes', 'on')
    suffix = f"{timeframe}_{'bin' if is_binary else 'otc'}"
    path = f"logs/option_{suffix}"
    os.makedirs(path, exist_ok=True)
    return path

# Определение пользовательских уровней логирования
ERROR_LEVEL = logging.ERROR  # Используем стандартный уровень для error
REPORT_LEVEL = 25  # Новый уровень для report
COOKIES_LEVEL = 35  # Уровень для ошибок - отвал cookies

# Добавление новых уровней логирования
logging.addLevelName(REPORT_LEVEL, "REPORT")
logging.addLevelName(COOKIES_LEVEL, "COOKIES")


# Добавление новых методов в класс Logger
def report(self, message, *args, **kws):
    if self.isEnabledFor(REPORT_LEVEL):
        self._log(REPORT_LEVEL, message, args, **kws)

def cookies(self, message, *args, **kws):
    if self.isEnabledFor(COOKIES_LEVEL):
        self._log(COOKIES_LEVEL, message, args, **kws)



logging.Logger.report = report
logging.Logger.cookies = cookies

# Синглтон для aiogram Bot (один экземпляр на всё приложение)
_telegram_bot: Bot | None = None


def get_telegram_bot() -> Bot:
    """Получить singleton экземпляр бота"""
    global _telegram_bot
    if _telegram_bot is None:
        _telegram_bot = Bot(token=token)
    return _telegram_bot


async def close_telegram_bot():
    """Закрыть aiohttp-сессию aiogram-бота, если он был создан."""
    if _telegram_bot is not None:
        await _telegram_bot.session.close()


class TelegramBotHandler(Handler):  # Handler для логера, отправляющий сообщение в Telegram (async)
    def __init__(self):
        super().__init__()
        self.bot = get_telegram_bot()  # Используем синглтон
        self.setLevel(REPORT_LEVEL)
        self.err_fmt = logging.Formatter(
            f'‼️Сбой {frame}\n\n %(filename)s [LINE:%(lineno)d] '
            '#%(levelname)-8s [%(asctime)s] %(message)s')
        self.msg_fmt = logging.Formatter(f'📫{frame}\n\n %(message)s')

    async def _send_message(self, chat_id: int, text: str):
        """Асинхронная отправка сообщения"""
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except (Exception,) as error:
            print(f'Сбой отправки сообщения в Telegram — {error}')

    def emit(self, record: LogRecord):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Нет запущенного event loop - пропускаем отправку
            return

        try:
            if record.levelno >= ERROR_LEVEL:
                self.setFormatter(self.err_fmt)
                loop.create_task(self._send_message(error_channel, self.format(record=record)))
            elif record.levelno == REPORT_LEVEL:
                self.setFormatter(self.msg_fmt)
                loop.create_task(self._send_message(message_channel, self.format(record=record)))
            elif record.levelno == COOKIES_LEVEL:
                self.setFormatter(self.msg_fmt)
                loop.create_task(self._send_message(cookies_channel, self.format(record=record)))
        except (Exception,) as error:
            print(f'Сбой отправки сообщения в Telegram — {error}')


class _ExactLevelFilter(logging.Filter):
    """Пропускает только записи ровно указанного уровня (для пофайлового разбиения)."""

    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: LogRecord) -> bool:
        return record.levelno == self.level


# Уровень → имя файла внутри папки экземпляра (в каждом файле — только свой уровень)
_LEVEL_FILES = [
    (REPORT_LEVEL, 'report.log'),
    (logging.WARNING, 'warning.log'),
    (COOKIES_LEVEL, 'cookies.log'),
    (logging.ERROR, 'error.log'),
]

# Синглтон списка файловых хендлеров (по одному на уровень, общие для всех логгеров)
_file_handlers: list[logging.Handler] | None = None


def _get_file_handlers() -> list[logging.Handler]:
    """Синглтон: RotatingFileHandler на каждый уровень, в каждом файле — только свой уровень."""
    global _file_handlers
    if _file_handlers is None:
        FORMAT = u'%(filename)s [LINE:%(lineno)d] #%(levelname)-8s [%(asctime)s]  %(message)s'
        log_dir = _get_log_dir()
        formatter = logging.Formatter(FORMAT)
        _file_handlers = []
        for level, fname in _LEVEL_FILES:
            handler = logging.handlers.RotatingFileHandler(
                filename=f"{log_dir}/{fname}", maxBytes=1000000, backupCount=5, encoding='utf8'
            )
            handler.setFormatter(formatter)
            handler.setLevel(level)
            handler.addFilter(_ExactLevelFilter(level))
            _file_handlers.append(handler)
    return _file_handlers


def init_logger(name):  # инициализация логера
    logger = logging.getLogger(name)
    if logger.handlers:  # уже сконфигурирован — не плодим хендлеры при повторном вызове
        return logger
    logger.setLevel(REPORT_LEVEL)
    logger.propagate = False  # не дублировать записи в root-логгер
    logger.addHandler(TelegramBotHandler())
    FORMAT = u'%(filename)s [LINE:%(lineno)d] #%(levelname)-8s [%(asctime)s]  %(message)s'

    # Stream handler (консоль)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(FORMAT))
    sh.setLevel(REPORT_LEVEL)
    logger.addHandler(sh)

    # File handlers (синглтон): по одному файлу на уровень
    for fh in _get_file_handlers():
        logger.addHandler(fh)

    return logger
