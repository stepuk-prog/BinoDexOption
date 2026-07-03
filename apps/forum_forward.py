"""Пересылка вех серии плюсов в темы форума общим ботом-модератором.

Юзерботы не могут постить в темы форума, поэтому веху «N прогнозов в ряд» пересылает
ОТДЕЛЬНЫЙ бот-модератор (один на все программы) — форвардом (не копией), чтобы в теме было
видно канал-источник. Фича включается только при заданных FORUM_BOT_TOKEN/FORUM/FORUM_TOPICS
(см. settings.config). Пересылка — ВТОРИЧНОЕ действие: любые ошибки логируем и глотаем, чтобы
не рвать цикл плюсов и не терять сам пост-веху в канале.

Требования к боту-модератору (настраивает владелец):
  • админ/участник канала-источника (channel_id) — иначе не сможет форвардить ИЗ него;
  • админ форума с правом писать в темы.
"""
import asyncio
import random

from aiogram import Bot

from logs import init_logger
from settings.config import (channel_id, forum_bot_token, forum_forward_enabled,
                             forum_id, forum_topics)
from settings.timing import TG_SEND_TIMEOUT

logger = init_logger(__name__)

# Синглтон бота-модератора (aiogram). Создаётся лениво при первой пересылке; сессия
# закрывается на выходе через close_moderator_bot() (apps/exit_app.py).
_moderator_bot: Bot | None = None


def _get_moderator_bot() -> Bot:
    global _moderator_bot
    if _moderator_bot is None:
        _moderator_bot = Bot(token=forum_bot_token)
    return _moderator_bot


async def forward_plus_milestone(message_id: int, count: int) -> None:
    """Переслать веху серии плюсов (message_id в channel_id) в СЛУЧАЙНУЮ тему форума.
    No-op, если фича не сконфигурена. Ошибки не пробрасываются (вторичное действие)."""
    if not forum_forward_enabled:
        return
    topic = random.choice(forum_topics)
    try:
        await asyncio.wait_for(
            _get_moderator_bot().forward_message(
                chat_id=forum_id, message_thread_id=topic,
                from_chat_id=channel_id, message_id=message_id),
            timeout=TG_SEND_TIMEOUT)
        logger.report(f'Отправлено сообщение о плюсах на форум, в тему {topic}')
    except (Exception,) as error:
        logger.warning(f'Не удалось переслать веху {count} в тему форума {topic}: {error}')


async def close_moderator_bot() -> None:
    """Закрыть aiohttp-сессию бота-модератора (если создавался). Без падений на выходе."""
    global _moderator_bot
    if _moderator_bot is not None:
        try:
            await _moderator_bot.session.close()
        except (Exception,) as error:
            logger.warning(f'Ошибка закрытия бота-модератора: {error}')
