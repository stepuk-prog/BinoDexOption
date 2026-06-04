"""OTC через binodex.app.

Логин — storage_state (Privy) из binodex.cookies.binodex_cookies (контекст создаётся с
ним в browser_app.init_browser). Страница — из binodex.cookies.pages (bino_option/otc).
Выбор пары — модалка binodex по селекторам из binodex_settings.

Цена кадра — медиана нескольких быстрых чтений window.chartData.price вокруг screenshot
(см. docs/BINODEX_PRICE.md): это значение, которое движок рисует на ярлыке графика. Оно
точнее WS-тика — WS опережает график на ~150 мс (график плавно доезжает до свежего тика),
поэтому WS-цена «убегала вперёд» от картинки. WS-трекер оставлен как фолбэк (если chartData
недоступен) и под liveness (подтверждение загрузки пары, init, feed_dead). Округление до
decimals делает main_app через option_data.round (= otc_assets.decimals). Скрин — зона
графика (canvas) + QR.
"""
import asyncio
import re
import statistics
import time
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page, WebSocket

from classes.Option_class import Option
from classes.price_tracker import WebSocketPriceTracker, symbol_key
from classes.result_types import OperationResult
from classes.exceptions import CookiesExpired
from apps.exit_app import close_program
from logs import init_logger
from settings.config import shot_path, screenshot_path, database, prog_key
from settings.timing import TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_LONG, MAX_SCREENSHOT_ATTEMPTS
from settings.screenshot_set import win_x_otc, win_y_otc, otc_qr_x, otc_qr_y, paste_overlay
from settings.browser_config import (otc_select_pair, otc_category_valute, otc_input_pair,
                                     otc_modal_pair_item, screen_zone_otc, otc_settings_btn)

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

PRICE_WS_HINT = "api-coins.binodex.io"  # WS котировок binodex

# Цена графика прямо со страницы: движок binodex держит её в window.chartData = {symbol, price}.
# price — анимированное значение, которое рисуется на ярлыке (округляется до decimals в main_app).
CHART_DATA_JS = ("() => { const c = window.chartData;"
                 " return (c && typeof c.price === 'number')"
                 " ? { symbol: c.symbol, price: c.price } : null; }")
# Медиана нескольких быстрых чтений вокруг кадра гасит редкий анимационный выброс ярлыка
# (проверено: 3+3 чтения → 9/10 совпадений с нарисованным ценником; см. docs/BINODEX_PRICE.md).
CHART_READS_BEFORE = 3  # чтений chartData вплотную ДО screenshot
CHART_READS_AFTER = 3   # и сразу ПОСЛЕ
# Кнопка настроек аккаунта (otc_settings_btn) есть в тулбаре ТОЛЬКО когда торговый UI полностью
# прогрузился. На сплеше (зависший Privy-токен без редиректа) её нет — хотя кнопка выбора пары
# присутствует, потому on_trade/UI-gate по ней и feed_dead (котировок-WS стримит все пары) сплеш
# не ловят. Отсутствие этой кнопки — точный DOM-маркер «завис на сплеше».
UI_READY_TIMEOUT = 15.0   # сек ждать кнопку настроек при загрузке (init_otc)
UI_DEAD_CONFIRM = 3.0     # сек подтверждения «UI пропал → сплеш» в рантайм-детекте (otc_session_dead)

logger = init_logger(__name__)


def on_trade(url: str) -> bool:
    """binodex: авторизация активна, если остались на …/trade (Privy редиректит
    неавторизованных). Детерминированный детект отвала cookies (§4.1) — основной сигнал."""
    return url.rstrip('/').endswith('/trade')

# Глобальный трекер цен (один на процесс; страница регистрирует WS-перехват в init_otc)
_price_tracker: WebSocketPriceTracker | None = None


def get_price_tracker() -> WebSocketPriceTracker:
    global _price_tracker
    if _price_tracker is None:
        _price_tracker = WebSocketPriceTracker()
    return _price_tracker


