"""Отслеживание цен binodex OTC через WebSocket (wss://api-coins.binodex.io/market).

Фреймы Socket.IO вида: 42/graphic,["graphic",{"symbol":"EUR/USD-OTC","price":1.13933,...}].
Цена-число берётся отсюда (на странице она в <canvas>, из DOM не снять).
"""
import json

from logs import init_logger

logger = init_logger(__name__)


class WebSocketPriceTracker:
    """Котировки binodex по символам '<pair>-OTC' (например 'EUR/USD-OTC')."""

    def __init__(self):
        self.prices: dict[str, float] = {}  # 'EUR/USD-OTC' -> price
        self.ws_connected: bool = False

    def handle_message(self, payload):
        """Разобрать входящий WS-фрейм binodex и обновить self.prices."""
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
                self.prices[data['symbol']] = float(data['price'])
        except (Exception,) as error:
            logger.debug(f"WS: ошибка разбора котировки — {error}")

    def get_price(self, asset: str = None) -> float | None:
        """Цена по активу. asset вида 'EUR/USD' или 'EUR/USD OTC' → ключ 'EUR/USD-OTC'.
        При заданном, но не найденном активе → None (не отдаём цену чужой пары)."""
        if asset:
            base = asset.replace(' OTC', '').replace('-OTC', '').strip()  # 'EUR/USD'
            return self.prices.get(f"{base}-OTC")
        # asset не указан — последняя полученная цена (без копии всего списка значений)
        if self.prices:
            return next(reversed(self.prices.values()))
        return None
