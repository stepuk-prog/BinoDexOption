"""Штатная остановка процесса: флаг, событие и прерываемый сон. ОДНО место на программу.

Почему отдельным модулем, а не в `apps/app.py` (где это жило до 12-09-2026): ждать остановку
нужно и там, куда `apps.app` тянуть нельзя. `apps.app` импортирует `apps.browser_app`, то есть
Playwright и весь стартовый bootstrap настроек, а потребители — `apps/my_exeptions.py` (его
`apps.app` импортирует сам, вышло бы кольцо) и `apps/binodex_feed.py` (он существует ровно
затем, чтобы проверять фид БЕЗ браузера). Здесь же зависимостей нет вообще, кроме asyncio,
поэтому импортировать можно откуда угодно и на уровне модуля.

`asyncio.Event` с Python 3.10 не привязывается к loop'у при создании, поэтому объявить событие
на уровне модуля (до старта loop'а) безопасно.
"""
import asyncio

_shutdown_requested = False
_shutdown_event = asyncio.Event()


def request_shutdown() -> None:
    """Пометить штатную остановку (зовётся из обработчика SIGTERM/SIGINT).

    Делает две вещи: поднимает флаг (по нему `exit_main` не шлёт баг-картинку, а `find_price`/
    `screenshot` не шумят в error-канал — сбои после сноса драйвера это не сбои) и будит всех,
    кто ждёт через `sleep_or_stop(shutdown_event(), ...)`."""
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_event.set()


def shutdown_requested() -> bool:
    """Была ли запрошена штатная остановка."""
    return _shutdown_requested


def shutdown_event() -> asyncio.Event:
    """Событие штатной остановки. Геттер, а не объект в импорте: импортёр иначе защёлкнул бы
    ссылку на момент импорта."""
    return _shutdown_event


async def sleep_or_stop(stop_event, seconds: float) -> bool:
    """Прерываемый сон: True — проснулись по сигналу остановки, False — по таймауту.

    ОДНА реализация на программу (их было две — `main._interruptible_sleep` и
    `main_app._sleep_or_stop`: одинаковая механика, разные сигнатуры и разный контракт возврата).
    Нужна везде, где ждём долго: иначе `sleep(option_time/dgn_time/backoff/FloodWait)` держал бы
    graceful-shutdown минутами, с риском SIGKILL и недозакрытых БД/браузера.

    `stop_event=None` (самый ранний init, событие ещё не создано) — обычный sleep."""
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    if stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
