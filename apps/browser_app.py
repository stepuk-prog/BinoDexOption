import asyncio

from playwright.async_api import async_playwright, BrowserContext, Page

from classes.browser_manager import BrowserManager
from classes.exceptions import CookiesExpired
from apps.exit_app import close_program
from apps.otc_app import open_otc_browser
from logs import init_logger
from settings import win_x, win_y
from settings.browser_set import browser_launch_options, context_options
from settings.browser_config import tf_menu, tf_link, search_val, symbol, \
    tf_link_price, pop_up2, pop_up3, scope_chip
from settings.config import (cookies, database, binary, prog_key, cookies_tv_id,
                             cookies_pocket_id)
from apps.cookie_utils import add_cookies_to_context
from settings.timing import (
    POPUP_SETTLE_DELAY, ELEMENT_RETRY_DELAY,
    TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_EXTRA_LONG
)
from classes.result_types import BrowserInitResult, OperationResult

logger = init_logger(__name__)


def _is_signin_url(url: str) -> bool:
    """TradingView редиректит неавторизованных на /signin — детерминированный детект
    отвала cookies (§4.1, основной сигнал; проверяется на init после goto+reload)."""
    return '/signin' in url or '/accounts/signin' in url


def setup_dialog_handler(page: Page):
    """Автоматическое закрытие JavaScript диалогов (alert, confirm, prompt)"""
    async def handle_dialog(dialog):
        logger.debug(f"🔔 Автозакрытие диалога: {dialog.type} - {dialog.message}")
        await dialog.dismiss()
    page.on('dialog', handle_dialog)


def setup_popup_blocker(context: BrowserContext, manager: 'BrowserManager'):
    """Автоматическое закрытие неожиданных всплывающих окон (новых вкладок)"""
    async def handle_popup(page: Page):
        # Если страница не зарегистрирована в manager.pages - это неожиданный popup
        await asyncio.sleep(POPUP_SETTLE_DELAY)
        if page not in manager.pages.values() and not page.is_closed():
            url = page.url
            logger.debug(f"🚫 Закрытие popup окна: {url}")
            try:
                await page.close()
            except (Exception,) as error:  # гонка: popup мог закрыться сам — не роняем event-колбэк
                logger.debug(f"Popup закрытие (best-effort): {error}")
    context.on('page', handle_popup)


async def close_dom_popups(page: Page):
    """Закрытие всплывающих окон TradingView по селекторам из БД и конкретным селекторам"""
    # Попапы из БД (pop_up2, pop_up3) — значения из БД это частичные имена классов
    for selector in [pop_up2, pop_up3]:
        try:
            await page.locator(f"[class*='{selector}']").first.click(timeout=2000)
            logger.debug(f"🔕 Закрыт попап: {selector}")
        except (Exception,):
            pass

    # Модальное окно "Easter sale" — ждём кнопку закрытия до 5 сек
    try:
        close_btn = page.locator("button[class*='closeButton']")
        await close_btn.first.wait_for(state='visible', timeout=5000)
        await close_btn.first.click()
        logger.debug("🔕 Закрыто модальное окно")
    except (Exception,):
        pass

    # Тост-уведомление "Easter sale ждёт"
    try:
        toast_close = page.locator("[class*='toastCommonBase'] [class*='closeButton']")
        await toast_close.first.wait_for(state='visible', timeout=2000)
        await toast_close.first.click()
        logger.debug("🔕 Закрыт тост")
    except (Exception,):
        pass


