import asyncio
import signal

from datetime import datetime, timedelta

from apps.app import get_water, time_sleep, request_shutdown
from apps.browser_app import init_load
from apps.exit_app import (close_program, session_dead_shutdown, session_failed,
                           session_recoverable, write_status_offline)
from apps.main_app import main
from apps.otc_app import otc_session_dead
from apps.binodex_feed import feed_alive, wait_for_feed
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from logs import init_logger
from messages import weekend_message, start_message
from settings.config import get_app, channel_id, binary, database, program_id, cook_name_otc
from settings.timing import (USERBOT_RETRY_DELAY, USERBOT_CONNECT_ATTEMPTS, TG_SEND_TIMEOUT,
                             USERBOT_CONNECT_TIMEOUT)
from settings.constant import EXIT_BROWSER, EXIT_COOKIES, EXIT_SETUP, BROWSER_MAX_ATTEMPTS

logger = init_logger(__name__)

# Отвал cookies — реакция зависит от режима (§4.3):
#   • TV  — Survive: НЕ выход, пауза + пересоздание браузера (куки перечитываются из БД), анти-спам
#           бэкофф (первые COOKIES_FAST_ATTEMPTS попыток 120с, далее 300с), крутимся пока не починят.
#   • OTC — релогин INLINE в основном браузере (apps/otc_login, из otc_app.init_otc);
#           _recover_otc_cookies считает циклы — до RECOVER_ATTEMPTS, потом плановый выход (§4.3).
# Прочий провал init (не cookies) — пауза INIT_RETRY_DELAY и повтор.
INIT_RETRY_DELAY = 10
SETUP_ATTEMPTS = 3  # попыток поднять/настроить OTC-сайт (SetupError mounted=True, селекторы) перед плановым выходом
SETUP_OUTAGE_BACKOFF = 300  # сек паузы при front-end аутэйдже binodex (SetupError mounted=False) — выживаем, не выходим
PROXY_BAN_TTL = 600  # сек: бан OTC-прокси, не поднявшего front-end (битый колокейшен/дохлый прокси)
# OTC: сколько front-end-аутэйджей подряд в прокси-режиме терпим (перебирая прокси), прежде чем
# переотбить ПРЯМОЙ режим. Прокси-фолбэк рассчитан на отравленный CDN-эдж (прокси садится на
# здоровый колокейшен в обход битого); но если лёг сам front-end binodex (аутэйдж), прокси не
# помогут — нельзя залипать в карусели навсегда: каждые N неудач возвращаемся на direct, чтобы
# поймать восстановление (иначе нода не оживёт без рестарта процесса — латч). §4.5.
PROXY_REPROBE_AFTER = 3
COOKIES_RETRY_DELAY_FAST = 120     # TV
COOKIES_RETRY_DELAY_SLOW = 300     # TV
COOKIES_FAST_ATTEMPTS = 5          # TV

# Счётчик подряд идущих отвалов cookies (для бэкоффа). Сбрасывается при успешном init.
_cookie_fails = 0
# OTC: счётчик циклов «init не поднялся даже после inline-релогина». Сбрасывается при успешном init.
_otc_recover_cycles = 0
# Событие остановки (SIGTERM/SIGINT). Глобально — чтобы cookies-backoff (до 300с) в
# _init_with_retry прерывался сигналом, а не ждал SIGKILL. Ставится в bot() ДО первого init.
_stop_event: asyncio.Event | None = None
# OTC: прокси-фолбэк. False = прямой режим (дефолт). Включается, когда прямой режим не поднял
# front-end binodex (SetupError mounted=False — напр. отравленный CDN-эдж); прокси из
# settings.proxy_data через локальный релей. НЕ sticky навсегда: после PROXY_REPROBE_AFTER неудач
# подряд прокси-режим сбрасывается обратно в direct (переотбивка — front-end мог восстановиться,
# иначе нода залипает до рестарта). FIN/TradingView фолбэк не использует.
_use_proxy = False
# OTC: счётчик front-end-аутэйджей подряд в прокси-режиме (для переотбивки direct). Сбрасывается
# при успешном init и при каждом новом заходе в прокси-режим.
_proxy_outage_streak = 0


