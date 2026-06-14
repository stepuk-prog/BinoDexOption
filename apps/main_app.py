import asyncio
import random
from typing import TYPE_CHECKING

from apps.app import exit_main, screenshot, find_point, find_option_data, check_cookies_price
from apps.my_exeptions import send_photo_safe
from apps.otc_app import (parce_otc, screenshot_otc, reload_otc_page, select_otc_pair,
                          _ui_loaded, UI_DEAD_CONFIRM)
from logs import init_logger
from messages.message import (first_message, second_message, dogon_message, third_message, prepare_dogon_message,
                              dop_dogon_message, minus_dogon_message)
from settings.config import option_data, binary, overlap, overlap_random, screenshot_path
from settings.timing import BETWEEN_MESSAGES_DELAY, POST_SCREENSHOT_DELAY
from settings.image_paths import DOGON_IMAGES, NEW_FORECAST_IMAGES

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

used_val = [0]
prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
count_price = 0  # счетчик количества одинаковой цены подряд
# Отправлено ли ПЕРВОЕ сообщение опциона = началась «середина опциона» (после первого, до итога).
# По нему обёртка main() решает: непредвиденный сбой в этом окне → баг-картинка в канал (подписчики
# не должны остаться без итога); до первого сообщения — тихо (пояснять нечего).
_posted = False
logger = init_logger(__name__)

# OTC: binodex периодически (тест-режим) висит БЕЗ единой торговой пары — модалка пар пуста,
# хотя сессия/UI/WS живы. Это не краш: не рестартим процесс сразу, а ждём появления пар.
# Цикл: NO_PAIRS_RELOADS быстрых reload+выбор пары (пауза NO_PAIRS_RELOAD_PAUSE), не помогло —
# длинная пауза NO_PAIRS_LONG_SLEEP и повтор. После NO_PAIRS_MAX_CYCLES пустых циклов сдаёмся
# (рестарт диспетчером — вдруг поможет свежий браузер). Все паузы прерываются stop_event.
NO_PAIRS_RELOADS = 3
NO_PAIRS_RELOAD_PAUSE = 5      # сек между быстрыми reload
NO_PAIRS_LONG_SLEEP = 600      # сек (10 мин) между циклами
NO_PAIRS_MAX_CYCLES = 6        # циклов без пар до рестарта (~1 ч)

# OTC: binodex сам может свалиться на сплеш В ТЕЧЕНИЕ опциона (новая версия/переинициализация
# Privy — без нашего reload, см. memory binodex-stuck-splash). Чтобы опцион не прерывался, за
# HEALTH_LEAD сек до КАЖДОЙ фиксации результата (итог И каждый шаг догона) проверяем живость UI
# и при сплеше поднимаем reload+переселект ТОЙ ЖЕ пары — результат снимется с опозданием, а не
# потеряется. Лид прячется в хвосте ожидания экспирации, поэтому в норме задержки нет.
HEALTH_LEAD = 15              # сек до фиксации результата — упреждающая проверка/восстановление UI


