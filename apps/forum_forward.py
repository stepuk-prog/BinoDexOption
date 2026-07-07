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
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from logs import init_logger
from messages import partner_message
from settings.config import (bot_link, channel_id, database, forum_bot_token,
                             forum_forward_enabled, forum_id, forum_topics,
                             partner_message_enabled)
from settings.image_paths import PARTNER_IMAGE
from settings.timing import TG_SEND_TIMEOUT

logger = init_logger(__name__)

_BUTTON_ICON = '5330115548900501467'   # 🔑 (custom-emoji иконка кнопки)
_BUTTON_COLOR = 'success'              # зелёная кнопка (aiogram style: success/primary/danger)


def _kb_partner() -> InlineKeyboardMarkup:
    """Зелёная inline-кнопка «Получить бесплатный доступ» → BOT_LINK (kb_chat_free из ForumTrade)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Получить бесплатный доступ', url=bot_link,
                             style=_BUTTON_COLOR, icon_custom_emoji_id=_BUTTON_ICON),
    ]])


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
        # ПОСЛЕ вехи-форварда — партнёрское сообщение (фото+кнопка) в ту же тему (send_photo, НЕ
        # форвард). id запоминаем как extra, чтобы удалить его вместе с вехой в следующий раз.
        extra_id = await _send_partner_message(topic)
        # Запоминаем id вехи-форварда (+ партнёрки) — чтобы удалить их перед следующей пересылкой.
        await database.save_forum_message(forum_id, topic, sent.message_id, extra_id)
    except (Exception,) as error:
        logger.warning(f'Не удалось переслать веху {count} в тему форума: {error}')


async def _send_partner_message(topic: int) -> int | None:
    """Партнёрское фото+кнопка в тему `topic` сразу после вехи-форварда (send_photo, НЕ форвард).
    Вторичное действие: сбой логируем и глотаем (веха уже отправлена). No-op если BOT_LINK не
    задан. Возвращает message_id отправленного сообщения (для удаления в след. раз) | None."""
    if not partner_message_enabled:
        return None
    try:
        sent = await asyncio.wait_for(
            _get_moderator_bot().send_photo(
                chat_id=forum_id, message_thread_id=topic,
                photo=FSInputFile(PARTNER_IMAGE), caption=partner_message(),
                parse_mode=ParseMode.HTML, reply_markup=_kb_partner()),
            timeout=TG_SEND_TIMEOUT)
        logger.report(f'Партнёрское сообщение отправлено в тему {topic}')
        return sent.message_id
    except (Exception,) as error:
        logger.warning(f'Не удалось отправить партнёрское сообщение в тему {topic}: {error}')
        return None


async def _delete_previous(topic: int) -> None:
    """Удалить ранее отправленные в теме `topic` веху-форвард И партнёрское сообщение (если были).
    Вторичное действие: любые ошибки глотаем (сообщение могло быть удалено вручную или устареть за
    лимит TG). Каждый id удаляем отдельно — чтобы уже удалённое одно не блокировало второе."""
    try:
        prev = await database.get_forum_message(forum_id, topic)
        if not prev:  # None (записи нет) / False (сбой БД) — удалять нечего/нечем
            return
        # message_id (веха-форвард) + extra_message_id (партнёрка, может быть NULL / не от нас).
        ids = [prev['message_id']]
        if prev['extra_message_id']:
            ids.append(prev['extra_message_id'])
        for mid in ids:
            try:
                await asyncio.wait_for(
                    _get_moderator_bot().delete_message(chat_id=forum_id, message_id=mid),
                    timeout=TG_SEND_TIMEOUT)
            except (Exception,) as error:
                logger.warning(f'Не смог ❌ удалить сообщение {mid} в теме {topic}: {error}')
        logger.report(f'Предыдущие сообщения в теме {topic} удалены ✅')
    except (Exception,) as error:
        logger.warning(f'Не смог ❌ удалить предыдущие сообщения в теме {topic}: {error}')


async def close_moderator_bot() -> None:
    """Закрыть aiohttp-сессию бота-модератора (если создавался). Без падений на выходе."""
    global _moderator_bot
    if _moderator_bot is not None:
        try:
            await _moderator_bot.session.close()
        except (Exception,) as error:
            logger.warning(f'Ошибка закрытия бота-модератора: {error}')