# JavaScript для подавления всплывающих окон TradingView
TV_POPUP_SUPPRESS_JS = """
() => {
    const closeSelectors = [
        'button[class*="closeButton"]',
        'button[class*="close-button"]',
        '[aria-label="Close"]',
        '[aria-label="Закрыть"]',
    ];

    // Попытка закрыть попап внутри элемента
    const tryClose = (el) => {
        for (const sel of closeSelectors) {
            const btn = el.querySelector(sel);
            if (btn) {
                try { btn.click(); } catch(e) {}
                try { el.remove(); } catch(e) {}
                return true;
            }
        }
        return false;
    };

    // Проверка: является ли элемент промо/модальным попапом
    const isPopup = (el) => {
        const cls = el.className || '';
        return /modal-|dialog-|toast/i.test(cls) && !/menu|dropdown/.test(cls);
    };

    // MutationObserver на #overlap-manager-root — мгновенная реакция
    const watchOverlap = () => {
        const root = document.getElementById('overlap-manager-root');
        if (!root) return false;

        const observer = new MutationObserver((mutations) => {
            for (const m of mutations) {
                for (const node of m.addedNodes) {
                    if (!(node instanceof HTMLElement)) continue;
                    // Ищем попап в добавленном узле или среди его потомков
                    if (isPopup(node)) {
                        tryClose(node);
                    } else {
                        node.querySelectorAll('[class*="modal-"], [class*="dialog-"], [class*="toast"]').forEach(el => {
                            if (isPopup(el)) tryClose(el);
                        });
                    }
                }
            }
        });
        observer.observe(root, { childList: true, subtree: true });
        return true;
    };

    // Пробуем подключить observer сразу, если DOM ещё не готов — ждём
    if (!watchOverlap()) {
        const wait = setInterval(() => {
            if (watchOverlap()) clearInterval(wait);
        }, 200);
    }
}
"""

# JavaScript для маскировки автоматизации (Firefox-совместимый)
STEALTH_JS = """
() => {
    // Firefox: удаляем webdriver из прототипа Navigator
    try {
        delete Navigator.prototype.webdriver;
    } catch (e) {}

    // Переопределяем webdriver на уровне прототипа
    try {
        Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: () => undefined,
            configurable: true
        });
    } catch (e) {}

    // Дополнительно на экземпляре navigator
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true
        });
    } catch (e) {}

    // Firefox: создаём реалистичный PluginArray
    const makePluginArray = () => {
        const plugins = [
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
            { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
            { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
            { name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1 },
            { name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: '', length: 1 }
        ];

        const pluginArray = Object.create(PluginArray.prototype);
        plugins.forEach((p, i) => {
            const plugin = Object.create(Plugin.prototype);
            Object.defineProperties(plugin, {
                name: { value: p.name, enumerable: true },
                filename: { value: p.filename, enumerable: true },
                description: { value: p.description, enumerable: true },
                length: { value: p.length, enumerable: true }
            });
            pluginArray[i] = plugin;
        });
        Object.defineProperty(pluginArray, 'length', { value: plugins.length, enumerable: true });
        pluginArray.item = (i) => pluginArray[i] || null;
        pluginArray.namedItem = (name) => plugins.find(p => p.name === name) || null;
        pluginArray.refresh = () => {};

        return pluginArray;
    };

    try {
        Object.defineProperty(Navigator.prototype, 'plugins', {
            get: makePluginArray,
            configurable: true
        });
    } catch (e) {}

    // Languages
    try {
        Object.defineProperty(Navigator.prototype, 'languages', {
            get: () => ['ru-RU', 'ru', 'en-US', 'en'],
            configurable: true
        });
    } catch (e) {}

    // Permissions query
    try {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );
    } catch (e) {}

    // Hardware concurrency (не меняем если реальное значение выше)
    // deviceMemory - только для Chrome, Firefox его не имеет

    // Screen colorDepth
    try {
        Object.defineProperty(Screen.prototype, 'colorDepth', {
            get: () => 24,
            configurable: true
        });
    } catch (e) {}
}
"""