def setup_websocket_tracker(page: Page):
    """Перехват WS-котировок binodex (graphic-фреймы) → трекер."""
    tracker = get_price_tracker()

    def on_websocket(ws: WebSocket):
        if PRICE_WS_HINT not in ws.url:
            return
        logger.info(f"🔌 WS котировок binodex: {ws.url}")
        tracker.ws_connected = True

        def on_frame(data):
            # callback Playwright синхронный: исключение здесь всплыло бы в event loop
            # и могло уронить перехват WS — глушим с логом.
            try:
                payload = getattr(data, 'payload', data)
                tracker.handle_message(payload)
            except (Exception,) as error:
                logger.debug(f"WS on_frame: {error}")

        def on_close(*_args):
            # WS закрылся: фид котировок оборвался (часто — протух токен Privy без
            # редиректа страницы). feed_dead подхватит это как сигнал отвала (§4.4).
            tracker.ws_connected = False
            logger.info("🔌 WS котировок binodex закрыт")

        ws.on("framereceived", on_frame)
        ws.on("close", on_close)

    page.on("websocket", on_websocket)


async def _otc_page_url() -> str | None:
    """URL OTC-страницы из binodex.cookies.pages (bino_option/otc)."""
    rows = await database.pages(program=prog_key, mode='otc')
    if not rows:
        return None
    return rows[0]['url']


async def _pair_modal_open(page: Page) -> bool:
    """Модалка выбора открыта, если видна кнопка категории."""
    try:
        return await page.locator(otc_category_valute).first.is_visible()
    except (Exception,):
        return False


async def _close_pair_modal(page: Page):
    """Закрыть модалку выбора пары — разными способами, пока категория ещё видна
    (модалка binodex не закрывается одним способом надёжно)."""
    async def _click_select():
        await page.click(otc_select_pair, timeout=TIMEOUT_SHORT)

    async def _escape():
        await page.keyboard.press('Escape')

    async def _click_chart():
        # position — TypedDict Position; dict-литерал корректен в рантайме, инспекцию типа подавляем.
        # noinspection PyTypeChecker
        await page.locator(screen_zone_otc).first.click(timeout=TIMEOUT_SHORT, position={'x': 8, 'y': 8})

    for method in (_click_select, _escape, _click_chart, _click_select):
        if not await _pair_modal_open(page):
            return
        try:
            await method()
        except (Exception,):
            pass
        # Ждём закрытия (с учётом анимации), но не слепо: выходим сразу, как закрылась.
        for _ in range(10):  # до ~1с на метод
            if not await _pair_modal_open(page):
                return
            await asyncio.sleep(0.1)


async def select_otc_pair(page: Page, pair: str) -> bool:
    """Выбрать '<pair> OTC' в модалке binodex (pair вида 'EUR/USD').
    Открыть выбор → категория Валюты → ввести пару → клик по элементу '<pair> ... OTC' →
    закрыть модалку → дождаться, пока сайт прогрузит пару (WS отдаст котировку). True при успехе."""
    try:
        await page.click(otc_select_pair, timeout=TIMEOUT_MEDIUM)
        await page.locator(otc_category_valute).first.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
        await page.click(otc_category_valute, timeout=TIMEOUT_MEDIUM)
        # реальное поле — вложенный input/textarea внутри обёртки. fill() сам ждёт
        # его готовность (auto-wait) — отдельная пауза не нужна.
        inner = page.locator(otc_input_pair).locator('input, textarea').first
        try:
            await inner.fill(pair, timeout=TIMEOUT_SHORT)
        except (Exception,):
            await page.click(otc_input_pair, timeout=TIMEOUT_SHORT)
            await page.keyboard.type(pair, delay=40)
        # Ждём появления нужного пункта '<pair> … OTC' (auto-wait вместо слепой паузы):
        # фильтруем по тексту пары и по 'OTC' (без регистра).
        target_item = (page.locator(otc_modal_pair_item)
                       .filter(has_text=pair)
                       .filter(has_text=re.compile('OTC', re.IGNORECASE))
                       .first)
        try:
            await target_item.wait_for(state='visible', timeout=TIMEOUT_SHORT)
        except (Exception,):
            logger.warning(f"OTC: не нашёл '{pair} … OTC' в модалке")
            await _close_pair_modal(page)
            return False
        await target_item.click(timeout=TIMEOUT_SHORT)

        await asyncio.sleep(1.0)            # дать сайту переключить график (WS-цена не пруф — стримятся все пары)
        await _close_pair_modal(page)        # закрыть модалку (иначе перекрывает график и блокирует прогрузку)

        # Дождаться, пока сайт прогрузит новую пару и WS отдаст её котировку (до 8с —
        # рабочие пары приходят за 1–3с). Если не пришла, пара на binodex не грузится
        # (бывает по отдельным парам) → возвращаем False, parce_otc возьмёт следующую.
        tracker = get_price_tracker()
        target = pair + ' OTC'
        for _ in range(32):
            if tracker.get_price(target) is not None:
                return True
            await asyncio.sleep(0.25)
        logger.warning(f"OTC: пара '{pair}' не прогрузилась на binodex (нет WS-котировки за 8с) — пропускаю")
        return False
    except (Exception,) as error:
        logger.warning(f"OTC: ошибка выбора пары {pair} — {error}")
        return False


