"""Отслеживание цен binodex OTC через WebSocket (wss://api-coins.binodex.io/market).

Фреймы Socket.IO вида: 42/graphic,["graphic",{"symbol":"EUR/USD-OTC","timestamp":...,
"price":1.13933,"high":...,"low":...}]. Цена-число берётся отсюда (на странице она в
<canvas>, из DOM не снять).

Помимо последней цены трекер хранит короткую историю тиков с временем приёма
(`(recv_wall, price)`), чтобы по моменту скриншота (`t_shot`) выбрать ту цену, что
реально нарисована на графике, а не более свежий тик, успевший прийти после кадра
(см. get_price_at). Эмпирически нарисованный ценник = последний тик до момента кадра.
"""
import json
import time
from collections import deque

from logs import init_logger

logger = init_logger(__name__)


def _symbol_key(asset: str | None) -> str | None:
    """asset вида 'EUR/USD' или 'EUR/USD OTC' → ключ котировок 'EUR/USD-OTC'."""
    if not asset:
        return None
    base = asset.replace(' OTC', '').replace('-OTC', '').strip()  # 'EUR/USD'
    return f"{base}-OTC"


class WebSocketPriceTracker:
    """Котировки binodex по символам '<pair>-OTC' (например 'EUR/USD-OTC')."""

    MAX_HISTORY = 64  # тиков на символ (~10с при ~6 тик/с) — хватает на окно вокруг кадра

    def __init__(self):
        self.prices: dict[str, float] = {}                  # 'EUR/USD-OTC' -> последняя цена
        self.history: dict[str, deque] = {}                 # symbol -> deque[(recv_wall, price)]
        self.ws_connected: bool = False
        self.last_tick: float | None = None                 # monotonic-время последнего тика (feed_dead)

    def handle_message(self, payload):
        """Разобрать входящий WS-фрейм binodex и обновить последнюю цену + историю тиков."""
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = payload.decode('utf-8')
            except (Exception,) as error:
                logger.debug(f"WS: не удалось декодировать payload — {error}")
                return
        payload = str(payload)
        # Socket.IO-префикс ('42/graphic,') до JSON-массива ['graphic', {...}]
        i = payload.find(',[')
        if i == -1:
            return
        try:
            arr = json.loads(payload[i + 1:])
            data = arr[1] if isinstance(arr, list) and len(arr) > 1 else None
            if (isinstance(data, dict) and 'symbol' in data
                    and isinstance(data.get('price'), (int, float))):
                symbol = data['symbol']
                price = float(data['price'])
                self.prices[symbol] = price
                # время ПРИЁМА кадра (локальные часы) — им же мерится t_shot в screenshot_otc,
                # серверный data['timestamp'] не используем (часы сервера/клиента могут расходиться).
                dq = self.history.get(symbol)
                if dq is None:
                    dq = self.history[symbol] = deque(maxlen=self.MAX_HISTORY)
                dq.append((time.time(), price))
                self.last_tick = time.monotonic()  # фид жив — отметка для feed_dead
        except (Exception,) as error:
            logger.debug(f"WS: ошибка разбора котировки — {error}")

    def get_price(self, asset: str = None) -> float | None:
        """Последняя цена по активу. asset вида 'EUR/USD' или 'EUR/USD OTC' → ключ 'EUR/USD-OTC'.
        При заданном, но не найденном активе → None (не отдаём цену чужой пары)."""
        key = _symbol_key(asset)
        if key:
            return self.prices.get(key)
        # asset не указан — последняя полученная цена (без копии всего списка значений)
        if self.prices:
            return next(reversed(self.prices.values()))
        return None

    def get_price_at(self, asset: str, at_wall: float, back_ms: float = 0.0) -> float | None:
        """Цена, отрисованная на графике на момент кадра at_wall (локальное time.time()):
        последний тик с временем приёма <= (at_wall - back_ms). Это убирает «забег вперёд» —
        прод раньше брал самый свежий тик, который часто приходил уже ПОСЛЕ кадра.

        back_ms — необязательный сдвиг назад (мс) под лаг отрисовки ярлыка; по умолчанию 0
        (последний тик до кадра — он совпал с ценником в 8 из 10 проверочных кадров).
        Если нет истории — откат на последнюю известную цену (get_price)."""
        key = _symbol_key(asset)
        if not key:
            return self.get_price(asset)
        dq = self.history.get(key)
        if not dq:
            return self.get_price(asset)
        cutoff = at_wall - back_ms / 1000.0
        chosen = None
        for recv_wall, price in dq:  # deque упорядочен от старых к новым
            if recv_wall <= cutoff:
                chosen = price
            else:
                break
        # все тики позже cutoff (кадр снят раньше первого тика в окне) — берём самый ранний
        return chosen if chosen is not None else dq[0][1]

    def feed_dead(self, max_silence: float) -> bool:
        """WS-фид котировок мёртв = WS закрыт (`ws_connected=False`) И давно нет тика
        (> max_silence сек). Консервативно требуем оба условия, чтобы кратковременный
        реконнект Socket.IO не дал ложного срабатывания. Дополняет URL-детект /trade (§4.4)."""
        if self.ws_connected:
            return False
        if self.last_tick is None:
            return True  # WS закрыт и тиков не было вовсе
        return (time.monotonic() - self.last_tick) > max_silence