def _reset_cookie_fails():
    global _cookie_fails, _otc_recover_cycles
    _cookie_fails = 0
    _otc_recover_cycles = 0


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


# OTC: релогин — INLINE в основном браузере (apps/otc_login, из otc_app.init_otc). Здесь только
# счётчик-предохранитель: сколько раз подряд init не поднялся даже после inline-логина.
RECOVER_ATTEMPTS = 3       # циклов init без успеха перед плановым выходом


async def _recover_otc_cookies() -> bool:
    """OTC-предохранитель. Сам релогин теперь INLINE в основном браузере (apps/otc_login,
    вызывается из otc_app.init_otc). Сюда CookiesExpired доходит = init не поднялся ДАЖЕ после
    inline-логина. Считаем подряд такие циклы: до RECOVER_ATTEMPTS → продолжаем (новый init снова
    попробует inline-релогин); исчерпали без единого успешного init → куки не восстановить →
    exit(EXIT_COOKIES): диспетчер рефрешит куки / рестартит. status НЕ трогаем (инвариант: §4.3).
    :return: True — продолжаем (re-init); False — остановлены сигналом."""
    global _otc_recover_cycles
    if _stop_event is not None and _stop_event.is_set():
        return False
    _otc_recover_cycles += 1
    if _otc_recover_cycles > RECOVER_ATTEMPTS:
        logger.cookies(f'OTC: {RECOVER_ATTEMPTS} циклов восстановления подряд без успешного init '
                       f'({cook_name_otc}) — релогин не помог. Останавливаю работу')
        await close_program(manager=None, status=EXIT_COOKIES,
                            text=f'Не восстановить сессию binodex для {cook_name_otc} 🍪🛑 (код {EXIT_COOKIES})')  # sys.exit
        return False  # страховка (close_program делает sys.exit)
    logger.warning(f'OTC: init не поднялся после inline-релогина ({cook_name_otc}), '
                   f'цикл {_otc_recover_cycles}/{RECOVER_ATTEMPTS} — повтор init')
    return True


async def _ban_current_proxy() -> None:
    """Текущий OTC-прокси не поднял front-end (битый колокейшен/дохлый) → бан в БД + перечитка
    пула (забаненный выпадает из выборки) → следующий init возьмёт другой. Сбой бана не критичен."""
    from settings.proxy import get_current_proxy, load_proxies_from_db
    proxy = get_current_proxy()
    if proxy is None:
        return
    try:
        await database.ban_proxy(proxy.ip, PROXY_BAN_TTL)
        await load_proxies_from_db(database)  # перечитать пул без забаненного
        logger.warning(f'OTC-прокси: {proxy.ip} забанен на {PROXY_BAN_TTL // 60} мин '
                       f'(не поднял front-end) — ротация на следующий')
    except (Exception,) as err:
        logger.warning(f'OTC-прокси: бан {proxy.ip} не удался: {err}')


async def _mark_proxy_success() -> None:
    """Текущий OTC-прокси поднял рабочий init → плюс в статистику (и снятие бана)."""
    from settings.proxy import get_current_proxy
    proxy = get_current_proxy()
    if proxy is None:
        return
    try:
        await database.update_proxy_stats(proxy.ip, success=True)
    except (Exception,):
        pass