async def _try_send(photo, caption, mes_type: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Отправка поста с обработкой обрыва связи и таймаутом — тонкая обёртка над единым
    send_photo_safe (клиент берётся из get_app()-синглтона внутри send_photo_safe). Возврат (ok, err)."""
    return await send_photo_safe(photo, caption, mes_type, timeout)


async def _sleep_or_stop(stop_event, seconds: float):
    """Прерываемый сон: вернётся по таймауту ИЛИ при выставленном stop_event
    (SIGTERM/SIGINT). Иначе долгий sleep(option_time/dgn_time) блокировал бы
    graceful-shutdown на минуты (риск SIGKILL и недозакрытия БД/браузера)."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _ensure_otc_alive(manager: "BrowserManager", stop_event):
    """OTC: перед фиксацией результата СНАЧАЛА дёшево проверить, жив ли UI — видна ли кнопка
    настроек аккаунта (точный маркер «не сплеш»). Видна → ничего не делаем, БЕЗ reload. И только
    если пропала (binodex сам свалился на сплеш в течение опциона, не наш reload) — поднять reload
    (он ретраит сплеш) и ВЕРНУТЬ ТУ ЖЕ пару (reload сбрасывает выбор пары). Best-effort: не вышло —
    результат снимется как раньше с ошибкой → exit_main. FIN не трогаем. SIGTERM пропускаем."""
    if binary or stop_event.is_set():
        return
    page = manager.pages['main']
    if await _ui_loaded(page, UI_DEAD_CONFIRM):   # кнопка настроек на месте → UI жив, reload не нужен
        return
    logger.cookies('OTC: кнопка настроек пропала в течение опциона (сплеш) — reload+переселект, '
                   'не прерывая опцион')
    if not await reload_otc_page(manager=manager):
        logger.warning('OTC: reload в течение опциона не поднял UI — результат может не сняться')
        return
    # reload сбрасывает выбранную пару → возвращаем ту же. option_data.name = '<pair> OTC',
    # select_otc_pair ждёт голую пару (сам добавит ' OTC' для проверки WS-котировки).
    bare = option_data.name[:-4] if option_data.name.endswith(' OTC') else option_data.name
    if not await select_otc_pair(page, bare):
        logger.warning(f'OTC: не вернул пару {bare} после reload в течение опциона — результат под вопросом')


async def _wait_result(manager: "BrowserManager", stop_event, seconds: float):
    """Дождаться экспирации перед фиксацией результата, но за HEALTH_LEAD сек до конца проверить
    живость OTC-UI и при сплеше восстановить (reload+переселект). Лид прячется в хвосте ожидания —
    в норме (UI жив, проверка ~мгновенна) задержки нет; при сплеше результат снимется с опозданием,
    но опцион не прервётся. stop_event прерывает паузы (после вызова проверять stop_event.is_set())."""
    lead = min(float(HEALTH_LEAD), seconds) if not binary else 0.0
    await _sleep_or_stop(stop_event, seconds - lead)
    if lead and not stop_event.is_set():
        await _ensure_otc_alive(manager, stop_event)
        await _sleep_or_stop(stop_event, lead)


async def _capture(manager: "BrowserManager", qr, *, seek_point: bool):
    """Снять скрин текущего окна (единый код вместо 4 дублей if binary/else).
    FIN — окно price/main + опциональный поиск точки входа (seek_point); OTC — окно main.
    :return: кортеж (ok, price|error) от screenshot/screenshot_otc."""
    if binary:
        if seek_point:
            fp_ok, fp_err = await find_point(manager, option_data.resume)
            if not fp_ok:
                logger.warning("find_point не нашёл точку входа (%s) — продолжаю по текущей цене", fp_err)
        return await screenshot(manager=manager, take_shot=True, qr=qr)
    page = manager.pages['main']
    return await screenshot_otc(page=page, asset=option_data.name, qr=qr)


async def _acquire_otc_pair(manager: "BrowserManager", stop_event) -> str:
    """Подобрать OTC-пару с устойчивостью к тест-режиму binodex (периодически пар нет вовсе).
    Логика — см. константы NO_PAIRS_* выше. Развилки исходов:
      'ok'            — пара выбрана, можно работать дальше;
      'reload_failed' — reload не поднял UI (новая версия/сплеш/редирект) → отдаём штатному
                        otc_session_dead (пересоздание браузера/авто-рефреш кук), НЕ ждём пары;
      'no_pairs'      — пар нет дольше ~часа → рестарт (свежий браузер);
      'stopped'       — пришёл сигнал остановки (SIGTERM/SIGINT) во время пауз.
    """
    for cycle in range(1, NO_PAIRS_MAX_CYCLES + 1):
        for _ in range(NO_PAIRS_RELOADS):
            if stop_event.is_set():
                return 'stopped'
            if not await reload_otc_page(manager=manager):
                return 'reload_failed'   # сессия/сплеш — не «нет пар», лечит otc_session_dead
            if await parce_otc(manager=manager, log_data=option_data, valute=used_val):
                return 'ok'
            await _sleep_or_stop(stop_event, NO_PAIRS_RELOAD_PAUSE)
        if stop_event.is_set():
            return 'stopped'
        logger.report(f'OTC: на binodex нет торговых пар — сплю {NO_PAIRS_LONG_SLEEP // 60} мин '
                      f'(цикл {cycle}/{NO_PAIRS_MAX_CYCLES})')
        await _sleep_or_stop(stop_event, NO_PAIRS_LONG_SLEEP)
        if stop_event.is_set():
            return 'stopped'
    return 'no_pairs'


async def main(manager: "BrowserManager", qr, stop_event):
    """Тонкая обёртка над _run_option: ловит НЕПРЕДВИДЕННОЕ исключение середины опциона (после
    первого сообщения, до итогового) и шлёт баг-картинку в канал (channel_mess по флагу _posted),
    а не молчаливый краш/рестарт без пояснения подписчикам. Явные сбои покрыты в _run_option."""
    global _posted
    _posted = False
    try:
        return await _run_option(manager, qr, stop_event)
    except (Exception,) as error:
        logger.error(f'Непредвиденная ошибка в опционе: {error}')
        return await exit_main(channel_mess=_posted, result=False,
                               bug_text=f'Непредвиденная ошибка - {error}', check_cookies=count_price)


async def _run_option(manager: "BrowserManager", qr, stop_event):
    global used_val, prev_price, count_price, _posted
    prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
    count_price = 0  # счетчик количества одинаковой цены подряд

    logger.info("🔄 Начало main(), binary=%s", binary)
    logger.info("📑 Доступные страницы: %s", list(manager.pages.keys()))

    if binary:
        logger.info("🔍 Вызов find_option_data...")
        await find_option_data(manager=manager, log_data=option_data, used_val=used_val)
        logger.info("✅ find_option_data завершён")
        logger.info("📸 Вызов screenshot(screen=None)...")
        screen_shot = await screenshot(manager=manager, take_shot=False, qr=qr)
        logger.info("✅ screenshot завершён: %s", screen_shot[0])
    else:
        # Перед каждым опционом перезагружаем страницу binodex и подбираем пару. binodex
        # периодически (тест-режим) висит без единой пары — это НЕ краш: _acquire_otc_pair
        # переждёт (см. константы NO_PAIRS_*), а не уронит процесс в рестарт-петлю.
        outcome = await _acquire_otc_pair(manager, stop_event)
        if outcome == 'stopped':  # SIGTERM во время ожидания пар — выходим без рестарта
            return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)
        if outcome == 'reload_failed':  # новая версия/сплеш/редирект → otc_session_dead пересоздаст браузер
            return await exit_main(channel_mess=False, result=False, fall=False,
                                   bug_text='binodex не поднялся после reload (новая версия/сплеш)',
                                   check_cookies=count_price)
        if outcome == 'no_pairs':  # пар нет ~час → рестарт диспетчером (свежий браузер)
            return await exit_main(channel_mess=False, result=False,
                                   bug_text='На binodex нет торговых пар дольше часа (тест-режим)',
                                   check_cookies=count_price)
        page = manager.pages['main']
        screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

    if not screen_shot[0]:
        # OTC: первый кадр не снялся (нет цены графика / пустой канвас, частый транзиент тест-режима
        # binodex) — НЕ рестартим процесс. fall=False → возврат в главный цикл, где штатная браузер-
        # фри ветка (main.py: feed_alive → _await_binodex_feed) переждёт аутэйдж без релогина и спама;
        # при живом фиде — просто повтор следующего цикла с новой парой. FIN: браузер-фри ожидания
        # нет, поэтому там кадр-сбой по-прежнему уводит в рестарт (fall=True).
        return await exit_main(channel_mess=False, result=False, fall=bool(binary),
                               bug_text=f'Ошибка проверки скриншота - {screen_shot[1]}', check_cookies=count_price)

    message_text = first_message()
    new_prognoz_img = random.choice(NEW_FORECAST_IMAGES)  # рандомно из 3 в pictures/new_prognoz/
    ok, err = await _try_send(new_prognoz_img, message_text, 'первое сообщение', timeout=30.0)
    if not ok:
        return await exit_main(channel_mess=False, result=False, bug_text=err, check_cookies=count_price)
    _posted = True   # первое сообщение ушло → «середина опциона»: непредвиденный сбой ниже = баг-картинка

    used_val.append(option_data.id_val)
    if len(used_val) >= 4:  # держим последние 3 id → актив не повторяется в окне из 4 рынков подряд
        del used_val[0]

    await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

    screen_shot = await _capture(manager, qr, seek_point=True)

    if not screen_shot[0]:
        return await exit_main(channel_mess=True, result=False,
                               bug_text=f'Ошибка снятия скриншота для поста с тех. анализом - {screen_shot[1]}',
                               check_cookies=count_price)

    option_data.price = round(screen_shot[1], option_data.round)
    if not binary:  # Проверка на отвал cookies
        prev_price = option_data.price

    option_data.set_option_time()  # FIN/OTC 3m/5m: рандомное время экспирации + синхронизация name_tf
    option_data.levels()
    message_text = second_message()

    ok, err = await _try_send(screenshot_path, message_text, 'второе сообщение')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

    await _wait_result(manager, stop_event, option_data.option_time)
    if stop_event.is_set():  # SIGTERM во время ожидания экспирации — выходим без постов
        return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

    screen_shot = await _capture(manager, qr, seek_point=False)

    if screen_shot[0]:
        option_data.itg_price = round(screen_shot[1], option_data.round)
    else:
        return await exit_main(channel_mess=True, result=False,
                               bug_text=f'Ошибка снятия скриншота для итогового поста - {screen_shot[1]}',
                               check_cookies=count_price)

    option_data.comparing_lists()

    if not binary:  # Проверка на отвал cookies
        count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                      new_price=option_data.itg_price,
                                                      round_par=option_data.round,
                                                      count=count_price)

    if not option_data.dgn:  # если опцион закончился без догона
        message_text = third_message()
        ok, err = await _try_send(screenshot_path, message_text, 'итоговое сообщение')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
        return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    # Перетасованный список картинок для перекрытий — без повторов в рамках одного прогноза.
    dogon_pool = random.sample(DOGON_IMAGES, len(DOGON_IMAGES))

    for index in range(min(overlap + 1, len(option_data.dogon_par))):
        dogon = option_data.dogon_par[index]
        option_data.dogon_settings(dogon_par=dogon)
        if index + 1 > overlap - overlap_random:
            option_data.random_dogon()

        text_message = prepare_dogon_message(idx=index)
        ok, err = await _try_send(screenshot_path, text_message, 'первое сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

        img = dogon_pool[index % len(dogon_pool)]
        text_message = dop_dogon_message()
        ok, err = await _try_send(img, text_message, 'доп. сообщение догона')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(POST_SCREENSHOT_DELAY)

        screen_shot = await _capture(manager, qr, seek_point=True)

        if not screen_shot[0]:
            return await exit_main(channel_mess=True, result=False,
                                   bug_text=f'Ошибка снятия скриншота для поста с догоном - {screen_shot[1]}',
                                   check_cookies=count_price)

        option_data.price = round(screen_shot[1], option_data.round)
        if not binary:  # Проверка на отвал cookies
            count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                          new_price=option_data.price,
                                                          round_par=option_data.round,
                                                          count=count_price)

        text_message = dogon_message()
        ok, err = await _try_send(screenshot_path, text_message, 'Сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await _wait_result(manager, stop_event, option_data.dgn_time)
        if stop_event.is_set():  # SIGTERM во время ожидания итога догона — выходим без постов
            return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

        screen_shot = await _capture(manager, qr, seek_point=False)

        if not screen_shot[0]:
            return await exit_main(channel_mess=True, result=False,
                                   bug_text=f'Ошибка снятия скриншота для итога догона - {screen_shot[1]}',
                                   check_cookies=count_price)

        option_data.itg_price = round(screen_shot[1], option_data.round)
        if not binary:  # Проверка на отвал cookies
            count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                          new_price=option_data.itg_price,
                                                          round_par=option_data.round,
                                                          count=count_price)

        if option_data.comparing_lists_dogon():
            text_message = third_message()
            ok, err = await _try_send(screenshot_path, text_message, 'итоговое сообщение')
            if not ok:
                return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
            return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    option_data.minus = True
    option_data.plus = False
    text_message = minus_dogon_message()
    ok, err = await _try_send(screenshot_path, text_message, 'итоговое сообщение по последнему догону с минусом')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
    return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
