"""Переиспользуемая media-сессия для аплоада фото (pyrofork 2.3.x).

ЗАЧЕМ. pyrofork на КАЖДЫЙ аплоад строит новую `Session(is_media=True)` и гасит её в
`finally` (`pyrogram/methods/advanced/save_file.py`: создание — строка 144, `stop()` —
223). Постоянного media-соединения нет: каждый пост с фото открывает новое TCP.
Основная сессия при этом живёт часами (её держит штатный `ping_worker`,
`PING_INTERVAL = 5с`), поэтому текстовые посты уходят даже тогда, когда фото не уходит.

Инцидент 2026-08-15: Telegram начал дропать SYN к амстердамским адресам (DC2/DC4 и
media-DC `149.154.167.151`) с подсетей Fornex и Unihost — замер дал 16–53% потерь на
установление соединения при нулевых потерях на транзитных хопах, к DC Майами/Сингапура
и к контрольным адресам. Установленные соединения фильтр не трогает — страдает только
момент коннекта. В итоге падал ровно `send_photo`: каждый пост заново проходил через
полисер, три попытки pyrofork (3 × `TCP.TIMEOUT` 10с + 2 × sleep 1с ≈ 32с) не
укладывались в `TG_SEND_TIMEOUT`, и таймаут трактовался как фатальный → рестарт юнита
с переподъёмом Firefox.

ЧТО ДЕЛАЕТ. Подменяет `Session` в модуле `save_file` на фабрику, отдающую ОДНУ живую
сессию на процесс: `start()` идемпотентен, `stop()` — no-op. Соединение переживает
паузы между постами за счёт того же `ping_worker`, что держит основную сессию. SYN
через полисер остаётся один — при первом аплоаде после старта бота.

ОБЛАСТЬ. Только аплоад (модуль `save_file`). Скачивание (`client.py`) и CDN-сессии не
затронуты — фабрика отдаёт им оригинальный `Session`.

ПРОТУХШАЯ SESSION ЮЗЕРБОТА. Патч НЕ глотает и НЕ ретраит ошибки молча: любая ошибка
`start()`/`invoke()` закрывает и выбрасывает сессию из кэша (следующий аплоад поднимет
новую) и пробрасывается наверх БЕЗ изменений. Различение «ключ реально мёртв» vs
«транзиент на медиа-DC при живом ключе» остаётся там, где было — за
`apps/my_exeptions.session_dead()`, который бьёт `get_me()` по ОСНОВНОЙ сессии. Патч
эту диагностику не подменяет и не маскирует.
Отдельно закрыт риск залипания на отозванном ключе: `auth_key` сверяется при каждой
выдаче сессии из кэша, и при смене (релогин, переавторизация юзербота) кэш
пересоздаётся, а старое соединение закрывается — переиспользование не может пережить
смену ключа.

ВЕРСИЯ. Контракт узкий: `save_file` дёргает у сессии ровно три метода — `start()`
(150), `invoke()` (109), `stop()` (223). Проверено на pyrofork 2.3.69; в
`requirements.txt` он закреплён как `pyrofork~=2.3.69`. При обновлении минорной версии
патч сам себя отключает, если в модуле не окажется ожидаемого `Session` (см. `install`).
"""

import asyncio
from typing import Any, Optional

import pyrogram.methods.advanced.save_file as _save_file_module
from pyrogram.errors import Unauthorized
from pyrogram.session import Session as _RealSession

from logs import init_logger

logger = init_logger(__name__)

# Ошибки, после которых переиспользовать соединение НЕЛЬЗЯ (в отличие от RPC-ошибок при живом
# канале, см. ReusableMediaSession.invoke): обрыв/таймаут сокета — сессия мертва; Unauthorized —
# ключ, на котором она поднята, больше не годится. OSError покрывает ConnectionError и (в 3.11)
# TimeoutError, но перечисляем явно — контракт не должен зависеть от иерархии версии Python.
_CONNECTION_DEAD = (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError, Unauthorized)

# Оригинальный конструктор — им поднимаем реальные соединения и обслуживаем
# не-media/CDN вызовы. Берём один раз при импорте, до любой подмены.
# ЧЕРЕЗ getattr: прямое обращение бросило бы AttributeError на ИМПОРТЕ, если в будущей
# версии pyrofork имени не окажется, — и бот не поднялся бы вовсе, а ветка самоотключения
# в install() до этого просто не дожила бы.
_ORIGINAL_SESSION = getattr(_save_file_module, "Session", None)

# Фоновые задачи гашения. Держим ссылки: loop хранит на таск только слабую ссылку, и
# сборщик вправе прибрать его посреди работы — тогда соединение так и осталось бы висеть,
# ровно то, ради чего задача и заводилась.
_closing_tasks: set[asyncio.Task] = set()