async def parce_otc(log_data: Option, manager: "BrowserManager", valute: list) -> bool:
    """Подобрать активную OTC-пару из БД и выбрать её на binodex.
    :return: True при успешном выборе."""
    page = manager.pages['main']
    active_otc_list = await database.option_data_pocket(exclude_ids=valute, tf=log_data.find_timeframe)
    if not active_otc_list:  # None/False (ошибка пула) или пусто
        return False
    for otc in active_otc_list:
        log_data.add_option_data(otc)  # log_data.name = 'EUR/USD' (из БД)
        if not await select_otc_pair(page, log_data.name):  # сам ждёт прогрузку пары (WS)
            logger.warning(f"OTC-пара {log_data.name} не выбралась, пробую следующую")
            continue
        log_data.name = log_data.name + ' OTC'
        return True
    return False


async def _read_chart_prices(page: Page, symbol: str | None, count: int) -> list[float]:
    """`count` быстрых чтений window.chartData.price. Если symbol задан — берём только тики
    этой пары (chartData.symbol == symbol), чтобы не схватить цену чужой пары сразу после
    переключения. Ошибки evaluate глушим (страница могла моргнуть) — вернём что успели."""
    out: list[float] = []
    for _ in range(count):
        try:
            data = await page.evaluate(CHART_DATA_JS)
        except (Exception,):
            data = None
        if not isinstance(data, dict):
            continue
        if symbol and data.get('symbol') != symbol:
            continue
        price = data.get('price')
        if isinstance(price, (int, float)):
            out.append(float(price))
    return out


async def _ui_loaded(page: Page, timeout: float) -> bool:
    """True, если торговый UI binodex полностью прогрузился — кнопка настроек аккаунта
    (otc_settings_btn) видна в пределах timeout. На сплеше (зависший Privy-токен без редиректа)
    этой кнопки нет, хотя кнопка выбора пары может присутствовать — поэтому это точный DOM-маркер
    «не сплеш», который on_trade/feed_dead не дают. locator.wait_for сам поллит до появления."""
    try:
        await page.locator(otc_settings_btn).first.wait_for(state='visible', timeout=int(timeout * 1000))
        return True
    except (Exception,):
        return False


async def screenshot_otc(page: Page, asset: str = None, qr=None):
    """Скрин зоны графика binodex + цена графика (медиана чтений window.chartData.price
    вокруг кадра) + QR. chartData.price — то значение, что движок рисует на ярлыке; это
    точнее WS-тика, который опережает график на ~150 мс (см. docs/BINODEX_PRICE.md). Если
    chartData недоступен — фолбэк на WS-цену по моменту кадра (get_price_at).
    :return: (success, price|error_text, screenshot_path|'')."""
    symbol = symbol_key(asset)
    last_error = 'нет цены графика OTC'
    for attempt in range(1, MAX_SCREENSHOT_ATTEMPTS + 1):
        try:
            element = page.locator(screen_zone_otc).first
            await element.wait_for(state='visible', timeout=TIMEOUT_LONG)
            # Цена графика = медиана быстрых чтений chartData.price ВОКРУГ кадра (несколько
            # до screenshot + несколько после). Медиана гасит редкий анимационный выброс
            # ярлыка. t_shot фиксируем для фолбэка на WS, если chartData не отдал значений.
            reads = await _read_chart_prices(page, symbol, CHART_READS_BEFORE)
            t_shot = time.time()
            await element.screenshot(path=shot_path)
            reads += await _read_chart_prices(page, symbol, CHART_READS_AFTER)
            price = statistics.median(reads) if reads else get_price_tracker().get_price_at(asset, t_shot)
            if price is None:  # ни chartData, ни WS не дали цену
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: нет цены графика OTC для {asset}")
                await asyncio.sleep(0.5)
                continue
            with Image.open(shot_path) as img:
                if qr:
                    paste_overlay(img, qr[0], otc_qr_x, otc_qr_y)  # на OTC один QR (qr110)
                img.save(screenshot_path)
            return True, price, screenshot_path
        except (Exception,) as error:
            last_error = str(error)
            logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS} скриншота OTC: {error}")
    return False, f'Ошибка записи скриншота OTC - {last_error}', ''


