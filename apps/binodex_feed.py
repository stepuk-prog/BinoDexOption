"""Браузер-фри health-чек фида котировок binodex (api-coins.binodex.app, Socket.IO).

Позволяет понять «binodex отдаёт котировки» БЕЗ запуска headless-браузера. В аутэйдже binodex
(сайт на /trade, но WS не шлёт ценовые кадры, window.chartData=None — рынок закрыт / сбой на
стороне binodex) бессмысленно держать тяжёлый Firefox и рестартиться по кругу: дешевле слушать
market-WS напрямую и поднять браузер только когда котировки вернутся.

Протокол: Engine.IO v4 поверх WebSocket, namespace `/graphic`. Хэндшейк — `0{...}` (open) →
шлём `40/graphic,` (connect ns) → `40...` (ack) → шлём SUBSCRIBE → приходят
`42/graphic,["graphic",{symbol,price,...}]`. На ping `2` отвечаем `3`. Авторизация не нужна
(проверено), только заголовок Origin. Один ценовой кадр = фид жив.
"""
import asyncio

import aiohttp

from classes.price_tracker import symbol_key
from logs import init_logger
from settings.browser_config import otc_ws_origin, otc_api_url

logger = init_logger(__name__)

# Домен переехал api-coins.binodex.io → .app (грабли 2026-07-20). Браузер реально коннектится
# на .app — health-чек зеркалим туда же (на .io пока алиас, но не полагаемся).
_WS_URL = 'wss://api-coins.binodex.app/market/?EIO=4&transport=websocket'
_HEADERS = {'Origin': otc_ws_origin, 'User-Agent': 'Mozilla/5.0'}  # origin из binodex_settings

# auth/config API binodex (Privy-логин + /config) — ОТДЕЛЬНЫЙ сервис от market-WS. Может лежать
# (Cloudflare 502 = origin недоступен/крашлупит) при ЖИВОМ фиде: тогда app-shell не монтируется
# (privyLogin падает), а релогин/прокси/смена движка бесполезны — надо ждать восстановления.
# База — из binodex_settings.api_url (домен уже переезжал .io→.app; грабли 2026-07-23: 502-аутэйдж).
_API_HEALTH_URL = otc_api_url.rstrip('/') + '/config'
API_ALIVE_TIMEOUT = 8.0       # ждать ответ API в одной попытке (origin может тупить перед 502)

FEED_PROBE_PAIR = 'EUR/USD'   # дефолтная пара — присутствует всегда
FEED_ALIVE_TIMEOUT = 10.0     # сколько ждать первого ценового кадра в одной попытке
FEED_WAIT_POLL = 30.0         # пауза между попытками в wait_for_feed (аутэйдж тянется минутами)
FEED_WAIT_HEARTBEAT = 600.0   # как часто логировать «binodex всё ещё недоступен» при затяжном ожидании

# ПОДТВЕРЖДЕНИЕ ВОЗВРАТА (2026-08-17). binodex выходит из техработ ВОЛНАМИ: 16-08 API отдал 200
# около 17:00, программы распарковали — и через ~10 минут снова 503. Одного успешного пробника
# мало: он ловит окно между волнами. Поэтому после первого признака жизни держим binodex под
# наблюдением ещё FEED_CONFIRM_WINDOW секунд и только потом говорим «поднялся». В окне следим за
# ОБОИМИ сервисами сразу: WS должен слать тики НАШЕЙ пары без пауз длиннее FEED_CONFIRM_GAP,
# auth-API — держать не-5xx на каждой перепроверке. Срыв → назад в ожидание, «вверх» НЕ рапортуем.
#
# Критерий непрерывности взят у BinodexScreens (`apps/binodex_feed._probe_stable`, STABLE_GAP=3.0),
# где защита от мигания живёт давно и проверена на живом фиде; отличий два, оба намеренные:
#   • окно меряем В СЕКУНДАХ (минута), а не в числе тиков — так просили и так предсказуемее;
#   • параллельно перепроверяем auth-API. У Screens гейт аутэйджа фид-онли, и ровно этим он
#     16-08 пропустил волну: фид жил, а api.binodex.app отдавал 503 MAINTENANCE.
FEED_CONFIRM_WINDOW = 60.0    # сколько держать binodex под наблюдением после первого признака жизни
FEED_CONFIRM_GAP = 3.0        # максимальная пауза между тиками нашей пары внутри окна
FEED_CONFIRM_API_EVERY = 20.0  # как часто перепроверять auth-API внутри окна


