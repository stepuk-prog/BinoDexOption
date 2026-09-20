"""Штатная остановка процесса: флаг, событие и прерываемый сон. ОДНО место на программу.

Почему отдельным модулем, а не в `apps/app.py` (где это жило до 12-09-2026): ждать остановку
нужно и там, куда `apps.app` тянуть нельзя. `apps.app` импортирует `apps.browser_app`, то есть
Playwright и весь стартовый bootstrap настроек, а потребители — `apps/my_exeptions.py` (его
`apps.app` импортирует сам, вышло бы кольцо) и `apps/binodex_feed.py` (он существует ровно
затем, чтобы проверять фид БЕЗ браузера). Здесь же зависимостей нет вообще, кроме asyncio и threading,
поэтому импортировать можно откуда угодно и на уровне модуля.

`asyncio.Event` с Python 3.10 не привязывается к loop'у при создании, поэтому объявить событие
на уровне модуля (до старта loop'а) безопасно.
"""
import asyncio
import threading

_shutdown_requested = False
_shutdown_event = asyncio.Event()
# То же самое для ПОТОКОВ. `asyncio.Event` из потока ждать нельзя (он не потокобезопасен и
# завязан на loop), а ждать остановку нужно именно оттуда: ожидание кода из письма при
# inline-релогине крутится в `asyncio.to_thread` (binocore.binodex.wait_for_code). Два события
# вместо одного — не дубль состояния: оба ставит ровно один `request_shutdown`, и каждое умеет
# то, чего не умеет другое.
_stop_thread_event = threading.Event()


def request_shutdown() -> None:
    """Пометить штатную остановку (зовётся из обработчика SIGTERM/SIGINT).

    Делает две вещи: поднимает флаг (по нему `exit_main` не шлёт баг-картинку, а `find_price`/
    `screenshot` не шумят в error-канал — сбои после сноса драйвера это не сбои) и будит всех,
    кто ждёт остановку: и async-сторону (`sleep_or_stop(shutdown_event(), ...)`), и потоки
    (`wait_stop`)."""
    global _shutdown_requested
    _shutdown_requested = True
    _shutdown_event.set()
    _stop_thread_event.set()


def shutdown_requested() -> bool:
    """Была ли запрошена штатная остановка."""
    return _shutdown_requested


def shutdown_event() -> asyncio.Event:
    """Событие штатной остановки. Геттер, а не объект в импорте: импортёр иначе защёлкнул бы
    ссылку на момент импорта."""
    return _shutdown_event


def wait_stop(seconds: float) -> bool:
    """Пауза `seconds`, прерываемая остановкой процесса. True — пора уходить, False — вышло время.

    БЛОКИРУЮЩАЯ по построению: её зовут ИЗ ПОТОКА вместо `time.sleep`. Контракт ровно тот,
    которого ждёт `binocore.binodex.wait_for_code` от своего `stop_wait` (apps/otc_login) —
    менять знак возврата нельзя, там он читается как «пора уходить»."""
    return _stop_thread_event.wait(seconds)


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
