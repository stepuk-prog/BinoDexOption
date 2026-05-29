import asyncio
import json
import re
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeout, WebSocket

from classes.Option_class import Option
from settings.config import cookies, shot_path, screenshot_path
from apps.cookie_utils import add_cookies_to_context
from settings.timing import (
    TIMEOUT_SHORT, TIMEOUT_LONG,
    ELEMENT_RETRY_DELAY, MAX_SCREENSHOT_ATTEMPTS, MAX_PRICE_ATTEMPTS
)
from classes.result_types import OperationResult
from apps.exit_app import close_program
from database import AsyncDatabase
from logs import init_logger
from settings.screenshot_set import win_x_otc, win_y_otc, otc_qr_x, otc_qr_y
from settings.browser_config import (input_otc, otc_val_list_close, otc_val_list_open, screen_zone_otc, otcprice,
                                     change_tf, chart_type, s30, timeframe_otc, otc_screen, name_valute_list_css,
                                     list_valute_css, percent_value, check_google)

if TYPE_CHECKING:
    from apps.browser_app import BrowserManager

PRICE_RE = re.compile(r"\d+\.\d+")

logger = init_logger(__name__)

# Глобальный экземпляр асинхронной базы данных
_database: AsyncDatabase | None = None


class WebSocketPriceTracker:
    """Отслеживание цен через WebSocket"""

    def __init__(self):
        self.prices: dict[str, float] = {}  # asset_name -> price
        self.last_message: str = ""
        self.ws_connected: bool = False
        self._debug_mode: bool = False  # Отключено

    def handle_message(self, payload):
        """Обработка входящего WebSocket сообщения"""
        # Конвертируем bytes в str если нужно
        if isinstance(payload, bytes):
            try:
                payload = payload.decode('utf-8')
            except (Exception,):
                return

        self.last_message = payload

        # Временное логирование для отладки
        if self._debug_mode and payload.startswith('[['):
            logger.info(f"WS DATA: {payload[:200]}")

        # Парсим данные
        try:
            # Формат PocketOption: [["SYMBOL",timestamp, price]]
            if payload.startswith('[['):
                data = json.loads(payload)
                self._parse_stream_data(data)
        except json.JSONDecodeError:
            pass
        except (Exception,):
            pass

    def _parse_stream_data(self, data):
        """Парсинг потоковых данных котировок PocketOption"""
        # Формат: [["GBPJPY_otc", 1768488749.874, 216.517]]
        if isinstance(data, list):
            for item in data:
                if isinstance(item, list) and len(item) >= 3:
                    symbol = item[0]  # "GBPJPY_otc"
                    price = item[2]   # 216.517
                    if isinstance(price, (int, float)):
                        self.prices[symbol] = float(price)

    def get_price(self, asset: str = None) -> float | None:
        """Получить последнюю цену для актива"""
        if asset:
            # Нормализуем имя актива: "GBP/JPY" -> "GBPJPY", "GBPJPY OTC" -> "GBPJPY_otc"
            normalized = asset.replace('/', '').replace(' ', '_').upper()

            # Ищем точное совпадение
            if asset in self.prices:
                return self.prices[asset]

            # Ищем с суффиксом _otc
            otc_key = normalized.replace('_OTC', '') + '_otc'
            if otc_key in self.prices:
                return self.prices[otc_key]

            # Ищем частичное совпадение
            for key, price in self.prices.items():
                key_normalized = key.replace('_otc', '').upper()
                if normalized.replace('_OTC', '') == key_normalized:
                    return price

        # Вернуть последнюю полученную цену, если актив не указан
        if self.prices:
            return list(self.prices.values())[-1]
        return None


# Глобальный трекер цен
_price_tracker: WebSocketPriceTracker | None = None


def get_price_tracker() -> WebSocketPriceTracker:
    """Получить глобальный трекер цен"""
    global _price_tracker
    if _price_tracker is None:
        _price_tracker = WebSocketPriceTracker()
    return _price_tracker


async def get_database() -> AsyncDatabase:
    """Получение асинхронного подключения к базе данных."""
    global _database
    if _database is None:
        _database = AsyncDatabase()
        await _database.connect()
    return _database


