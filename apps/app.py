import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page

from apps.browser_app import init_valute_browser
from apps.exit_app import close_program
from apps.my_exeptions import send_photo_safe
from logs import init_logger
from classes.Option_class import Option
from messages import main_bug_message, dop_plus10_message, plus_message
from settings import qr110_x, qr110_y, qr85_x, qr85_y, paste_overlay
from settings.browser_config import move_field, price_field, pop_up, screen_zone
from settings.config import (channel_id, option_data, get_app, binary, program_id,
                            shot_path, screenshot_path, database,
                            main_cycle_pause_min, main_cycle_pause_max)
from settings.constant import qr110_path, qr85_path, bear_color, bull_color, find_time
from settings.timing import CHECK_PLUS_DELAY, POST_SCREENSHOT_DELAY, TG_SEND_TIMEOUT, TIMEOUT_MEDIUM
from settings.image_paths import PLUS_SERIES_IMAGE, PLUS_IMAGE_DIR

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

logger = init_logger(__name__)

# Флаг штатной остановки (SIGTERM/SIGINT): при нём exit_main не шлёт main_bug_message.
_shutdown_requested = False


def request_shutdown():
    """Пометить штатную остановку — подавляет сообщение о сбое (main_bug_message)."""
    global _shutdown_requested
    _shutdown_requested = True


async def _close_popup(page):
    """Best-effort закрытие popup по селектору pop_up (общий код для find_price/screenshot)."""
    try:
        popup = page.locator(f".{pop_up}")
        if await popup.count() > 0:
            await popup.first.click(timeout=3000)
    except (Exception,):
        pass


def get_water():
    """Загрузка QR-оверлеев (qr110, qr85)"""
    try:
        qr110 = Image.open(qr110_path)
        qr85 = Image.open(qr85_path)
        return True, (qr110, qr85)
    except (Exception,) as error:
        logger.error(f'Не могу загрузить QR - {error}')
        return False, None


async def check_plus():
    """Проверка количества плюсов"""
    kol_plus = await database.plus_counter(program_id=program_id)
    if not kol_plus:  # False/None — ошибка пула или нет строки счётчика
        return True, ''
    plus_milestones = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
    count = kol_plus['plus']

    if count in plus_milestones:
        await asyncio.sleep(CHECK_PLUS_DELAY)  # пауза перед постом-вехой (не в каждом плюсовом цикле)
        caption = plus_message(count)
        photo = f'{PLUS_IMAGE_DIR}/{count}.png'
        ok, err = await send_photo_safe(photo, caption, mes_type=f'сообщение {count} плюс')
        if not ok:
            return False, f'Ошибка отправки сообщения {count} плюс! - {err}'
        await asyncio.sleep(POST_SCREENSHOT_DELAY)
        bug_fix = await dop_plus_message()
        if bug_fix[0]:
            return True, ''
        else:
            return False, bug_fix[1]

    return True, ''


async def check_minus():
    """Сброс серии плюсов при минусе — инкремент счётчика минусов в БД."""
    await database.minus_counter(program_id=program_id)
    return True, ''


async def exit_main(channel_mess: bool,
                    result: bool, bug_text='',
                    fall=True,
                    check_cookies: int = 0) -> tuple[bool, bool, bool, str, int]:
    """
    Выход из main
    :param channel_mess: если True - отправлять в канал сообщение
    :param result: если True - опцион удачно завершился
    :param bug_text: текст ошибки
    :param fall: True - критическая ошибка - перезапуск программы
    :param check_cookies: если больше 2 - подозрение на отвал cookies - перезагрузка
    :return: result, plus - если окончился плюсом, fall - перезапуск
    """
    plus = False
    # Штатная остановка (SIGTERM/SIGINT): ничего не шлём в канал и не трогаем счётчики —
    # просто чистим состояние и выходим. Иначе ошибочный выход на shutdown ушёл бы
    # в plus-ветку (check_plus/dop_plus в канал + инкремент серии).
    if _shutdown_requested:
        option_data.clear_data()
        return result, plus, fall, bug_text, check_cookies
    if channel_mess:
        try:
            await asyncio.wait_for(
                get_app().send_photo(chat_id=channel_id, photo='pictures/bug.png',
                                     caption=main_bug_message()),
                timeout=TG_SEND_TIMEOUT)
        except (Exception,) as error:
            logger.error(f'Ошибка отправки сообщения о сбое программы - {error}')
    else:
        plus = True
        if option_data.plus:
            check = await check_plus()
            if not check[0]:
                return result, plus, False, check[1], check_cookies
        if option_data.minus:
            check = await check_minus()
            if not check[0]:
                return result, False, False, check[1], check_cookies
    option_data.clear_data()
    return result, plus, fall, bug_text, check_cookies


