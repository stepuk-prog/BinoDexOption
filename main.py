import asyncio
import signal
import sys

from datetime import datetime, timedelta

from apps.app import get_water, time_sleep, request_shutdown, sleep_or_stop
from apps.browser_app import init_load
from apps.exit_app import (close_program, session_dead_shutdown, session_failed,
                           session_lost, session_lost_shutdown, session_recoverable,
                           write_status_offline)
from apps.main_app import main
from apps.my_exeptions import send_photo_safe
from apps.premium_watch import check_premium
from apps.otc_app import otc_session_dead
from apps.binodex_feed import binodex_ready, wait_for_feed, FEED_CONFIRM_WINDOW
from classes import upload_session
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from logs import init_logger
from messages import weekend_message, start_message
from settings.config import get_app, binary, database, program_id, cook_name_otc
from settings.fatal import fatal_exit
from settings.timing import (BROWSER_CLOSE_TIMEOUT, USERBOT_RETRY_DELAY, USERBOT_CONNECT_ATTEMPTS, USERBOT_CONNECT_TIMEOUT)
from settings.timing import SHUTDOWN_TOTAL_BUDGET, SYSTEMD_STOP_TIMEOUT
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
# Сколько ПОЛНЫХ циклов «прокси исчерпаны → назад на direct» терпим при front-end аутэйдже,
# прежде чем признать его устойчивым С ЭТОЙ НОДЫ и отдать её диспетчеру: exit(EXIT_SETUP) →
# GD переносит провайдер-диверсно (front-end может подняться с egress другой ноды; если лёг
# глобально — GD после пары диверсных попыток встанет в ALARM+hold по контракту). Раньше здесь
# выживали БЕСКОНЕЧНО → egress-специфичный аутэйдж не эскалировал, ловил только оператор. §4.5.
# =1: после ОДНОЙ полной провайдер-диверсии (direct+прокси, ~15 мин) отдаём ноду диспетчеру.
SETUP_OUTAGE_MAX_CYCLES = 1
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
# Отвал юзербота, пойманный в ФОНОВОЙ задаче pyrogram (см. _install_session_guard). Хранится
# до финального close_program: сворачиваемся мы штатно, через stop_event, а КОД выхода
# выбирается уже на выходе — по session_lost().
_session_dead_error: BaseException | None = None
# OTC: прокси-фолбэк. False = прямой режим (дефолт). Включается, когда прямой режим не поднял
# front-end binodex (SetupError mounted=False — напр. отравленный CDN-эдж); прокси из
# settings.proxy_data через локальный релей. НЕ sticky навсегда: после PROXY_REPROBE_AFTER неудач
# подряд прокси-режим сбрасывается обратно в direct (переотбивка — front-end мог восстановиться,
# иначе нода залипает до рестарта). FIN/TradingView фолбэк не использует.
# Предохранители подъёма браузера: считаются МЕЖДУ вызовами _init_with_retry (её зовёт и
# _recreate_browser), иначе лимит не накопится — см. комментарий внутри функции.
_setup_streak = 0
_browser_fails = 0
_outage_cycles = 0

_use_proxy = False
# OTC: счётчик front-end-аутэйджей подряд в прокси-режиме (для переотбивки direct). Сбрасывается
# при успешном init и при каждом новом заходе в прокси-режим.
_proxy_outage_streak = 0


def _reset_cookie_fails():
    global _cookie_fails, _otc_recover_cycles
    _cookie_fails = 0
    _otc_recover_cycles = 0


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
    return await sleep_or_stop(_stop_event, delay)


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
    from settings.proxy import get_current_proxy, load_proxies_from_db, PROXY_SCOPE
    proxy = get_current_proxy()
    if proxy is None:
        return
    try:
        await database.ban_proxy(proxy.ip, PROXY_BAN_TTL, PROXY_SCOPE)
        await load_proxies_from_db(database)  # перечитать пул без забаненного
        logger.warning(f'Прокси({PROXY_SCOPE}): {proxy.ip} забанен на {PROXY_BAN_TTL // 60} мин '
                       f'(не поднял front-end) — ротация на следующий')
    except (Exception,) as err:
        logger.warning(f'Прокси({PROXY_SCOPE}): бан {proxy.ip} не удался: {err}')


