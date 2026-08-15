import asyncio
from datetime import datetime, timedelta

from pyrogram.errors import Unauthorized, FloodWait

from apps.exit_app import session_dead_shutdown, session_failed
from logs import init_logger
from settings.config import get_app, channel_id
from settings.timing import (TG_HISTORY_PROBE_LIMIT, TG_HISTORY_PROBE_SKEW,
                             TG_HISTORY_PROBE_TIMEOUT, TG_RECONNECT_TIMEOUT, TG_SEND_TIMEOUT,
                             TRANSIENT_401_MAX_STRIKES)

logger = init_logger(__name__)

# Счётчик подряд НЕвылеченных транзиент-401 (Unauthorized при живом ключе). Глобален на
# процесс: цепочка рвётся любым успешным постом (_reset_transient_strikes). Дойдя до
# TRANSIENT_401_MAX_STRIKES — эскалация в session_dead_shutdown: трактуем как мёртвую
# session, которую get_me-проба не уличила (таймаут/сеть на самой пробе).
_transient_401_strikes = 0

# Потолок ожидания FloodWait: Telegram при флуд-лимите просит подождать .value сек. Ждём окно и
# повторяем ОДИН раз; окно выше потолка / повторный FloodWait → теряем пост (бот продолжает).
_FLOODWAIT_MAX = 120


def _reset_transient_strikes() -> None:
    """Сброс цепочки транзиент-401: успешный пост доказал, что session жива и постит."""
    global _transient_401_strikes
    _transient_401_strikes = 0


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
    except (Exception,):
        return False


async def _find_posted(caption: str, since: datetime):
    """Проба «пост всё-таки ушёл» — перед повтором после таймаута.

    Таймаут `wait_for` рвёт ОЖИДАНИЕ ответа, а не саму отправку: `send_photo` мог доехать
    до Telegram, а подтверждение потеряться. Слепой повтор дал бы дубль в канале, поэтому
    сначала заглядываем в историю: канал у экземпляра свой (option['channel_id']), постит в
    него только этот бот.

    Сверяем ПЛОСКИЙ текст, а не то, что отправляли. Подписи постов — HTML
    (`<b>`/`<i>`/`<blockquote>`/`<emoji>`), а Telegram хранит текст БЕЗ разметки (она уезжает
    в entities), поэтому `message.caption` короче исходной строки и сравнение «в лоб» не
    совпало бы НИКОГДА — то есть проба всегда возвращала бы None и каждый таймаут давал дубль.
    Прогоняем свою подпись через ТОТ ЖЕ парсер, которым её отправлял Pyrogram
    (`client.parser`, режим клиента), и сравниваем результат.

    Смотрим ТОЛЬКО сообщения свежее `since`: подпись сама по себе не уникальна — у
    баг-картинки (`main_bug_message`) текст статический, и без окна проба нашла бы ПРОШЛОЕ
    такое же сообщение и доложила о доставке того, что не доставлено.

    Идёт по ОСНОВНОЙ сессии (она живёт часами и не страдает от фильтра на установление
    соединений), поэтому проба дешёвая.

    :param since: момент начала отправки (naive local — как `message.date`, который
                  pyrofork собирает через `datetime.fromtimestamp` без tz)
    :return: message_id найденного поста | None (не нашли ИЛИ проба не удалась — тогда
             повторяем: потерянный сигнал и рестарт юнита дороже редкого дубля).
    """
    async def _scan():
        parsed = await get_app().parser.parse(caption)
        plain = (parsed or {}).get('message') or ''
        if not plain:  # распарсить не смогли — сверять не с чем, честнее не угадывать
            return None
        async for message in get_app().get_chat_history(channel_id, limit=TG_HISTORY_PROBE_LIMIT):
            posted_at = message.date
            if posted_at is None:
                continue
            if posted_at.tzinfo is not None:  # страховка на случай смены типа в pyrofork
                posted_at = posted_at.astimezone().replace(tzinfo=None)
            if posted_at < since:  # старее нашей отправки — чужой/прошлый пост
                continue
            if (message.caption or '') == plain:
                return message.id
        return None

    try:
        return await asyncio.wait_for(_scan(), timeout=TG_HISTORY_PROBE_TIMEOUT)
    except (Exception,) as error:
        logger.warning("Проба истории канала не удалась (%s) — повторяю отправку вслепую", error)
        return None