def _subscribe_frame(pair: str) -> str:
    return ('42/graphic,["graphic",{"method":"SUBSCRIBE","symbol":"%s",'
            '"interval":"30s","otc":true}]' % pair)


async def _frames(pair: str, want: str | None = None):
    """Поток ценовых кадров market-WS по паре: хэндшейк → SUBSCRIBE → yield на каждом кадре с
    ценой. `want` — подстрока символа (`"EUR/USD-OTC"`): задана → отдаём ТОЛЬКО тики этой пары
    (фид общий, чужие кадры идут по тому же сокету). Генератор завершается, когда сервер закрыл
    сокет или отказал в namespace (`44`).

    Следуем протоколу Engine.IO v4: connect namespace (`40/graphic,`) шлём ТОЛЬКО после
    open-кадра `0{...}` (раньше слали сразу — хрупко и вопреки docstring). `44` (namespace
    connect error) → фид недоступен, быстрый выход (а не ожидание внешнего таймаута).

    Один кадр нужен `_probe` (жив/не жив), непрерывный поток — `_watch` (подтверждение возврата),
    поэтому подключение вынесено в общий генератор, а не продублировано."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(_WS_URL, headers=_HEADERS, heartbeat=None) as ws:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.data
                if data.startswith('0'):
                    await ws.send_str('40/graphic,')
                elif data.startswith('44'):
                    logger.debug(f'binodex _frames: namespace connect error: {data[:120]}')
                    return
                elif data.startswith('40'):
                    await ws.send_str(_subscribe_frame(pair))
                elif data == '2':
                    await ws.send_str('3')
                elif data.startswith('42') and '"price"' in data:
                    if want is None or want in data:
                        yield data


async def _probe(pair: str) -> bool:
    """Одно подключение: True на первом же ценовом кадре (фид жив), False — сокет закрылся раньше.
    Генератор закрываем явно: выход по `return` из `async for` иначе оставил бы WS на совести GC."""
    gen = _frames(pair)
    try:
        async for _ in gen:
            return True
        return False
    finally:
        await gen.aclose()


async def _watch(pair: str, window: float, max_gap: float, abort: asyncio.Event) -> bool:
    """Держать market-WS открытым `window` секунд и требовать НЕПРЕРЫВНЫХ тиков ИМЕННО нашей пары.

    True — окно выдержано (подача устойчива). False — пауза между тиками превысила `max_gap`, сокет
    закрылся до конца окна, либо выставлен `abort` (SIGTERM/сорвался второй наблюдатель). Чужие
    символы серию не наращивают (фильтр `want`) — иначе живой соседний тикер маскировал бы молчание
    нашего, как это учтено в BinodexScreens._probe_stable.

    Выход — ТОЛЬКО по `abort`, а не по отмене таска: `asyncio.wait_for` в 3.11 ГЛОТАЕТ отмену, если
    внутренний future успел завершиться (`if fut.done(): return fut.result()`). Тики идут по
    несколько раз в секунду, поэтому cancel почти всегда приходится ровно на этот момент — таск
    «не отменяется» и досиживает всё окно. Проверено тестом: на cancel таск завершался через полное
    окно с cancelled=False. Явный флаг убирает эту зависимость от внутренностей wait_for."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    gen = _frames(pair, want='"%s"' % symbol_key(pair))
    try:
        while loop.time() < deadline:
            if abort.is_set():
                return False
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=max_gap)
            except asyncio.TimeoutError:
                logger.debug(f'binodex _watch: тиков нет дольше {max_gap}с — фид мигает')
                return False
            except StopAsyncIteration:
                logger.debug('binodex _watch: WS закрылся до конца окна подтверждения')
                return False
        return not abort.is_set()
    finally:
        await gen.aclose()