async def _mark_proxy_success() -> None:
    """Текущий OTC-прокси поднял рабочий init → плюс в статистику (триггер снимет бан scope'а)."""
    from settings.proxy import get_current_proxy, PROXY_SCOPE
    proxy = get_current_proxy()
    if proxy is None:
        return
    try:
        await database.update_proxy_stats(proxy.ip, True, PROXY_SCOPE)
    except (Exception,):
        pass


async def _init_with_retry():
    """init_load с обработкой отвала cookies. OTC (§4.3): CookiesExpired → авто-восстановление
    рефрешером (3 попытки → иначе выход). TV: Survive-backoff (сообщение + пауза, на повторе init
    перечитает куки из БД), БЕЗ выхода. Прочий провал init_load → пауза INIT_RETRY_DELAY и повтор.
    Паузы прерываются сигналом остановки.
    :return: BrowserManager либо None (остановлены сигналом во время init/backoff)."""
    global _use_proxy, _proxy_outage_streak, _setup_streak, _browser_fails, _outage_cycles
    # Счётчики МОДУЛЬНЫЕ (как _cookie_fails/_otc_recover_cycles). Локальными они обнулялись на
    # каждом входе, а _recreate_browser зовёт эту функцию заново — в цикле «браузер поднялся →
    # опцион упал → пересоздание» лимиты не накапливались, и предохранители не срабатывали
    # никогда. Обнуляются ВСЕ разом при успешном подъёме (ветка `if manager:` ниже).
    while True:
        if _stop_event is not None and _stop_event.is_set():
            return None
        try:
            manager = await init_load(use_proxy=_use_proxy)
        except FeedOutage as error:
            # OTC: аутэйдж binodex — market-WS молчит ЛИБО auth-API api.binodex.app лежит (5xx),
            # подтверждено браузер-фри. НЕ отвал кук и НЕ front-end-аутэйдж: релогин/прокси не помогут,
            # пока сервис binodex недоступен. Ждём восстановления БРАУЗЕР-ФРИ (без рефреша, без выхода,
            # без спама в cookies-канал). binodex_ready вернулся → новый виток init.
            logger.warning(f'OTC: аутэйдж binodex ({error}) — жду восстановления браузер-фри')
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
                        _outage_cycles += 1
                        if _outage_cycles >= SETUP_OUTAGE_MAX_CYCLES:
                            # Аутэйдж УСТОЙЧИВ с этой ноды (direct+прокси исчерпаны
                            # SETUP_OUTAGE_MAX_CYCLES раз) — не залипаем тут вечно. Отдаём ноду
                            # диспетчеру: exit(EXIT_SETUP) → GD провайдер-диверсный перенос (front-end
                            # может подняться с egress другой ноды; глобальный аутэйдж → GD после
                            # диверсных попыток встанет в ALARM+hold). status НЕ трогаем (инвариант
                            # §4.3: краш ≠ self-disable → GD обязан failover'ить).
                            logger.report(f'OTC: front-end аутэйдж binodex не преодолён за '
                                          f'{SETUP_OUTAGE_MAX_CYCLES} цикла (direct+прокси) — отдаю ноду '
                                          f'диспетчеру для провайдер-диверсного переноса ☄️ (код {EXIT_SETUP})')
                            await close_program(manager=None, status=EXIT_SETUP,
                                                text=f'OTC: front-end аутэйдж binodex устойчив с этой ноды '
                                                     f'({SETUP_OUTAGE_MAX_CYCLES} цикла) — перенос на другого '
                                                     f'провайдера ☄️ (код {EXIT_SETUP})')
                            return None  # close_program делает sys.exit; страховка
                        logger.report(f'OTC: {PROXY_REPROBE_AFTER} прокси подряд не подняли front-end — '
                                      f'похоже на аутэйдж binodex, не битый эдж; возвращаюсь на прямой режим '
                                      f'(переотбивка), пауза {SETUP_OUTAGE_BACKOFF // 60} мин '
                                      f'[цикл {_outage_cycles}/{SETUP_OUTAGE_MAX_CYCLES}]')
                    else:
                        logger.warning(f'OTC: прокси не поднял front-end binodex — ротация '
                                       f'({_proxy_outage_streak}/{PROXY_REPROBE_AFTER}), пауза '
                                       f'{SETUP_OUTAGE_BACKOFF // 60} мин, выживаю: {error}')
                    if await sleep_or_stop(_stop_event, SETUP_OUTAGE_BACKOFF):
                        return None
                    continue
                # FIN: прокси не применяем — прежнее поведение (выживание с backoff)
                logger.warning(f'front-end не поднялся — аутэйдж, пауза {SETUP_OUTAGE_BACKOFF // 60} '
                               f'мин, выживаю: {error}')
                if await sleep_or_stop(_stop_event, SETUP_OUTAGE_BACKOFF):
                    return None
                continue
            # OTC: апп смонтирован, но наш селектор не найден — сменились селекторы binodex.
            # Рефреш бесполезен. SETUP_ATTEMPTS повторов (временный сбой) → не помогло → плановый
            # выход (нужно вручную обновить селекторы; §4.5).
            _setup_streak += 1
            logger.warning(f'OTC: сайт не настроился ({_setup_streak}/{SETUP_ATTEMPTS}): {error}')
            if _setup_streak >= SETUP_ATTEMPTS:
                logger.cookies(f'OTC: сайт не настраивается за {SETUP_ATTEMPTS} попытки '
                               f'({cook_name_otc}) — нужно ручное вмешательство (селекторы binodex). Останавливаю')
                await close_program(manager=None, status=EXIT_SETUP,
                                    text=f'OTC: сайт не настраивается — проверить селекторы binodex ⚙️🛑 (код {EXIT_SETUP})')
                return None  # close_program делает sys.exit; страховка
            if await sleep_or_stop(_stop_event, INIT_RETRY_DELAY):
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
            # ВСЕ предохранители подъёма — на ноль: браузер поднялся и настроился, значит
            # прежние провалы были транзиентными. Без этого сброса счётчики стали бы
            # монотонными за жизнь процесса: три SetupError с сутками нормальной работы между
            # ними дали бы ложный EXIT_SETUP «селекторы сломались».
            _browser_fails = _setup_streak = _outage_cycles = 0
            if _use_proxy:
                await _mark_proxy_success()  # прокси поднял рабочий init → плюс в статистику
            return manager
        if not binary and _use_proxy:
            # На прокси init провалился (вероятно прокси мёртв) → бан+ротация; это НЕ поломка
            # браузера ноды, поэтому _browser_fails не трогаем (иначе прокси-карусель ложно дала бы EXIT_BROWSER).
            await _ban_current_proxy()
        else:
            _browser_fails += 1
            if _browser_fails >= BROWSER_MAX_ATTEMPTS:
                # Браузер не поднялся подряд BROWSER_MAX_ATTEMPTS раз (не куки/селекторы/фид/прокси —
                # те идут своими ветками): нода, вероятно, не может поднять Firefox → отдаём диспетчеру
                # (failover на другую ноду), exit(EXIT_BROWSER). status НЕ трогаем (инвариант).
                await close_program(manager=None, status=EXIT_BROWSER,
                                    text=f'Браузер не поднялся {BROWSER_MAX_ATTEMPTS}× — отдаю ноду диспетчеру ☄️ (код {EXIT_BROWSER})')
                return None  # close_program делает sys.exit; страховка
        logger.error(f'init_load провалился — пауза {INIT_RETRY_DELAY}с и повтор')
        if await sleep_or_stop(_stop_event, INIT_RETRY_DELAY):
            return None


