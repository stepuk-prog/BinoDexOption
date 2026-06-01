# Как правильно снимать цену binodex OTC (референс для других проектов)

Документ описывает проверенную на живом сайте схему «скриншот графика + цена под ним»
для binodex.app (OTC). Цель — чтобы цена в подписи **совпадала с ценой, нарисованной на
графике в кадре**. Схема портируемая: ниже суть, правило синхронизации и скелет кода.

## 1. Источник цены — WebSocket, не DOM

На странице `/trade` цена нарисована внутри `<canvas>` — из DOM её не снять. Число берётся
из WebSocket котировок:

- **URL:** `wss://api-coins.binodex.io/market/?EIO=4&transport=websocket` (Socket.IO).
- **Событие (входящий фрейм):**
  ```
  42/graphic,["graphic",{"symbol":"EUR/USD-OTC","timestamp":1780307015001,"price":1.23065,"high":1.23066,"low":1.23065}]
  ```
  - `symbol` — `'<pair>-OTC'` (напр. `'EUR/USD-OTC'`);
  - `price` — текущая цена (5 знаков в потоке, график показывает округлённой — см. §4);
  - `timestamp` — серверное время тика (epoch ms) — **для синхронизации не используем** (часы
    сервера и клиента могут расходиться);
  - `high`/`low` — экстремумы строящейся свечи.
- **Частота:** ~5–6 тиков/с (тик каждые ~150–200 мс).
- **Подписка (страница шлёт сама):**
  ```
  42/graphic,["graphic",{"method":"SUBSCRIBE","symbol":"EUR/USD","interval":"15s","otc":true}]
  ```
  График строится из этого же фида (свечи 15с) — т.е. нарисованный ценник = `price` отсюда.

> Прочие WS страницы (`api.binodex.io/market` → `pairs`, `api.binodex.io/binodex`) цену **не**
> стримят (список пар / служебный канал) — слушать только `api-coins.binodex.io`.

Перехват ставить **до навигации**, чтобы поймать поток с самого старта:
```python
def on_ws(ws):
    if 'api-coins.binodex.io' in ws.url:
        ws.on('framereceived', lambda d: tracker.handle_message(getattr(d, 'payload', d)))
page.on('websocket', on_ws)
```

## 2. Проблема, которую решаем

График и наша цена — из одного фида, но **наивно они не совпадают**:

1. Ярлык последней цены на графике обновляется с **задержкой отрисовки** ~0–500 мс
   относительно свежего тика (троттлинг рендера). В кадре может быть нарисован тик «300–500 мс
   назад», тогда как из WS уже пришёл более свежий.
2. Типичная ошибка: читать «последний тик в памяти» **после** `screenshot()`. Этот тик часто
   приходит уже ПОСЛЕ кадра → цена «убегает вперёд» от графика.

Эмпирика (10 скринов EUR/USD): нарисованный ценник = **последний WS-тик, пришедший ДО момента
кадра**, округлённый до decimals актива. Совпало в 8/10; в 2/10 ярлык отставал ещё на ~1 тик
(лаг отрисовки). Подробности проверки — см. историю в `scripts/probe_ws_match.py`.

## 3. Правило синхронизации (главное)

1. Трекер хранит **историю тиков с локальным временем приёма**: `deque[(recv_wall, price)]`
   на символ (`recv_wall = time.time()` в момент получения фрейма). ~64 тика на символ хватает.
2. `t_shot = time.time()` фиксируется **вплотную ПЕРЕД** `element.screenshot()`.
3. Цена кадра = **последний тик с `recv_wall <= t_shot`** (не самый свежий из памяти).
   Опционально — лёгкий сдвиг назад `back_ms` под лаг отрисовки (по умолчанию 0).
4. Округлять до **decimals актива** (см. §4), как это делает график.

Ключ: время приёма — **локальные часы** (`time.time()`), тем же `time.time()` мерим `t_shot`.
Серверный `timestamp` из фрейма для cutoff не годится (рассинхрон часов).

## 4. Decimals (число знаков) — per-asset

Число знаков у каждого актива своё и совпадает с тем, что рисует график. В этом проекте —
колонка `round` в `option_data.otc_data_view`. Эталон: EUR/USD=4, AUD/USD=5, JPY-пары=2.
В свою цену надо подставлять то же число знаков, иначе подпись покажет лишний/недостающий знак.

## 5. Скелет кода (портируемый)

Трекер:
```python
import time
from collections import deque

def _symbol_key(asset):  # 'EUR/USD' | 'EUR/USD OTC' -> 'EUR/USD-OTC'
    if not asset: return None
    base = asset.replace(' OTC', '').replace('-OTC', '').strip()
    return f"{base}-OTC"

class PriceTracker:
    MAX_HISTORY = 64
    def __init__(self):
        self.prices = {}                  # symbol -> последняя цена
        self.history = {}                 # symbol -> deque[(recv_wall, price)]

    def handle_message(self, payload):
        # ... распарсить graphic-фрейм до dict data с symbol/price ...
        symbol, price = data['symbol'], float(data['price'])
        self.prices[symbol] = price
        dq = self.history.setdefault(symbol, deque(maxlen=self.MAX_HISTORY))
        dq.append((time.time(), price))   # ВРЕМЯ ПРИЁМА, локальные часы

    def get_price_at(self, asset, at_wall, back_ms=0.0):
        dq = self.history.get(_symbol_key(asset))
        if not dq: return self.prices.get(_symbol_key(asset))
        cutoff = at_wall - back_ms / 1000.0
        chosen = None
        for recv_wall, price in dq:        # от старых к новым
            if recv_wall <= cutoff: chosen = price
            else: break
        return chosen if chosen is not None else dq[0][1]
```

Снятие кадра:
```python
element = page.locator(chart_zone).first
await element.wait_for(state='visible', timeout=...)
t_shot = time.time()                       # момент кадра — ДО screenshot
await element.screenshot(path=shot_path)
price = tracker.get_price_at(asset, t_shot)
if price is None:                          # цены нет → ретрай
    ...
price = round(price, asset_decimals)       # как на графике
```

## 6. Чек-лист переноса в другой проект

- [ ] WS-перехват `api-coins.binodex.io` поставлен **до** `page.goto`.
- [ ] Парсинг `graphic`-фрейма: префикс `,[` → `json.loads` → `arr[1]` → `{symbol, price}`.
- [ ] Трекер хранит `(recv_wall=time.time(), price)`, не только последнюю цену.
- [ ] `t_shot` берётся вплотную ПЕРЕД `screenshot()`.
- [ ] Цена = `get_price_at(asset, t_shot)` (последний тик до кадра), **не** «свежий после кадра».
- [ ] Округление до per-asset decimals (совпадает с графиком).
- [ ] `price == 0.0` — валидная цена, не путать с «нет цены» (проверять `is None`).

## 7. Остаточное ограничение и запрос к сайту

Плавающий лаг отрисовки (~0–500 мс) на своей стороне до конца не убрать — остаётся редкий
промах ±1 в последнем знаке. Полностью снимается только если сайт отдаст **отрисованную**
цену со страницы: глобал `window.__lastPrice` или DOM-атрибут `data-last-price` (+ её
`timestamp`). Тогда читаем ровно нарисованное число с их округлением. Текст запроса
разработчикам — см. отдельное предложение.
