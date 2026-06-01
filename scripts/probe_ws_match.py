"""Сверка цены WS ↔ цена на скрине binodex OTC (TIMEFRAME=1m, BINARY=0).

Делает N скринов зоны графика. Для каждого скрина фиксирует t_shot (локальное время
прямо перед element.screenshot()) и сохраняет окно WS-тиков вокруг этого момента: для
каждого тика — Δ(мс) от t_shot, серверный timestamp и price. Потом руками открываешь
shot_NN.png и смотришь, цена какого тика (по Δ) реально нарисована на графике —
так находим систематический сдвиг «кадр ↔ тик».

Ничего не постит и не пишет в БД. Запуск:
    TIMEFRAME=1m BINARY=0 PYTHONPATH=. .venv/bin/python scripts/probe_ws_match.py
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
from settings.browser_config import screen_zone_otc
from apps.browser_app import init_browser
from apps.otc_app import init_otc, select_otc_pair

N_SHOTS = 10
SHOT_GAP = 2.5                 # пауза между скринами (чтобы цена успела сдвинуться)
WIN_BEFORE, WIN_AFTER = 1.2, 0.6   # окно тиков вокруг t_shot, сек
OUT_DIR = 'pictures/ws_test'   # gitignored (pictures/), скрины shot_NN.png + summary.txt

# Лог всех тиков выбранного символа: (wall_recv, server_ts, price)
ticks: list[tuple[float, int, float]] = []
_symbol_filter = {'val': None}   # ключ вида 'EUR/USD-OTC'


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
        ticks.append((time.time(), int(data.get('timestamp', 0)), float(data['price'])))
    except Exception:
        pass


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    await database.connect()
    rows = await database.option_data_pocket(tf='1m', exclude_ids=[])
    pairs = [r['name_val'] for r in (rows or [])]
    if not pairs:
        print('❌ нет активных OTC-пар'); return

    res = await init_browser()
    if not res.success:
        print('❌ init_browser:', res.manager_or_error); return
    manager = res.manager
    page = manager.pages['main']

    page.on('websocket', lambda ws: (
        ws.on('framereceived', lambda d: on_frame(getattr(d, 'payload', d)))
        if 'api-coins.binodex.io' in ws.url else None))

    if not await init_otc(manager):
        print('❌ init_otc'); await manager.close(); await database.close(); return

    # выбрать первую пару, которая прогрузилась
    chosen = None
    for pair in pairs:
        if await select_otc_pair(page, pair):
            chosen = pair
            break
    if not chosen:
        print('❌ ни одна пара не прогрузилась'); await manager.close(); await database.close(); return
    _symbol_filter['val'] = chosen + '-OTC'
    print(f'Пара: {chosen}  (symbol={_symbol_filter["val"]})\n')

    element = page.locator(screen_zone_otc).first
    await element.wait_for(state='visible', timeout=10000)

    summary = []
    for n in range(1, N_SHOTS + 1):
        await asyncio.sleep(SHOT_GAP)
        t_shot = time.time()                       # фиксируем момент кадра
        shot_file = os.path.join(OUT_DIR, f'shot_{n:02d}.png')
        await element.screenshot(path=shot_file)
        t_after = time.time()

        # окно тиков вокруг t_shot
        win = [(w, ts, p) for (w, ts, p) in ticks
               if t_shot - WIN_BEFORE <= w <= t_shot + WIN_AFTER]
        latest_before = max((t for t in win if t[0] <= t_shot), default=None,
                            key=lambda t: t[0])

        lines = [f'=== shot_{n:02d}.png ===  t_shot={t_shot:.3f}  '
                 f'(screenshot занял {(t_after - t_shot) * 1000:.0f} мс)']
        for (w, ts, p) in win:
            d_ms = (w - t_shot) * 1000
            mark = '  <-- latest before t_shot' if latest_before and w == latest_before[0] else ''
            lines.append(f'   Δ={d_ms:+7.0f}мс  ts={ts}  price={p}{mark}')
        if not win:
            lines.append('   (нет тиков в окне)')
        block = '\n'.join(lines)
        print(block + '\n')
        summary.append(block)

    with open(os.path.join(OUT_DIR, 'summary.txt'), 'w') as f:
        f.write('\n\n'.join(summary))
    print(f'✅ {N_SHOTS} скринов в {OUT_DIR}/ (shot_NN.png) + summary.txt')
    print('   Открой картинки и сверь нарисованную цену с price тиков по Δ.')

    await manager.close()
    await database.close()


if __name__ == '__main__':
    asyncio.run(main())