async def _init_with_retry():
    """init_load с обработкой отвала cookies. OTC (§4.3): CookiesExpired → авто-восстановление
    рефрешером (3 попытки → иначе выход). TV: Survive-backoff (сообщение + пауза, на повторе init
    перечитает куки из БД), БЕЗ выхода. Прочий провал init_load → пауза INIT_RETRY_DELAY и повтор.
    Паузы прерываются сигналом остановки.
    :return: BrowserManager либо None (остановлены сигналом во время init/backoff)."""
    global _use_proxy, _proxy_outage_streak
    setup_streak = 0  # подряд идущих SetupError (UI/селекторы) — до SETUP_ATTEMPTS, потом выход
    browser_fails = 0  # подряд провалов подъёма браузера (НЕ куки/селекторы/прокси) → EXIT_BROWSER
    while True:
        if _stop_event is not None and _stop_event.is_set():
            return None
        try:
            manager = await init_load(use_proxy=_use_proxy)
        except FeedOutage as error:
            # OTC: авторизация жива, но market-WS молчит браузер-фри — аутэйдж binodex (детектор уже
            # подтвердил feed_alive=False), НЕ отвал кук. Ждём возврат фида БРАУЗЕР-ФРИ (без рефреша,
            # без выхода, без спама в cookies-канал). Фид вернулся → новый виток init.
            logger.warning(f'OTC: аутэйдж binodex ({error}) — жду восстановления фида браузер-фри')
            if not await _await_binodex_feed(at_start=True):
                return None  # остановлены сигналом во время ожидания фида
            continue
        except SetupError as error:
            if not error.mounted:
                # OTC: front-end binodex не поднялся при живых /trade+WS+токене — либо завис на
                # загрузочном сплеше (JS-бандл не смонтировался), либо упал в error-boundary
                # «Something went wrong» (ленивый чанк не загрузился, напр. отравленный CDN-кэш).
                # И то и другое — front-end АУТЭЙДЖ binodex, НЕ селекторы и НЕ куки (релогин не чинит).
                if not binary and not _use_proxy:
                    # Прямой режим не поднял front-end → включаем прокси-фолбэк и СРАЗУ ретраим, без
                    # долгого backoff: прокси может сесть на здоровый CF-колокейшен в обход битого
                    # (settings.proxy_data). Новый заход в прокси-режим → стрик с нуля. §4.5.
                    _use_proxy = True
                    _proxy_outage_streak = 0
                    logger.report(f'OTC: прямой режим не поднял front-end binodex — перехожу на '
                                  f'прокси-фолбэк (settings.proxy_data): {error}')
                    continue
                if not binary:  # уже на прокси и снова аутэйдж → прокси сел на битый колокейшен/мёртв
                    await _ban_current_proxy()
                    _proxy_outage_streak += 1
                    if _proxy_outage_streak >= PROXY_REPROBE_AFTER:
                        # Перебрали PROXY_REPROBE_AFTER прокси без mount — это уже не «битый эдж в
                        # обход», а аутэйдж самого front-end binodex (или весь регион). Прокси не
                        # спасут → возвращаемся на прямой режим: front-end мог восстановиться, и
                        # direct оживёт сам, БЕЗ рестарта процесса (фикс латча). §4.5.
                        _use_proxy = False
                        _proxy_outage_streak = 0
                        logger.report(f'OTC: {PROXY_REPROBE_AFTER} прокси подряд не подняли front-end '
                                      f'— похоже на аутэйдж binodex, не битый эдж; возвращаюсь на '
                                      f'прямой режим (переотбивка), пауза {SETUP_OUTAGE_BACKOFF // 60} мин')
                    else:
                        logger.warning(f'OTC: прокси не поднял front-end binodex — ротация '
                                       f'({_proxy_outage_streak}/{PROXY_REPROBE_AFTER}), пауза '
                                       f'{SETUP_OUTAGE_BACKOFF // 60} мин, выживаю: {error}')
                    if await _interruptible_sleep(SETUP_OUTAGE_BACKOFF):
                        return None
                    continue
                # FIN: прокси не применяем — прежнее поведение (выживание с backoff)
                logger.warning(f'front-end не поднялся — аутэйдж, пауза {SETUP_OUTAGE_BACKOFF // 60} '
                               f'мин, выживаю: {error}')
                if await _interruptible_sleep(SETUP_OUTAGE_BACKOFF):
                    return None
                continue
            # OTC: апп смонтирован, но наш селектор не найден — сменились селекторы binodex.
            # Рефреш бесполезен. SETUP_ATTEMPTS повторов (временный сбой) → не помогло → плановый
            # выход (нужно вручную обновить селекторы; §4.5).
            setup_streak += 1
            logger.warning(f'OTC: сайт не настроился ({setup_streak}/{SETUP_ATTEMPTS}): {error}')
            if setup_streak >= SETUP_ATTEMPTS:
                logger.cookies(f'OTC: сайт не настраивается за {SETUP_ATTEMPTS} попытки '
                               f'({cook_name_otc}) — нужно ручное вмешательство (селекторы binodex). Останавливаю')
                await close_program(manager=None, status=EXIT_SETUP,
                                    text=f'OTC: сайт не настраивается — проверить селекторы binodex ⚙️🛑 (код {EXIT_SETUP})')
                return None  # close_program делает sys.exit; страховка
            if await _interruptible_sleep(INIT_RETRY_DELAY):
                return None
            continue
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
            _proxy_outage_streak = 0  # init поднялся (direct или прокси) → стрик аутэйджей сброшен
            browser_fails = 0         # браузер поднялся → сбрасываем счётчик провалов подъёма
            if _use_proxy:
                await _mark_proxy_success()  # прокси поднял рабочий init → плюс в статистику
            return manager
        if not binary and _use_proxy:
            # На прокси init провалился (вероятно прокси мёртв) → бан+ротация; это НЕ поломка
            # браузера ноды, поэтому browser_fails не трогаем (иначе прокси-карусель ложно дала бы EXIT_BROWSER).
            await _ban_current_proxy()
        else:
            browser_fails += 1
            if browser_fails >= BROWSER_MAX_ATTEMPTS:
                # Браузер не поднялся подряд BROWSER_MAX_ATTEMPTS раз (не куки/селекторы/фид/прокси —
                # те идут своими ветками): нода, вероятно, не может поднять Firefox → отдаём диспетчеру
                # (failover на другую ноду), exit(EXIT_BROWSER). status НЕ трогаем (инвариант).
                await close_program(manager=None, status=EXIT_BROWSER,
                                    text=f'Браузер не поднялся {BROWSER_MAX_ATTEMPTS}× — отдаю ноду диспетчеру ☄️ (код {EXIT_BROWSER})')
                return None  # close_program делает sys.exit; страховка
        logger.error(f'init_load провалился — пауза {INIT_RETRY_DELAY}с и повтор')
        if await _interruptible_sleep(INIT_RETRY_DELAY):
            return None


