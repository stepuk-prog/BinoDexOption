"""Сверка МЕДИАНЫ нескольких чтений window.chartData.price с кадром графика binodex OTC.

Цель: проверить, что медиана N быстрых чтений chartData вокруг screenshot() отсекает
анимационный выброс (см. docs/BINODEX_PRICE.md §7, история — probe_chartdata_match) и
даёт стабильное совпадение с нарисованным ярлыком.

Для каждого кадра:
  • несколько чтений chartData.price ВПЛОТНУЮ до screenshot,
  • screenshot (t_shot фиксируется перед ним),
  • несколько чтений chartData.price сразу после,
  • медиана всех чтений → round(decimals). Для сравнения печатается и WS (текущий прод).
Скрины — shot_NN.png; открой и сверь нарисованный ярлык с round(median) ниже.

Ничего не постит и не пишет в БД. Запуск:
    TIMEFRAME=1m BINARY=0 PYTHONPATH=. .venv/bin/python scripts/probe_chartdata_median.py
"""
import asyncio
import os
import statistics
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
from apps.otc_app import init_otc, select_otc_pair, get_price_tracker

N_SHOTS = 10
SHOT_GAP = 2.0
READS_BEFORE = 3      # быстрых чтений chartData до кадра
READS_AFTER = 3       # и после кадра (всего медиана по READS_BEFORE+READS_AFTER)
OUT_DIR = 'pictures/chartdata_median_test'   # gitignored (pictures/)

JS_PRICE = "() => (window.chartData && typeof window.chartData.price === 'number') ? window.chartData.price : null"


async def read_price(page):
    try:
        return await page.evaluate(JS_PRICE)
    except Exception:
        return None


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    await database.connect()
    rows = await database.option_data_pocket(tf='1m', exclude_ids=[])
    rows = rows or []
    if not rows:
        print('❌ нет активных OTC-пар'); await database.close(); return
    by_name = {r['name_val']: r for r in rows}

    res = await init_browser()
    if not res.success:
        print('❌ init_browser:', res.manager_or_error); await database.close(); return
    manager = res.manager
    page = manager.pages['main']

    if not await init_otc(manager):
        print('❌ init_otc'); await manager.close(); await database.close(); return
    tracker = get_price_tracker()

    chosen = None
    for name in by_name:
        if await select_otc_pair(page, name):
            chosen = name
            break
    if not chosen:
        print('❌ ни одна пара не прогрузилась'); await manager.close(); await database.close(); return

    target_asset = chosen + ' OTC'
    decimals = by_name[chosen].get('round')
    if decimals is None:
        decimals = 5
        print(f'⚠ нет колонки round у {chosen}, беру {decimals}')
    print(f'Пара: {chosen}  (round={decimals}, чтений на кадр={READS_BEFORE}+{READS_AFTER})\n')

    element = page.locator(screen_zone_otc).first
    await element.wait_for(state='visible', timeout=10000)

    table = []
    for n in range(1, N_SHOTS + 1):
        await asyncio.sleep(SHOT_GAP)
        reads = []
        for _ in range(READS_BEFORE):
            v = await read_price(page)
            if v is not None:
                reads.append(v)
        t_shot = time.time()
        shot_file = os.path.join(OUT_DIR, f'shot_{n:02d}.png')
        await element.screenshot(path=shot_file)
        for _ in range(READS_AFTER):
            v = await read_price(page)
            if v is not None:
                reads.append(v)
        ws_price = tracker.get_price_at(target_asset, t_shot)

        med = statistics.median(reads) if reads else None
        r_med = round(med, decimals) if med is not None else None
        r_ws = round(ws_price, decimals) if ws_price is not None else None
        table.append((n, r_med, r_ws))
        spread = (max(reads) - min(reads)) if reads else 0
        print(f'shot_{n:02d}: median={r_med}  (WS={r_ws})  '
              f'[{len(reads)} чтений, разброс {spread*10**decimals:.1f} в посл. знаке]')

    print(f'\nСкрины в {OUT_DIR}/ (shot_NN.png). Сверь ярлык с median:')
    print('  ' + '  '.join(f'{n}:{r_med}' for (n, r_med, _r_ws) in table))

    await manager.close()
    await database.close()


if __name__ == '__main__':
    asyncio.run(main())
