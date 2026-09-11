"""Отслеживание цен binodex OTC через WebSocket (wss://api-coins.binodex.io/market).

Фреймы Socket.IO вида: 42/graphic,["graphic",{"symbol":"EUR/USD-OTC","timestamp":...,
"price":1.13933,"high":...,"low":...}]. Цена-число берётся отсюда (на странице она в
<canvas>, из DOM не снять).

Помимо последней цены трекер хранит короткую историю тиков с временем приёма
(`(recv_wall, price)`) для get_price_at — выбора цены по моменту кадра (`t_shot`).

NB: основной источник цены кадра теперь — window.chartData.price на странице (медиана
чтений в otc_app.screenshot_otc); WS-цена/get_price_at оставлены как ФОЛБЭК и под liveness
(подтверждение загрузки пары, feed_dead). WS опережает график на ~150 мс, поэтому как цену
кадра не годится — подробно docs/BINODEX_PRICE.md.
"""
import json
import time
from collections import deque

from logs import init_logger

logger = init_logger(__name__)


def symbol_key(asset: str | None) -> str | None:
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
        # Подключался ли WS хоть раз ЗА ЖИЗНЬ ПРОЦЕССА. Переживает reset() намеренно: он
        # различает «фид отвалился» (лечится пересозданием браузера) и «фида тут вообще нет».
        self.ws_ever_connected: bool = False
        self.last_tick: float | None = None                 # monotonic-время последнего тика (feed_dead)

    def handle_message(self, payload):
        """Разобрать входящий WS-фрейм binodex и обновить последнюю цену + историю тиков."""
        if isinstance(payload, (bytes, bytearray)):
            try:
                payload = payload.decode('utf-8')
            except (Exception,) as error:
                logger.info(f"WS: не удалось декодировать payload — {error}")
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
            logger.info(f"WS: ошибка разбора котировки — {error}")

    def reset(self) -> None:
        """Сбросить состояние под НОВУЮ браузер-сессию (init_otc после ребута/отвала).

        Трекер — process-global (один на процесс), а цены/история/liveness привязаны к
        конкретной странице и WS: без сброса состояние прошлой сессии течёт в новую. Хуже
        всего это било по выбору пары — select_otc_pair видел цену из ПРОШЛОЙ сессии и
        мгновенно считал пару загруженной, то есть первый кадр опциона уходил с чужого или
        пустого графика. Метод потерялся при копировании программы (ссылка на него в
        комментарии выше осталась), возвращён 11-09-2026."""
        self.prices.clear()
        self.history.clear()
        self.ws_connected = False
        self.last_tick = None

    def get_price(self, asset: str = None) -> float | None:
        """Последняя цена по активу. asset вида 'EUR/USD' или 'EUR/USD OTC' → ключ 'EUR/USD-OTC'.
        При заданном, но не найденном активе → None (не отдаём цену чужой пары)."""
        key = symbol_key(asset)
        if key:
            return self.prices.get(key)
        # asset не указан — последняя полученная цена (без копии всего списка значений)
        if self.prices:
            return next(reversed(self.prices.values()))
        return None

    def has_tick_since(self, asset: str, wall: float) -> bool:
        """Пришёл ли по символу тик ПОЗЖЕ момента `wall` (time.time()).

        Явный критерий вместо игр с get_price_at(back_ms=...): тот считает cutoff как
        at_wall - back_ms и отдаёт последний тик НЕ ПОЗЖЕ cutoff, то есть ровно наоборот —
        цену ДО момента. Плюс при непустой истории он почти никогда не отдаёт None (падает на
        самый ранний тик), так что «нет свежего тика» по нему не отличить.

        Нужен для подтверждения выбора пары: в пределах одной сессии пара возвращается каждые
        несколько опционов, и старые тики подтверждали бы загрузку мгновенно."""
        dq = self.history.get(symbol_key(asset))
        return bool(dq) and dq[-1][0] >= wall

    def get_price_at(self, asset: str, at_wall: float, back_ms: float = 0.0) -> float | None:
        """Цена, отрисованная на графике на момент кадра at_wall (локальное time.time()):
        последний тик с временем приёма <= (at_wall - back_ms). Это убирает «забег вперёд» —
        прод раньше брал самый свежий тик, который часто приходил уже ПОСЛЕ кадра.

        back_ms — необязательный сдвиг назад (мс) под лаг отрисовки ярлыка; по умолчанию 0
        (последний тик до кадра — он совпал с ценником в 8 из 10 проверочных кадров).
        Если нет истории — откат на последнюю известную цену (get_price)."""
        key = symbol_key(asset)
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
        if not self.ws_ever_connected:
            # WS не поднимался НИ РАЗУ за процесс: либо домен переехал (хинт перехвата
            # устарел — так уже было, .io → .app), либо фид недоступен отсюда. Пересоздание
            # браузера это не лечит, а «мёртвый фид» после КАЖДОГО опциона уводило программу
            # в вечный цикл close+re-init с одним warning в файл. Детект в этом случае
            # деградирован (о чём честно пишет _verify_otc_ready), цена берётся из chartData.
            return False
        if self.last_tick is None:
            return True  # WS был жив, закрылся и тиков не принёс
        return (time.monotonic() - self.last_tick) > max_silence
