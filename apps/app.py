import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page

from apps.browser_app import init_valute_browser
from apps.exit_app import close_program
from apps.my_exeptions import lost_connection_photo, lost_connection
from database import AsyncDatabase
from logs import init_logger
from settings.Option_class import Option
from messages import (main_bug_message, dop_plus10_message, plus50_message, plus5_message,
                      plus10_message, plus15_message, plus20_message, plus25_message, plus30_message, plus35_message,
                      plus40_message, plus45_message)
from settings import qr110_x, qr110_y, qr85_x, qr85_y
from settings.browser_config import move_field, price_field, pop_up, screen_zone
from settings.config import channel_id, option_data, get_app, binary, program_id, shot_path, screenshot_path
from settings.constant import qr110_path, qr85_path, bear_color, bull_color, find_time
from settings.timing import CHECK_PLUS_DELAY, POST_SCREENSHOT_DELAY
from settings.image_paths import PLUS_SERIES_IMAGE

if TYPE_CHECKING:
    from apps.browser_app import BrowserManager

# Глобальный экземпляр асинхронной базы данных
# Инициализируется при первом использовании через get_database()
_database: AsyncDatabase | None = None
logger = init_logger(__name__)

# Флаг штатной остановки (SIGTERM/SIGINT): при нём exit_main не шлёт main_bug_message.
_shutdown_requested = False


def request_shutdown():
    """Пометить штатную остановку — подавляет сообщение о сбое (main_bug_message)."""
    global _shutdown_requested
    _shutdown_requested = True


async def get_database() -> AsyncDatabase:
    """Получение асинхронного подключения к базе данных."""
    global _database
    if _database is None:
        _database = AsyncDatabase()
        await _database.connect()
    return _database


def check_work_hour() -> bool:
    """
    Проверка времени работы стандартных пар
    :return:
    """
    current_hour = datetime.now().hour
    if 6 < current_hour < 22:
        return True
    else:
        return False


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
    database = await get_database()
    kol_plus = await database.plus_counter(program_id=program_id)
    await asyncio.sleep(CHECK_PLUS_DELAY)

    plus_messages = {
        5: plus5_message,
        10: plus10_message,
        15: plus15_message,
        20: plus20_message,
        25: plus25_message,
        30: plus30_message,
        35: plus35_message,
        40: plus40_message,
        45: plus45_message,
        50: plus50_message,
    }

    if kol_plus['plus'] in plus_messages:
        message_text = plus_messages[kol_plus['plus']]()
        try:
            await get_app().send_message(chat_id=channel_id, text=message_text)
        except (Exception,) as error:
            bug_fix = await lost_connection(error=error, text=message_text,
                                            mes_type=f'сообщение {kol_plus["plus"]} плюс')
            if not bug_fix[0]:
                error_text = f'Ошибка отправки сообщения {kol_plus["plus"]} плюс! - {bug_fix[1]}'
                return False, error_text
        await asyncio.sleep(POST_SCREENSHOT_DELAY)
        bug_fix = await dop_plus_message()
        if bug_fix[0]:
            return True, ''
        else:
            return False, bug_fix[1]

    return True, ''


async def check_minus():
    """Сброс серии плюсов при минусе — инкремент счётчика минусов в БД."""
    database = await get_database()
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
    if channel_mess and not _shutdown_requested:
        try:
            await get_app().send_photo(chat_id=channel_id, photo='pictures/bug.png',
                                       caption=main_bug_message())
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
        logger.report(f'Ошибка имитации движения мыши - {error}')
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
        try:
            popup = page.locator(f".{pop_up}")
            if await popup.count() > 0:
                await popup.first.click(timeout=3000)
        except (Exception,):
            pass

        price_element = page.locator(f"xpath={price_field}").first
        price_text = await price_element.text_content()
        return True, price_text or ""
    except (Exception,) as error:
        error_text = f"Не удалось загрузить цену - {error}"
        logger.error(error_text)
        return False, error_text


async def screenshot(manager: "BrowserManager", screen: str | None, qr) -> tuple[bool, float | str]:
    """
    Снятие скриншота с окна main.
    :param manager: менеджер браузера
    :param screen: None — только цена без скриншота; иначе снимаем скрин и кладём QR.
    :param qr: кортеж (qr110, qr85) — QR-оверлеи
    :return: (success, price или error_message)
    """
    try:
        price_result = await get_price(manager)
        if not price_result[0]:
            return False, price_result[1]

        if screen is None:  # если требуется только цена без скриншота
            return True, price_result[1]

        # Грузится только окно main — все скрины снимаются с него.
        page = manager.pages['main']
        await page.bring_to_front()

        # Закрытие popup, если есть
        try:
            popup = page.locator(f".{pop_up}")
            if await popup.count() > 0:
                await popup.first.click(timeout=3000)
        except (Exception,):
            pass

        if not await mouse_move(page, move_field, 0):
            return False, 'Ошибка имитации движения мыши'

        element = page.locator(f"xpath={screen_zone}").first
        await element.screenshot(path=shot_path)

        with Image.open(shot_path) as img:
            if qr:
                qr110, qr85 = qr
                img.paste(qr110, (qr110_x, qr110_y), mask=qr110 if qr110.mode in ('RGBA', 'LA') else None)
                img.paste(qr85, (qr85_x, qr85_y), mask=qr85 if qr85.mode in ('RGBA', 'LA') else None)
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
    i_color = 0
    if 'ПОКУПАТЬ' in resume:
        color = bull_color
    else:
        color = bear_color

    while_time = (datetime.now() + timedelta(minutes=find_time))
    page = manager.pages['price']
    await page.bring_to_front()

    while i_color == 0:
        try:
            # Проверка тайм-аута в начале итерации
            if datetime.now() > while_time:
                error_text = 'Время поиска точки входа превысило лимит'
                return False, error_text

            price_element = page.locator(f"xpath={price_field}").first
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

    return True, ''


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
    database = await get_database()
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
    try:
        await get_app().send_photo(chat_id=channel_id, photo=PLUS_SERIES_IMAGE, caption=message_text)
        return True, ''
    except (Exception,) as error:
        bug_fix = await lost_connection_photo(error=error, photo=PLUS_SERIES_IMAGE, text=message_text,
                                              mes_type='дополнительное сообщение плюсов')
        if not bug_fix[0]:
            error_text = f"Ошибка отправки дополнительного сообщения плюсов - {str(error)}"
            return False, error_text
        else:
            return True, ''


async def time_sleep():
    """Случайная задержка"""
    sleep_time = random.randint(100, 120)
    if binary:
        return sleep_time
    else:
        return sleep_time + 30