async def _recreate_browser(manager):
    """Закрыть текущий браузер и поднять заново через _init_with_retry (Survive §4.3).
    :return: новый BrowserManager либо None (остановлены сигналом)."""
    try:
        # Верхняя граница: зависший Firefox-close не должен подвесить пересоздание браузера.
        await asyncio.wait_for(manager.close(), timeout=BROWSER_CLOSE_TIMEOUT)
    except (Exception,) as error:
        logger.warning(f'Ошибка закрытия браузера при пересоздании: {error}')  # утечка Firefox не должна быть незаметной
    return await _init_with_retry()


async def _await_binodex_feed(at_start: bool) -> bool:
    """OTC-аутэйдж: binodex не отдаёт котировки (рынок закрыт/сбой на стороне binodex). Ждём фид
    БРАУЗЕР-ФРИ (apps/binodex_feed) — без рестарт-петли и спама алертов: ОДНО уведомление вниз +
    одно вверх. True — котировки вернулись (можно поднимать браузер); False — остановлены сигналом.
    at_start=True — текст «браузер не поднимаю»; False (рантайм) — «выгрузил браузер»."""
    if at_start:
        logger.report('🕓 binodex недоступен (фид/бэкенд) — браузер не поднимаю, жду восстановления')
    else:
        logger.report('🕓 binodex стал недоступен (фид/бэкенд) — выгрузил браузер, посты на паузе, жду восстановления')
    if not await wait_for_feed(_stop_event):
        return False  # SIGTERM во время ожидания
    logger.report(f'✅ binodex снова доступен (фид+API) и держится {int(FEED_CONFIRM_WINDOW)}с '
                  f'без срывов — поднимаю браузер, продолжаю работу')
    return True


