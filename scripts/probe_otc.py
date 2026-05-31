"""Диагностический прогон OTC-флоу на живом binodex (TIMEFRAME=1m, BINARY=0).

Видимый браузер. Замеряет, через сколько реально готов каждый шаг выбора пары —
чтобы заменить слепые asyncio.sleep на ожидание конкретного состояния (auto-wait).
Телеграм-отправка логгера отключена (не спамим каналы). Ничего не постит, не пишет в БД.

Запуск:  TIMEFRAME=1m BINARY=0 .venv/bin/python scripts/probe_otc.py
"""
import asyncio
import os
import time

os.environ['TIMEFRAME'] = '1m'
os.environ['BINARY'] = '0'

# Видимый браузер (дефолт в browser_set — headless=True). Меняем ДО импорта browser_app.
import settings.browser_set as bset
bset.browser_launch_options['headless'] = False

# Глушим Telegram-хендлер логгера, чтобы прогон ничего не отправил в каналы.
import logs.log_init as li
li.TelegramBotHandler.emit = lambda self, record: None

from settings.config import database, screenshot_path
from apps.browser_app import init_browser
from apps.otc_app import (init_otc, get_price_tracker, select_otc_pair,
                          screenshot_otc, get_price, _pair_modal_open)
from apps.app import get_water


def now():
    return time.monotonic()


async def main():
    await database.connect()
    rows = await database.option_data_pocket(tf='1m', exclude_ids=[])
    if not rows:
        print('❌ нет активных OTC-пар в БД'); return
    pairs = [r['name_val'] for r in rows[:6]]
    print('Кандидаты пар из БД:', pairs)

    water = get_water()  # QR-оверлеи (как в проде)
    qr = water[1] if water[0] else None
    print('QR загружены:', bool(qr))

    res = await init_browser()
    if not res.success:
        print('❌ init_browser:', res.manager_or_error); return
    manager = res.manager
    page = manager.pages['main']

    ok = await init_otc(manager)
    print('init_otc (логин binodex):', ok)
    if not ok:
        await manager.close(); return
    tracker = get_price_tracker()

    for pair in pairs:
        print(f'\n==== ПАРА {pair} ====')
        target = pair + ' OTC'
        # 1) Реальный выбор пары (после замены sleep→auto-wait)
        t0 = now()
        ok = await select_otc_pair(page, pair)
        dt = now() - t0
        print(f'  select_otc_pair: {ok}  за {dt:.2f}s, модалка_открыта={await _pair_modal_open(page)}, '
              f'WS-цена={tracker.get_price(target)}')
        if not ok:
            continue
        # 2) Реальный get_price (прод-функция)
        gp = await get_price(asset=target)
        # 3) Реальный screenshot_otc: canvas-зона + цена из WS + QR-оверлей + сохранение
        t0 = now()
        shot_ok, shot_val, shot_file = await screenshot_otc(page, asset=target, qr=qr)
        dt = now() - t0
        size = os.path.getsize(shot_file) if shot_file and os.path.exists(shot_file) else 0
        print(f'  get_price: {gp}')
        print(f'  screenshot_otc: {shot_ok}  за {dt:.2f}s, цена={shot_val}, файл={shot_file or "—"} ({size} байт)')

    print(f'\nИтоговый скрин лежит в {screenshot_path}')
    print('⏳ держу браузер открытым 5с для наблюдения...')
    await asyncio.sleep(5)
    await manager.close()
    await database.close()
    print('✅ probe завершён')


if __name__ == '__main__':
    asyncio.run(main())