async def send_photo_safe(photo, caption, mes_type: str,
                          timeout: float = TG_SEND_TIMEOUT,
                          return_message: bool = False):
    """Единый помощник отправки фото в основной канал с таймаутом и восстановлением при
    обрыве (раньше этот паттерн дублировался в main_app._try_send, app.check_plus и
    app.dop_plus_message). :return: (ok, error_text); при return_message=True — плюс третий
    элемент message_id отправленного поста (None при неудаче) — нужен для пересылки вехи в
    тему форума (apps/forum_forward)."""
    # Засекаем ДО отправки: проба ниже смотрит только сообщения свежее этого момента.
    started_at = datetime.now() - timedelta(seconds=TG_HISTORY_PROBE_SKEW)
    try:
        sent = await asyncio.wait_for(
            get_app().send_photo(chat_id=channel_id, photo=photo, caption=caption),
            timeout=timeout)
        _reset_transient_strikes()  # пост ушёл — цепочка невылеченных транзиентов прервана
        return (True, '', sent.id) if return_message else (True, '')
    except asyncio.TimeoutError:
        # Таймаут — НЕ приговор посту (2026-08-15). Одна потеря пакета стоила рестарта юнита с
        # переподъёмом Firefox (~60-90с простоя + протухший сигнал), хотя обрыв связи в
        # lost_connection_photo лечится restart+повтором. Выравниваем: проба «не ушёл ли пост» →
        # один повтор → и только потом провал (его трактовку выше не трогаем).
        logger.warning("⏳ Таймаут отправки (%s) — проверяю, не ушёл ли пост", mes_type)
        posted_id = await _find_posted(caption, started_at)
        if posted_id is not None:
            logger.info("Пост (%s) всё-таки доставлен — повтор не нужен (id=%s)", mes_type, posted_id)
            _reset_transient_strikes()  # доказано: session жива и постит
            return (True, '', posted_id) if return_message else (True, '')
        try:
            # Повтор идёт по НОВОЙ media-сессии, если прежнюю уронило — то есть новый шанс
            # установить соединение (см. classes/upload_session).
            sent = await asyncio.wait_for(
                get_app().send_photo(chat_id=channel_id, photo=photo, caption=caption),
                timeout=TG_RECONNECT_TIMEOUT)
            logger.info("Пост (%s) ушёл с повтора после таймаута", mes_type)
            _reset_transient_strikes()
            return (True, '', sent.id) if return_message else (True, '')
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут отправки (%s): повтор тоже не уложился", mes_type)
            return (False, 'Таймаут Pyrogram', None) if return_message else (False, 'Таймаут Pyrogram')
        except (Exception,) as error:
            # Повтор упал не таймаутом (обрыв/401/FloodWait) — отдаём в штатную heal-ветку.
            logger.error("❌ Ошибка повтора после таймаута (%s): %s", mes_type, error)
            result = await lost_connection_photo(error=error, photo=photo, text=caption,
                                                 mes_type=mes_type, started_at=started_at)
            return result if return_message else result[:2]
    except (Exception,) as error:
        logger.error("❌ Ошибка отправки (%s): %s", mes_type, error)
        result = await lost_connection_photo(error=error, photo=photo, text=caption,
                                             mes_type=mes_type, started_at=started_at)
        return result if return_message else result[:2]