def check_cookies_price(old_price: float, new_price: float, round_par: int, count: int) -> tuple[int, float]:
    """
    Проверка повторения цены
    :param old_price: старая цена
    :param new_price: новая цена
    :param round_par: параметр округления
    :param count: счетчик повторов
    :return:
    """
    EPS = 10 ** (-round_par)
    if abs(new_price - old_price) <= EPS:
        new_count = count + 1
    else:
        new_count = 0
    return new_count, new_price


async def mouse_move(page: Page, element_xpath: str, move: int) -> bool:
    """
    Эмулятор движения мыши
    :param page: страница браузера
    :param element_xpath: xpath элемента для движения
    :param move: тип движения (1 - большое, иначе маленькое)
    :return: True при успехе
    """
    if move == 1:
        mv = 200
    else:
        mv = 5
    try:
        element = page.locator(f"xpath={element_xpath}").first
        box = await element.bounding_box()
        if box:
            center_x = box['x'] + box['width'] / 2
            center_y = box['y'] + box['height'] / 2
            await page.mouse.move(center_x, center_y)
            await page.mouse.move(center_x + mv, center_y + mv)
            await page.mouse.move(center_x, center_y)
        return True
    except (Exception,) as error:
        logger.warning(f'Ошибка имитации движения мыши - {error}')
        return False


async def get_price(manager: "BrowserManager") -> tuple[bool, float | str]:
    """
    Получение цены
    :param manager: менеджер браузера
    :return: (success, price или error_message)
    """
    result = await find_price(manager)
    if result[0]:
        strprice = result[1]
        page = manager.pages['price']
        if not await mouse_move(page, move_field, 1):
            return False, 'Ошибка имитации движения мыши'
        price = float(clear_price(strprice))
        return True, price
    else:
        return False, result[1]


async def find_price(manager: "BrowserManager") -> tuple[bool, str]:
    """
    Поиск цены в браузере
    :param manager: менеджер браузера
    :return: (success, price_text или error_message)
    """
    try:
        page = manager.pages['price']
        await page.bring_to_front()

        # Закрытие popup, если есть
        await _close_popup(page)

        price_element = page.locator(f"xpath={price_field}").first
        # timeout — иначе при отсутствии элемента висим на дефолтных 30с
        price_text = await price_element.text_content(timeout=TIMEOUT_MEDIUM)
        return True, price_text or ""
    except (Exception,) as error:
        error_text = f"Не удалось загрузить цену - {error}"
        # при штатной остановке драйвер уже снесён — это не сбой, не шумим в error-канал
        if _shutdown_requested:
            logger.warning(error_text)
        else:
            logger.error(error_text)
        return False, error_text


