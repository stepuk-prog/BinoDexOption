import asyncio
import signal

from datetime import datetime, timedelta

from apps.app import get_water, time_sleep, request_shutdown
from apps.browser_app import init_load
from apps.cookie_refresh import refresh_otc_cookies
from apps.exit_app import (close_program, session_dead_shutdown, session_failed,
                           write_status_offline)
from apps.main_app import main
from apps.otc_app import otc_session_dead
from classes.exceptions import CookiesExpired
from logs import init_logger
from messages import weekend_message, start_message
from settings.config import get_app, channel_id, binary, database, program_id, cook_name_otc
from settings.timing import USERBOT_RETRY_DELAY, USERBOT_CONNECT_ATTEMPTS, TG_SEND_TIMEOUT

logger = init_logger(__name__)

# Отвал cookies — реакция зависит от режима (§4.3):
#   • TV  — Survive: НЕ выход, пауза + пересоздание браузера (куки перечитываются из БД), анти-спам
#           бэкофф (первые COOKIES_FAST_ATTEMPTS попыток 120с, далее 300с), крутимся пока не починят.
#   • OTC — есть авто-рефрешер (binodex_session.py): восстанавливаем куки сами (3 попытки), при
#           неуспехе — выход со status=false (предохранитель §4.3). См. _recover_otc_cookies.
# Прочий провал init (не cookies) — пауза INIT_RETRY_DELAY и повтор.
INIT_RETRY_DELAY = 10
COOKIES_RETRY_DELAY_FAST = 120     # TV
COOKIES_RETRY_DELAY_SLOW = 300     # TV
COOKIES_FAST_ATTEMPTS = 5          # TV

# Счётчик подряд идущих отвалов cookies (для бэкоффа). Сбрасывается при успешном init.
_cookie_fails = 0
# Событие остановки (SIGTERM/SIGINT). Глобально — чтобы cookies-backoff (до 300с) в
# _init_with_retry прерывался сигналом, а не ждал SIGKILL. Ставится в bot() ДО первого init.
_stop_event: asyncio.Event | None = None


def _reset_cookie_fails():
    global _cookie_fails
    _cookie_fails = 0


