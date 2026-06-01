import asyncio

from apps.exit_app import session_dead_shutdown, session_failed
from logs import init_logger
from settings.config import get_app, channel_id
from settings.timing import TG_RECONNECT_TIMEOUT, TG_SEND_TIMEOUT

logger = init_logger(__name__)


async def send_photo_safe(photo, caption, mes_type: str,
                          timeout: float = TG_SEND_TIMEOUT) -> tuple[bool, str]:
    """Единый помощник отправки фото в основной канал с таймаутом и восстановлением при
    обрыве (раньше этот паттерн дублировался в main_app._try_send, app.check_plus и
    app.dop_plus_message). :return: (ok, error_text)."""
    try:
        await asyncio.wait_for(
            get_app().send_photo(chat_id=channel_id, photo=photo, caption=caption),
            timeout=timeout)
        return True, ''
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут отправки (%s)", mes_type)
        return False, 'Таймаут Pyrogram'
    except (Exception,) as error:
        logger.error("❌ Ошибка отправки (%s): %s", mes_type, error)
        return await lost_connection_photo(error=error, photo=photo, text=caption, mes_type=mes_type)


async def lost_connection_photo(error, photo, text, mes_type):
    """
    # обработка исключений Pyrogram для фото сообщений
    :param error: перехваченная ошибка
    :param photo: фото неотправленного сообщения
    :param text: текст неотправленного сообщения
    :param mes_type: Тип сообщения (первое, итоговое и т.д.)
    :return: возвращает True, либо False, если исправить ошибку не удалось
    """
    bot = get_app()
    if session_failed(error):  # §3.1: тип Unauthorized ИЛИ строковый маркер
        await session_dead_shutdown(error)  # session мертва — штатный стоп без рестарта (sys.exit)
        return False, 'Сессия юзербота недействительна'  # явный возврат: не полагаемся только на sys.exit
    if 'Connection lost' in str(error):
        try:
            # Таймаут на restart+resend — зависший reconnect не должен вешать цикл (правило 6)
            await asyncio.wait_for(bot.restart(), timeout=TG_RECONNECT_TIMEOUT)
            logger.error(f'Потеря соединения ({mes_type}). Ошибка - {error}.Переподключился')
            await asyncio.wait_for(
                bot.send_photo(chat_id=channel_id, photo=photo, caption=text),
                timeout=TG_RECONNECT_TIMEOUT)
            return True, ''
        except (Exception,) as err:
            return False, f'Переподключиться не удалось - {err}'
    else:
        error_message = f'Ошибка отправки {mes_type}! - {error}'
        return False, error_message
