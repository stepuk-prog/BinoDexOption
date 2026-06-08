import asyncio

from pyrogram.errors import Unauthorized

from apps.exit_app import session_dead_shutdown, session_failed
from logs import init_logger
from settings.config import get_app, channel_id
from settings.timing import TG_RECONNECT_TIMEOUT, TG_SEND_TIMEOUT

logger = init_logger(__name__)


async def session_dead() -> bool:
    """Проба «мейн-сессия реально разлогинена» vs «транзиент медиа-DC».

    401 на send_photo бывает двух видов: (а) ключ реально мёртв; (б) сбой на ОТДЕЛЬНОЙ
    сессии к медиа-DC (save_file → session.start → Ping timeout) при ЖИВОМ ключе аккаунта.
    Хоронить юзербота на (б) нельзя. get_me() бьёт в мейн-DC: Unauthorized → ключ реально
    мёртв (True); таймаут/сеть → ключ жив, это транзиент (False). get_me лёгкий — короткий
    таймаут достаточен."""
    try:
        await asyncio.wait_for(get_app().get_me(), timeout=15)
        return False
    except Unauthorized:
        return True
    except Exception:
        return False


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
    # session_failed = тип Unauthorized ИЛИ строковый маркер (AUTH_KEY_* и пр.). Но голый
    # Unauthorized («Auth key not found») бывает транзиентом на медиа-DC при ЖИВОМ ключе —
    # хоронить бота тогда нельзя. Маркеры однозначно мёртвые; голый 401 различаем get_me().
    if session_failed(error):
        key_alive_transient = isinstance(error, Unauthorized) and not await session_dead()
        if not key_alive_transient:
            await session_dead_shutdown(error)  # session мертва — штатный стоп без рестарта (sys.exit)
            return False, 'Сессия юзербота недействительна'  # явный возврат: не полагаемся только на sys.exit
        # иначе: транзиент-401 при живом ключе → лечим как обрыв (restart + resend) ниже
    if 'Connection lost' in str(error) or isinstance(error, Unauthorized):
        try:
            # Таймаут на restart+resend — зависший reconnect не должен вешать цикл (правило 6)
            await asyncio.wait_for(bot.restart(), timeout=TG_RECONNECT_TIMEOUT)
            logger.error(f'Транзиент-сбой отправки ({mes_type}): {error}. Переподключился (restart)')
            await asyncio.wait_for(
                bot.send_photo(chat_id=channel_id, photo=photo, caption=text),
                timeout=TG_RECONNECT_TIMEOUT)
            return True, ''
        except (Exception,) as err:
            # Транзиент не вылечился restart+повтором (ключ жив) → ⚠️ в session-канал, бот
            # продолжает (status не трогаем). Это не 🔒-отвал — переавторизация не требуется.
            logger.session(f'⚠️ Пост ({mes_type}) не доставлен: транзиент-сбой отправки '
                           f'не вылечился restart+повтором: {err}')
            return False, f'Переподключиться не удалось - {err}'
    else:
        error_message = f'Ошибка отправки {mes_type}! - {error}'
        return False, error_message