async def parce_otc(log_data: Option, manager: "BrowserManager", valute: list) -> bool:
    """
    Определение валюты для опциона OTC
    :param log_data: класс с данными опциона
    :param manager: менеджер браузера
    :param valute: список с отработанными валютами
    :return: True в случае успешного завершения
    """
    database = await get_database()
    page = manager.pages['main']
    active_otc_list = await database.option_data_pocket(exclude_ids=valute, tf=log_data.find_timeframe)
    if not active_otc_list:  # None/False (ошибка пула) или пустой список
        return False
    for otc in active_otc_list:
        log_data.add_option_data(otc)
        result = await change_otc(valute=log_data.name, page=page)
        if result == 1:
            log_data.name = log_data.name + ' OTC'
            return True
        if result == 0:  # сбой переключения (а не «пара неактивна») — логируем, пробуем следующую
            logger.warning(f"Сбой переключения OTC-пары {log_data.name}, пробую следующую")
    return False


def _parse_price(text: str) -> float | None:
    """Получение цены OTC из строки tooltip"""
    text = text.replace(",", ".")
    m = PRICE_RE.search(text)
    return float(m.group(0)) if m else None


async def get_price(page: Page, asset: str = None, timeout: int = TIMEOUT_SHORT // 1000,
                    attempts: int = MAX_PRICE_ATTEMPTS, delay: float = ELEMENT_RETRY_DELAY) -> float | bool:
    """
    Возвращает текущую цену актива.
    Сначала пробует WebSocket, затем tooltip.
    :param page: Активная страница Playwright
    :param asset: Название актива (например 'GBPJPY')
    :param timeout: таймаут в секундах
    :param attempts: число попыток
    :param delay: пауза между попытками
    :return: float или False
    """
    # Способ 1: Получить из WebSocket (быстро и надёжно)
    tracker = get_price_tracker()
    if tracker.ws_connected and tracker.prices:
        price = tracker.get_price(asset)
        if price:
            logger.info(f"💰 Цена из WebSocket: {asset} = {price}")
            return price

    # Способ 2: Fallback на tooltip
    logger.warning(f"WebSocket цена недоступна для {asset}, пробуем tooltip...")
    hover_selector = "div.estimated-profit-block__tooltip, .tooltip2, [class*='estimated-profit']"

    for attempt in range(1, attempts + 1):
        try:
            # Наводим мышь на элемент, чтобы появился tooltip
            hover_el = page.locator(hover_selector)
            if await hover_el.count() > 0:
                await hover_el.first.hover(timeout=timeout * 1000)
                await asyncio.sleep(0.3)

            # Ищем tooltip с ценой
            text_el = page.locator(otcprice)
            count = await text_el.count()
            if count == 0:
                logger.warning(f"Попытка {attempt}/{attempts}: tooltip не появился")
                await asyncio.sleep(delay)
                continue

            raw = (await text_el.first.text_content() or "").strip()
            price = _parse_price(raw)
            if price is not None:
                return price
            else:
                logger.warning(f"Попытка {attempt}/{attempts}: не удалось извлечь число из '{raw}'")
        except PlaywrightTimeout:
            logger.warning(f"Попытка {attempt}/{attempts}: таймаут hover/tooltip")
        except (Exception,) as err:
            logger.error(f"Попытка {attempt}/{attempts}: ошибка — {err}")
        await asyncio.sleep(delay)

    logger.error("Не удалось получить цену ни из WebSocket, ни из tooltip")
    return False


async def screenshot_otc(page: Page, asset: str = None, qr=None) -> tuple[bool, float | bool, str] | tuple[bool, str, str]:
    """
    Получение скриншота Pocket (с QR-оверлеем).
    :param page: страница браузера
    :param asset: название актива для получения цены
    :param qr: кортеж (qr110, qr85) — на OTC кладём только qr110
    :return:
    """
    for attempt in range(1, MAX_SCREENSHOT_ATTEMPTS + 1):
        try:
            element = page.locator(screen_zone_otc)
            await element.wait_for(state='visible', timeout=TIMEOUT_LONG)

            # Параллельно получаем цену и делаем скриншот для минимизации рассинхрона
            price_task = get_price(page, asset=asset)
            screenshot_task = element.screenshot(path=shot_path)
            price, _ = await asyncio.gather(price_task, screenshot_task)

            if not price:
                msg = f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: не удалось получить цену OTC"
                logger.warning(msg)
                continue
            with Image.open(shot_path) as img:
                if qr:
                    qr110 = qr[0]
                    img.paste(qr110, (otc_qr_x, otc_qr_y), mask=qr110 if qr110.mode in ('RGBA', 'LA') else None)
                img.save(screenshot_path)
            return True, price, screenshot_path
        except (Exception,) as e:
            logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS} скриншота OTC: {e}")
    error_text = 'Ошибка записи скриншота'
    return False, error_text, ''