async def _api_watch(window: float, every: float, abort: asyncio.Event) -> bool:
    """Перепроверять auth-API каждые `every` секунд в течение окна. Первый же 5xx/таймаут → False
    (ровно этот сценарий и был 16-08: 200 на первой пробе, 503 через несколько минут). `abort` —
    как в _watch: выходим сами, не полагаясь на отмену таска."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return not abort.is_set()
        try:
            await asyncio.wait_for(abort.wait(), timeout=min(every, left))
            return False          # abort выставлен во время паузы
        except asyncio.TimeoutError:
            pass
        if not await api_alive():
            logger.debug('binodex _api_watch: auth-API снова не отвечает')
            return False


async def confirm_stable(pair: str = FEED_PROBE_PAIR, stop_event=None,
                         window: float = FEED_CONFIRM_WINDOW) -> bool:
    """Пауза-подтверждение после первого признака жизни: держится ли binodex `window` секунд.

    Следим за ОБОИМИ сервисами параллельно (WS-поток тиков + перепроверки auth-API) и падаем на
    первом же срыве, не досиживая окно. True — выдержал, можно поднимать браузер и рапортовать
    «вверх». False — мигнул (или пришёл stop_event), возвращаемся в ожидание молча.

    Наблюдатели останавливаются общим флагом `abort` (сорвался один → второй не досиживает окно;
    SIGTERM → выходят оба). На отмену таска НЕ полагаемся — см. _watch про wait_for в 3.11."""
    abort = asyncio.Event()
    feed_task = asyncio.create_task(_watch(pair, window, FEED_CONFIRM_GAP, abort))
    api_task = asyncio.create_task(_api_watch(window, FEED_CONFIRM_API_EVERY, abort))
    tasks = {feed_task, api_task}
    if stop_event is not None:
        async def _relay() -> None:
            await stop_event.wait()
            abort.set()           # SIGTERM в окне — не «мигание», просто сворачиваемся
        relay = asyncio.create_task(_relay())
    else:
        relay = None
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    ok = task.result()
                except (Exception,) as error:
                    logger.debug(f'binodex confirm_stable: наблюдатель упал — {error}')
                    ok = False
                if not ok:
                    abort.set()
                    return False
        return not abort.is_set()
    finally:
        abort.set()               # страховка: наблюдатели не должны пережить выход
        if relay is not None:
            relay.cancel()
            tasks.add(relay)
        # Ждём фактического выхода: _watch заметит abort на следующем тике (≤ FEED_CONFIRM_GAP),
        # зато сокет закроется здесь, а не когда-нибудь на совести GC.
        await asyncio.gather(*tasks, return_exceptions=True)


async def feed_alive(pair: str = FEED_PROBE_PAIR, timeout: float = FEED_ALIVE_TIMEOUT) -> bool:
    """True — binodex прислал хотя бы один ценовой кадр за timeout (фид жив). Браузер не нужен.
    Любой сбой (нет сети/таймаут/нет кадров) → False (аутэйдж/недоступность)."""
    try:
        return bool(await asyncio.wait_for(_probe(pair), timeout=timeout))
    except (Exception,) as err:
        logger.debug(f"binodex feed_alive: {err}")
        return False


async def api_alive(timeout: float = API_ALIVE_TIMEOUT) -> bool:
    """False ТОЛЬКО при УВЕРЕННОМ падении auth/config API binodex (api.binodex.app): 5xx
    (502/503/504 от Cloudflare — origin недоступен/крашлупит) ЛИБО таймаут/сетевой сбой. Это и есть
    backend-аутэйдж, при котором релогин/прокси/смена движка бесполезны (всё тянет тот же API) →
    надо ЖДАТЬ восстановления браузер-фри. Всё остальное (2xx/3xx/4xx) → True.

    ВАЖНО (грабли 2026-07-23, инцидент флапал 502↔403): 4xx НЕ считаем за «down». В частности
    403 «Just a moment…» = Cloudflare managed-challenge — НЕОДНОЗНАЧНО: реальный браузер может его
    пройти, а если нет — это egress/бот-блок, чьё лечение прокси/failover (EXIT_SETUP → другой
    провайдер), а НЕ вечное браузер-фри ожидание. Если бы challenge держался постоянно, а мы считали
    его «down» — бот НИКОГДА бы не стартовал. Поэтому за «жди» отвечает только уверенный 5xx/таймаут;
    challenge/бот-блок уходит в штатный прокси/failover-поток. Редиректы не следуем (302 ≠ здоровье)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_API_HEALTH_URL, headers=_HEADERS, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return resp.status < 500
    except (Exception,) as err:
        logger.debug(f"binodex api_alive: {err}")
        return False


