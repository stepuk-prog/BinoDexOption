"""Браузер-фри health-чек фида котировок binodex (api-coins.binodex.io, Socket.IO).

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
from settings.browser_config import otc_ws_origin

logger = init_logger(__name__)

_WS_URL = 'wss://api-coins.binodex.io/market/?EIO=4&transport=websocket'
_HEADERS = {'Origin': otc_ws_origin, 'User-Agent': 'Mozilla/5.0'}  # origin из binodex_settings

FEED_PROBE_PAIR = 'EUR/USD'   # дефолтная пара — присутствует всегда
FEED_ALIVE_TIMEOUT = 10.0     # сколько ждать первого ценового кадра в одной попытке
FEED_WAIT_POLL = 30.0         # пауза между попытками в wait_for_feed (аутэйдж тянется минутами)
FEED_WAIT_HEARTBEAT = 600.0   # как часто логировать «фид всё ещё молчит» при затяжном ожидании


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


async def wait_for_feed(stop_event=None, pair: str = FEED_PROBE_PAIR) -> bool:
    """Ждать, пока binodex снова начнёт отдавать котировки (браузер не держим). Возвращает
    True — фид вернулся; False — прервано stop_event (SIGTERM). Уведомления «вниз/вверх» —
    на вызывающем (шлёт ОДНО сообщение до и одно после).
    Heartbeat в лог раз в ~10 мин ожидания — чтобы затяжное молчание фида (в т.ч. смена протокола
    binodex) не было «серым отказом» без следов."""
    waited = 0.0
    next_heartbeat = FEED_WAIT_HEARTBEAT   # порог следующего лога; растёт окнами — не зависит от
    while not (stop_event is not None and stop_event.is_set()):   # кратности HEARTBEAT/POLL
        if await feed_alive(pair):
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
            logger.warning(f'wait_for_feed: фид binodex молчит уже ~{int(waited // 60)} мин (pair={pair})')
            next_heartbeat += FEED_WAIT_HEARTBEAT
    return False