async def weekly_post(kind: str, photo: str, caption: str, mes_type: str,
                      now: datetime | None = None) -> None:
    """Недельный пост (приветственный/выходной) — РОВНО ОДИН за неделю на программу.

    Повтор отсекает отметка в БД (`settings.week_post`: program_id + kind + понедельник этой
    недели), а не время старта. До 14-09-2026 защитой от дубля служило узкое окно
    `понедельник 3:00–3:25`, и оно промахивалось мимо реальности: крон диспетчера поднимает
    FIN в 4:55 (`55 4 * * 0`), так что приветственный пост не уходил ВООБЩЕ — ни у русской
    пары, ни у английской. Выходной пост той защиты не имел вовсе: рестарт вечером пятницы
    слал его второй раз.

    Отметка ставится ДО отправки (см. claim_week_post); если отправка не удалась — снимается,
    и следующий подъём попробует снова.
    """
    now = now or datetime.now()
    week_start = (now - timedelta(days=now.isoweekday() - 1)).date()   # понедельник этой недели
    claimed = await database.claim_week_post(program_id, kind, week_start)
    if claimed is False:
        # Спросить БД не смогли. Молчим: лишний пост подписчикам виден, пропущенный — нет.
        logger.warning('%s: отметка недели недоступна (сбой БД) — пост не отправляю', mes_type)
        return
    if claimed is None:
        logger.info('%s на этой неделе (%s) уже отправлено — пропускаю', mes_type, week_start)
        return
    # Через send_photo_safe, а не голый send_photo: у транспорта есть проба доставки и повтор
    # при обрыве — ровно тот сценарий (таймаут при потере SYN), ради которого он и написан.
    ok, err = await send_photo_safe(photo, caption, mes_type=mes_type)
    if not ok:
        logger.error(f'Ошибка отправки: {mes_type} - {err}')
        await database.release_week_post(program_id, kind, week_start)


