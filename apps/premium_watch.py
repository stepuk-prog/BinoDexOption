"""Premium юзербота: проверка живости, отметка в БД и алерт в свою тему форума ошибок.

Кастом-эмодзи (`MessageEntityCustomEmoji`) по MTProto шлёт ТОЛЬКО Premium-аккаунт. Проверки не
было ни в одной программе семьи, поэтому истечение Premium ломало оформление постов молча.

Сама логика (когда спрашивать, что считать потерей, когда повторять алерт) — общая, живёт в
`binocore.tg` (стандарт §3.5). Здесь только подключение к этой программе: чем метить в БД, куда
слать алерт и под каким именем. Клиент берём через `get_app()` — он создаётся ЛЕНИВО внутри
event loop, поэтому держать ссылку на модульном уровне нельзя.
"""
from binocore import tg as core_tg

from logs import init_logger
from settings.config import database, get_app, prog_name, user_bot_id

logger = init_logger(__name__)
core_tg.configure(logger=logger)


async def _mark_premium(premium: bool) -> None:
    """Отметка Premium в telegram.telegram — ставится И снимается, чтобы таблица отражала факт."""
    await database.set_account_premium(user_bot_id, premium)


async def _notify_premium(text: str) -> None:
    """Алерт в выделенную тему форума ошибок (PREMIUM_TOPIC). Уровень PREMIUM=36 — свой адрес,
    чтобы длинная история «Premium не продлён» не топила тему ошибок режима."""
    logger.premium(text)


# Один сторож на процесс: он держит состояние (что знали в прошлый раз) и таймер интервала.
premium_guard = core_tg.PremiumGuard(mark=_mark_premium, notify=_notify_premium,
                                     program=prog_name, account=str(user_bot_id))


async def check_premium(force: bool = False) -> bool | None:
    """Проверить Premium, если подошёл срок (раз в 2 часа; на старте — force=True).

    Зовётся из главного цикла: сторож сам решает, идти ли в Telegram, поэтому вызов дешёвый.
    Ничего не бросает и НЕ влияет на публикацию — без Premium посты уходят как есть (решение
    владельца), задача проверки в том, чтобы об этом узнали."""
    return await premium_guard.ensure(get_app(), force=force)
