import asyncio
import sys
from typing import TYPE_CHECKING

from logs import init_logger
from settings.timing import LOGGER_FLUSH_DELAY, COOKIES_ERROR_DELAY

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

logger = init_logger(__name__)


async def _close_userbot():
    """Остановка Pyrogram-юзербота (idempotent — стоп только если ещё подключён)."""
    try:
        from settings.config import get_app  # lazy — избегаем циклических импортов
        app = get_app()
        if getattr(app, "is_connected", False):
            await app.stop()
    except (Exception,) as e:
        logger.warning(f"Ошибка остановки юзербота: {e}")


async def _close_database():
    """Закрытие async-пулов и sync-соединений БД."""
    # Async-пулы (apps.app / apps.otc_app)
    try:
        from apps.app import _database as app_db
        from apps.otc_app import _database as otc_db
        for db in (app_db, otc_db):
            if db is not None:
                await db.close()
    except (Exception,) as e:
        logger.warning(f"Ошибка закрытия async-пулов БД: {e}")
    # Sync-соединения (Program + binodex)
    try:
        from settings.config import database, database_fin
        for db in (database, database_fin):
            conn = getattr(db, "connection", None)
            if conn is not None and getattr(conn, "closed", 1) == 0:
                conn.close()
    except (Exception,) as e:
        logger.warning(f"Ошибка закрытия sync-соединений БД: {e}")


async def _close_telegram_logger():
    """Закрытие aiogram-сессии логгера (последним; единственный report о закрытии — в close_program)."""
    try:
        from logs.log_init import close_telegram_bot
        await asyncio.sleep(LOGGER_FLUSH_DELAY)  # дать aiogram дослать pending-сообщения (последний report)
        await close_telegram_bot()               # aiogram 3.x: закрываем aiohttp-сессию (если бот создан)
    except (Exception,) as e:
        print(f"Ошибка закрытия aiogram-бота: {e}")  # logger уже могут быть погашены


async def close_program(manager: "BrowserManager | None", status: int, text: str, cookies: bool = False):
    """
    Полное закрытие программы: браузер → юзербот → БД (пулы+соединения) → aiogram.
    :param manager: BrowserManager — для отключения браузера (None на ранних выходах)
    :param status: 0 — штатное завершение, 1 — на перезагрузку
    :param text: текст, отправляемый с завершением/ошибкой
    :param cookies: ошибка от падения cookies
    """
    # 1. Браузер (на ранних выходах manager может отсутствовать)
    if manager is not None:
        try:
            await manager.close()
        except (Exception,) as e:
            logger.warning(f"Ошибка закрытия браузера: {e}")

    # 2. Причина выхода в лог
    if status == 1:
        if cookies:
            logger.cookies("Отвалился COOKIES")
            await asyncio.sleep(COOKIES_ERROR_DELAY)
        else:
            logger.error(f'‼️Аварийный выход‼️Ошибка - {text}. Перегружаюсь...')
    else:
        logger.report(text)

    # 3. Юзербот и БД
    await _close_userbot()
    await _close_database()

    # 4. Финальный report + закрытие aiogram (после него логгер-бот недоступен)
    await _close_telegram_logger()

    sys.exit(status)


async def session_dead_shutdown(error):
    """
    Недействительна session юзербота → штатный стоп: ошибка в error-канал, отдельный
    алерт (как cookies), запись status=false в БД, graceful-выход (status=0, без рестарта,
    пока не обновят session). Вызывается и на старте, и при отвале во время отправки.
    """
    from settings.config import database, program_id  # lazy — избегаем циклических импортов
    logger.error(f"Недействительна session юзербота: {error}")
    logger.cookies("🔒 Отвал юзербота — недействительна session, требуется обновление данных. Останавливаюсь.")
    try:
        database.close_program(program_id=program_id)
    except (Exception,) as e:
        logger.warning(f"Не удалось записать close_program в БД: {e}")
    await close_program(manager=None, status=0, text="Отвал юзербота (session) 🔒")