async def binodex_ready(pair: str = FEED_PROBE_PAIR) -> bool:
    """binodex готов к подъёму браузера: И auth-API (api.binodex.app) жив, И market-WS отдаёт кадр.
    Любой из двух мёртв → False (держим браузер-фри ожидание, НЕ молотим релогин/прокси/рестарт).
    API проверяем ПЕРВЫМ: при backend-аутэйдже фид часто ещё живой, но толку от него нет — app-shell
    без API не поднимется."""
    if not await api_alive():
        return False
    return await feed_alive(pair)


async def wait_for_feed(stop_event=None, pair: str = FEED_PROBE_PAIR) -> bool:
    """Ждать, пока binodex снова станет РАБОТОСПОСОБЕН (browser-free): и auth-API отвечает, и
    market-WS отдаёт котировки (binodex_ready). Возвращает True — binodex поднялся; False — прервано
    stop_event (SIGTERM). Уведомления «вниз/вверх» — на вызывающем (ОДНО сообщение до и одно после).
    Ждём ИМЕННО полной готовности: если вернуть True на живом фиде при лежащем API, бот поднимет
    браузер и снова упрётся в невмонтированный app-shell → петля релогина/прокси (грабли 2026-07-23).
    Heartbeat в лог раз в ~10 мин — какой из сервисов (API/WS) ещё лежит, чтобы затяжной аутэйдж
    не был «серым отказом» без следов.
    Признак жизни сам по себе НЕ означает возврат: техработы идут волнами, поэтому перед True
    выдерживаем окно подтверждения (confirm_stable). Мигнуло в окне — тихо (warning в файл, без
    TG-спама) уходим на новый круг ожидания."""
    waited = 0.0
    next_heartbeat = FEED_WAIT_HEARTBEAT   # порог следующего лога; растёт окнами — не зависит от
    while not (stop_event is not None and stop_event.is_set()):   # кратности HEARTBEAT/POLL
        if await binodex_ready(pair):
            if await confirm_stable(pair, stop_event):
                return True
            if stop_event is not None and stop_event.is_set():
                return False       # SIGTERM пришёл в окне подтверждения
            logger.warning(f'wait_for_feed: binodex подал признак жизни, но не удержал '
                           f'{int(FEED_CONFIRM_WINDOW)}с — продолжаю ждать (pair={pair})')
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=FEED_WAIT_POLL)
                return False  # stop_event выставлен во время паузы
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(FEED_WAIT_POLL)
        waited += FEED_WAIT_POLL
        if waited >= next_heartbeat:
            down = 'auth-API api.binodex.app' if not await api_alive() else 'market-WS котировок'
            logger.warning(f'wait_for_feed: binodex недоступен уже ~{int(waited // 60)} мин '
                           f'({down} не отвечает, pair={pair})')
            next_heartbeat += FEED_WAIT_HEARTBEAT
    return False