async def open_otc_browser(manager: "BrowserManager") -> OperationResult:
    """Открытие binodex для OTC."""
    return OperationResult(success=bool(await init_otc(manager=manager)))


async def init_otc(manager: "BrowserManager") -> bool:
    """Загрузка binodex.app/trade: WS-перехват → страница из cookies.pages → проверка
    логина (остались на /trade) → ожидание WS-котировок."""
    page = manager.pages['main']
    setup_websocket_tracker(page)  # подписка ДО навигации — поймать поток с самого старта

    url = await _otc_page_url()
    if not url:
        await close_program(manager=manager, status=1, text="Нет OTC-страницы в binodex.cookies.pages")
        return False

    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=TIMEOUT_LONG)
        await page.set_viewport_size({'width': win_x_otc, 'height': win_y_otc})
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f"Не загрузился binodex - {error}")
        return False

    try:
        try:
            await page.wait_for_load_state('networkidle', timeout=TIMEOUT_LONG)
        except (Exception,):
            pass  # постоянный WS-поток может мешать networkidle — не критично

        # Логин активен, только если остались на /trade (Privy-сессия из storage_state).
        # Отвал cookies (§4.3 Survive): поднимаем CookiesExpired → init_load пробросит в
        # main.py::_init_with_retry (backoff + пересоздание, БЕЗ выхода; куки перечитаются из БД).
        if not on_trade(page.url):
            raise CookiesExpired(f'binodex OTC: вход слетел (редирект с /trade на {page.url})')

        # Остались на /trade — но это ещё не гарантия, что SPA доехала. При протухшем Privy-токене
        # без редиректа страница виснет на сплеше: торговый UI не рендерится, кнопка выбора пары
        # отсутствует. URL-детект (on_trade) такой случай НЕ ловит → дальше main-цикл таймаутит по
        # всем парам на otc_select_pair (.row_w). Поэтому жёстко ждём готовность UI; не дождались —
        # это тот же отвал cookies (§4.3): CookiesExpired → _init_with_retry (алерт в cookies-канал +
        # backoff + пересоздание браузера, куки перечитаются из БД).
        try:
            await page.locator(otc_select_pair).first.wait_for(state='visible', timeout=TIMEOUT_LONG)
        except CookiesExpired:
            raise
        except (Exception,):
            raise CookiesExpired('binodex OTC: торговый UI не прогрузился (завис на /trade, '
                                 'кнопка выбора пары не появилась) — storage_state протух')

        # Кнопка выбора пары видна — но UI ещё НЕ обязательно прогрузился: при «залипшем»
        # Privy-токене сайт остаётся на /trade и рисует тулбар частично, а чарт виснет на сплеше.
        # Кнопки настроек аккаунта при этом НЕТ — её отсутствие точный DOM-маркер сплеша (on_trade
        # держит /trade, котировок-WS стримит все пары → feed_dead тоже молчит). Без этого бот
        # постил бы скрин сплеша (цена с WS-фолбэка) ИЛИ рестартовал по кругу, НЕ запуская
        # рефрешер. Нет кнопки настроек — тот же отвал cookies (§4.3): CookiesExpired →
        # _recover_otc_cookies (cold relogin do_setup=True).
        if not await _ui_loaded(page, UI_READY_TIMEOUT):
            raise CookiesExpired('binodex OTC: торговый UI не прогрузился (нет кнопки настроек '
                                 'аккаунта — завис на сплеше) — storage_state протух')

        # Ждём поток котировок (до 10 сек)
        tracker = get_price_tracker()
        for _ in range(20):
            if tracker.ws_connected and tracker.prices:
                logger.report("✅ binodex: WS котировок подключён")
                break
            await asyncio.sleep(0.5)
        return True
    except CookiesExpired:
        raise  # наружу → init_load → _init_with_retry (cookies-backoff), не глотать
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f'Ошибка загрузки OTC binodex - {error}')
        return False