async def init_browser(storage_state=None) -> BrowserInitResult:
    """Инициализация браузера Playwright.
    storage_state — свежий OTC Privy-стейт из БД (Survive §4.3); None → фоллбэк на
    import-снимок cookies (для probe-скриптов, что зовут init_browser() без БД-перечитки)."""
    state = storage_state if storage_state is not None else cookies
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.firefox.launch(**browser_launch_options)
        # OTC (binodex): контекст со storage_state (Privy держит сессию в localStorage,
        # одних cookies мало). FIN (TV): обычный контекст, куки добавляются позже add_cookies.
        if not binary and isinstance(state, dict):
            # state здесь — storage_state-dict из jsonb (Playwright принимает обычный dict);
            # тип StorageState — TypedDict, поэтому инспекцию типа подавляем.
            # noinspection PyTypeChecker
            context = await browser.new_context(storage_state=state, **context_options)
        else:
            context = await browser.new_context(**context_options)

        # Добавляем stealth скрипт на уровне контекста (для всех страниц)
        await context.add_init_script(STEALTH_JS)
        # Подавление всплывающих окон TradingView
        await context.add_init_script(TV_POPUP_SUPPRESS_JS)

        page = await context.new_page()
        await page.set_viewport_size({'width': win_x, 'height': win_y})

        manager = BrowserManager(
            browser=browser,
            context=context,
            pages={'main': page},  # первая страница всегда 'main'
            playwright=pw
        )

        # Подключаем автоматическое подавление всплывающих окон
        setup_dialog_handler(page)
        setup_popup_blocker(context, manager)

        return BrowserInitResult(success=True, manager_or_error=manager)
    except (Exception,) as error:
        # Подчищаем частично поднятое, чтобы не оставить осиротевший Firefox-процесс
        try:
            if browser:
                await browser.close()
        except (Exception,):
            pass
        try:
            if pw:
                await pw.stop()
        except (Exception,):
            pass
        return BrowserInitResult(success=False, manager_or_error=f"Ошибка подключения браузера - {error}")


async def open_tv_browser(manager: BrowserManager, cookies_override=None):
    """
    Загрузка браузера по cookies для TradingView
    :param manager: менеджер браузера
    :param cookies_override: свежие TV-куки из БД (Survive §4.3); None → import-снимок cookies
    :return: tuple (success, error_message)
    """
    tv_cookies = cookies_override if cookies_override is not None else cookies
    # Страницы TV из общей binodex.cookies.pages по (program, mode='tv').
    # Таблица содержит только нужные страницы (main, price) в порядке order_idx —
    # main идёт первой (idx == 0), все скриншоты снимаются с неё.
    list_screen = await database.pages(program=prog_key, mode='tv')
    if not list_screen:  # False (сбой БД) или пусто — без страниц браузер не поднять
        await close_program(manager=manager, status=1,
                            text='Не удалось получить страницы браузера из БД')
        return OperationResult(success=False)

    for idx, page_data in enumerate(list_screen):
        page_name = page_data['description']  # ключ из БД: main, price

        if idx == 0:
            # Первая страница - используем существующую (уже 'main')
            page = manager.pages['main']
            try:
                await page.goto(page_data['url'], wait_until='domcontentloaded', timeout=TIMEOUT_EXTRA_LONG)

                # Добавляем cookies (свежие из БД — Survive §4.3)
                await add_cookies_to_context(manager.context, tv_cookies)
                # NB: проактивный TTL (§4.4a) для TV здесь НЕ делаем — у TV-кук этого деплоя
                # `expires` уже в прошлом, а сессия живёт (TV держит её server-side/sliding),
                # т.е. срок в куке не отражает жизнь сессии (тот же капкан, что у Privy) →
                # давал ложные «истекла». Реальную смерть TV-кук ловит реактивный /signin-детект.

                await page.reload(wait_until='domcontentloaded', timeout=TIMEOUT_EXTRA_LONG)
            except (Exception,) as error:
                await close_program(manager=manager, status=1,
                                    text=f'Ошибка загрузки страницы {page_data["url"]} - {error}')
                return OperationResult(success=False)

            # Отвал cookies TV (§4.1/§4.3): после goto+reload остались на /signin → куки
            # мертвы. CookiesExpired → init_load → _init_with_retry (backoff + пересоздание,
            # БЕЗ выхода; куки перечитаются из БД на следующем init).
            if _is_signin_url(page.url):
                raise CookiesExpired(f'TradingView: редирект на /signin ({page.url}) — куки протухли')
        else:
            # Открываем новую вкладку через JavaScript
            try:
                current_page = manager.pages['main']

                # Ожидаем новую страницу и открываем её одновременно
                async with manager.context.expect_page(timeout=TIMEOUT_EXTRA_LONG) as new_page_info:
                    # URL передаём аргументом, а не в строку JS — кавычка в URL не сломает evaluate
                    await current_page.evaluate("u => window.open(u)", page_data['url'])

                page = await new_page_info.value
                manager.pages[page_name] = page  # СРАЗУ регистрируем, чтобы handle_popup не закрыл
                await page.wait_for_load_state('domcontentloaded', timeout=TIMEOUT_MEDIUM)
                await page.set_viewport_size({'width': win_x, 'height': win_y})
                setup_dialog_handler(page)
            except (Exception,) as error:
                await close_program(manager=manager, status=1,
                                    text=f'Ошибка загрузки страницы {page_data["url"]} - {error}')
                return OperationResult(success=False)

        page = manager.pages[page_name]
        await page.bring_to_front()

        # Закрытие всплывающих DOM-окон
        await close_dom_popups(page)

        # Настройка таймфрейма (грузим только main и price). Категорию и актив
        # ставит init_valute_browser позже — приминг поиска символа здесь не нужен.
        tek_frame = tf_link_price if page_data['description'] == 'price' else tf_link

        try:
            await page.locator(f"xpath={tf_menu}").first.click(force=True, timeout=TIMEOUT_MEDIUM)
            await page.locator(f"xpath={tek_frame}").first.click(force=True, timeout=TIMEOUT_MEDIUM)
        except (Exception,) as error:
            await close_program(manager=manager, status=1,
                                text=f'Не могу переключить таймфрейм для страницы {page_data["url"]} - {error}')
            return OperationResult(success=False)

    # Закрытие попапов на всех страницах (попапы уже гарантированно появились)
    for page_name, page in manager.pages.items():
        await page.bring_to_front()
        await close_dom_popups(page)

    logger.report("✅ open_tv_browser завершён, страницы: %s", list(manager.pages.keys()))
    return OperationResult(success=True)


