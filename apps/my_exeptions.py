from logs import init_logger
from settings.config import get_app, channel_id

logger = init_logger(__name__)


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
    if 'Connection lost' in str(error):
        try:
            await bot.restart()
            logger.error(f'Потеря соединения ({mes_type}). Ошибка - {error}.Переподключился')
            await bot.send_photo(chat_id=channel_id, photo=photo, caption=text)
            return True, ''
        except (Exception,) as err:
            return False, err
    else:
        error_message = f'Ошибка отправки {mes_type}! - {error}'
        return False, error_message


# обработка исключений Pyrogram для сообщений
async def lost_connection(error, text, mes_type):
    """
    :param error: перехваченная ошибка
    :param text: текст неотправленного сообщения
    :param mes_type: Тип сообщения (первое, итоговое и т.д.)
    :return:
    """
    bot = get_app()
    if 'Connection lost' in str(error):
        try:
            await bot.restart()
            logger.error(f'Потеря соединения ({mes_type}). Ошибка - {error}.Переподключился')
            await bot.send_message(chat_id=channel_id, text=text)
            return True, ''
        except (Exception,) as err:
            return False, f'Переподключиться не удалось - {err}. Перегружаюсь....'
    else:
        error_message = f'Ошибка отправки {mes_type}! - {error}'
        return False, error_message
