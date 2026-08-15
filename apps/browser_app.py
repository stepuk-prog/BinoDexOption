
import asyncio
import os
import re
import time

from playwright.async_api import async_playwright, BrowserContext, Page

from classes.browser_manager import BrowserManager
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from apps.exit_app import close_program
from apps.otc_app import open_otc_browser
from logs import init_logger
from settings import win_x, win_y
from settings.browser_set import browser_launch_options, context_options, chromium_launch_options
from settings.browser_config import tf_menu, tf_link, search_val, symbol, \
    tf_link_price, pop_up2, pop_up3, scope_chip, panel_toggle, panel_wrap
from settings.config import (cookies, database, binary, browser_engine, prog_key, cookies_tv_id,
                             cookies_pocket_id)
from apps.cookie_utils import add_cookies_to_context
from settings.timing import (
    POPUP_SETTLE_DELAY, ELEMENT_RETRY_DELAY, EVAL_TIMEOUT,
    TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_EXTRA_LONG
)
from classes.result_types import BrowserInitResult, OperationResult

logger = init_logger(__name__)

# Грабли 2026-08-02 (node-6, лёг BinoStoch): Playwright ≥1.5x перепроверяет host-requirements,
# если маркеру <build>/DEPENDENCIES_VALIDATED больше 30 дней (kMaximumReValidationPeriod).
# Проверка ОБХОДИТ каталог билда и stat-ит каждую запись, а `<build>/firefox/lock` — симлинк на
# "<ip>:+<pid>", висячий ПО ДИЗАЙНУ: его оставляет любой запущенный Firefox, в т.ч. ДРУГОГО бота
# на той же ноде (кэш ~/.cache/ms-playwright общий для всего флота). Итог: `ENOENT ... stat
# '.../firefox/lock'` на КАЖДЫЙ launch, маркер не обновляется → нода залипает навсегда.
# Лечится снятием симлинка: владельцу он после старта не нужен (читается только при старте,
# профиль Playwright лежит в /tmp). Флотовый свип того же класса — pw_lock_sweep.sh
# (ExecStartPre юнитов + ночной таймер pw-lock-sweep.timer, ставит DeployManager).
_PW_STALE_LOCK_RE = re.compile(r"ENOENT.*?stat '([^']*/(?:firefox|chrome-linux\d*)/lock)'")


def _heal_stale_pw_lock(error_text: str) -> bool:
    """Снять висячий lock-симлинк из каталога билда Playwright. True — сняли, есть смысл в ретрае."""
    match = _PW_STALE_LOCK_RE.search(error_text)
    if not match:
        return False
    lock_path = match.group(1)
    # Только висячий СИМЛИНК: обычный файл/живая цель — не наш случай, руками не трогаем.
    if not os.path.islink(lock_path) or os.path.exists(lock_path):
        return False
    try:
        os.unlink(lock_path)
    except OSError as error:
        logger.error(f'Висячий Playwright-lock {lock_path} не снялся: {error}')
        return False
    logger.report(f'Снят висячий Playwright-lock {lock_path} (блокировал запуск браузера) — повтор запуска')
    return True


async def launch_healing_stale_lock(launcher, **launch_kwargs):
    """`launcher.launch(...)` с ОДНИМ ретраем после снятия висячего Playwright-lock (см. выше).
    Не наш случай → исключение пробрасывается как было."""
    try:
        return await launcher.launch(**launch_kwargs)
    except (Exception,) as error:
        if not _heal_stale_pw_lock(str(error)):
            raise
        return await launcher.launch(**launch_kwargs)


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
        # Если страница не зарегистрирована в manager.pages - это неожиданный popup.
        # Весь колбэк best-effort: event-хендлер НЕ должен бросать в диспетчер Playwright
        # (иначе «Task exception was never retrieved») — page.url/is_closed на гонке/
        # disposed-странице тоже могут кинуть, поэтому try охватывает всё тело.
        try:
            await asyncio.sleep(POPUP_SETTLE_DELAY)
            if page not in manager.pages.values() and not page.is_closed():
                logger.debug(f"🚫 Закрытие popup окна: {page.url}")
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