async def _reset_search_category(page) -> None:
    """Сброс категории поиска символа на «Все» (первая вкладка).
    TV запоминает выбранную категорию между открытиями — иначе пара другого типа
    может не найтись. Первая вкладка — «Все» во всех локалях."""
    try:
        tab = page.locator('#symbol-search-tabs button[role="tab"]').first
        await tab.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
        if await tab.get_attribute('aria-selected') != 'true':
            await tab.click(timeout=TIMEOUT_MEDIUM)
            # auto-wait вместо слепой паузы: ждём, пока вкладка реально станет выбранной
            await page.locator('#symbol-search-tabs button[role="tab"][aria-selected="true"]') \
                .first.wait_for(state='visible', timeout=TIMEOUT_SHORT)
    except (Exception,) as e:
        logger.warning(f"Не удалось сбросить категорию поиска TV: {e}")


async def _remove_fxcm_chip(page) -> None:
    """Снятие вторичного чипа биржи (FXCM-scope) в диалоге поиска. TV запоминает scope
    после выбора FXCM-символа и иначе отсеивает поиск по формату EXCHANGE:SYMBOL.
    Без падений (если чипа нет — просто выходим)."""
    chip = page.locator(scope_chip).first
    try:
        if await chip.count() > 0 and await chip.is_visible():
            # Сначала крестик × внутри чипа, иначе повторный клик по чипу снимает фильтр
            try:
                await chip.locator("button").first.click(timeout=TIMEOUT_SHORT)
            except (Exception,):
                await chip.click(timeout=TIMEOUT_SHORT)
    except (Exception,) as e:
        logger.warning(f"Не удалось снять FXCM-чип поиска TV: {e}")


async def _click_fxcm_pair(page, pair: str) -> bool:
    """Клик по FXCM-строке в диалоге поиска по data-symbol-name="FX:<pair>" + фолбэки.
    Строки рендерятся через overlap-manager-root → ищем на уровне page; visibility
    у строк TV нестабилен → ждём attached и пробуем несколько стратегий клика."""
    candidates = [
        page.locator(f'[data-symbol-name="FX:{pair}"]').first,
        page.locator(
            f'[data-name="symbol-search-dialog-content-item"]:has([title="FXCM"]):has-text("{pair}")'
        ).first,
        page.locator(f'div[class*="itemRow"]:has([title="FXCM"]):has-text("{pair}")').first,
    ]
    for loc in candidates:
        try:
            await loc.wait_for(state='attached', timeout=TIMEOUT_SHORT)
        except (Exception,):
            continue
        try:
            await loc.scroll_into_view_if_needed(timeout=1500)
        except (Exception,):
            pass
        for strategy in ('normal', 'force', 'js'):
            try:
                if strategy == 'normal':
                    await loc.click(timeout=2000)
                elif strategy == 'force':
                    await loc.click(timeout=2000, force=True)
                else:
                    await loc.evaluate('el => el.click()')
                return True
            except (Exception,):
                continue
    return False


