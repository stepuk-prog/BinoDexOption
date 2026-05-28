import asyncio
import random
from typing import TYPE_CHECKING

from apps.app import exit_main, screenshot, find_point, find_option_data, check_cookies_price
from apps.my_exeptions import lost_connection_photo
from apps.otc_app import parce_otc, screenshot_otc
from logs import init_logger
from messages.message import (first_message, second_message, dogon_message, third_message, prepare_dogon_message,
                              dop_dogon_message, minus_dogon_message)
from settings.config import channel_id, option_data, get_app, binary, overlap, overlap_random, screenshot_path
from settings.timing import BETWEEN_MESSAGES_DELAY, POST_SCREENSHOT_DELAY
from settings.image_paths import DOGON_IMAGES, NEW_FORECAST_IMAGES

if TYPE_CHECKING:
    from apps.browser_app import BrowserManager

used_val = [0]
prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
count_price = 0  # счетчик количества одинаковой цены подряд
logger = init_logger(__name__)


async def main(manager: "BrowserManager", qr):
    global used_val, prev_price, count_price
    prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
    count_price = 0  # счетчик количества одинаковой цены подряд
    bot = get_app()  # Pyrogram Client

    logger.info("🔄 Начало main(), binary=%s", binary)
    logger.info("📑 Доступные страницы: %s", list(manager.pages.keys()))

    if binary:
        pic_str = ''
        logger.info("🔍 Вызов find_option_data...")
        await find_option_data(manager=manager, log_data=option_data, used_val=used_val)
        logger.info("✅ find_option_data завершён")
        logger.info("📸 Вызов screenshot(screen=None)...")
        screen_shot = await screenshot(manager=manager, screen=None, qr=qr)
        logger.info("✅ screenshot завершён: %s", screen_shot[0])
    else:
        pic_str = '_otc'
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
    try:
        await asyncio.wait_for(
            bot.send_photo(chat_id=channel_id, photo=new_prognoz_img, caption=message_text),
            timeout=30.0
        )
        logger.info("✅ Первое сообщение отправлено")
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут отправки сообщения (30 сек)")
        return await exit_main(channel_mess=False, result=False, bug_text='Таймаут Pyrogram', check_cookies=count_price)
    except (Exception,) as error:
        logger.error("❌ Ошибка отправки: %s", error)
        bug_fix = await lost_connection_photo(error=error, photo=new_prognoz_img, text=message_text,
                                              mes_type='первое сообщение')
        if not bug_fix[0]:
            return await exit_main(channel_mess=False, result=False, bug_text=bug_fix[1], check_cookies=count_price)

    used_val.append(option_data.id_val)
    if len(used_val) >= 5:
        del used_val[0]

    await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

    if binary:
        await find_point(manager, option_data.resume)
        screen_shot = await screenshot(manager=manager, screen='main', qr=qr)
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

    option_data.levels()
    message_text = second_message()

    try:
        await bot.send_photo(chat_id=channel_id, photo=screenshot_path, caption=message_text)
    except (Exception,) as error:
        bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=message_text,
                                              mes_type='второе сообщение')
        if not bug_fix[0]:
            return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)

    await asyncio.sleep(option_data.option_time)

    if binary:
        screen_shot = await screenshot(manager=manager, screen='itog', qr=qr)
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
        try:
            await bot.send_photo(channel_id, screenshot_path, caption=message_text)
            return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
        except (Exception,) as error:
            bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=message_text,
                                                  mes_type='итоговое сообщение')
            if not bug_fix[0]:
                return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)
            else:
                return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    # Перетасованный список картинок для перекрытий — без повторов в рамках одного прогноза.
    dogon_pool = random.sample(DOGON_IMAGES, len(DOGON_IMAGES))

    for index in range(overlap + 1):
        dogon = option_data.dogon_par[index]
        option_data.dogon_settings(dogon_par=dogon)
        if index + 1 > overlap - overlap_random:
            option_data.random_dogon()

        text_message = prepare_dogon_message(idx=index)
        try:
            await bot.send_photo(chat_id=channel_id, photo=screenshot_path, caption=text_message)
        except (Exception,) as error:
            bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=message_text,
                                                  mes_type='первое сообщение о догоне')
            if not bug_fix[0]:
                return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)

        await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

        img = dogon_pool[index % len(dogon_pool)]
        text_message = dop_dogon_message()

        try:
            await bot.send_photo(chat_id=channel_id, photo=img, caption=text_message)
        except (Exception,) as error:
            bug_fix = await lost_connection_photo(error=error, photo=img, text=message_text,
                                                  mes_type='доп. сообщение догона')
            if not bug_fix[0]:
                return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)

        await asyncio.sleep(POST_SCREENSHOT_DELAY)

        if binary:
            await find_point(manager=manager, resume=option_data.resume)
            screen_shot = await screenshot(manager=manager, screen='dogon', qr=qr)
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
        try:
            await bot.send_photo(chat_id=channel_id, photo=screenshot_path, caption=text_message)
        except (Exception,) as error:
            bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=message_text,
                                                  mes_type='Сообщение о догоне')
            if not bug_fix[0]:
                return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)

        await asyncio.sleep(option_data.dgn_time)

        if binary:
            screen_shot = await screenshot(manager=manager, screen='itog', qr=qr)
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
            option_data.kol_dogon = index + 1
            text_message = third_message()
            try:
                await bot.send_photo(chat_id=channel_id, photo=screenshot_path, caption=text_message)
                return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
            except (Exception,) as error:
                bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=message_text,
                                                      mes_type='итоговое сообщение')
                if not bug_fix[0]:
                    return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1],
                                           check_cookies=count_price)
                else:
                    return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

        index = index + 1
        if len(option_data.dogon_par) == index:
            break

    option_data.minus = True
    option_data.plus = False
    text_message = minus_dogon_message()
    try:
        await bot.send_photo(chat_id=channel_id, photo=screenshot_path, caption=text_message)
        return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
    except (Exception,) as error:
        bug_fix = await lost_connection_photo(error=error, photo=screenshot_path, text=text_message,
                                              mes_type='итоговое сообщение по последнему догону с минусом')
        if not bug_fix[0]:
            return await exit_main(channel_mess=True, result=False, bug_text=bug_fix[1], check_cookies=count_price)
        else:
            return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
