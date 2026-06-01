"""Дамп СЫРЫХ WS-фреймов binodex (TIMEFRAME=1m, BINARY=0) — диагностика рассинхрона
цена-из-WS ↔ цена-на-скрине.

Ловит все WebSocket'ы страницы и все framereceived. Группирует Socket.IO-фреймы по
event-namespace (часть до запятой, напр. '42/graphic'), считает их, собирает множество
символов и по одному ПОЛНОМУ сэмплу на каждый namespace — чтобы увидеть, есть ли в потоке
свечной фид и серверный timestamp, который можно привязать к моменту скрина.

Ничего не постит и не пишет в БД. Запуск:
    TIMEFRAME=1m BINARY=0 PYTHONPATH=. .venv/bin/python scripts/probe_ws_dump.py
"""
import asyncio
import json
import os
import time
from collections import Counter, defaultdict

os.environ['TIMEFRAME'] = '1m'
os.environ['BINARY'] = '0'

import settings.browser_set as bset
bset.browser_launch_options['headless'] = True  # без окна — только сбор фреймов

import logs.log_init as li
li.TelegramBotHandler.emit = lambda self, record: None

from settings.config import database
from apps.browser_app import init_browser
from apps.otc_app import init_otc, select_otc_pair, get_price_tracker

CAPTURE_SECONDS = 20

frame_counts: Counter = Counter()
sample_by_ns: dict[str, str] = {}
symbols_by_ns: dict[str, set] = defaultdict(set)
ws_urls: set = set()
ns_by_url: dict[str, set] = defaultdict(set)   # какой url какие namespace отдаёт
sent_by_url: dict[str, list] = defaultdict(list)  # исходящие (subscribe) по url


def short_url(u: str) -> str:
    return u.split('://', 1)[-1].split('?', 1)[0]


def record_sent(url, payload):
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode('utf-8')
        except Exception:
            return
    s = str(payload)[:300]
    lst = sent_by_url[short_url(url)]
    if s not in lst:
        lst.append(s)


def record_frame(payload, url=''):
    su = short_url(url)
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode('utf-8')
        except Exception:
            frame_counts['<binary-undecodable>'] += 1
            return
    payload = str(payload)
    i = payload.find(',[')
    if i == -1:
        # не socketio-data фрейм (ping/handshake и т.п.) — учтём по префиксу
        frame_counts[f'<non-data:{payload[:3]!r}>'] += 1
        return
    ns = payload[:i]                       # напр. '42/graphic'
    frame_counts[ns] += 1
    ns_by_url[su].add(ns)
    if ns not in sample_by_ns:
        sample_by_ns[ns] = payload[:1500]  # первый полный сэмпл (обрезан для читаемости)
    try:
        arr = json.loads(payload[i + 1:])
        data = arr[1] if isinstance(arr, list) and len(arr) > 1 else None
        if isinstance(data, dict) and 'symbol' in data:
            symbols_by_ns[ns].add(data['symbol'])
    except Exception:
        pass


async def main():
    await database.connect()
    rows = await database.option_data_pocket(tf='1m', exclude_ids=[])
    pairs = [r['name_val'] for r in (rows or [])[:3]]
    print('Кандидаты пар:', pairs)

    res = await init_browser()
    if not res.success:
        print('❌ init_browser:', res.manager_or_error); return
    manager = res.manager
    page = manager.pages['main']

    # Свой перехват ВСЕХ ws (не только котировочного), до навигации
    def on_ws(ws):
        ws_urls.add(ws.url)
        u = ws.url
        ws.on('framereceived', lambda d: record_frame(getattr(d, 'payload', d), u))
        ws.on('framesent', lambda d: record_sent(u, getattr(d, 'payload', d)))
    page.on('websocket', on_ws)

    ok = await init_otc(manager)
    print('init_otc:', ok)
    if not ok:
        await manager.close(); await database.close(); return

    # Выберем пару, чтобы поток наверняка содержал нашу OTC-котировку
    if pairs:
        sel = await select_otc_pair(page, pairs[0])
        print(f'select_otc_pair({pairs[0]}):', sel)

    print(f'\n⏳ собираю фреймы {CAPTURE_SECONDS}с...')
    await asyncio.sleep(CAPTURE_SECONDS)

    print('\n===== WS URLs =====')
    for u in sorted(ws_urls):
        print(' ', u)

    print('\n===== Namespaces по URL =====')
    for su, nss in ns_by_url.items():
        print(f'  {su}: {sorted(nss)}')

    print('\n===== Исходящие (subscribe) фреймы по URL =====')
    for su, msgs in sent_by_url.items():
        print(f'  --- {su} ---')
        for m in msgs:
            print(f'    {m}')

    print('\n===== Namespaces (event) и счётчики =====')
    for ns, n in frame_counts.most_common():
        syms = symbols_by_ns.get(ns)
        syms_s = f'  symbols={sorted(syms)[:6]}' if syms else ''
        print(f'  {ns:30s} x{n}{syms_s}')

    print('\n===== Полные сэмплы по namespace =====')
    for ns, sample in sample_by_ns.items():
        print(f'\n--- {ns} ---')
        print(sample)

    await manager.close()
    await database.close()
    print('\n✅ dump завершён')


if __name__ == '__main__':
    asyncio.run(main())