async def _interruptible_sleep(seconds: float) -> bool:
    """Сон, прерываемый сигналом остановки. True — проснулись по сигналу (надо завершаться),
    False — по таймауту. До установки _stop_event (самый первый init) — обычный sleep."""
    if _stop_event is None:
        await asyncio.sleep(seconds)
        return False
    if _stop_event.is_set():
        return True
    try:
        await asyncio.wait_for(_stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _handle_cookie_failure(detail: str = '') -> bool:
    """Отвал cookies — сообщение в cookies-канал + анти-спам пауза (120с×N, далее 300с) +
    возврат (caller пересоздаёт браузер, init перечитает куки из БД). См. §4.3.
    :return: True, если пауза прервана сигналом остановки (надо завершаться)."""
    global _cookie_fails
    _cookie_fails += 1
    delay = (COOKIES_RETRY_DELAY_FAST if _cookie_fails <= COOKIES_FAST_ATTEMPTS
             else COOKIES_RETRY_DELAY_SLOW)
    mode_label = 'TV' if binary else 'OTC'
    logger.cookies(f'{mode_label}: отвал cookies (попытка {_cookie_fails}, пауза {delay // 60} мин, '
                   f'пересоздаю браузер). {detail}'.rstrip())
    return await _interruptible_sleep(delay)


# OTC: авто-восстановление кук — оркестратор apps/cookie_refresh.py (asyncpg + воркер-подпроцесс).
RECOVER_ATTEMPTS = 3       # попыток восстановления перед выходом


async def _recover_otc_cookies() -> bool:
    """OTC: восстановить куки авто-рефрешем (до RECOVER_ATTEMPTS попыток). Авто-рефрешер есть, поэтому
    политика — НЕ вечный Survive, а предохранитель (§4.3): получилось → продолжаем; нет после всех
    попыток → пишем status=false и выходим (status=0, диспетчер не рестартит мёртвые куки по кругу).
    :return: True — куки восстановлены (caller переинициализируется); False — остановлены сигналом."""
    logger.cookies(f'Куки отвалились для {cook_name_otc}. Пытаюсь восстановить')
    for attempt in range(1, RECOVER_ATTEMPTS + 1):
        if _stop_event is not None and _stop_event.is_set():
            return False
        if await refresh_otc_cookies():
            logger.cookies(f'Куки для {cook_name_otc} успешно восстановлены. Продолжаю работу')
            return True
        logger.warning(f'Восстановление куки для {cook_name_otc}: попытка {attempt}/{RECOVER_ATTEMPTS} не удалась')
    logger.cookies(f'Не могу восстановить куки для {cook_name_otc}. Останавливаю работу')
    await write_status_offline(program_id)
    await close_program(manager=None, status=0,
                        text=f'Не восстановить куки для {cook_name_otc} 🍪🛑')  # делает sys.exit
    return False


async def _init_with_retry():
    """init_load с обработкой отвала cookies. OTC (§4.3): CookiesExpired → авто-восстановление
    рефрешером (3 попытки → иначе выход). TV: Survive-backoff (сообщение + пауза, на повторе init
    перечитает куки из БД), БЕЗ выхода. Прочий провал init_load → пауза INIT_RETRY_DELAY и повтор.
    Паузы прерываются сигналом остановки.
    :return: BrowserManager либо None (остановлены сигналом во время init/backoff)."""
    while True:
        if _stop_event is not None and _stop_event.is_set():
            return None
        try:
            manager = await init_load()
        except CookiesExpired as error:
            if not binary:  # OTC: рефрешер (3 попытки); при провале _recover_otc_cookies сам выйдет
                if await _recover_otc_cookies():
                    continue  # успех → новый виток init_load прочитает свежие куки из БД
                return None   # сюда — только если остановлены сигналом
            if await _handle_cookie_failure(str(error)):  # TV: пауза прервана сигналом
                return None
            continue  # пересоздаём на новом витке — init перечитает куки
        if manager:
            _reset_cookie_fails()  # init удался → куки живы, сбрасываем бэкофф
            return manager
        logger.error(f'init_load провалился — пауза {INIT_RETRY_DELAY}с и повтор')
        if await _interruptible_sleep(INIT_RETRY_DELAY):
            return None


async def _recreate_browser(manager):
    """Закрыть текущий браузер и поднять заново через _init_with_retry (Survive §4.3).
    :return: новый BrowserManager либо None (остановлены сигналом)."""
    try:
        await manager.close()
    except (Exception,) as error:
        logger.warning(f'Ошибка закрытия браузера при пересоздании: {error}')  # утечка Firefox не должна быть незаметной
    return await _init_with_retry()


async def bot():
    """Запуск бота"""
    logger.report('🚀 Стартую')

    # Поднимаем пулы БД (program + binodex) до первого запроса. Раньше get_app/
    # session_dead_shutdown — последний пишет close_program в БД при отвале юзербота.
    await database.connect()

    # Создаём Pyrogram Client внутри event loop
    app = get_app()

    # Запуск юзербота — две ветки (§3.2). A: ключ доказано мёртв (session_failed) →
    # сразу штатный стоп с записью status=false и session-алертом, без ретраев (каждая
    # попытка пойдёт с тем же отозванным ключом). B: transient-обрыв (сеть/таймаут) →
    # до USERBOT_CONNECT_ATTEMPTS попыток; не переподключились → тот же плановый выход.
    last_error = None
    for attempt in range(1, USERBOT_CONNECT_ATTEMPTS + 1):
        try:
            await app.start()
            break
        except (Exception,) as error:
            if session_failed(error):                # ветка A — без ретраев
                await session_dead_shutdown(error)   # sys.exit(0); return — страховка
                return
            last_error = error                       # ветка B — копим и ретраим
            logger.warning(f"Попытка {attempt}/{USERBOT_CONNECT_ATTEMPTS} запуска юзербота: {error}")
            try:
                if getattr(app, "is_connected", False):
                    await app.stop()
            except (Exception,):
                pass
            if attempt < USERBOT_CONNECT_ATTEMPTS:
                await asyncio.sleep(USERBOT_RETRY_DELAY)
    else:
        # Все попытки исчерпаны без переподключения → session невалидна (§3.2) → плановый выход.
        await session_dead_shutdown(last_error,
                                    reason=f'нет переподключения за {USERBOT_CONNECT_ATTEMPTS} попыток')
        return

    if binary:
        now = datetime.now()  # один снимок времени — иначе возможен переход минуты/часа между вызовами
        if now.isoweekday() == 1 and now.hour == 3 and now.minute < 25:
            try:
                await asyncio.wait_for(
                    app.send_photo(chat_id=channel_id, photo='pictures/start_week.png', caption=start_message()),
                    timeout=TG_SEND_TIMEOUT)
            except (Exception,) as error:
                logger.error(f'Ошибка отправки стартового сообщения - {error}')
        if (now + timedelta(hours=2)).weekday() >= 5:
            try:
                await asyncio.wait_for(
                    app.send_photo(chat_id=channel_id, photo='pictures/end_week.png', caption=weekend_message()),
                    timeout=TG_SEND_TIMEOUT)
            except (Exception,) as error:
                logger.error(f'Ошибка отправки сообщения о выходных - {error}')
            await write_status_offline(program_id)
            await close_program(manager=None, status=0, text='Закрываюсь 🔱 (выходные)')
            return

    water_naked = get_water()
    qr = water_naked[1] if water_naked[0] else None

    # Graceful shutdown по SIGTERM/SIGINT (systemctl stop / диспетчер) — async-вариант:
    # signal.signal+KeyboardInterrupt в asyncio не ловится внутри корутины, поэтому через
    # loop.add_signal_handler + Event. Ставим ДО init: cookies-backoff (до 300с) в
    # _init_with_retry прерывается этим сигналом (иначе SIGTERM ждал бы SIGKILL).
    global _stop_event
    stop_event = asyncio.Event()
    _stop_event = stop_event
    loop = asyncio.get_running_loop()

    def _on_stop_signal():
        stop_event.set()
        request_shutdown()  # подавить main_bug_message — это штатная остановка, не сбой

    # Через переменную — гасит ложную инспекцию сигнатуры add_signal_handler
    # (*args в стабе ошибочно считается обязательным; рантайму он не нужен).
    register_signal = loop.add_signal_handler
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            register_signal(_sig, _on_stop_signal)
        except NotImplementedError:
            pass  # Windows — graceful по сигналам недоступен

    # Survive §4.3: init с бэкоффом при отвале cookies — без выхода, крутим пока не починят.
    manager = await _init_with_retry()
    if manager is None:  # остановлены сигналом во время init/cookies-backoff (close_program сам гасит юзербот)
        await write_status_offline(program_id)
        await close_program(manager=None, status=0, text='Остановлен сигналом 🛑')
        return

    logger.info("✅ Браузер инициализирован, страницы: %s", list(manager.pages.keys()))
    logger.info("🔄 Переход в main loop...")

    while not stop_event.is_set():
        res_option = await main(manager=manager, qr=qr, stop_event=stop_event)

        # Остановка по сигналу (SIGTERM/SIGINT): ошибка из-за гибели Playwright-драйвера —
        # это штатный стоп, не сбой; уходим в graceful-ветку ниже (status=false).
        if stop_event.is_set():
            break

        # OTC: отвал cookies в рантайме (§4.1). ОСНОВНОЙ сигнал — otc_session_dead (редирект с
        # /trade ИЛИ мёртвый WS-фид). ВТОРИЧНЫЙ — эвристика «цена не меняется N циклов» (на плоском
        # рынке даёт ложняки). Реакция — пересоздание браузера: если куки реально мертвы, init
        # упрётся в CookiesExpired → _init_with_retry запустит авто-восстановление рефрешером
        # (3 попытки → иначе выход). Если умер только WS (куки живы) — init поднимется без рефреша.
        if not binary:
            dead, reason = await otc_session_dead(manager)
            if not dead and res_option.check_cookies > 2:
                dead, reason = True, 'цена не меняется N циклов подряд (вторичный сигнал)'
            if dead:
                logger.cookies(f'OTC: отвал cookies в рантайме ({reason}) — пересоздаю браузер')
                manager = await _recreate_browser(manager)
                if manager is None:  # остановлены сигналом во время пересоздания
                    break
                continue

        # Критическая ошибка (краш, НЕ cookies) → выход; диспетчер рестартит (§1).
        if not res_option.result and res_option.fall:
            await close_program(manager=manager, status=1,  # сам гасит юзербот (_close_userbot)
                                text=f'Перезагрузка бота ☄️. Ошибка - {res_option.bug_text}')
            return  # close_program делает sys.exit; явный выход (правило 9)

        # Прерываемый сон: проснёмся сразу при сигнале остановки
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=await time_sleep())
        except asyncio.TimeoutError:
            pass

        if binary and not stop_event.is_set():
            if (datetime.now() + timedelta(hours=2)).weekday() >= 5:
                if not res_option.plus:
                    # Прерываемый сон (как выше) — иначе SIGTERM завис бы тут на 100–150с
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=await time_sleep())
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    await asyncio.wait_for(
                        app.send_photo(chat_id=channel_id, photo='pictures/end_week.png', caption=weekend_message()),
                        timeout=TG_SEND_TIMEOUT)
                except (Exception,) as error:
                    logger.error(f'Ошибка отправки сообщения о выходных - {error}')
                await write_status_offline(program_id)
                await close_program(manager=manager, status=0, text='Закрываюсь 🔱')  # сам гасит юзербот
                return

    # Сюда — только по SIGTERM/SIGINT: помечаем программу остановленной (status=false)
    # и чисто закрываемся. Юзербот гасит сам close_program (_close_userbot с таймаутом);
    # единственное сообщение о закрытии шлёт close_program(text=...) ниже.
    await write_status_offline(program_id)
    await close_program(manager=manager, status=0, text='Остановлен сигналом 🛑')


if __name__ == "__main__":
    asyncio.run(bot())