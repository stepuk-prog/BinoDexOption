"""Пересылка вех серии плюсов в темы форума общим ботом-модератором.

Юзерботы не могут постить в темы форума, поэтому веху «N прогнозов в ряд» пересылает
ОТДЕЛЬНЫЙ бот-модератор (один на все программы) — форвардом (не копией), чтобы в теме было
видно канал-источник. Фича включается только при заданных FORUM_BOT_TOKEN/FORUM/FORUM_TOPICS
(см. settings.config). Пересылка — ВТОРИЧНОЕ действие: любые ошибки логируем и глотаем, чтобы
не рвать цикл плюсов и не терять сам пост-веху в канале.

Требования к боту-модератору (настраивает владелец):
  • админ/участник канала-источника (channel_id) — иначе не сможет форвардить ИЗ него;
  • админ форума с правом писать в темы.

Уровни логов здесь: рутинные шаги (переслал / отправил партнёрку / подчистил прошлые) — `info`,
то есть только в файл. В Telegram уходят ERROR/REPORT, а исправная пересылка вехи — не событие
для оператора: на каждую веху это три сообщения в канал логов от КАЖДОГО инстанса. Заметной
должна быть поломка, и она остаётся видимой (`warning` в файл, реальные сбои — через ERROR).
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


async def _quiet_topics() -> set | None:
    """Темы форума, в которые СЕЙЧАС писать нельзя (окно тишины из БД).

    None — окно накрывает форум целиком; пустое множество — окон нет.
    Недоступная БД трактуется как «окон нет»: тишина — вторичная функция, и глушить
    из-за сбоя БД нельзя, иначе разовая проблема с базой молча остановит пересылку."""
    try:
        rows = await database.forum_quiet_topics(forum_id)
    except (Exception,) as error:
        logger.warning(f'Окно тишины форума не прочитано ({error}) — пересылаю как обычно')
        return set()
    if not rows:  # [] — окон нет; False — сбой БД (контракт execute_query)
        return set()
    quiet = set()
    for row in rows:
        if row['topics'] is None:  # окно на весь форум
            return None
        quiet.update(row['topics'])
    return quiet


async def forward_plus_milestone(message_id: int, count: int) -> None:
    """Переслать веху серии плюсов (message_id в channel_id) в СЛУЧАЙНУЮ тему форума.
    Перед пересылкой удаляет прошлую веху В ЭТОЙ ТЕМЕ (любой программы) — держим только
    свежую. No-op, если фича не сконфигурена. Ошибки не пробрасываются (вторичное действие)."""
    if not forum_forward_enabled:
        return
    # Всё тело — под try: пересылка вторичная, любой сбой лишь логируем и НЕ рвём цикл плюсов.
    try:
        # Окно тишины (settings.forum_quiet): на время усиленной рассылки о видео в эти темы
        # не пишем — иначе пост тонет среди вех. Молчим ТОЧЕЧНО: если закрыта часть тем,
        # веха уходит в оставшиеся, а не пропадает целиком.
        quiet = await _quiet_topics()
        allowed = [] if quiet is None else [t for t in forum_topics if t not in quiet]
        if not allowed:
            logger.info('Окно тишины форума — веху в темы не пересылаю')
            return
        topic = random.choice(allowed)
        await _delete_previous(topic)
        sent = await asyncio.wait_for(
            _get_moderator_bot().forward_message(
                chat_id=forum_id, message_thread_id=topic,
                from_chat_id=channel_id, message_id=message_id),
            timeout=TG_SEND_TIMEOUT)
        logger.info(f'Отправлено сообщение о плюсах на форум, в тему {topic}')
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
        logger.info(f'Партнёрское сообщение отправлено в тему {topic}')
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
        logger.info(f'Предыдущие сообщения в теме {topic} удалены ✅')
    except (Exception,) as error:
        logger.warning(f'Не смог ❌ удалить предыдущие сообщения в теме {topic}: {error}')


async def close_moderator_bot() -> None:
    """Закрыть aiohttp-сессию бота-модератора (если создавался). Без падений на выходе."""
    if _moderator_bot is not None:   # только читаем — global не нужен
        try:
            await _moderator_bot.session.close()
        except (Exception,) as error:
            logger.warning(f'Ошибка закрытия бота-модератора: {error}')
