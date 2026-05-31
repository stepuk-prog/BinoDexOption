"""
Одноразовый скрипт вычитки: шлёт все сообщения С СООТВЕТСТВУЮЩИМИ КАРТИНКАМИ
в форум-тему для проверки вёрстки.

- FIN/OTC дедуплицируются по ТЕКСТУ: совпадает → один пост [ALL]; различается → [FIN]/[OTC].
- Метка (имя функции + вариант) кладётся первой строкой подписи (меньше сообщений → меньше флуда).
- Отправляет юзер-бот OTC 1m (TIMEFRAME=1m, BINARY=0, TEST=0 форсятся до импорта config).
- Картинки: посты с графиком → образец screenshot_1m_otc.png; остальные — свои штатные
  (new_prognoz, dogon, pluses/{N}.png, seria_plus, bug, startweek/endweek).
- sleep_threshold высокий — Pyrogram сам пережидает FLOOD_WAIT.

Запуск:
    .venv/bin/python check_messages.py
"""
import asyncio
import os
import random

os.environ['TIMEFRAME'] = '1m'
os.environ['BINARY'] = '0'
os.environ['TEST'] = '0'

from settings.config import get_app, option_data
from settings.image_paths import NEW_FORECAST_IMAGES, DOGON_IMAGES, PLUS_SERIES_IMAGE, PLUS_IMAGE_DIR
import messages.message as msg
from messages.message import (
    first_message, second_message, third_message, prepare_dogon_message,
    dop_dogon_message, dogon_message, minus_dogon_message, main_bug_message,
    dop_plus10_message, plus_message, weekend_message, start_message,
)

FORUM_ID = -1002073071755
THREAD_ID = 18736
SEND_DELAY = 4.0                              # пауза между постами (анти-флуд)
SCREENSHOT = 'pictures/screenshot_1m_otc.png'  # образец для постов с графиком

SAMPLE = {
    'name_val': 'EUR/USD', 'round': 5, 'val_id': 1,
    'base_emoji': '🇪🇺', 'second_emoji': '🇺🇸',
    'resume': 'Рекомендация на ПОКУПКУ', 'buy': True,
    'move_potential_down': 20, 'move_potential_up': 80,
    'dir_force_down': 40, 'dir_force_up': 60,
    'long_trend_down': 30, 'long_trend_up': 70,
    'volume_profile_down': 35, 'volume_profile_up': 65,
    'average_interest_down': 45, 'average_interest_up': 85,
    'volume_balance_down': 50, 'volume_balance_up': 70,
    'prop_ind': 75, 'kol_ind': 6,
    'price_reversal_down': 20, 'price_reversal_up': 30,
    'potential_change_down': 25, 'potential_change_up': 35,
    'itog_stat_down': 70, 'itog_stat_up': 85,
}


def _prep(binary_flag: bool):
    msg.binary = binary_flag
    option_data.binary = binary_flag
    option_data.add_option_data(SAMPLE)
    option_data.price = 1.23456
    option_data.itg_price = 1.23460
    option_data.support = '1.23400 — 1.23380'
    option_data.resistance = '1.23510 — 1.23530'
    option_data.dogon_settings(dogon_par=2)


def _render(fn):
    _prep(True);  random.seed(42); fin = fn()
    _prep(False); random.seed(42); otc = fn()
    return fin, otc


async def _desc(app, text: str):
    """Отдельное сообщение-описание перед постом."""
    try:
        await app.send_message(chat_id=FORUM_ID, message_thread_id=THREAD_ID, text=text)
    except Exception as e:
        print(f"⚠️ описание: {e}")
    await asyncio.sleep(1.0)


async def _send(app, photo, text: str):
    try:
        await app.send_photo(chat_id=FORUM_ID, message_thread_id=THREAD_ID,
                             photo=photo, caption=text)
    except Exception as e:
        print(f"⚠️ ошибка отправки: {e}")
    await asyncio.sleep(SEND_DELAY)


async def _block(app, desc: str, fn, photo):
    """Описание отдельным сообщением, затем сам пост с картинкой. FIN/OTC дедуп по тексту."""
    fin, otc = _render(fn)
    print(f"→ {desc}")
    if fin == otc:
        await _desc(app, desc)
        await _send(app, photo, fin)
    else:
        await _desc(app, f'{desc} (FIN)')
        await _send(app, photo, fin)
        await _desc(app, f'{desc} (OTC)')
        await _send(app, photo, otc)


async def main():
    app = get_app()
    app.sleep_threshold = 300  # сам пережидать FLOOD_WAIT до 5 мин, а не падать
    async with app:
        for n in (5, 10, 15, 20, 25, 30, 35, 40, 45, 50):
            await _block(app, f'Серия {n} плюсов подряд', lambda n=n: plus_message(n),
                        f'{PLUS_IMAGE_DIR}/{n}.png')

    print('✅ Готово')


if __name__ == '__main__':
    asyncio.run(main())