async def find_valute(page: Page) -> dict[str, Locator | None | bool]:
    """Поиск активной валютной пары в списке"""
    result: dict[str, Locator | None | bool] = {'active': False, 'element': None}
    try:
        # Ждём появления всех элементов списка
        await page.locator(list_valute_css).first.wait_for(timeout=15000)
        items = page.locator(list_valute_css)
        count = await items.count()

        for i in range(count):
            item = items.nth(i)
            name_el = item.locator(name_valute_list_css)
            name = (await name_el.text_content() or "").strip()

            if 'OTC' in name:
                payout_el = item.locator(percent_value)
                payout_text = (await payout_el.text_content() or "").strip()
                if payout_text and payout_text.upper() != "N/A":
                    result['active'] = True
                    result['element'] = item
                    break
    except (Exception,):
        logger.error("Ошибка поиска элемента в списке")
    return result


async def change_otc(valute: str, page: Page) -> int:
    """
    Переключение валюты на сайте Pocket
    :param valute: имя валютной пары, на которую нужно переключиться
    :param page: страница браузера
    :return: 1 - Успешное переключение, 2 - пара не активна, 0 - ошибка при выполнении
    """
    try:
        await check_design(page=page)
        # Открываем список
        button = page.locator(f".{otc_val_list_open}").first
        await button.click(timeout=15000)
    except (Exception,) as err:
        logger.error(f"Ошибка раскрытия списка валют — {err}")
        return 0

    try:
        # Вводим название пары
        valute_input = page.locator(f".{input_otc}")
        await valute_input.wait_for(state='visible', timeout=15000)
        await valute_input.clear()
        await valute_input.fill(valute)

        # Находим нужный элемент
        element = await find_valute(page=page)
        if element['active']:
            await element['element'].click(timeout=TIMEOUT_SHORT)
            return 1
        else:
            return 2
    except (Exception,) as error:
        logger.error(f"Ошибка проверки состояния OTC пары - {error}")
        return 0
    finally:
        try:
            await otc_list_close(page=page)
        except (Exception,):
            pass
    return 0  # fallback, не должен достигаться


async def otc_list_close(page: Page) -> bool:
    """Закрытие списка валют"""
    # Способ 1: Нажать Escape
    try:
        await page.keyboard.press('Escape')
        await asyncio.sleep(0.3)
        # Проверяем, закрылся ли список
        input_field = page.locator(f".{input_otc}")
        if await input_field.count() == 0 or not await input_field.is_visible():
            return True
    except (Exception,):
        pass

    # Способ 2: Клик по селектору из БД
    try:
        clicker = page.locator(otc_val_list_close)
        if await clicker.count() > 0:
            await clicker.first.click(timeout=3000)
            return True
    except (Exception,):
        pass

    # Способ 3: Клик по графику (вне списка)
    try:
        chart = page.locator(".chart-area, .trading-chart, canvas")
        if await chart.count() > 0:
            await chart.first.click(timeout=3000)
            return True
    except (Exception,):
        pass

    logger.warning("Не удалось закрыть меню выбора валют")
    return False


async def design_customization(page: Page) -> bool:
    """Настройка свечей на графике"""
    try:
        clicker = page.locator(f".{timeframe_otc}").first
        await clicker.click(timeout=15000)
    except (Exception,) as error:
        logger.error(f'Не удалось открыть список выбора таймфреймов - {error}')
        return False

    try:
        clicker = page.locator(f"xpath={change_tf}").first
        await clicker.click(timeout=15000)
    except (Exception,) as error:
        logger.error(f'Не удалось выбрать таймфрейм H4 из списка выбора таймфреймов - {error}')
        return False

    try:
        clicker = page.locator(f".{chart_type}").first
        await clicker.click(timeout=15000)
    except (Exception,) as error:
        logger.error(f'Не удалось открыть окно выбора масштаба свечи - {error}')
        return False

    try:
        items = page.locator(s30)
        count = await items.count()
        for i in range(count):
            span = items.nth(i)
            text = (await span.text_content() or "").strip()
            if text == "S30":
                await span.click()
                break
    except (Exception,) as error:
        logger.error(f'Не удалось выбрать масштаб свечи - {error}')
        return False

    if not await otc_list_close(page=page):
        return False
    return True


