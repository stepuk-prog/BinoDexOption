"""Замер задержки chartData (= график) относительно WS-фида котировок binodex OTC.

Гипотеза (docs/BINODEX_PRICE.md §7): график отстаёт от WS — свежий тик приходит по WS
первым, а window.chartData.price плавно (easing) ползёт к нему за несколько кадров.
Здесь меряем эту задержку численно.

Метод:
  • WS-фрейм → пишем (t_monotonic, price);
  • в тесном цикле опрашиваем window.chartData.price → пишем (t_monotonic, price);
  • обе серии интерполируем на общую сетку и ищем lag>=0, максимизирующий корреляцию
    chartData(t) ↔ WS(t - lag). Это и есть отставание графика от WS.

Ничего не постит и не пишет в БД. Запуск:
    TIMEFRAME=1m BINARY=0 PYTHONPATH=. .venv/bin/python scripts/probe_lag.py
"""
import asyncio
import json
import os
import time

os.environ['TIMEFRAME'] = '1m'
os.environ['BINARY'] = '0'

import settings.browser_set as bset
bset.browser_launch_options['headless'] = True

import logs.log_init as li
li.TelegramBotHandler.emit = lambda self, record: None

from settings.config import database
from apps.browser_app import init_browser
from apps.otc_app import init_otc, select_otc_pair

DURATION = 12.0          # сколько секунд собирать
POLL_GAP = 0.0           # пауза между опросами chartData (0 = максимально часто)
LAG_MIN, LAG_MAX, LAG_STEP = -0.10, 1.20, 0.01   # перебор lag, сек
GRID = 0.02              # шаг общей сетки, сек

JS_PRICE = "() => (window.chartData && typeof window.chartData.price === 'number') ? window.chartData.price : null"

ws_series: list[tuple[float, float]] = []   # (t_monotonic, price)
_symbol_filter = {'val': None}


def on_frame(payload):
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode('utf-8')
        except Exception:
            return
    payload = str(payload)
    i = payload.find(',[')
    if i == -1:
        return
    try:
        arr = json.loads(payload[i + 1:])
        data = arr[1] if isinstance(arr, list) and len(arr) > 1 else None
        if not (isinstance(data, dict) and 'price' in data and 'symbol' in data):
            return
        if _symbol_filter['val'] and data['symbol'] != _symbol_filter['val']:
            return
        ws_series.append((time.monotonic(), float(data['price'])))
    except Exception:
        pass


def interp(series, t):
    """Линейная интерполяция price в момент t по отсортированной серии (t,price). None вне диапазона."""
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    lo, hi = 0, len(series) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    t0, p0 = series[lo]
    t1, p1 = series[hi]
    if t1 == t0:
        return p0
    return p0 + (p1 - p0) * (t - t0) / (t1 - t0)


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0
    return sxy / (sxx * syy) ** 0.5


async def main():
    await database.connect()
    rows = await database.option_data_pocket(tf='1m', exclude_ids=[])
    pairs = [r['name_val'] for r in (rows or [])]
    if not pairs:
        print('❌ нет активных OTC-пар'); await database.close(); return

    res = await init_browser()
    if not res.success:
        print('❌ init_browser:', res.manager_or_error); await database.close(); return
    manager = res.manager
    page = manager.pages['main']

    page.on('websocket', lambda ws: (
        ws.on('framereceived', lambda d: on_frame(getattr(d, 'payload', d)))
        if 'api-coins.binodex.io' in ws.url else None))

    if not await init_otc(manager):
        print('❌ init_otc'); await manager.close(); await database.close(); return

    chosen = None
    for pair in pairs:
        if await select_otc_pair(page, pair):
            chosen = pair
            break
    if not chosen:
        print('❌ ни одна пара не прогрузилась'); await manager.close(); await database.close(); return
    _symbol_filter['val'] = chosen + '-OTC'
    print(f'Пара: {chosen}  (symbol={_symbol_filter["val"]})')
    print(f'Собираю {DURATION:.0f}с…')

    cd_series: list[tuple[float, float]] = []
    t_end = time.monotonic() + DURATION
    while time.monotonic() < t_end:
        try:
            v = await page.evaluate(JS_PRICE)
        except Exception:
            v = None
        if v is not None:
            cd_series.append((time.monotonic(), float(v)))
        if POLL_GAP:
            await asyncio.sleep(POLL_GAP)

    await manager.close()
    await database.close()

    print(f'\nСобрано: WS={len(ws_series)} тиков, chartData={len(cd_series)} чтений')
    if len(ws_series) < 5 or len(cd_series) < 20:
        print('❌ мало данных'); return

    t0 = max(ws_series[0][0], cd_series[0][0])
    t1 = min(ws_series[-1][0], cd_series[-1][0])
    grid = []
    t = t0
    while t <= t1:
        grid.append(t); t += GRID

    best = (None, -2.0)
    results = []
    lag = LAG_MIN
    while lag <= LAG_MAX + 1e-9:
        xs, ys = [], []
        for tg in grid:
            cd = interp(cd_series, tg)
            ws = interp(ws_series, tg - lag)
            if cd is not None and ws is not None:
                xs.append(cd); ys.append(ws)
        r = pearson(xs, ys)
        results.append((lag, r, len(xs)))
        if r > best[1]:
            best = (lag, r)
        lag += LAG_STEP

    print('\nКорреляция chartData(t) ↔ WS(t - lag):')
    # печать каждые ~50мс, чтобы видеть форму
    for lag, r, n in results:
        if abs((lag / LAG_STEP) % 5) < 0.5:
            bar = '#' * int(max(0, r) * 40)
            print(f'  lag={lag*1000:+6.0f}мс  r={r:+.3f}  {bar}')
    print(f'\n➡ Максимум корреляции при lag = {best[0]*1000:.0f} мс  (r={best[1]:.3f})')
    print(f'   Т.е. график (chartData) отстаёт от WS примерно на {best[0]*1000:.0f} мс.')


if __name__ == '__main__':
    asyncio.run(main())