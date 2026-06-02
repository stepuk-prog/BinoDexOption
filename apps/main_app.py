import asyncio
import random
from typing import TYPE_CHECKING

from apps.app import exit_main, screenshot, find_point, find_option_data, check_cookies_price
from apps.my_exeptions import send_photo_safe
from apps.otc_app import parce_otc, screenshot_otc
from logs import init_logger
from messages.message import (first_message, second_message, dogon_message, third_message, prepare_dogon_message,
                              dop_dogon_message, minus_dogon_message)
from settings.config import option_data, get_app, binary, overlap, overlap_random, screenshot_path
from settings.timing import BETWEEN_MESSAGES_DELAY, POST_SCREENSHOT_DELAY
from settings.image_paths import DOGON_IMAGES, NEW_FORECAST_IMAGES

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

used_val = [0]
prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
count_price = 0  # счетчик количества одинаковой цены подряд
logger = init_logger(__name__)


async def _try_send(bot, photo, caption, mes_type: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Отправка поста с обработкой обрыва связи и таймаутом — тонкая обёртка над единым
    send_photo_safe (bot не нужен: клиент берётся из get_app()-синглтона). Возврат (ok, err)."""
    return await send_photo_safe(photo, caption, mes_type, timeout)


async def _sleep_or_stop(stop_event, seconds: float):
    """Прерываемый сон: вернётся по таймауту ИЛИ при выставленном stop_event
    (SIGTERM/SIGINT). Иначе долгий sleep(option_time/dgn_time) блокировал бы
    graceful-shutdown на минуты (риск SIGKILL и недозакрытия БД/браузера)."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def main(manager: "BrowserManager", qr, stop_event):
    global used_val, prev_price, count_price
    prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
    count_price = 0  # счетчик количества одинаковой цены подряд
    bot = get_app()  # Pyrogram Client

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
        result = await parce_otc(manager=manager, log_data=option_data, valute=used_val)
        if not result:
            return await exit_main(channel_mess=False, result=False,
                                   bug_text='Ошибка загрузки валюты на график', check_cookies=count_price)
        page = manager.pages['main']
        screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

    if not screen_shot[0]:
        return await exit_main(channel_mess=False, result=False,
                               bug_text=f'Ошибка проверки скриншота - {screen_shot[1]}', check_cookies=count_price)

    message_text = first_message()
    new_prognoz_img = random.choice(NEW_FORECAST_IMAGES)  # рандомно из 3 в pictures/new_prognoz/
    ok, err = await _try_send(bot, new_prognoz_img, message_text, 'первое сообщение', timeout=30.0)
    if not ok:
        return await exit_main(channel_mess=False, result=False, bug_text=err, check_cookies=count_price)

    used_val.append(option_data.id_val)
    if len(used_val) >= 4:  # держим последние 3 id → актив не повторяется в окне из 4 рынков подряд
        del used_val[0]

    await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

    if binary:
        fp_ok, fp_err = await find_point(manager, option_data.resume)
        if not fp_ok:
            logger.warning("find_point не нашёл точку входа (%s) — продолжаю по текущей цене", fp_err)
        screen_shot = await screenshot(manager=manager, take_shot=True, qr=qr)
    else:
        page = manager.pages['main']
        screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

    if not screen_shot[0]:
        return await exit_main(channel_mess=True, result=False,
                               bug_text=f'Ошибка снятия скриншота для поста с тех. анализом - {screen_shot[1]}',
                               check_cookies=count_price)

    option_data.price = round(screen_shot[1], option_data.round)
    if not binary:  # Проверка на отвал cookies
        prev_price = option_data.price

    option_data.set_option_time()  # FIN 3m/5m: рандомное время экспирации + синхронизация name_tf
    option_data.levels()
    message_text = second_message()

    ok, err = await _try_send(bot, screenshot_path, message_text, 'второе сообщение')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

    await _sleep_or_stop(stop_event, option_data.option_time)
    if stop_event.is_set():  # SIGTERM во время ожидания экспирации — выходим без постов
        return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

    if binary:
        screen_shot = await screenshot(manager=manager, take_shot=True, qr=qr)
    else:
        page = manager.pages['main']
        screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

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
        ok, err = await _try_send(bot, screenshot_path, message_text, 'итоговое сообщение')
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
        ok, err = await _try_send(bot, screenshot_path, text_message, 'первое сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

        img = dogon_pool[index % len(dogon_pool)]
        text_message = dop_dogon_message()
        ok, err = await _try_send(bot, img, text_message, 'доп. сообщение догона')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(POST_SCREENSHOT_DELAY)

        if binary:
            fp_ok, fp_err = await find_point(manager=manager, resume=option_data.resume)
            if not fp_ok:
                logger.warning("find_point (догон) не нашёл точку входа (%s) — продолжаю", fp_err)
            screen_shot = await screenshot(manager=manager, take_shot=True, qr=qr)
        else:
            page = manager.pages['main']
            screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

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
        ok, err = await _try_send(bot, screenshot_path, text_message, 'Сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await _sleep_or_stop(stop_event, option_data.dgn_time)
        if stop_event.is_set():  # SIGTERM во время ожидания итога догона — выходим без постов
            return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

        if binary:
            screen_shot = await screenshot(manager=manager, take_shot=True, qr=qr)
        else:
            page = manager.pages['main']
            screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

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
            ok, err = await _try_send(bot, screenshot_path, text_message, 'итоговое сообщение')
            if not ok:
                return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
            return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    option_data.minus = True
    option_data.plus = False
    text_message = minus_dogon_message()
    ok, err = await _try_send(bot, screenshot_path, text_message, 'итоговое сообщение по последнему догону с минусом')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
    return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