async def reload_otc_page(manager: "BrowserManager") -> bool:
    """Перезагрузка binodex перед каждым новым опционом (вызов из main_app). binodex
    периодически выкатывает новую версию фронта и показывает баннер «Доступна новая версия.
    Обновите страницу», зависая на сплеше при ЖИВЫХ URL (/trade держится), UI и WS — отвал-кук-
    детект (on_trade/_ui_loaded/feed_dead) такое НЕ ловит. Регулярный reload подхватывает новую
    версию заранее, до того как чарт зависнет. WS-перехват НЕ переустанавливаем: page.on('websocket')
    переживает reload (повторная подписка задвоила бы хендлеры), старый WS закроется → новый
    откроется → трекер сам перецепится.
    :return: True — UI снова готов к скрину; False — не поднялся (вызывающий уйдёт в exit_main →
    main-цикл по otc_session_dead пересоздаст браузер)."""
    page = manager.pages.get('main')
    if page is None:
        return False
    try:
        await page.reload(wait_until='domcontentloaded', timeout=TIMEOUT_LONG)
    except (Exception,) as error:
        logger.warning(f'OTC: reload страницы перед опционом не удался - {error}')
        return False
    try:
        await page.wait_for_load_state('networkidle', timeout=TIMEOUT_LONG)
    except (Exception,):
        pass  # постоянный WS-поток может мешать networkidle — не критично (как в init_otc)
    # Та же readiness-лестница, что и в init_otc, но мягкая (bool вместо CookiesExpired):
    # завис после reload — это не отвал кук, а недогруз фронта, лечится пересозданием браузера.
    if not on_trade(page.url):
        logger.warning(f'OTC: после reload редирект с /trade на {page.url}')
        return False
    try:
        await page.locator(otc_select_pair).first.wait_for(state='visible', timeout=TIMEOUT_LONG)
    except (Exception,):
        logger.warning('OTC: после reload не появилась кнопка выбора пары (завис на сплеше)')
        return False
    if not await _ui_loaded(page, UI_READY_TIMEOUT):
        logger.warning('OTC: после reload нет кнопки настроек аккаунта (завис на сплеше)')
        return False
    tracker = get_price_tracker()
    for _ in range(20):  # ждём переподключения WS-котировок (до 10 сек), как в init_otc
        if tracker.ws_connected and tracker.prices:
            break
        await asyncio.sleep(0.5)
    logger.info('🔄 OTC: страница перезагружена перед опционом — UI готов')
    return True


OTC_WS_SILENCE_LIMIT = 30  # сек без тика при закрытом WS = мёртвый фид (внутренний тайминг)


async def otc_session_dead(manager: "BrowserManager") -> tuple[bool, str]:
    """Рантайм-детект отвала OTC-сессии (§4.4). Три сигнала:
      (a) редирект с /trade — Privy storage_state протух (основной, URL-детект);
      (b) торговый UI пропал — нет кнопки настроек аккаунта при живом URL/WS (Privy-токен
          залип без редиректа: тулбар отрисован частично, /trade держится, котировок-WS стримит
          все пары → (a) и (c) молчат, но страница свалилась на сплеш);
      (c) WS-фид котировок мёртв — токен WS мог протухнуть без редиректа страницы
          (дополняет (a); точнее и раньше, чем ждать сбоя данных).
    Возвращает (dead, reason) — reason для лога вызывающим."""
    page = manager.pages.get('main')
    if page is not None:
        try:
            if not on_trade(page.url):
                return True, 'редирект с /trade (Privy storage_state протух)'
        except (Exception,):
            pass
        # На живом графике кнопка настроек видна сразу (нет ложняка); нет её весь
        # UI_DEAD_CONFIRM — страница реально свалилась на сплеш.
        if not await _ui_loaded(page, UI_DEAD_CONFIRM):
            return True, 'торговый UI пропал — завис на сплеше (нет кнопки настроек, storage_state протух)'
    if get_price_tracker().feed_dead(OTC_WS_SILENCE_LIMIT):
        return True, f'WS-фид котировок мёртв (закрыт, нет тика > {OTC_WS_SILENCE_LIMIT}с)'
    return False, ''
