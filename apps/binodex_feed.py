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


def _subscribe_frame(pair: str) -> str:
    return ('42/graphic,["graphic",{"method":"SUBSCRIBE","symbol":"%s",'
            '"interval":"30s","otc":true}]' % pair)


async def _probe(pair: str) -> bool:
    """Одно подключение: хэндшейк → SUBSCRIBE → True на первом ценовом кадре.

    Следуем протоколу Engine.IO v4: connect namespace (`40/graphic,`) шлём ТОЛЬКО после
    open-кадра `0{...}` (раньше слали сразу — хрупко и вопреки docstring). `44` (namespace
    connect error) → фид недоступен, быстрый выход (а не ожидание внешнего таймаута)."""
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(_WS_URL, headers=_HEADERS, heartbeat=None) as ws:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.data
                if data.startswith('0'):
                    await ws.send_str('40/graphic,')
                elif data.startswith('44'):
                    logger.debug(f'binodex _probe: namespace connect error: {data[:120]}')
                    return False
                elif data.startswith('40'):
                    await ws.send_str(_subscribe_frame(pair))
                elif data == '2':
                    await ws.send_str('3')
                elif data.startswith('42') and '"price"' in data:
                    return True
    return False


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
    не был «серым отказом» без следов."""
    waited = 0.0
    next_heartbeat = FEED_WAIT_HEARTBEAT   # порог следующего лога; растёт окнами — не зависит от
    while not (stop_event is not None and stop_event.is_set()):   # кратности HEARTBEAT/POLL
        if await binodex_ready(pair):
            return True
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