def _loop_exception_handler(loop, context: dict) -> None:
    """Обработчик необработанных исключений event loop'а.

    Нас интересует ровно одно: отвал юзербота из ФОНОВЫХ задач pyrogram. Он не долетает ни до
    одного нашего except: pyrogram роняет Unauthorized ВНУТРИ Session.restart(), эту задачу
    никто не ждёт («Task exception was never retrieved» в journald), а наши вызовы не падают, а
    ВИСЯТ и обрываются собственными таймаутами. Так тестовая ForumTrade 738 крутилась вхолостую
    1 ч 51 мин 14-09-2026 — ни одного поста и ни одного 🔒-алерта. Всё прочее отдаём дефолтному
    обработчику: глушить чужие ошибки нельзя."""
    error = context.get('exception')
    if isinstance(error, Exception) and session_failed(error):
        _note_session_dead(error)
        return
    loop.default_exception_handler(context)


def _note_session_dead(error: BaseException) -> None:
    """Запомнить отвал и свернуть работу ШТАТНО — через тот же stop_event, что и SIGTERM.

    Выходить прямо отсюда нельзя (обработчик лупа синхронный, а close_program — корутина, да и
    уборка не отработала бы), поэтому просто просим цикл закончиться; код выхода подставит
    _shutdown_on_session_event на выходе. Сообщаем один раз: pyrogram переподключается по кругу
    и способен уронить десяток одинаковых задач подряд."""
    global _session_dead_error
    if _session_dead_error is not None:
        return
    _session_dead_error = error
    logger.error(f'Отвал юзербота в фоновой задаче pyrogram: {type(error).__name__}: {error} — '
                 f'сворачиваю работу')
    if _stop_event is not None:
        _stop_event.set()
    request_shutdown()   # это не «сбой цикла» — главный алерт уйдёт из shutdown ниже


def _install_session_guard(loop) -> None:
    """Повесить обработчик на луп. Ставится в bot() сразу после подъёма юзербота."""
    loop.set_exception_handler(_loop_exception_handler)


async def _shutdown_on_session_event(manager) -> bool:
    """Закрыться нужным кодом, если сторож поймал отвал. True — выход сделан (дальше не идём).

    Код РАЗНЫЙ, и различает их session_lost: голый `[401 Unauthorized]` без ID — потеря
    авторизации соединения → 20 (перезапуск на месте); `[401 с ID]` (AUTH_KEY_UNREGISTERED,
    SESSION_REVOKED, USER_DEACTIVATED…) — ключ реально отозван → 13 (оператор)."""
    if _session_dead_error is None:
        return False
    reason = 'фоновая задача pyrogram'
    if session_lost(_session_dead_error):
        await session_lost_shutdown(_session_dead_error, reason=reason, manager=manager)
    else:
        await session_dead_shutdown(_session_dead_error, reason=reason, manager=manager)
    return True


def _check_shutdown_budget() -> None:
    """Предупредить, если сумма потолков уборки не влезает в TimeoutStopSec юнита.

    Тихий рассинхрон здесь стоит дорого и виден только по факту: systemd убивает процесс
    SIGKILL'ом посреди закрытия браузера, Playwright оставляет висячий lock в общем кэше
    ms-playwright, и следующий запуск на этой ноде уже не поднимает браузер. Проверка
    бесплатная и делается один раз на старте. Слагаемые — в settings/timing.py.
    Реестр BinoCore: shutdown-budget-match."""
    if SHUTDOWN_TOTAL_BUDGET >= SYSTEMD_STOP_TIMEOUT:
        logger.error(f'Бюджет остановки {SHUTDOWN_TOTAL_BUDGET:.0f}с не влезает в '
                     f'TimeoutStopSec={SYSTEMD_STOP_TIMEOUT}с (systemd/binodex-*.service) — systemd успеет '
                     f'прислать SIGKILL посреди уборки. Поднимите TimeoutStopSec или урежьте '
                     f'потолки SHUTDOWN_*')