def _spawn_close(coro) -> None:
    """Запустить закрытие в фоне, не блокируя вызывающего, и удержать ссылку на таск."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # loop уже не крутится — гасить некому и незачем
        coro.close()      # корутину, которую никто не ждёт, гасим явно (иначе RuntimeWarning)
        return
    _closing_tasks.add(task)
    task.add_done_callback(_closing_tasks.discard)


def _close_session_later(session) -> None:
    """Погасить осиротевшее соединение в фоне.

    Нужна там, где ждать нельзя: в `except BaseException` после `CancelledError` любой
    `await` снова словил бы отмену, и сокет остался бы висеть.
    """
    async def _stop():
        try:
            await session.stop()
        except (Exception,) as error:
            logger.debug(f"Ошибка закрытия осиротевшей media-сессии: {error}")

    _spawn_close(_stop())


class ReusableMediaSession:
    """Прокси над `Session`: одно media-соединение, переживающее аплоады.

    Реализует ровно тот контракт, который использует `save_file`: `start`/`invoke`/`stop`.
    `stop()` намеренно ничего не делает — соединение держим живым (в этом весь смысл);
    реальное закрытие — `close()` на остановке бота.
    """

    def __init__(self, client, dc_id, auth_key, test_mode):
        self._client = client
        self._dc_id = dc_id
        self._auth_key = auth_key
        self._test_mode = test_mode
        self._session: Optional[_RealSession] = None
        # Один аплоад может идти несколькими воркерами (`workers_count` = 4 для файлов
        # >10МБ), плюс возможны параллельные посты — поднимаем соединение под локом.
        self._lock = asyncio.Lock()

    @property
    def auth_key(self):
        """Ключ, на котором поднято соединение — для сверки при выдаче из кэша."""
        return self._auth_key

    async def start(self) -> None:
        """Поднять соединение, если его ещё нет. Идемпотентно.

        Своего таймаута не ставим: вызов уже накрыт внешним `wait_for` в
        `my_exeptions.send_photo_safe` (`TG_SEND_TIMEOUT`), а второй потолок поверх
        собственных ретраев pyrofork только запутал бы диагностику.
        """
        async with self._lock:
            if self._session is not None:
                return
            session = _ORIGINAL_SESSION(self._client, self._dc_id, self._auth_key,
                                        self._test_mode, is_media=True)
            try:
                await session.start()
            except BaseException:
                # BaseException, а не (Exception,): внешний wait_for в send_photo_safe рвёт
                # start() через CancelledError, который мимо Exception проходит. К этому
                # моменту Session мог уже поднять сокет и recv_task (первый Ping/
                # InitConnection идут ПОСЛЕ connection.connect()), а ссылки на него ни у кого
                # нет — каждый такой таймаут оставлял бы висящий сокет и таск.
                # Закрываем в фоне: ждать в отменяемом контексте нельзя.
                self._session = None
                _close_session_later(session)
                raise
            self._session = session
            logger.info(f"Media-сессия аплоада поднята (DC{self._dc_id}) — переиспользуется")

    async def invoke(self, data: Any, *args, **kwargs):
        """Проксировать вызов в живую сессию.

        Соединение выбрасываем ТОЛЬКО на ошибках уровня связи/ключа (`_CONNECTION_DEAD`).
        Раньше инвалидировала любая ошибка — и это работало против самого патча: pyrofork
        внутри `Session.invoke` сам ретраит сетевые сбои (`OSError`/5xx, до `MAX_RETRIES`=10)
        и отсыпается на FloodWait, поэтому наружу выходят в основном RPC-ошибки при ЖИВОМ
        соединении (`FILE_PART_*`, FloodWait выше порога). Рвать на них соединение — значит
        гнать следующий аплоад заново через SYN-фильтр ради чужой проблемы.

        Любая ошибка пробрасывается КАК ЕСТЬ: решение о судьбе юзербота (мёртвый ключ vs
        транзиент медиа-DC) принимает `my_exeptions`/`exit_app` по своей пробе `get_me()`,
        а не этот слой.

        Отмена (`CancelledError`) выбрасывает соединение — см. ветку ниже.
        """
        session = self._session
        if session is None:
            # Соединение выбросила ПРЕДЫДУЩАЯ ошибка (обрыв/отмена), а `save_file` зовёт
            # `start()` РОВНО ОДИН раз в начале аплоада — следующие части файла его сами не
            # переподнимут. Без переподъёма здесь весь текущий аплоад падал на
            # «media-сессия не поднята», а внешний `wait_for` срубал его по таймауту:
            # инцидент 2026-08-15 15:15 на NODE-1 (signals_fast) — `OSError: Connection lost`
            # на одной части, дальше `ConnectionError` на следующей и `TIMEOUT 60s` наверху.
            # `start()` идемпотентен и берёт лок, так что повторный вход безопасен.
            await self.start()
            session = self._session
            if session is None:      # start() отработал, а сессии нет — отдаём честную ошибку
                raise ConnectionError("media-сессия аплоада не поднята")
        try:
            return await session.invoke(data, *args, **kwargs)
        except _CONNECTION_DEAD:
            await self._invalidate()
            raise
        except Exception:
            # RPC-ошибка при ЖИВОМ соединении (FILE_PART_*, FloodWait выше порога) —
            # соединение исправно, рвать его ради чужой проблемы нельзя (см. docstring).
            raise
        except BaseException:
            # Сюда попадает только не-Exception, то есть на практике CancelledError:
            # внешний `wait_for(...)` в `my_exeptions.send_photo_safe` отменяет отправку, и отмена прилетает
            # ровно в этот await. Раньше ветки не было → соединение оставалось в кэше, хотя
            # аплоад на нём оборвали на полуслове, а `bot.restart()` его не чинит (прокси
            # живёт здесь, а не в client.media_sessions, и его stop() — no-op). Гасим без
            # await: в отменяемом контексте любой await словил бы отмену снова (та же
            # логика, что в start()).
            self._drop_session_nowait()
            raise

    async def stop(self) -> None:
        """no-op: `save_file` зовёт `stop()` в `finally`, а нам нужно живое соединение."""

    def _drop_session_nowait(self) -> None:
        """Забыть соединение и погасить его в фоне — версия `_invalidate` для отменяемого
        контекста (`except` после `CancelledError`), где ждать нельзя: и `async with
        self._lock`, и `session.stop()` снова словили бы отмену, а сокет остался бы висеть.

        Лок намеренно не берём. Гонка возможна только с `start()`, и её исход безопасен:
        тот увидит `_session is None` и поднимет новое соединение — ровно то, что нужно.
        """
        session, self._session = self._session, None
        if session is not None:
            _close_session_later(session)

    async def _invalidate(self) -> None:
        """Закрыть и забыть текущее соединение — следующий аплоад поднимет новое."""
        async with self._lock:
            session, self._session = self._session, None
        if session is None:
            return
        try:
            await session.stop()
        except (Exception,) as error:
            logger.debug(f"Ошибка закрытия media-сессии аплоада при инвалидации: {error}")

    async def close(self) -> None:
        """Реальное закрытие (остановка бота / смена ключа)."""
        await self._invalidate()


# Кэш прокси: ключ — (id клиента, dc_id). Клиент в процессе один, но id в ключе
# защищает от путаницы при пересоздании Client (тест-сценарии, реавторизация).
_sessions: dict[tuple[int, int], ReusableMediaSession] = {}


def _close_later(proxy: ReusableMediaSession) -> None:
    """Закрыть отставленный прокси, не блокируя синхронную фабрику."""
    _spawn_close(proxy.close())


def _session_factory(client, dc_id, auth_key, test_mode, is_media=False, is_cdn=False):
    """Подмена конструктора `Session` внутри `save_file`.

    Media-сессии отдаём из кэша; CDN и не-media — оригинальным конструктором, чтобы
    не менять поведение за пределами аплоада.
    """
    if is_cdn or not is_media:
        return _ORIGINAL_SESSION(client, dc_id, auth_key, test_mode,
                                 is_media=is_media, is_cdn=is_cdn)

    key = (id(client), dc_id)
    proxy = _sessions.get(key)
    # Смена auth_key = переавторизация юзербота: старое соединение больше не наше.
    if proxy is not None and proxy.auth_key != auth_key:
        logger.warning("auth_key юзербота сменился — пересоздаю media-сессию аплоада")
        _close_later(proxy)
        proxy = None
    if proxy is None:
        proxy = _sessions[key] = ReusableMediaSession(client, dc_id, auth_key, test_mode)
    return proxy


def install() -> bool:
    """Включить переиспользование media-сессии аплоада. Идемпотентно.

    :return: True — патч активен; False — версия pyrofork разошлась с ожидаемой,
             оставляем штатное поведение (сессия на каждый аплоад).
    """
    if getattr(_save_file_module, "Session", None) is _session_factory:
        return True
    if _ORIGINAL_SESSION is None or not hasattr(_save_file_module, "Session"):
        logger.warning("pyrofork: в save_file нет Session — переиспользование media-сессии "
                       "не включено (аплоад продолжит открывать соединение на каждый пост)")
        return False
    _save_file_module.Session = _session_factory
    logger.info("Media-сессия аплоада переиспользуется (одно соединение вместо одного на пост)")
    return True


async def close_all() -> None:
    """Закрыть все кэшированные media-сессии — на остановке бота."""
    proxies = list(_sessions.values())
    _sessions.clear()
    for proxy in proxies:
        try:
            await proxy.close()
        except (Exception,) as error:
            logger.warning(f"Ошибка закрытия media-сессии аплоада: {error}")