# JavaScript для подавления всплывающих окон TradingView.
# ВАЖНО — IIFE, а не голая `() => {...}`: add_init_script в Python-биндинге отдаёт исходник КАК ЕСТЬ
# (в отличие от evaluate, который сам вызывает функцию), поэтому стрелочная функция лишь вычислялась в
# значение и НИКОГДА не выполнялась — скрипт инжектился, но не работал. Обёртка (…)() запускает тело и
# держит свои const'ы в собственной области видимости (не течём в глобалы страницы).
TV_POPUP_SUPPRESS_JS = """
(() => {
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
})();
"""

# JavaScript для маскировки автоматизации (Firefox-совместимый).
# IIFE — по той же причине, что и у TV_POPUP_SUPPRESS_JS (см. комментарий выше): без вызова тело не
# выполнялось, т.е. маскировка не применялась вообще (navigator.webdriver оставался true).
STEALTH_JS = """
(() => {
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
})();
"""


async def _proxy_launch_options(chromium: bool = False) -> dict:
    """launch-опции + :50100-HTTP-прокси из binodex.settings.proxy_data. Chromium (binodex) умеет
    http-proxy с auth НАТИВНО (username/password в опции proxy) — local-relay не нужен. Firefox (TV)
    socks5/http-auth напрямую не жуёт → через локальный релей (settings/local_proxy). Выбранный прокси
    запоминается в settings.proxy.current_proxy — main по нему ведёт stats/ban. Сбой подбора/релея →
    базовые опции (без прокси): init упадёт штатно, main забанит/повернёт."""
    from settings.proxy import load_proxies_from_db, get_unused_proxy, proxy_list, PROXY_SCOPE
    base = chromium_launch_options if chromium else browser_launch_options
    if not proxy_list:
        await load_proxies_from_db(database)
    proxy = get_unused_proxy()
    if not proxy:
        logger.error(f'Прокси({PROXY_SCOPE}): нет активных :50100 (settings.proxy_data) — поднимаю напрямую')
        return base
    opts = base.copy()
    if chromium:
        # Chromium: нативный proxy-auth, без релея.
        server = {'server': f'http://{proxy.ip}:{proxy.port}'}
        if proxy.login and proxy.password:
            server['username'] = proxy.login
            server['password'] = proxy.password
        opts['proxy'] = server
        logger.report(f'Прокси({PROXY_SCOPE}): Chromium через {proxy.ip}:{proxy.port} (нативный auth)')
        return opts
    # Firefox (TV): через локальный релей (Playwright-Firefox не жуёт http-auth напрямую).
    from settings.local_proxy import start_local_proxy
    if proxy.login and proxy.password:
        # start_local_proxy синхронный (time.sleep + socket.connect до ~3.3с) → в тред, иначе
        # блокировал бы event loop (WS-колбэки, обработчик SIGTERM) на всё окно старта релея.
        host, port = await asyncio.to_thread(
            start_local_proxy, proxy.ip, proxy.port, proxy.login, proxy.password)
        if not host:
            logger.error(f'Прокси({PROXY_SCOPE}): локальный релей для {proxy.ip} не поднялся — поднимаю напрямую')
            return browser_launch_options
        opts['proxy'] = {'server': f'http://{host}:{port}'}
        logger.report(f'Прокси({PROXY_SCOPE}): браузер через {proxy.ip}:{proxy.port} (релей {host}:{port})')
    else:
        opts['proxy'] = {'server': f'http://{proxy.ip}:{proxy.port}'}
        logger.report(f'Прокси({PROXY_SCOPE}): браузер через {proxy.ip}:{proxy.port}')
    return opts


# Static-именованные entry-файлы binodex (app.js/app.css) на любом поддомене binodex.app.
_BINODEX_APPJS_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)?binodex\.app/assets/app\.(?:js|css)")