async def bot():
    """Запуск бота"""
    _check_shutdown_budget()
    logger.report('🚀 Стартую')

    # Поднимаем пулы БД (program + binodex) до первого запроса. Раньше get_app/
    # session_dead_shutdown — последний пишет close_program в БД при отвале юзербота.
    await database.connect()

    # Создаём Pyrogram Client внутри event loop
    app = get_app()

    # Аплоад фото — на ОДНОМ переиспользуемом media-соединении вместо нового на каждый
    # пост (иначе каждый send_photo заново проходит установление TCP; при SYN-фильтре
    # Telegram это стоило рестартов юнита — см. classes/upload_session).
    upload_session.install()

    # Запуск юзербота — две ветки (§3.2). A: ключ доказано мёртв (session_failed) →
    # сразу штатный стоп с session-алертом и кодом EXIT_USERBOT, без ретраев (каждая
    # попытка пойдёт с тем же отозванным ключом). status НЕ трогаем — судьбу программы
    # решает диспетчер по коду выхода (13 = account-уровень → ALARM, релокация бесполезна). B: transient-обрыв (сеть/таймаут) ИЛИ
    # AUTH_KEY_DUPLICATED (session_recoverable — ключ занят другой нодой при failover,
    # отпустится сам) → до USERBOT_CONNECT_ATTEMPTS попыток; не переподключились → тот же
    # плановый выход (отвал session, код EXIT_USERBOT).
    last_error = None
    for attempt in range(1, USERBOT_CONNECT_ATTEMPTS + 1):
        try:
            # Таймаут: SIGTERM-хендлер ставится ниже (после init), поэтому зависший хендшейк
            # Pyrogram здесь нельзя прервать сигналом — оборачиваем wait_for (TimeoutError → ветка B).
            await asyncio.wait_for(app.start(), timeout=USERBOT_CONNECT_TIMEOUT)
            # Premium спрашиваем сразу после подъёма клиента: отметка в БД выставится на старте,
            # а не через два часа. Сбой проверки старт не валит — внутри всё поглощается.
            await check_premium(force=True)
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
        if (now + timedelta(hours=2)).weekday() >= 5:
            # Выходные: прощаемся и уходим. Приветственный пост в этой ветке НЕ трогаем —
            # иначе подъём в субботу дал бы «начало недели» и следом «до понедельника».
            await weekly_post('end', 'pictures/end_week.png', weekend_message(),
                              'сообщение о выходных', now)
            await write_status_offline(program_id)
            await close_program(manager=None, status=0, text='Закрываюсь 🔱 (выходные)')
            return
        # Приветствие — на ПЕРВОМ за неделю подъёме в рабочие дни (по плану это понедельник
        # 4:55 по крону; если старт задержался — уйдёт при том подъёме, который случился).
        await weekly_post('start', 'pictures/start_week.png', start_message(),
                          'стартовое сообщение', now)

    water_naked = get_water()
    if not water_naked[0]:
        # QR на кадре — не украшение: это единственная ссылка на бота в посте, её проверяют
        # отдельно (zbarimg). Не доехавшая на ноду картинка — обычный отказ выкатки — давала
        # СУТКИ постов без QR: load_rgba отдаёт None, кадр спокойно собирается дальше, а
        # единственный след (WARNING в файле) в Telegram не уходит по построению.
        # Останавливаемся с кодом EXIT_SETUP=12 — «нужен человек», диспетчер не будет
        # перезапускать впустую (ревизия 17-09-2026, п.2.1; паритет с английской парой).
        fatal_exit('Оверлей QR не загрузился — кадры уходили бы без QR-кода')
    qr = water_naked[1]

    # Graceful shutdown по SIGTERM/SIGINT (systemctl stop / диспетчер) — async-вариант:
    # signal.signal+KeyboardInterrupt в asyncio не ловится внутри корутины, поэтому через
    # loop.add_signal_handler + Event. Ставим ДО init: cookies-backoff (до 300с) в
    # _init_with_retry прерывается этим сигналом (иначе SIGTERM ждал бы SIGKILL).
    global _stop_event
    stop_event = asyncio.Event()
    _stop_event = stop_event
    loop = asyncio.get_running_loop()

    # Сторож юзербота — ПОСЛЕ создания stop_event: он сворачивает работу именно через него,
    # а до этого момента разбудить цикл было бы нечем. Юзербот к этой строке уже поднят.
    _install_session_guard(loop)

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

    # OTC-аутэйдж ДО запуска браузера: binodex недоступен (market-WS молчит ЛИБО auth-API
    # api.binodex.app лежит) → не поднимаем тяжёлый браузер, ждём восстановления браузер-фри
    # (apps/binodex_feed.binodex_ready) и стартуем, только когда и фид, и API вернутся.
    if not binary and not await binodex_ready():
        if not await _await_binodex_feed(at_start=True):
            if await _shutdown_on_session_event(None):
                return
            await close_program(manager=None, status=0, text='Остановлен сигналом 🛑')
            return

    # Survive §4.3: init с бэкоффом при отвале cookies — без выхода, крутим пока не починят.
    manager = await _init_with_retry()
    if manager is None:  # остановлены сигналом во время init/cookies-backoff (close_program сам гасит юзербот)
        if await _shutdown_on_session_event(None):
            return
        await close_program(manager=None, status=0, text='Остановлен сигналом 🛑')
        return

    logger.info("✅ Браузер инициализирован, страницы: %s", list(manager.pages.keys()))
    logger.info("🔄 Переход в main loop...")

    while not stop_event.is_set():
        # Premium аккаунта: сторож сам решает, подошёл ли срок (раз в 2 часа), поэтому вызов
        # в цикле дешёвый — почти всегда это сравнение таймера. Кастом-эмодзи в постах шлёт
        # только Premium-аккаунт, а истекает он посреди прогона.
        await check_premium()

        res_option = await main(manager=manager, qr=qr, stop_event=stop_event)

        # Остановка по сигналу (SIGTERM/SIGINT): ошибка из-за гибели Playwright-драйвера —
        # это штатный стоп, не сбой; уходим в graceful-ветку ниже (выход с кодом 0;
        # programdata.status НЕ трогаем — стоп инициировал диспетчер, см. хвост функции).
        if stop_event.is_set():
            break

        # OTC-аутэйдж в рантайме: опцион не снялся И binodex недоступен (браузер-фри binodex_ready:
        # market-WS молчит ЛИБО auth-API api.binodex.app лежит). Это аутэйдж на стороне binodex
        # (рынок закрыт/сбой/backend-502), а НЕ наш отвал кук/краш → не рестартим и не спамим
        # алертами: выгружаем тяжёлый браузер, ждём восстановления браузер-фри, поднимаемся при
        # возврате. Отвал кук/краш — binodex при этом ЖИВ (binodex_ready=True), поэтому сюда не
        # попадают и отрабатывают штатные ветки ниже.
        if not binary and not res_option.result and not await binodex_ready():
            try:
                # Верхняя граница: зависший Firefox-close не должен подвесить аварийную выгрузку.
                await asyncio.wait_for(manager.close(), timeout=BROWSER_CLOSE_TIMEOUT)
            except (Exception,) as error:
                logger.warning(f'закрытие браузера не завершилось штатно — {error}')
            if not await _await_binodex_feed(at_start=False):
                break  # SIGTERM во время ожидания
            manager = await _init_with_retry()
            if manager is None:  # остановлены сигналом во время повторного init
                break
            continue

        # OTC: отвал cookies в рантайме (§4.1). ОСНОВНОЙ сигнал — otc_session_dead (редирект с
        # /trade ИЛИ мёртвый WS-фид). ВТОРИЧНЫЙ — эвристика «цена не менялась N проверок подряд
        # ВНУТРИ одного опциона» (prev_price/count_price обнуляются в начале каждого
        # _run_option, межопционной памяти у неё нет) — на плоском
        # рынке даёт ложняки). Реакция — пересоздание браузера: если куки реально мертвы, init
        # упрётся в CookiesExpired → _init_with_retry запустит авто-восстановление рефрешером
        # (3 попытки → иначе выход). Если умер только WS (куки живы) — init поднимется без рефреша.
        if not binary:
            dead, reason = await otc_session_dead(manager)
            if not dead and res_option.check_cookies > 2:
                dead, reason = True, 'цена не менялась N проверок ВНУТРИ опциона (вторичный сигнал)'
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

        # Прерываемый сон: проснёмся сразу при сигнале остановки (общий sleep_or_stop, как и
        # остальные ожидания программы — своей копии wait_for здесь больше нет).
        await sleep_or_stop(stop_event, await time_sleep())

        if binary and not stop_event.is_set():
            if (datetime.now() + timedelta(hours=2)).weekday() >= 5:
                if not res_option.plus:
                    # Прерываемый сон (как выше) — иначе SIGTERM завис бы тут на 100–150с
                    await sleep_or_stop(stop_event, await time_sleep())
                    continue
                await weekly_post('end', 'pictures/end_week.png', weekend_message(),
                                  'сообщение о выходных')
                await write_status_offline(program_id)
                await close_program(manager=manager, status=0, text='Закрываюсь 🔱')  # сам гасит юзербот
                return

    # Отвал юзербота, пойманный сторожем лупа: цикл вышел не по сигналу, а по нему — код
    # выхода тогда 13 или 20, а не 0 (иначе диспетчер счёл бы это штатной остановкой).
    if await _shutdown_on_session_event(manager):
        return

    # Сюда — только по SIGTERM/SIGINT: чисто закрываемся с кодом 0 (штатная остановка извне).
    # status НЕ трогаем (инвариант: status=false выставляет только плановый weekend-выход binary;
    # стоп инициировал диспетчер — он сам управляет своим состоянием). Юзербот гасит сам
    # close_program (_close_userbot с таймаутом); единственное сообщение о закрытии — ниже.
    await close_program(manager=manager, status=0, text='Остановлен сигналом 🛑')