async def screenshot(manager: "BrowserManager", take_shot: bool, qr) -> tuple[bool, float | str]:
    """
    Снятие скриншота с окна main.
    :param manager: менеджер браузера
    :param take_shot: False — только цена без скриншота; True — снимаем скрин и кладём QR.
    :param qr: кортеж (qr110, qr85) — QR-оверлеи
    :return: (success, price или error_message)
    """
    try:
        price_result = await get_price(manager)
        if not price_result[0]:
            return False, price_result[1]

        if not take_shot:  # если требуется только цена без скриншота
            return True, price_result[1]

        # Грузится только окно main — все скрины снимаются с него.
        page = manager.pages['main']
        await page.bring_to_front()

        # Закрытие popup, если есть
        await _close_popup(page)

        if not await mouse_move(page, move_field, 0):
            return False, 'Ошибка имитации движения мыши'

        element = page.locator(f"xpath={screen_zone}").first
        await element.screenshot(path=shot_path)

        with Image.open(shot_path) as img:
            if qr:
                qr110, qr85 = qr
                paste_overlay(img, qr110, qr110_x, qr110_y)
                paste_overlay(img, qr85, qr85_x, qr85_y)
            img.save(screenshot_path)

        return True, price_result[1]
    except (Exception,) as error:
        error_text = f'Ошибка записи скриншота - {str(error)}'
        return False, error_text


async def find_point(manager: "BrowserManager", resume: str) -> tuple[bool, str]:
    """
    Поиск точки входа
    :param manager: менеджер браузера
    :param resume: направление сигнала
    :return: (success, error_message)
    """
    if 'ПОКУПАТЬ' in resume:
        color = bull_color
    else:
        color = bear_color

    while_time = (datetime.now() + timedelta(minutes=find_time))
    page = manager.pages['price']
    await page.bring_to_front()
    price_element = page.locator(f"xpath={price_field}").first  # локатор постоянен — вне цикла

    # Выход только изнутри: нашли цвет (True), превысили лимит времени или ошибка (False).
    while True:
        try:
            # Проверка тайм-аута в начале итерации
            if datetime.now() > while_time:
                error_text = 'Время поиска точки входа превысило лимит'
                return False, error_text

            await mouse_move(page, price_field, 1)

            # Получаем цвет элемента через evaluate
            tp = await price_element.evaluate("el => getComputedStyle(el).color")
            tp = str(tp)

            if color in tp:
                return True, ''

            # Пауза между проверками, чтобы не грузить CPU
            await asyncio.sleep(0.1)

        except (Exception,) as error:
            error_text = f'Ошибка определения входа в опцион - {str(error)}'
            return False, error_text


def clear_price(price_str: str) -> str:
    """
    Очистка строки с ценой от всех символов, кроме цифр, точки и запятой
    с последующей заменой запятой на точку
    """
    cleaned_string = re.sub(r'[^\d,.]', '', price_str)
    return cleaned_string.replace(',', '.')


async def find_option_data(manager: "BrowserManager", log_data: Option, used_val: list):
    """
    Поиск данных для опциона
    :param manager: менеджер браузера
    :param used_val: список последних использованных валютных пар
    :param log_data: класс с данными
    :return: словарь с данными для опциона
    """
    active_binary_list = await database.option_data_tv(tf=log_data.find_timeframe, exclude_ids=used_val)
    if not active_binary_list:  # None или пустой список
        await close_program(manager=manager, status=1, text='Не найдено валютных пар для опциона')
        return False  # close_program вызывает sys.exit, но на всякий случай

    if len(active_binary_list) >= 3:
        log_data.add_option_data(active_binary_list[random.randint(0, 2)])
    else:
        log_data.add_option_data(active_binary_list[0])

    await init_valute_browser(manager, log_data.name.replace('/', ''))
    return True


async def dop_plus_message():
    """Дополнительное сообщение для плюсов"""
    message_text = dop_plus10_message()
    ok, err = await send_photo_safe(PLUS_SERIES_IMAGE, message_text,
                                    mes_type='дополнительное сообщение плюсов')
    if ok:
        return True, ''
    return False, f"Ошибка отправки дополнительного сообщения плюсов - {err}"


async def time_sleep():
    """Случайная пауза между циклами main (§7: границы в .env MAIN_CYCLE_PAUSE_MIN/MAX,
    дефолты = историческому хардкоду 100/120; для OTC +30, как было)."""
    sleep_time = random.randint(main_cycle_pause_min, main_cycle_pause_max)
    if binary:
        return sleep_time
    else:
        return sleep_time + 30