# Навигации-перезагрузки cache-buster'а самого binodex: ?boot-recovery=<ts> / ?chunk-recovery=<ts>.
_BINODEX_RECOVERY_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?binodex\.app/[^?]*\?(?:[^#]*&)?(?:boot|chunk)-recovery=")


async def _bust_binodex_cdn(context) -> None:
    """Обойти два дефекта фронта binodex.app в Playwright-Firefox (свежий профиль без кэша).

    1) ПРОТУХШИЙ CDN-КЭШ: эдж отдаёт устаревший `/assets/app.js` (static-имя, cf-cache HIT ~сутки),
       ссылающийся на удалённый чанк → 404 с MIME text/plain → Firefox блокирует ES-модуль → SPA не
       бутстрапится. Добавляем cache-bust query к static-entry (app.js/app.css) → CF MISS → свежий
       app.js с живыми чанками. Хэш-чанки иммутабельны — не трогаем.

    2) ЦИКЛ CHUNK-RECOVERY (инцидент 2026-07-20): binodex ловит сбой динамического import()
       chunk-error-хендлером и перезагружает страницу с `?chunk-recovery=<ts>`/`?boot-recovery=<ts>`
       (cache-buster). В Firefox (и десктопном, и Playwright — в Chrome бага НЕТ) один чанк/ресурс
       (вероятно ресурс Privy под Cloudflare-челленджем) стабильно «падает» → хендлер перезагружает
       УЖЕ РАБОЧУЮ страницу → перезагрузка обрывает запросы (NS_BINDING_ABORTED) → снова «сбой» →
       бесконечный self-reload, SPA не оседает. Проверено: если ГЛУШИТЬ эти навигации-перезагрузки,
       страница остаётся смонтированной и торговый UI /trade поднимается штатно. Поэтому abort'им
       top-level навигации с recovery-параметром — рабочая страница не сносится. Хостовый фильтр:
       только binodex, TradingView-режим (FIN) не задет.
    """
    token = str(int(time.time()))

    async def _cb(route):
        url = route.request.url
        sep = "&" if "?" in url else "?"
        try:
            await route.continue_(url=f"{url}{sep}_cb={token}")
        except (Exception,):   # старый Playwright без override url — не ломаем загрузку
            await route.continue_()

    async def _kill_recovery(route):
        # Глушим ТОЛЬКО top-level навигацию-перезагрузку (не ресурсные запросы с тем же параметром):
        # abort оставляет уже отрисованную страницу живой. Не-навигацию пропускаем без изменений.
        req = route.request
        try:
            if req.is_navigation_request():
                await route.abort()
                return
        except (Exception,):
            pass
        await route.continue_()

    await context.route(_BINODEX_APPJS_RE, _cb)
    await context.route(_BINODEX_RECOVERY_RE, _kill_recovery)


def _use_chromium() -> bool:
    """Выбор движка. env BROWSER форсит явно (firefox|chromium); при auto (дефолт) — по режиму:
    binodex (OTC, not binary) → Chromium, TV (binary) → Firefox. Поведение фронта binodex скачет
    между движками (грабли 2026-07-20 / цикл ?boot-recovery) — BROWSER переключает без правок кода.
    См. settings.config.browser_engine."""
    if browser_engine == "chromium":
        return True
    if browser_engine == "firefox":
        return False
    return not binary


async def init_browser(storage_state=None, use_proxy: bool = False) -> BrowserInitResult:
    """Инициализация браузера Playwright.
    storage_state — свежий OTC Privy-стейт из БД (Survive §4.3); None → фоллбэк на
    import-снимок cookies (для probe-скриптов, что зовут init_browser() без БД-перечитки).
    use_proxy — OTC-фолбэк: поднять браузер через прокси из settings.proxy_data (когда прямой
    режим не поднял front-end binodex — напр. отравленный CDN-эдж). См. _proxy_launch_options."""
    state = storage_state if storage_state is not None else cookies
    # Движок: env BROWSER (auto|firefox|chromium). auto — по режиму: binodex (OTC) → Chromium
    # (фронт binodex не бутстрапился в Firefox — boot-recovery-цикл / Privy 403, грабли 2026-07-20);
    # TV (binary) → Firefox. См. _use_chromium.
    binodex = _use_chromium()
    launch_options = (await _proxy_launch_options(chromium=binodex) if use_proxy
                      else (chromium_launch_options if binodex else browser_launch_options))
    # Контекст: Chromium НЕ подменяем UA (нативный Chrome-UA; Firefox-UA палил бы automation и
    # рассинхронил client hints). Firefox — наш useragent из context_options.
    ctx_options = ({k: v for k, v in context_options.items() if k != 'user_agent'}
                   if binodex else context_options)
    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
        launcher = pw.chromium if binodex else pw.firefox
        browser = await launch_healing_stale_lock(launcher, **launch_options)
        # OTC (binodex): контекст со storage_state (Privy держит сессию в localStorage,
        # одних cookies мало). FIN (TV): обычный контекст, куки добавляются позже add_cookies.
        if not binary and isinstance(state, dict):
            # state здесь — storage_state-dict из jsonb (Playwright принимает обычный dict);
            # тип StorageState — TypedDict, поэтому инспекцию типа подавляем.
            # noinspection PyTypeChecker
            context = await browser.new_context(storage_state=state, **ctx_options)
        else:
            context = await browser.new_context(**ctx_options)
        # Потолки по умолчанию на весь контекст: вызовы БЕЗ явного timeout= (fill/click/
        # bounding_box в TV-флоу, ввод кода в otc_login) иначе полагаются на встроенные 30с
        # Playwright — вдвое-втрое больше, чем любой наш явный потолок. Навигации оставляем
        # 30с (goto/reload по факту всюду передают TIMEOUT_LONG/EXTRA_LONG явно).
        context.set_default_timeout(TIMEOUT_MEDIUM)
        context.set_default_navigation_timeout(TIMEOUT_EXTRA_LONG)
        await _bust_binodex_cdn(context)   # обойти протухший CDN-кэш binodex app.js (иначе пустая страница)

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
                    # URL передаём аргументом, а не в строку JS — кавычка в URL не сломает evaluate.
                    # Верхняя граница по времени: у evaluate нет встроенного таймаута.
                    await asyncio.wait_for(
                        current_page.evaluate("u => window.open(u)", page_data['url']), timeout=EVAL_TIMEOUT)

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

    # Закрытие попапов + сворачивание правой widget-панели на ВСЕХ страницах.
    # Панель в дефолте лэйаута раскрыта и съедает ~350px ширины чарт-зоны → скрин
    # (screen_zone) сужается; в лэйаут состояние НЕ персистится и сбрасывается на каждом
    # старте чистого контекста, поэтому сворачиваем в коде. Делаем на обеих вкладках
    # (не только на main, откуда скрин): TV может синхронизировать состояние панели между
    # вкладками сессии — «разбалансировка» (свёрнута на main, открыта на price) рискует тем,
    # что price переоткроет панель и она вернётся на main. _collapse_right_panel тоггл-safe.
    for page_name, page in manager.pages.items():
        await page.bring_to_front()
        await close_dom_popups(page)
        await _collapse_right_panel(page)

    logger.report("✅ open_tv_browser завершён, страницы: %s", list(manager.pages.keys()))
    return OperationResult(success=True)


async def _collapse_right_panel(page) -> None:
    """Свернуть правую widget-панель TradingView (вотчлист/«Детали») на странице графика.
    Кнопка тулбара (panel_toggle) — ТОГГЛ, поэтому сворачиваем ТОЛЬКО если панель реально
    раскрыта (иначе клик её, наоборот, откроет). Признак раскрытой: ширина panel_wrap
    ~346px (свёрнутая — полоска иконок ~45px) И наличие нажатой кнопки. Селекторы — из БД
    (tv_settings: panel_toggle/panel_wrap). Best-effort: влияет лишь на ширину кадра."""
    if not panel_toggle or not panel_wrap:
        return
    try:
        box = await page.locator(panel_wrap).first.bounding_box()
        if not box or box['width'] <= 100:  # свёрнута/отсутствует — не трогаем (не откроем!)
            return
        btn = page.locator(panel_toggle).first
        if await btn.count():
            await btn.click(timeout=TIMEOUT_MEDIUM)
            logger.info("Правая widget-панель TV свёрнута (была %dpx)", round(box['width']))
    except (Exception,) as e:
        logger.warning(f"Не удалось свернуть правую панель TV: {e}")


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


async def _remove_exchange_chip(page) -> None:
    """Снятие вторичного чипа биржи (exchange-scope) в диалоге поиска. TV запоминает scope
    после выбора символа с биржей и иначе отсеивает поиск по формату EXCHANGE:SYMBOL.
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
        logger.warning(f"Не удалось снять exchange-чип поиска TV: {e}")


async def _click_exchange_pair(page, pair: str, exchange: str) -> bool:
    """Клик по строке нужной биржи в диалоге поиска по data-symbol-name="<exchange>:<pair>"
    + фолбэки. exchange — TV-код биржи из БД (assets.binary_assets.exchange), напр. 'OANDA'.
    Строки рендерятся через overlap-manager-root → ищем на уровне page; visibility
    у строк TV нестабилен → ждём attached и пробуем несколько стратегий клика."""
    candidates = [
        page.locator(f'[data-symbol-name="{exchange}:{pair}"]').first,
        page.locator(
            f'[data-name="symbol-search-dialog-content-item"]:has([title="{exchange}"]):has-text("{pair}")'
        ).first,
        page.locator(f'div[class*="itemRow"]:has([title="{exchange}"]):has-text("{pair}")').first,
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
                    # evaluate без встроенного таймаута — оборачиваем верхней границей
                    await asyncio.wait_for(loc.evaluate('el => el.click()'), timeout=EVAL_TIMEOUT)
                return True
            except (Exception,):
                continue
    return False


async def init_valute_browser(manager: BrowserManager, valute: str, exchange: str = 'OANDA'):
    """
    Настройка валюты в окне браузера (TradingView).
    :param manager: менеджер браузера
    :param valute: название валютной пары (например 'EURUSD')
    :param exchange: TV-код биржи котировок из БД (assets.binary_assets.exchange), напр. 'OANDA'
    """
    pair = valute.replace('/', '').replace(f'{exchange}:', '').upper()
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
            # И снятие exchange-чипа: иначе scope от прошлого выбора отсеивает EXCHANGE:SYMBOL.
            await _remove_exchange_chip(page)

            # Ввод символа в формате <exchange>:<pair>: exchange-префикс поднимает
            # нужный фид наверх вместо строк всех провайдеров.
            valute_input = page.locator(f".{search_val}").first
            await valute_input.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
            await valute_input.fill(f"{exchange}:{pair}")

            # Клик по строке нужной биржи по data-symbol-name; _click_exchange_pair сам ждёт
            # появления строки (wait_for attached), доп. пауза после ввода не нужна.
            if not await _click_exchange_pair(page, pair, exchange):
                await close_program(
                    manager=manager, status=1,
                    text=f"Ошибка загрузки данных в браузер - не найдена строка {exchange}:{pair}")
                return

            logger.info(f"✅ Валюта {exchange}:{pair} установлена на странице {page_name}")
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f"Ошибка загрузки данных в браузер - {error}")


async def init_load(use_proxy: bool = False) -> BrowserManager | bool:
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

    # use_proxy — только OTC-фолбэк (FIN/TradingView — другой домен, не задет инцидентом)
    result = await init_browser(storage_state=storage_state, use_proxy=(use_proxy and not binary))
    if not result.success:
        logger.error(result.manager_or_error)
        return False

    manager = result.manager

    try:
        if binary:
            browser_result = await open_tv_browser(manager, cookies_override=tv_override)
        else:
            browser_result = await open_otc_browser(manager)
    except (CookiesExpired, FeedOutage, SetupError):
        await manager.close()  # cleanup перед пробросом — не оставить осиротевший Firefox
        raise

    if not browser_result.success:
        logger.error(browser_result.error)
        return False

    return manager