def _log_fatal(error: BaseException) -> None:
    """Записать НЕПРЕДВИДЕННОЕ исключение, долетевшее до asyncio.run, и не обещать лишнего.

    Голый asyncio.run(bot()) отдавал только трейсбек в stderr: в error.log и journald не
    попадало ничего осмысленного. Эта функция закрывает ровно это — и ничего больше.

    Ресурсы здесь НЕ убираем, хотя первая версия пыталась (11-09-2026). Причины две. Первая:
    уборка и так есть — close_program в штатных ветках и общий teardown. Вторая: делать это
    отсюда НЕЛЬЗЯ — Playwright-, pyrogram- и asyncpg-объекты привязаны к УЖЕ ЗАКРЫТОМУ loop'у,
    так что каждый вызов в новом asyncio.run отбился бы `RuntimeError: attached to a different
    loop` и был молча проглочен. Телеграм-алерт отсюда тоже не уйдёт: emit кладёт отправку в
    create_task, а дождаться её в этой точке уже некому — asyncio.run отменит задачу на выходе.
    Поэтому функция синхронная: гарантирован файл и journald, а не видимость доставки.

    status НЕ трогаем: судьбу процесса решает диспетчер по коду выхода."""
    logger.error(f'НЕПРЕДВИДЕННЫЙ сбой вне охраняемых зон: '
                 f'{type(error).__name__}: {error}', exc_info=error)


if __name__ == "__main__":
    try:
        asyncio.run(bot())
    except KeyboardInterrupt:
        # Ctrl-C вне обработчика сигналов (ранний старт/shutdown) — штатная остановка.
        sys.exit(0)
    except SystemExit:
        raise                       # close_program уже отработал и выставил код выхода
    except BaseException as _error:
        # Синхронно и без нового loop'а — см. _log_fatal: уборка оттуда невозможна в принципе.
        _log_fatal(_error)
        sys.exit(1)