async def lost_connection_photo(error, photo, text, mes_type, started_at: datetime | None = None):
    """
    # обработка исключений Pyrogram для фото сообщений
    :param error: перехваченная ошибка
    :param photo: фото неотправленного сообщения
    :param text: текст неотправленного сообщения
    :param mes_type: Тип сообщения (первое, итоговое и т.д.)
    :param started_at: момент начала отправки — окно для пробы доставки перед resend'ом
                       (None → пробы не будет, повтор пойдёт вслепую, как раньше)
    :return: (ok, error_text, message_id) — message_id заполнен только при УСПЕШНОМ
             повторе отправки (для пересылки вехи в тему форума), иначе None.
    """
    global _transient_401_strikes
    bot = get_app()
    # FloodWait: Telegram просит подождать .value сек (флуд-лимит) — НЕ обрыв связи и НЕ отвал
    # сессии. Единственное лечение: выждать окно и повторить ОДИН раз (restart/ретраи бесполезны
    # и усугубят флуд). Окно выше потолка / повторный FloodWait → теряем пост, бот продолжает.
    # Без этой ветки FloodWait уходил бы в обрыв-эвристику ниже → пост терялся минуя ожидание.
    if isinstance(error, FloodWait):
        wait = int(getattr(error, 'value', 0) or 0) + 1
        if wait > _FLOODWAIT_MAX:
            logger.session(f'⚠️ Пост ({mes_type}) не доставлен: FloodWait {wait}s превышает '
                           f'потолок {_FLOODWAIT_MAX}s — пропуск')
            return False, 'FloodWait превышает потолок', None
        # Пробы доставки здесь НЕ нужно (в отличие от таймаута/обрыва ниже): FloodWait — это
        # ОТКАЗ Telegram принять запрос, пост заведомо не ушёл, дубля из повтора не будет.
        logger.warning(f'{mes_type}: FloodWait — ждём {wait}s и повторяю')
        await asyncio.sleep(wait)
        try:
            sent = await asyncio.wait_for(
                bot.send_photo(chat_id=channel_id, photo=photo, caption=text),
                timeout=TG_RECONNECT_TIMEOUT)
            _reset_transient_strikes()
            return True, '', sent.id
        except (Exception,) as err:
            logger.session(f'⚠️ Пост ({mes_type}) не доставлен: повтор после FloodWait не удался — {err}')
            return False, 'повтор после FloodWait не удался', None
    # session_failed = тип Unauthorized ИЛИ строковый маркер (AUTH_KEY_* и пр.). Но голый
    # Unauthorized («Auth key not found») бывает транзиентом на медиа-DC при ЖИВОМ ключе —
    # хоронить бота тогда нельзя. Маркеры однозначно мёртвые; голый 401 различаем get_me().
    if session_failed(error):
        key_alive_transient = isinstance(error, Unauthorized) and not await session_dead()
        if not key_alive_transient:
            await session_dead_shutdown(error)  # session мертва — штатный стоп без рестарта (sys.exit)
            return False, 'Сессия юзербота недействительна', None  # явный возврат: не полагаемся только на sys.exit
        # иначе: транзиент-401 при живом ключе → лечим как обрыв (restart + resend) ниже
    if 'Connection lost' in str(error) or isinstance(error, Unauthorized):
        # В heal-ветку с Unauthorized попадают ТОЛЬКО транзиент-401 при живом ключе (мёртвый
        # ключ ушёл в session_dead_shutdown выше). 'Connection lost' — сетевой обрыв, к
        # session-death не относится → счётчик-страйк не наращиваем.
        is_transient_401 = isinstance(error, Unauthorized)
        try:
            # Таймаут на restart+resend — зависший reconnect не должен вешать цикл (правило 6)
            await asyncio.wait_for(bot.restart(), timeout=TG_RECONNECT_TIMEOUT)
            logger.error(f'Транзиент-сбой отправки ({mes_type}): {error}. Переподключился (restart)')
            # Обрыв/401 рвут ОЖИДАНИЕ ответа, а не саму отправку — та же неопределённость, что у
            # таймаута выше: пост мог доехать до Telegram, а подтверждение потеряться, и слепой
            # resend дал бы дубль в канале. Пробу ставим ПОСЛЕ restart'а — по свежему соединению
            # она дешёвая и имеет шанс пройти (до restart'а канал связи и был сломан).
            posted_id = await _find_posted(text, started_at) if started_at else None
            if posted_id is not None:
                logger.info('Пост (%s) доехал до обрыва — повтор не нужен (id=%s)', mes_type, posted_id)
                _reset_transient_strikes()  # доказано: session жива и постит
                return True, '', posted_id
            sent = await asyncio.wait_for(
                bot.send_photo(chat_id=channel_id, photo=photo, caption=text),
                timeout=TG_RECONNECT_TIMEOUT)
            _reset_transient_strikes()  # вылечилось — цепочка прервана
            return True, '', sent.id
        except (Exception,) as err:
            if is_transient_401:
                _transient_401_strikes += 1
                if _transient_401_strikes >= TRANSIENT_401_MAX_STRIKES:
                    # N транзиент-401 ПОДРЯД не вылечились → вероятно session реально мертва,
                    # а get_me-проба её не уличила (таймаут/сеть на пробе). Эскалация в штатный
                    # стоп: 🔒 в session-канал + status=false + graceful-выход (без рестарта).
                    await session_dead_shutdown(
                        error, reason=f'{_transient_401_strikes} транзиент-401 подряд не вылечились')
                    return False, 'Сессия юзербота недействительна (эскалация транзиент-401)', None
                # Порог не достигнут → ⚠️ в session-канал, бот продолжает (status не трогаем).
                # Это пока не 🔒-отвал — переавторизация не требуется.
                logger.session(f'⚠️ Пост ({mes_type}) не доставлен: транзиент-401 не вылечился '
                               f'restart+повтором ({_transient_401_strikes}/{TRANSIENT_401_MAX_STRIKES}): {err}')
            else:
                # Обрыв связи не вылечился restart+повтором — пост потерян, бот продолжает.
                logger.session(f'⚠️ Пост ({mes_type}) не доставлен: обрыв связи '
                               f'не вылечился restart+повтором: {err}')
            return False, f'Переподключиться не удалось - {err}', None
    else:
        error_message = f'Ошибка отправки {mes_type}! - {error}'
        return False, error_message, None