async def init_valute_browser(manager: BrowserManager, valute: str):
    """
    Настройка валюты в окне браузера (TradingView, котировки FXCM).
    :param manager: менеджер браузера
    :param valute: название валютной пары (например 'EURUSD')
    """
    pair = valute.replace('/', '').replace('FX:', '').upper()
    try:
        for page_name, page in manager.pages.items():
            logger.info(f"🔄 Переключение валюты на странице: {page_name}")
            await page.bring_to_front()
            await page.wait_for_load_state('domcontentloaded', timeout=TIMEOUT_MEDIUM)
            await close_dom_popups(page)

            # Открыть поиск символа (force-фолбэк на случай перехвата клика оверлеем)
            symbol_btn = page.locator(f"#{symbol}").first
            for attempt in range(3):
                try:
                    await symbol_btn.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
                    await symbol_btn.click(timeout=TIMEOUT_MEDIUM)
                    break
                except (Exception,) as e:
                    logger.warning(f"Попытка {attempt + 1}/3 клика по symbol: {e}")
                    await close_dom_popups(page)
                    await asyncio.sleep(ELEMENT_RETRY_DELAY)
            else:
                await symbol_btn.click(force=True, timeout=TIMEOUT_MEDIUM)

            # Сброс категории на «Все» (sticky-фильтр TV иначе ломает поиск).
            # Диалог дождётся через wait_for внутри — фиксированный sleep не нужен.
            await _reset_search_category(page)
            # И снятие FXCM-чипа: иначе scope от прошлого выбора отсеивает EXCHANGE:SYMBOL.
            await _remove_fxcm_chip(page)

            # Ввод символа в формате FX:<pair> (FXCM): exchange-префикс поднимает
            # нужный фид наверх вместо строк всех провайдеров.
            valute_input = page.locator(f".{search_val}").first
            await valute_input.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
            await valute_input.fill(f"FX:{pair}")

            # Клик по FXCM-строке по data-symbol-name; _click_fxcm_pair сам ждёт
            # появления строки (wait_for attached), доп. пауза после ввода не нужна.
            if not await _click_fxcm_pair(page, pair):
                await close_program(
                    manager=manager, status=1,
                    text=f"Ошибка загрузки данных в браузер - не найдена FXCM-строка FX:{pair}")
                return

            logger.info(f"✅ Валюта FX:{pair} установлена на странице {page_name}")
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f"Ошибка загрузки данных в браузер - {error}")


async def init_load() -> BrowserManager | bool:
    """
    Запуск загрузки и настройки браузера. Survive §4.3: куки перечитываются из БД на
    КАЖДОМ init — пересоздание браузера после отвала cookies подхватывает свежий refresh
    без рестарта процесса. CookiesExpired пробрасывается наружу (после cleanup) →
    main.py::_init_with_retry (backoff + повтор).
    :return: BrowserManager либо False
    """
    tv_override = None       # свежие TV-куки из БД (только в FIN-ветке; иначе не используется)
    storage_state = None     # свежий OTC storage_state из БД (только в OTC-ветке)
    if binary:
        fresh = await database.get_tv_cookies(cookies_tv_id)  # list[dict] | None | False
        tv_override = fresh if fresh else None                # DB-сбой/пусто → import-снимок
    else:
        fresh = await database.get_otc_cookies(cookies_pocket_id)  # storage_state dict | None | False
        storage_state = fresh if fresh else cookies                # DB-сбой → import-снимок
        if not storage_state:
            logger.error('Нет storage_state OTC (ни в БД, ни в import-снимке) — init провалился')
            return False

    result = await init_browser(storage_state=storage_state)
    if not result.success:
        logger.error(result.manager_or_error)
        return False

    manager = result.manager

    try:
        if binary:
            browser_result = await open_tv_browser(manager, cookies_override=tv_override)
        else:
            browser_result = await open_otc_browser(manager)
    except CookiesExpired:
        await manager.close()  # cleanup перед пробросом — не оставить осиротевший Firefox
        raise

    if not browser_result.success:
        logger.error(browser_result.error)
        return False

    return manager