async def _recreate_browser(manager):
    """Закрыть текущий браузер и поднять заново через _init_with_retry (Survive §4.3).
    :return: новый BrowserManager либо None (остановлены сигналом)."""
    try:
        # Верхняя граница: зависший Firefox-close не должен подвесить пересоздание браузера.
        await asyncio.wait_for(manager.close(), timeout=50)
    except (Exception,) as error:
        logger.warning(f'Ошибка закрытия браузера при пересоздании: {error}')  # утечка Firefox не должна быть незаметной
    return await _init_with_retry()


async def _await_binodex_feed(at_start: bool) -> bool:
    """OTC-аутэйдж: binodex не отдаёт котировки (рынок закрыт/сбой на стороне binodex). Ждём фид
    БРАУЗЕР-ФРИ (apps/binodex_feed) — без рестарт-петли и спама алертов: ОДНО уведомление вниз +
    одно вверх. True — котировки вернулись (можно поднимать браузер); False — остановлены сигналом.
    at_start=True — текст «браузер не поднимаю»; False (рантайм) — «выгрузил браузер»."""
    if at_start:
        logger.report('🕓 binodex не отдаёт котировки — браузер не поднимаю, жду восстановления фида')
    else:
        logger.report('🕓 binodex перестал отдавать котировки — выгрузил браузер, посты на паузе, жду восстановления')
    if not await wait_for_feed(_stop_event):
        return False  # SIGTERM во время ожидания
    logger.report('✅ binodex снова отдаёт котировки — поднимаю браузер, продолжаю работу')
    return True


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
    # попытка пойдёт с тем же отозванным ключом). B: transient-обрыв (сеть/таймаут) ИЛИ
    # AUTH_KEY_DUPLICATED (session_recoverable — ключ занят другой нодой при failover,
    # отпустится сам) → до USERBOT_CONNECT_ATTEMPTS попыток; не переподключились → тот же
    # плановый выход (отвал session, код EXIT_USERBOT).
    last_error = None
    for attempt in range(1, USERBOT_CONNECT_ATTEMPTS + 1):
        try:
            # Таймаут: SIGTERM-хендлер ставится ниже (после init), поэтому зависший хендшейк
            # Pyrogram здесь нельзя прервать сигналом — оборачиваем wait_for (TimeoutError → ветка B).
            await asyncio.wait_for(app.start(), timeout=USERBOT_CONNECT_TIMEOUT)
            break
        except (Exception,) as error:
            if session_failed(error) and not session_recoverable(error):  # ветка A — без ретраев
                await session_dead_shutdown(error)   # sys.exit(0); return — страховка
                return
            last_error = error                       # ветка B (+ восстановимый дубль ключа) — копим и ретраим
            logger.warning(f"Попытка {attempt}/{USERBOT_CONNECT_ATTEMPTS} запуска юзербота: {error}")
            try:
                if getattr(app, "is_connected", False):
                    await asyncio.wait_for(app.stop(), timeout=USERBOT_CONNECT_TIMEOUT)
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

    # add_signal_handler(sig, callback, *args): *args опционален, но инспекция PyCharm ложно
    # считает его обязательным («Parameter 'args' unfilled») — подавляем точечно noinspection.
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            # noinspection PyArgumentList
            loop.add_signal_handler(_sig, _on_stop_signal)
        except NotImplementedError:
            pass  # Windows — graceful по сигналам недоступен

    # OTC-аутэйдж ДО запуска браузера: binodex не отдаёт котировки → не поднимаем тяжёлый Firefox,
    # ждём фид браузер-фри (apps/binodex_feed) и стартуем, только когда котировки вернутся.
    if not binary and not await feed_alive():
        if not await _await_binodex_feed(at_start=True):
            await close_program(manager=None, status=0, text='Остановлен сигналом 🛑')
            return

    # Survive §4.3: init с бэкоффом при отвале cookies — без выхода, крутим пока не починят.
    manager = await _init_with_retry()
    if manager is None:  # остановлены сигналом во время init/cookies-backoff (close_program сам гасит юзербот)
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

        # OTC-аутэйдж в рантайме: опцион не снялся И binodex не отдаёт котировки (браузер-фри
        # проверка feed_alive). Это аутэйдж на стороне binodex (рынок закрыт/сбой), а НЕ наш отвал
        # кук/краш → не рестартим и не спамим алертами: выгружаем тяжёлый браузер, ждём фид браузер-
        # фри, поднимаемся при восстановлении. Отвал кук/краш — фид при этом ЖИВ (feed_alive=True),
        # поэтому сюда не попадают и отрабатывают штатные ветки ниже.
        if not binary and not res_option.result and not await feed_alive():
            try:
                # Верхняя граница: зависший Firefox-close не должен подвесить аварийную выгрузку.
                await asyncio.wait_for(manager.close(), timeout=50)
            except (Exception,) as error:
                logger.warning(f'закрытие браузера не завершилось штатно — {error}')
            if not await _await_binodex_feed(at_start=False):
                break  # SIGTERM во время ожидания
            manager = await _init_with_retry()
            if manager is None:  # остановлены сигналом во время повторного init
                break
            continue

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
                # В лог, не в канал: «dead» часто транзиентный сплеш/WS-икота, а не отвал кук —
                # пересоздание это переживёт без рефреша (init разведёт: CookiesExpired / FeedOutage / SetupError).
                # Реальный отвал/невосстановление дойдёт до cookies-канала из _recover_otc_cookies.
                logger.warning(f'OTC: сессия не отвечает в рантайме ({reason}) — пересоздаю браузер')
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

    # Сюда — только по SIGTERM/SIGINT: чисто закрываемся с кодом 0 (штатная остановка извне).
    # status НЕ трогаем (инвариант: status=false выставляет только плановый weekend-выход binary;
    # стоп инициировал диспетчер — он сам управляет своим состоянием). Юзербот гасит сам
    # close_program (_close_userbot с таймаутом); единственное сообщение о закрытии — ниже.
    await close_program(manager=manager, status=0, text='Остановлен сигналом 🛑')


if __name__ == "__main__":
    asyncio.run(bot())