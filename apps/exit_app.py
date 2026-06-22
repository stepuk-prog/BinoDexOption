import asyncio
import sys
from typing import TYPE_CHECKING

from pyrogram.errors import Unauthorized

from logs import init_logger
from settings.timing import (LOGGER_FLUSH_DELAY, COOKIES_ERROR_DELAY, SHUTDOWN_STEP_TIMEOUT,
                             STATUS_WRITE_TIMEOUT)
from settings.constant import EXIT_USERBOT

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

logger = init_logger(__name__)

# Маркеры мёртвой session: pyrogram не всегда отдаёт ошибку типом Unauthorized — бывает
# обёрнуто/прокинуто текстом. Только isinstance(Unauthorized) недостаточно (так было в
# BinoOptions): обёрнутая ошибка ушла бы мимо детекта → диспетчер зациклил бы рестарт
# мёртвой session. См. семейный стандарт §3.1.
_SESSION_FAIL_MARKERS = (
    'AUTH_KEY_UNREGISTERED', 'AUTH_KEY_INVALID', 'AUTH_KEY_DUPLICATED',
    'SESSION_EXPIRED', 'SESSION_REVOKED', 'SESSION_PASSWORD_NEEDED',
    'USER_DEACTIVATED', 'USER_DEACTIVATED_BAN', 'PHONE_NUMBER_UNOCCUPIED',
    'NO SUCH FILE OR DIRECTORY',   # .session-файл пропал (тест-режим)
)


def session_failed(error: BaseException) -> bool:
    """session юзербота доказано мертва: тип Unauthorized ИЛИ строковый маркер (§3.1)."""
    return isinstance(error, Unauthorized) or any(
        m in str(error).upper() for m in _SESSION_FAIL_MARKERS)


async def write_status_offline(program_id: int):
    """Запись program.programdata.status=false — НЕ глотать сбой (§1.1): это единственный
    шаг shutdown, где тихий провал опасен (диспетчер начнёт перезапускать мёртвую
    session/куки по кругу). Поэтому с явным таймаутом и логом ошибки."""
    from settings.config import database  # lazy — избегаем циклических импортов
    try:
        if await asyncio.wait_for(database.close_program(program_id=program_id),
                                  timeout=STATUS_WRITE_TIMEOUT) is False:
            logger.error('Не удалось записать programdata.status=false — '
                         'диспетчер может перезапустить мёртвую session/куки')
    except (Exception,) as error:
        logger.error(f'Сбой записи programdata.status=false: {error} — возможен лишний рестарт')


async def _close_userbot():
    """Остановка Pyrogram-юзербота (idempotent — стоп только если ещё подключён)."""
    try:
        from settings.config import get_app  # lazy — избегаем циклических импортов
        app = get_app()
        if getattr(app, "is_connected", False):
            await asyncio.wait_for(app.stop(), timeout=SHUTDOWN_STEP_TIMEOUT)
    except (Exception,) as e:
        logger.warning(f"Ошибка остановки юзербота: {e}")


async def _close_database():
    """Закрытие пулов БД (единый async-интерфейс settings.config.database)."""
    try:
        from settings.config import database  # lazy — избегаем циклических импортов
        await database.close()
    except (Exception,) as e:
        logger.warning(f"Ошибка закрытия пулов БД: {e}")


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
    :param status: код выхода (sys.exit) — его читает диспетчер. 0 — штатно, 1 — краш/перезагрузка,
                   10/11/12/13 — browser/cookies/setup/userbot (таксономия в settings/constant.py)
    :param text: текст, отправляемый с завершением/ошибкой
    :param cookies: ошибка от падения cookies (легаси-флаг; пауза перед рестартом при status=1)
    """
    # 1. Браузер (на ранних выходах manager может отсутствовать)
    if manager is not None:
        try:
            await asyncio.wait_for(manager.close(), timeout=SHUTDOWN_STEP_TIMEOUT)
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


async def session_dead_shutdown(error, reason: str = ''):
    """
    session юзербота недоступна → стоп с кодом EXIT_USERBOT: ошибка в error-канал, критичный
    алерт в ВЫДЕЛЕННЫЙ session-канал (НЕ cookies — иначе поток cookies похоронит алерт, §3.3),
    exit(EXIT_USERBOT) → диспетчер инициирует реавторизацию (scripts/reauth_userbot.py). status
    НЕ трогаем (инвариант: status=false — только плановый weekend-выход binary). Вызывается на
    старте (мёртвый ключ или нет переподключения за N попыток) и при отвале во время работы.
    """
    suffix = f" ({reason})" if reason else ''
    logger.error(f"Недоступна session юзербота{suffix}: {error}")
    logger.session(f"🔒 Отвал юзербота — session недоступна{suffix}, требуется реавторизация. Останавливаюсь.")
    await close_program(manager=None, status=EXIT_USERBOT,
                        text=f"Отвал юзербота (session) 🔒 (код {EXIT_USERBOT})")