async def check_design(page: Page):
    """Проверка дизайна графика"""
    try:
        elem = page.locator(f".{timeframe_otc}").first
        await elem.wait_for(state='visible', timeout=10000)
        tf = (await elem.text_content() or "").strip()
    except PlaywrightTimeout:
        logger.error("⏰ Ошибка проверки дизайна графика - не дождался элемента с классом %s за 10 с", timeframe_otc)
        return None
    except (Exception,) as error:
        logger.error(f"Ошибка проверки дизайна графика - не удалось найти элемент {timeframe_otc} для определения "
                     f"дизайна графика - {error}")
        return None

    if tf != 'H2':
        await design_customization(page=page)
    return None


async def open_otc_browser(manager: "BrowserManager") -> OperationResult:
    """Открытие браузера для OTC торговли"""
    result = await init_otc(manager=manager)
    return OperationResult(success=bool(result))


def setup_websocket_tracker(page: Page):
    """Настройка перехвата WebSocket для отслеживания цен"""
    tracker = get_price_tracker()

    def on_websocket(ws: WebSocket):
        # Фильтруем только WebSocket с котировками
        if 'po.market' in ws.url:
            logger.info(f"🔌 WebSocket подключен: {ws.url}")
            tracker.ws_connected = True

            def on_frame(data):
                # Playwright передаёт объект с полем payload
                if hasattr(data, 'payload'):
                    tracker.handle_message(data.payload)
                elif isinstance(data, (str, bytes)):
                    tracker.handle_message(data)
                elif isinstance(data, dict) and 'payload' in data:
                    tracker.handle_message(data['payload'])

            def on_close(_ws: WebSocket):
                logger.info("🔌 WebSocket отключен")
                # Не сбрасываем ws_connected, если есть другие подключения

            ws.on("framereceived", on_frame)
            ws.on("close", on_close)

    page.on("websocket", on_websocket)


async def init_otc(manager: "BrowserManager") -> bool | None:
    """Инициализация OTC страницы"""
    page = manager.pages['main']

    # Настраиваем перехват WebSocket ДО загрузки страницы
    setup_websocket_tracker(page)

    try:
        await page.goto(otc_screen, wait_until='domcontentloaded')
        await page.set_viewport_size({'width': win_x_otc, 'height': win_y_otc})
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f"Не загрузился браузер - {error}")

    try:
        # Ждём полной загрузки страницы
        await page.wait_for_load_state('domcontentloaded')

        # Добавляем cookies
        if cookies:
            await add_cookies_to_context(manager.context, cookies)
        else:
            await close_program(manager=manager, status=1, text="Нет cookies для установки.")

        await page.reload(wait_until='domcontentloaded')

        # Закрываем модальные окна
        await close_modal(page=page)

        # Проверка на кнопку Google (проверка cookies)
        try:
            google_button = page.locator(f".{check_google}")
            if await google_button.count() > 0:
                await close_program(manager=manager, text='', status=1, cookies=True)
        except (Exception,):
            pass

        # Ждём подключения WebSocket
        tracker = get_price_tracker()
        for _ in range(10):
            if tracker.ws_connected:
                logger.info("✅ WebSocket для цен подключен")
                break
            await asyncio.sleep(0.5)

        return True
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f'Ошибка загрузки страницы OTC - {error}')


async def close_modal(page: Page):
    """Подавление модального окна"""
    try:
        close_btn = page.locator(".mfp-close")
        await close_btn.click(timeout=5000)
        await page.locator(".mfp-container").wait_for(state='hidden', timeout=10000)
    except (Exception,):
        pass

    try:
        ok_btn = page.locator("button.free-trades-welcome-modal__btn.btn-green")
        await ok_btn.click(timeout=5000)
        await page.locator(".free-trades-welcome-modal__banner, .free-trades-welcome-modal__content").wait_for(
            state='hidden', timeout=10000)
        return True
    except (Exception,):
        pass
