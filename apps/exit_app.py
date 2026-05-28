import asyncio
import sys
from typing import TYPE_CHECKING

from logs import init_logger
from settings.timing import LOGGER_FLUSH_DELAY, COOKIES_ERROR_DELAY

if TYPE_CHECKING:
    from apps.browser_app import BrowserManager

logger = init_logger(__name__)


async def _close_database():
    """Закрытие пула соединений БД, если он был инициализирован."""
    try:
        # Импортируем здесь, чтобы избежать циклических импортов
        from apps.app import _database as app_db
        from apps.otc_app import _database as otc_db

        for db in [app_db, otc_db]:
            if db is not None:
                await db.close()
    except (Exception,) as e:
        logger.warning(f"Ошибка закрытия БД: {e}")


async def close_program(manager: "BrowserManager", status: int, text: str, cookies: bool = False):
    """
    Закрытие программы и браузера
    :param manager: BrowserManager - для корректного отключения браузера
    :param status: Завершение программы status - 0 - нормальное завершение, 1 - на перезагрузку
    :param text: Текст, отправляемый с ошибкой
    :param cookies: Ошибка от падения cookies
    """
    # Закрываем браузер
    try:
        await manager.close()
    except (Exception,) as e:
        logger.warning(f"Ошибка закрытия браузера: {e}")

    # Закрываем пул БД
    await _close_database()

    if status == 1:
        if cookies:
            logger.cookies("Отвалился COOKIES")
            await asyncio.sleep(LOGGER_FLUSH_DELAY)
            await asyncio.sleep(COOKIES_ERROR_DELAY)
        else:
            logger.error(f'‼️Аварийный выход‼️Ошибка - {text}. Перегружаюсь...')
    else:
        logger.report(text)

    sys.exit(status)
