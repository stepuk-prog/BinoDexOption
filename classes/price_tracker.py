"""Отслеживание цен PocketOption через WebSocket."""
import json

from logs import init_logger

logger = init_logger(__name__)


class WebSocketPriceTracker:
    """Отслеживание цен через WebSocket"""

    def __init__(self):
        self.prices: dict[str, float] = {}  # asset_name -> price
        self.ws_connected: bool = False

    def handle_message(self, payload):
        """Обработка входящего WebSocket сообщения"""
        # Конвертируем bytes в str если нужно
        if isinstance(payload, bytes):
            try:
                payload = payload.decode('utf-8')
            except (Exception,) as error:
                logger.debug(f"WS: не удалось декодировать payload — {error}")
                return

        # Парсим данные
        try:
            # Формат PocketOption: [["SYMBOL",timestamp, price]]
            if payload.startswith('[['):
                data = json.loads(payload)
                self._parse_stream_data(data)
        except json.JSONDecodeError:
            pass  # шумный поток PocketOption — не-JSON кадры это норма
        except (Exception,) as error:
            logger.debug(f"WS: ошибка разбора котировки — {error}")

    def _parse_stream_data(self, data):
        """Парсинг потоковых данных котировок PocketOption"""
        # Формат: [["GBPJPY_otc", 1768488749.874, 216.517]]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, list) and len(item) >= 3:
                    symbol = item[0]  # "GBPJPY_otc"
                    price = item[2]   # 216.517
                    if isinstance(price, (int, float)):
                        self.prices[symbol] = float(price)

    def get_price(self, asset: str = None) -> float | None:
        """Получить последнюю цену для актива"""
        if asset:
            # Нормализуем имя актива: "GBP/JPY" -> "GBPJPY", "GBPJPY OTC" -> "GBPJPY_otc"
            normalized = asset.replace('/', '').replace(' ', '_').upper()

            # Ищем точное совпадение
            if asset in self.prices:
                return self.prices[asset]

            # Ищем с суффиксом _otc
            otc_key = normalized.replace('_OTC', '') + '_otc'
            if otc_key in self.prices:
                return self.prices[otc_key]

            # Ищем частичное совпадение
            for key, price in self.prices.items():
                key_normalized = key.replace('_otc', '').upper()
                if normalized.replace('_OTC', '') == key_normalized:
                    return price

        # Вернуть последнюю полученную цену, если актив не указан
        if self.prices:
            return list(self.prices.values())[-1]
        return None
