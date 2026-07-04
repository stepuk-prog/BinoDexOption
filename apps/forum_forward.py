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
from settings.config import (channel_id, database, forum_bot_token,
                             forum_forward_enabled, forum_id, forum_topics)
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
    Перед пересылкой удаляет прошлую веху В ЭТОЙ ТЕМЕ (любой программы) — держим только
    свежую. No-op, если фича не сконфигурена. Ошибки не пробрасываются (вторичное действие)."""
    if not forum_forward_enabled:
        return
    # Всё тело — под try: пересылка вторичная, любой сбой лишь логируем и НЕ рвём цикл плюсов.
    try:
        topic = random.choice(forum_topics)
        await _delete_previous(topic)
        sent = await asyncio.wait_for(
            _get_moderator_bot().forward_message(
                chat_id=forum_id, message_thread_id=topic,
                from_chat_id=channel_id, message_id=message_id),
            timeout=TG_SEND_TIMEOUT)
        logger.report(f'Отправлено сообщение о плюсах на форум, в тему {topic}')
        # Запоминаем id нового форварда — чтобы удалить его перед следующей пересылкой в тему.
        await database.save_forum_message(forum_id, topic, sent.message_id)
    except (Exception,) as error:
        logger.warning(f'Не удалось переслать веху {count} в тему форума: {error}')


async def _delete_previous(topic: int) -> None:
    """Удалить ранее пересланную веху в теме `topic` (если была). Вторичное действие:
    любые ошибки глотаем (сообщение могло быть удалено вручную или устареть за лимит TG)."""
    try:
        prev = await database.get_forum_message(forum_id, topic)
        if not prev:  # None (записи нет) / False (сбой БД) — удалять нечего/нечем
            return
        await asyncio.wait_for(
            _get_moderator_bot().delete_message(chat_id=forum_id, message_id=prev),
            timeout=TG_SEND_TIMEOUT)
        logger.report(f'Предыдущее сообщение в теме {topic} удалено ✅')
    except (Exception,) as error:
        logger.warning(f'Не смог ❌ удалить предыдущее сообщение в теме {topic}: {error}')


async def close_moderator_bot() -> None:
    """Закрыть aiohttp-сессию бота-модератора (если создавался). Без падений на выходе."""
    global _moderator_bot
    if _moderator_bot is not None:
        try:
            await _moderator_bot.session.close()
        except (Exception,) as error:
            logger.warning(f'Ошибка закрытия бота-модератора: {error}')
