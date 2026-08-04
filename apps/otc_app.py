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
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page, WebSocket

from classes.Option_class import Option
from classes.price_tracker import WebSocketPriceTracker, symbol_key
from classes.result_types import OperationResult
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from apps.exit_app import close_program
from apps.otc_login import otc_inline_login
from logs import init_logger
from settings.config import screenshot_path, database, prog_key, cookies_pocket_id
from settings.constant import bg_otc_color
from settings.timing import TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_LONG, MAX_SCREENSHOT_ATTEMPTS
from settings.screenshot_set import win_x_otc, win_y_otc, otc_qr_x, otc_qr_y, paste_overlay
from settings.browser_config import (otc_trade_url, otc_select_pair, otc_category_valute, otc_input_pair,
                                     otc_modal_pair_item, screen_zone_otc, otc_settings_btn, otc_login_email,
                                     otc_candle_scale, otc_candle_scale_item,
                                     otc_chart_scale, otc_chart_scale_item,
                                     otc_indicators_btn, otc_indicator_item, otc_indicator_name)

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

# WS котировок binodex. TLD-агностично: домен переехал api-coins.binodex.io → .app (грабли
# 2026-07-20, всплыло при переезде на Chromium — реальный WS теперь wss://api-coins.binodex.app/market/;
# Firefox до этого гейта не доходил). Иначе ws_connected не выставлялся → ложное «WS-токен протух».
PRICE_WS_HINT = "api-coins.binodex."  # WS котировок binodex (TLD-агностично: .io/.app)

# Цена графика прямо со страницы: движок binodex держит её в window.chartData = {symbol, price}.
# price — анимированное значение, которое рисуется на ярлыке (округляется до decimals в main_app).
CHART_DATA_JS = ("() => { const c = window.chartData;"
                 " return (c && typeof c.price === 'number')"
                 " ? { symbol: c.symbol, price: c.price } : null; }")
# Медиана нескольких быстрых чтений вокруг кадра гасит редкий анимационный выброс ярлыка
# (проверено: 3+3 чтения → 9/10 совпадений с нарисованным ценником; см. docs/BINODEX_PRICE.md).
CHART_READS_BEFORE = 3  # чтений chartData вплотную ДО screenshot
CHART_READS_AFTER = 3   # и сразу ПОСЛЕ
# Канвас на ~97% прозрачный даже с графиком (свечи/оси/часы ≈ 3% непрозрачных пикселей). Сразу
# после переключения пары канвас бывает пустым (свечи не дорисованы) — такой кадр не постим.
# Порог доли непрозрачных пикселей: ниже = «пусто» → ждём отрисовку (норм. график проходит с запасом).
CANVAS_MIN_OPAQUE = 0.005
CANVAS_READY_SECONDS = 6.0   # сколько ждать отрисовки свечей внутри попытки (отдельно от MAX_SCREENSHOT_ATTEMPTS)
# Кнопка настроек аккаунта (otc_settings_btn) есть в тулбаре ТОЛЬКО когда торговый UI полностью
# прогрузился. На сплеше (зависший Privy-токен без редиректа) её нет — хотя кнопка выбора пары
# присутствует, потому on_trade/UI-gate по ней и feed_dead (котировок-WS стримит все пары) сплеш
# не ловят. Отсутствие этой кнопки — точный DOM-маркер «завис на сплеше».
UI_READY_TIMEOUT = 15.0   # сек ждать кнопку настроек при загрузке (init_otc)
UI_DEAD_CONFIRM = 3.0     # сек подтверждения «UI пропал → сплеш» в рантайм-детекте (otc_session_dead)
# Зависший загрузочный сплеш binodex транзиентен: ~3% reload Privy/SPA не достраивается (#root
# пуст — только auth-iframe+лого, спиннер крутится вечно), следующий reload рендерится нормально.
# Поэтому reload_otc_page повторяет САМ reload, прежде чем отдать False (иначе бот зря уходит в
# пересоздание браузера / «нет пар»). Замер: 1/30 в scripts/probe_pair_modal.py (дамп splash_*).
RELOAD_RETRIES = 3        # попыток reload при не-готовности UI (зависший сплеш)
RELOAD_RETRY_PAUSE = 2.0  # сек между ретраями reload

logger = init_logger(__name__)

EVAL_TIMEOUT = 10.0  # сек: верхняя граница на evaluate/screenshot (у Playwright нет встроенного таймаута)


async def _eval(target, js, *args):
    """page/element.evaluate с верхней границей по времени (зависший рендер иначе вешает await навсегда)."""
    return await asyncio.wait_for(target.evaluate(js, *args), timeout=EVAL_TIMEOUT)


async def _shot(page, **kwargs):
    """page.screenshot с верхней границей по времени (как _eval — встроенного таймаута нет)."""
    return await asyncio.wait_for(page.screenshot(**kwargs), timeout=EVAL_TIMEOUT)


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
    """URL OTC-страницы — единый источник binodex_settings.trade_url
    (через browser_config.otc_trade_url), меняется в одном месте."""
    return otc_trade_url

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


_modal_diag_done = False  # подробный дамп модалки делаем один раз на процесс (см. _dump_pair_modal)


async def _modal_item_counts(page: Page) -> str:
    """Компактная диагностика для лога при промахе выбора пары: сколько пунктов матчит
    текущий селектор modal_pair_item и сколько из них содержат 'OTC'. Различает причины
    одинакового лога «не нашёл …»: items=0 → селектор отвалился (binodex сменил разметку);
    items>0, otc=0 → пункты есть, но OTC-вариантов сейчас нет; items>0, otc>0 → есть OTC,
    но фильтр has_text=pair не матчит (изменился формат текста, напр. слэш в паре)."""
    try:
        items = page.locator(otc_modal_pair_item)
        n = await items.count()
        otc = await items.filter(has_text=re.compile('OTC', re.IGNORECASE)).count()
        return f'items={n}, otc={otc}'
    except (Exception,) as err:
        return f'диаг-сбой:{err}'


# JS: для каждого ЛИСТОВОГО узла со словом 'OTC' в модалке вернуть цепочку предков (tag +
# «стабильное» ядро класса) до 6 уровней вверх — чтобы из лога подобрать новый селектор строки
# пары после ротации разметки binodex на CSS-modules. Ядро = класс без хеш-сегмента: режем
# хвост вида `_<хеш>` / `_<хеш>_<num>`, где хеш содержит цифру (`_futPerp_1wgz3_531` → `futPerp`,
# `_otcInlineBtn_1wgz3_32` → `otcInlineBtn`); семантические классы без хеша (`modal_pair_item`)
# не трогаем (в их хвосте нет цифры). Так в логе сразу виден кликабельный контейнер строки.
_DUMP_CHAIN_JS = r"""
() => {
  const core = (cn) => {
    const tok = ((typeof cn === 'string' ? cn : '').trim().split(/\s+/)[0]) || '';
    return tok.replace(/^_/, '').replace(/_(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,8}(_\d+)?$/, '');
  };
  const sel = (el) => {
    const c = core(el.className);
    return el.tagName.toLowerCase() + (c ? `[class*="${c}"]` : '');
  };
  const nodes = [...document.querySelectorAll('span, div, button, a, li')].filter(el => {
    const t = (el.innerText || '').trim();
    return t && t.length <= 40 && /OTC/i.test(t) && !el.querySelector('*');  // листовой узел
  });
  const out = [], seen = new Set();
  for (const n of nodes) {
    const chain = [];
    let el = n;
    for (let i = 0; i < 6 && el && el !== document.body; i++) { chain.push(sel(el)); el = el.parentElement; }
    const key = chain.join('<');
    if (seen.has(key)) continue;            // схлопываем одинаковые по структуре строки
    seen.add(key);
    const row = n.closest('button, a, li, [role="button"]') || n;
    out.push({ text: (row.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 50), chain });
    if (out.length >= 6) break;
  }
  return out;
}
"""


async def _dump_pair_modal(page: Page, phase: str) -> None:
    """Разовый (на процесс) подробный дамп разметки модалки выбора пары — для подбора нового
    селектора строки пары после ротации разметки binodex (CSS-modules с хешами в классах).
    Для каждого листового узла со словом 'OTC' печатает цепочку предков (tag + «стабильное» ядро
    класса без хеша) — из неё виден реальный кликабельный контейнер строки (кандидат в новый
    modal_pair_item). `phase` различает дамп ПОСЛЕ ввода пары в поиск (мог схлопнуться слэшем) и
    БЕЗ поиска (полный список — там и видна строка пары). Любые ошибки глушим — это диагностика."""
    try:
        old_cnt = await page.locator(otc_modal_pair_item).count()
        rows = await _eval(page, _DUMP_CHAIN_JS)
        logger.warning('OTC-DIAG [%s]: старый modal_pair_item=%s match, листовых узлов с OTC=%s',
                       phase, old_cnt, len(rows))
        for r in rows:
            logger.warning('OTC-DIAG [%s] «%s»: %s', phase, r['text'], ' < '.join(r['chain']))
        if not rows:
            logger.warning('OTC-DIAG [%s]: ни одного листового узла с OTC (список пуст/закрыт?)', phase)
    except (Exception,) as err:
        logger.warning('OTC-DIAG [%s] дамп модалки не удался: %s', phase, err)


async def select_otc_pair(page: Page, pair: str) -> bool:
    """Выбрать '<pair> OTC' в модалке binodex (pair вида 'EUR/USD').
    Открыть выбор → категория Валюты → ввести пару → клик по элементу '<pair> ... OTC' →
    закрыть модалку → дождаться, пока сайт прогрузит пару (WS отдаст котировку). True при успехе."""
    try:
        # Снять off-zone на время выбора: модалка выбора пары — вне зоны скрина, под off-zone
        # (visibility:hidden) её пункты не кликаются. off-zone ВОЗВРАЩАЕТСЯ в finally на любом исходе
        # (иначе для нерабочих пар и в 10-мин сне бот бы крутился на полном CPU).
        await _clear_offzone(page)
        await page.click(otc_select_pair, timeout=TIMEOUT_MEDIUM)
        await page.locator(otc_category_valute).first.wait_for(state='visible', timeout=TIMEOUT_MEDIUM)
        await page.click(otc_category_valute, timeout=TIMEOUT_MEDIUM)
        # input_pair = #input_pair — id теперь на самом <input>. fill() сам ждёт
        # его готовность (auto-wait) — отдельная пауза не нужна.
        inner = page.locator(otc_input_pair).first
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
            global _modal_diag_done
            logger.warning(f"OTC: не нашёл '{pair} … OTC' в модалке ({await _modal_item_counts(page)})")
            if not _modal_diag_done:  # подробный дамп — один раз на процесс, чтобы не флудить
                _modal_diag_done = True
                await _dump_pair_modal(page, 'после поиска')   # список, схлопнутый вводом '<pair>'
                # очищаем поиск → полный список (там видна строка пары) и дампим повторно:
                # различаем «селектор протух» (пусто и без поиска) vs «слэш схлопнул выдачу».
                try:
                    inner = page.locator(otc_input_pair).first
                    await inner.fill('', timeout=TIMEOUT_SHORT)
                    await asyncio.sleep(0.8)   # дать списку перерисоваться (one-shot диагностика)
                except (Exception,):
                    pass
                await _dump_pair_modal(page, 'без поиска')
            await _close_pair_modal(page)
            return False
        await target_item.click(timeout=TIMEOUT_SHORT)

        await asyncio.sleep(1.0)            # дать сайту переключить график (WS теперь стримит только выбранную пару)
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
    finally:
        # Слои кадра (подложка + off-zone) восстанавливаются на ЛЮБОМ исходе (успех/неудача/ошибка):
        # off-zone снимался в начале ради модалки, а подложку мог снести reload перед выбором пары.
        await _restore_frame_layers(page)


async def parce_otc(log_data: Option, manager: "BrowserManager", valute: list) -> bool:
    """Подобрать активную OTC-пару из БД и выбрать её на binodex.
    Сначала берём активные пары, исключая последние использованные (valute) — чтобы актив не
    повторялся в окне. Если после исключения кандидатов не осталось (узкий пул активных OTC на
    этом ТФ сузился до недавно использованных), повторяем запрос БЕЗ исключения — разрешаем
    повтор пары. Иначе бот ложно решил бы «пар нет» и ушёл бы в ожидание-простой, хотя пары
    на сайте есть (просто все недавно крутились). :return: True при успешном выборе."""
    page = manager.pages['main']
    active_otc_list = await database.option_data_pocket(exclude_ids=valute, tf=log_data.find_timeframe)
    if active_otc_list is False:  # ошибка пула (контракт execute_query) — не «нет пар»
        return False
    if not active_otc_list:  # пусто после исключения → разрешаем повтор недавних пар
        logger.info("OTC: активные пары исчерпаны исключением недавних — повторяю запрос с разрешением повтора")
        active_otc_list = await database.option_data_pocket(exclude_ids=[], tf=log_data.find_timeframe)
    if not active_otc_list:  # пусто и без исключения (нет активных пар на ТФ) либо ошибка пула
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
            data = await _eval(page, CHART_DATA_JS)
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


async def _login_modal_open(page: Page) -> bool:
    """True — на странице видна форма логина Privy (поле ввода почты login_email). При отвале кук
    binodex НЕ редиректит со /trade, а всплывает форма логина прямо на графике — это позитивный
    признак ОТВАЛА КУК, отличающий его от транзиентного сплеша (где формы нет, UI просто не достроен).
    Проверка мгновенная (is_visible, без ожидания) — вызывать ПОСЛЕ того, как UI не поднялся.
    Нет селектора в БД → False (детект деградирует к token/UI, без ложного рефреша)."""
    if not otc_login_email:
        return False
    try:
        return await page.locator(otc_login_email).first.is_visible()
    except (Exception,):
        return False


async def _app_shell_mounted(page: Page) -> bool:
    """Смонтирован ли торговый апп-шелл binodex (а не висящий загрузочный сплеш «лого+спиннер»).
    Маркер — кнопка выбора пары (otc_select_pair): в смонтированном /trade она есть, на сплеше
    (JS-бандл не поднялся) — нет. Отличает смену селектора настроек (апп смонтирован) от front-end
    аутэйджа binodex (апп не смонтировался). Короткий чек — длинные ожидания UI уже прошли выше."""
    try:
        await page.locator(otc_select_pair).first.wait_for(state='visible', timeout=2000)
        return True
    except (Exception,):
        return False


async def _privy_authenticated(page: Page) -> bool:
    """privy:token присутствует в localStorage = сессия Privy жива. Privy на буте САМ удаляет
    privy:token, если access-JWT протух, а обновить по privy:refresh_token не вышло → апп тихо
    уходит в Demo (без формы логина). Проверять ПОСЛЕ оседания UI: ранний гейт видит токен, только
    что восстановленный из storage_state, ещё до того как Privy его провалидирует и очистит."""
    try:
        return bool(await asyncio.wait_for(
            page.evaluate("() => !!localStorage.getItem('privy:token')"), timeout=5))
    except (Exception,):
        return False


async def _authed_safe(page: Page) -> bool:
    """privy:token жив? БЕЗОПАСНЫЙ дефолт True при сбое чтения (страница навигирует на boot-recovery/
    лендинг — eval может бросить): не прочитали токен → НЕ винить куки (не гнать релогин впустую).
    Отличается от _privy_authenticated (тот на любой сбой → False): здесь сбой чтения ≠ «токена нет».
    Нужен для трактовки редиректа с /trade — куки виним ТОЛЬКО когда токена достоверно нет."""
    try:
        return bool(await asyncio.wait_for(
            page.evaluate("() => !!localStorage.getItem('privy:token')"), timeout=5))
    except (Exception,):
        return True


async def _error_boundary_shown(page: Page) -> bool:
    """binodex показал React error-boundary («Something went wrong») — апп упал на буте. На
    битой/протухшей сессии Privy/инициализация бросает исключение → boundary, причём privy:token
    может ОСТАТЬСЯ (апп упал до его очистки), поэтому token-чек такой случай не ловит. Чистый
    контекст грузится без этого → трактуем как мёртвую сессию → релогин."""
    try:
        return bool(await asyncio.wait_for(page.evaluate(
            "() => (document.body.innerText || '').includes('Something went wrong')"), timeout=5))
    except (Exception,):
        return False


async def _raise_if_backend_down(detail: str) -> None:
    """Backend-аутэйдж binodex доминирует над всеми прочими причинами: если auth/config API
    (api.binodex.app) не отвечает (5xx/таймаут) браузер-фри — ни релогин, ни прокси, ни смена движка
    не помогут (Privy-логин и монтирование app-shell тянут ИМЕННО этот API). Кидаем FeedOutage →
    main выгружает браузер и ЖДЁТ восстановления браузер-фри (wait_for_feed→binodex_ready), без
    петли релогина/прокси-каруселей и без выхода. Проверяем ТОЛЬКО на диагностике сбоя (не на
    happy-path) — лишний сетевой пробой на каждом успешном init не нужен. Грабли 2026-07-23."""
    from apps.binodex_feed import api_alive  # лениво: модуль тянет browser_config (bootstrap)
    if not await api_alive():
        raise FeedOutage(f'binodex OTC: {detail} + auth-API api.binodex.app не отвечает (5xx/таймаут) '
                         f'браузер-фри — backend-аутэйдж binodex')


async def _raise_ui_dead(page: Page, detail: str) -> None:
    """UI не поднялся ИЛИ редирект с /trade — развести причину на классы (канон, docs/lifecycle-
    standard §4.5). Работает и на лендинге/boot-recovery (localStorage тот же origin, feed_alive
    браузер-фри, апп-шелл там не смонтирован → mounted=False). Всегда бросает:
      • видна форма логина → CookiesExpired (отвал кук → релогин);
      • формы нет, market-WS молчит браузер-фри (feed_alive=False) → FeedOutage (аутэйдж фида);
      • формы нет, фид ЖИВ, нет privy:token (Privy очистил → Demo) → CookiesExpired (сессия мертва, релогин);
      • формы нет, фид ЖИВ, токен ЕСТЬ, error-boundary «Something went wrong» → SetupError(mounted=False):
        front-end аутэйдж (JS-бандл/чанк не загрузился, напр. отравленный CDN-кэш) — релогин бесполезен,
        выживаем с бэкоффом, без выхода;
      • формы нет, фид ЖИВ, токен ЕСТЬ, апп-шелл СМОНТИРОВАН → SetupError(mounted=True): сменились
        селекторы → N ретраев → плановый выход;
      • формы нет, фид ЖИВ, токен ЕСТЬ, апп-шелл НЕ смонтировался (сплеш) → SetupError(mounted=False):
        front-end аутэйдж binodex → выживаем с бэкоффом, без выхода.
    ПЕРЕД всем этим: auth-API (api.binodex.app) 5xx браузер-фри → FeedOutage (backend-аутэйдж
    доминирует: релогин/прокси/движок бесполезны, пока API лежит; грабли 2026-07-23)."""
    await _raise_if_backend_down(detail)
    if await _login_modal_open(page):
        raise CookiesExpired(f'binodex OTC: {detail} + всплыла форма логина — куки протухли')
    from apps.binodex_feed import feed_alive  # лениво: модуль тянет browser_config (bootstrap)
    if not await feed_alive():
        raise FeedOutage(f'binodex OTC: {detail} + market-WS молчит браузер-фри — аутэйдж binodex')
    # За время ожидания UI binodex мог увести с /trade на ?boot-recovery=… / лендинг (само-сброс
    # фронта, когда апп-шелл не поднялся). Токен ЖИВ → это аутэйдж их фронта, НЕ куки (релогин
    # бесполезен) → SetupError(mounted=False): прокси-фолбэк + переподъём. Токена нет → сессия
    # реально протухла → CookiesExpired. authed — безопасный дефолт True (грабли 2026-07: boot-recovery).
    authed = await _authed_safe(page)
    if not page.url.rstrip('/').endswith('/trade'):
        if authed:
            raise SetupError(f'binodex OTC: {detail} + редирект с /trade на {page.url} при живой '
                             f'авторизации — аутэйдж фронта binodex (boot-recovery), не куки', mounted=False)
        raise CookiesExpired(f'binodex OTC: {detail} + редирект с /trade на {page.url}, '
                             f'нет privy:token — сессия протухла')
    # Токен очищен (Privy сбросил протухшую сессию на буте) → реальная смерть сессии → релогин.
    # Проверяем ДО error-boundary: иначе «Something went wrong» поверх мёртвой сессии увёл бы в
    # выживание-без-релогина вместо восстановления кук.
    if not authed:
        raise CookiesExpired(f'binodex OTC: {detail} + нет privy:token (Demo) — сессия протухла')
    # Токен ЖИВ, но апп упал с error-boundary «Something went wrong» — это НЕ битая сессия (релогин
    # её не чинит: логинится успешно, апп падает снова), а front-end аутэйдж: JS-бандл/ленивый чанк
    # не загрузился (напр. отравленный CDN-кэш отдаёт index.html вместо .js — был такой инцидент на
    # AMS-эдже Cloudflare). → SetupError(mounted=False): выживаем с бэкоффом, без релогина и выхода.
    if await _error_boundary_shown(page):
        raise SetupError(f'binodex OTC: {detail} + «Something went wrong» при живом токене — '
                         f'front-end аутэйдж binodex (JS-бандл/чанк не загрузился, напр. CDN-кэш)',
                         mounted=False)
    if await _app_shell_mounted(page):
        raise SetupError(f'binodex OTC: {detail}, фид жив, токен есть, апп смонтирован — '
                         f'сменились селекторы binodex')
    raise SetupError(f'binodex OTC: {detail}, фид жив, токен есть, но апп-шелл не смонтировался '
                     f'(висящий сплеш — front-end аутэйдж binodex)', mounted=False)


async def apply_chart_scale(page: Page) -> None:
    """Выставить масштабы графика: свеча '30S' → график 'H1'. binodex сбрасывает их на дефолт
    при КАЖДОМ запуске браузера (новый контекст из storage_state → M30; reload в рамках сессии
    значение держит — проверено), а раньше штатный setup шёл только на холодном
    релогине. Поэтому применяем здесь, в init_otc, на каждом старте браузера. Порядок важен: смена
    масштаба свечи сбрасывает масштаб графика, поэтому график (H1) ставим ПОСЛЕДНИМ. Пункты —
    по тексту (порядок списков binodex плавает). Ошибки не критичны для запуска (масштаб — оформление
    кадра, не данные) — логируем и продолжаем."""
    for opener, item, name in ((otc_candle_scale, otc_candle_scale_item, 'свеча 30S'),
                               (otc_chart_scale, otc_chart_scale_item, 'график H1')):
        try:
            await page.locator(opener).first.click(timeout=TIMEOUT_SHORT)
            item_loc = page.locator(item).first
            await item_loc.wait_for(state='visible', timeout=TIMEOUT_SHORT)
            # Контейнер-дропдаун binodex (.profile_add_wrap_selected_wrap_options) перехватывает
            # pointer events на своём же пункте (overlay/стэкинг) — обычный .click() ловит «intercepts
            # pointer events». Кликаем напрямую DOM-событием: пункт уже зарезолвлен и видим, оверлей
            # при dispatch_event не помеха (проверка перекрытия пропускается).
            await item_loc.dispatch_event('click')
            await page.wait_for_timeout(500)  # дать дропдауну закрыться перед следующим шагом
        except (Exception,) as error:
            logger.warning(f"OTC: не удалось выставить масштаб ({name}): {error}")


# ── Кадр OTC: своя подложка слоем в браузере + element.screenshot ─────────────────────────────────────
# Штатный фон binodex (`.wrap_bg`) ВЫКЛЕН за аккаунтом — он главный потребитель CPU headless-рендера
# (docs/BINODEX_CPU.md), поэтому график грузится на сплошном тёмном фоне. Свою подложку кладём
# СВОИМ слоем `#bino_bg` (fixed, z-index:-1, промоутнут на отдельный слой) ровно по боксу канваса:
# картинка ложится 1:1 с кадром, композиция не зависит от вьюпорта. Кадр снимаем element.screenshot
# — ярлык пары, часы «Время закрытия» и ценник (HTML/канвас) попадают в него сами.
#
# Раньше кадр собирался композитом в PIL (глобус-файл + прозрачный канвас + вырезка ярлыка по
# разнице кадров). Перезамер 2026-08-04 показал, что ПОД OFF-ZONE слой фона стоит всего +4…6 пт CPU
# (23%→27%) и +85 мс на кадр — дешевле, чем держать вырезку ярлыка (самая хрупкая часть пайплайна:
# двойной снимок региона, дёрганье visibility чужого элемента, попиксельное матирование).
_BG_LAYER_ID = 'bino_bg'   # ВАЖНО: тот же id захардкожен в белом списке _HIDE_OFFZONE_JS

# Слой подложки по боксу канваса. Пересоздаётся идемпотентно: reload страницы сносит DOM,
# поэтому зовётся и в init_otc, и после каждого выбора пары (рядом с off-zone).
_BG_LAYER_JS = r"""
({sel, color, id}) => {
  const cv = document.querySelector(sel);
  if (!cv) return null;
  const r = cv.getBoundingClientRect();
  let el = document.getElementById(id);
  if (!el) { el = document.createElement('div'); el.id = id; document.body.appendChild(el); }
  el.style.cssText = 'position:fixed; z-index:-1; pointer-events:none;' +
    `left:${r.left}px; top:${r.top}px; width:${r.width}px; height:${r.height}px;` +
    `background:${color}; transform:translateZ(0); will-change:transform;` +
    'backface-visibility:hidden;';
  el.style.setProperty('visibility', 'visible', 'important');   // пережить уже наложенный off-zone
  return {w: Math.round(r.width), h: Math.round(r.height)};
}
"""

# Отрисованы ли свечи: доля непрозрачных пикселей канваса по даунсэмплу 64×40 (drawImage, без
# PNG-энкода — дешевле прежнего toDataURL и не зависит от способа съёмки кадра). Пустой канвас → 0.
_CANVAS_FILL_JS = r"""
el => {
  const w = 64, h = 40;
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d');
  ctx.drawImage(el, 0, 0, w, h);
  const d = ctx.getImageData(0, 0, w, h).data;
  let n = 0;
  for (let i = 3; i < d.length; i += 4) if (d[i] > 16) n++;
  return n / (w * h);
}
"""

# Набор включённых индикаторов из localStorage binodex (`indicators/chart-<n>` = JSON-массив имён).
_INDICATORS_JS = r"""
() => {
  const out = [];
  for (const k of Object.keys(localStorage)) {
    if (!/^indicators\//.test(k)) continue;
    try { const v = JSON.parse(localStorage.getItem(k) || '[]'); if (Array.isArray(v)) out.push(...v); }
    catch (e) { /* мусор в ключе — просто пропускаем */ }
  }
  return out;
}
"""

async def apply_bg_layer(page: Page) -> bool:
    """Подложить свой слой под канвас (см. _BG_LAYER_JS). Зовётся в init_otc и после каждого
    выбора пары: reload страницы сносит DOM вместе со слоем. Идемпотентно — слой переиспользуется.
    Ошибка не критична: кадр соберётся на штатном тёмном фоне binodex (тёмно-синий) — логируем."""
    try:
        box = await _eval(page, _BG_LAYER_JS,
                          {'sel': screen_zone_otc, 'color': bg_otc_color, 'id': _BG_LAYER_ID})
        if not box:
            logger.warning('OTC: подложка не легла — нет канваса для позиционирования слоя')
            return False
        return True
    except (Exception,) as err:
        logger.warning(f'OTC: подложка не легла ({err}) — кадр будет на тёмном фоне binodex')
        return False


async def apply_indicator(page: Page) -> None:
    """Включить индикатор графика (binodex_settings.indicator_name, например 'Whale Absorption').
    binodex держит набор в localStorage (`indicators/chart-<n>`), а контекст поднимается из
    storage_state в БД, где индикатора может не быть → включаем на каждом старте браузера, как и
    масштабы (apply_chart_scale). Идемпотентно: уже включён → выходим без кликов. localStorage
    переживает reload в рамках сессии, поэтому зовём только из init_otc.
    Требует СНЯТОГО off-zone (модалка индикаторов вне зоны канваса) — так и стоит в init_otc.
    Ошибки не критичны (индикатор — оформление кадра, не данные): лог и продолжаем."""
    if not (otc_indicator_name and otc_indicators_btn and otc_indicator_item):
        return
    try:
        if otc_indicator_name in (await _eval(page, _INDICATORS_JS) or []):
            return                                    # уже включён (пришёл со storage_state)
        await page.locator(otc_indicators_btn).first.click(timeout=TIMEOUT_SHORT)
        item = page.locator(otc_indicator_item).filter(has_text=otc_indicator_name).first
        await item.wait_for(state='visible', timeout=TIMEOUT_SHORT)
        await item.click(timeout=TIMEOUT_SHORT)
        # Модалка закрывается сама по выбору; подтверждаем по localStorage, а не по DOM.
        for _ in range(10):
            if otc_indicator_name in (await _eval(page, _INDICATORS_JS) or []):
                logger.info(f"OTC: индикатор '{otc_indicator_name}' включён")
                return
            await asyncio.sleep(0.3)
        logger.warning(f"OTC: индикатор '{otc_indicator_name}' не включился — кадр без него")
    except (Exception,) as err:
        logger.warning(f"OTC: не удалось включить индикатор '{otc_indicator_name}': {err}")
    finally:
        try:                                          # страховка: модалка индикаторов не должна
            await page.keyboard.press('Escape')       # остаться открытой (перекроет график)
        except (Exception,):
            pass


# ── off-zone оптимизация CPU (~40→~22%): скрыть UI вне зоны скрина ─────────────────────────────────
# Весь UI вне канваса (правое торговое меню, аккаунт-бар, сайдбар) рендерится зря и в кадр не
# попадает (кадр — клип по боксу канваса) — прячем `visibility:hidden`, экономия ~17 пт. В БЕЛОМ
# СПИСКЕ остаются видимыми #setup_settings_open (по нему _ui_loaded детектит отвал кук в рантайме —
# НЕЛЬЗЯ прятать!), ярлык пары (он ВНУТРИ бокса канваса → идёт в кадр, плюс это кнопка модалки) и
# наш слой подложки #bino_bg (он вне цепочки канваса, иначе off-zone погасил бы фон кадра).
# Применяем после выбора пары и в init_otc; СНИМАЕМ на время select_otc_pair (модалка выбора —
# вне зоны, под off-zone не кликается).
_HIDE_OFFZONE_JS = r"""
(sel) => {
  const cv = document.querySelector(sel);
  if (!cv) return -1;
  const keep = new Set();
  for (let e = cv; e; e = e.parentElement) keep.add(e);
  let n = 0;
  // transition:none — у binodex на части UI висит `transition: .5s`, и visibility АНИМИРУЕТСЯ:
  // computed остаётся 'visible' ещё полсекунды после нашей установки. Кадр снимается раньше, и
  // спрятанное попадало в пост. Глушим переход, чтобы скрытие было мгновенным (снимается в
  // _CLEAR_OFFZONE_JS вместе с visibility).
  for (const el of document.querySelectorAll('body *')) {
    if (keep.has(el) || el === cv || el.contains(cv)) continue;
    el.style.setProperty('transition', 'none', 'important');
    el.style.setProperty('visibility', 'hidden', 'important');
    n++;
  }
  const show = (el) => {                                   // вернуть видимость элементу + предкам + потомкам
    if (!el) return;
    for (let e = el; e; e = e.parentElement) e.style.setProperty('visibility', 'visible', 'important');
    for (const d of el.querySelectorAll('*')) d.style.setProperty('visibility', 'visible', 'important');
  };
  const settings = document.querySelector('#setup_settings_open');
  show(settings);                                          // детект кук (_ui_loaded) — обязательно видим
  const pl = document.querySelector('#select_pair_add');   // ярлык пары (в кадре + кнопка модалки)
  show(pl);
  if (pl && pl.parentElement) show(pl.parentElement);
  show(document.getElementById('bino_bg'));                // подложка кадра (id = _BG_LAYER_ID)

  // Второй проход — ЧИСТКА КАДРА. Кадр снимается element.screenshot по боксу канваса, поэтому
  // любой видимый HTML внутри этого бокса попадёт в пост: тулбар-кнопки graph_pair_setting,
  // селектор масштаба, боковые ручки. Раньше (кадр из canvas.toDataURL) они были не важны, а
  // show(pl.parentElement) выше как раз возвращает видимость соседям ярлыка. Прячем всё видимое,
  // что ПЕРЕСЕКАЕТ бокс, кроме: цепочки канваса, ярлыка, подложки и кнопки детекта кук.
  // Правило геометрическое, а не по классам binodex — переживает ротацию их разметки.
  const b = cv.getBoundingClientRect();
  const bg = document.getElementById('bino_bg');
  for (const el of document.querySelectorAll('body *')) {
    if (el === cv || el.contains(cv) || el === bg) continue;
    if (pl && (el === pl || pl.contains(el) || el.contains(pl))) continue;
    if (settings && (el === settings || el.contains(settings))) continue;   // не трогать детект кук
    if (getComputedStyle(el).visibility !== 'visible') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    if (r.right <= b.left || r.left >= b.right || r.bottom <= b.top || r.top >= b.bottom) continue;
    el.style.setProperty('transition', 'none', 'important');   // без этого скрытие «доезжает» 0.5с
    el.style.setProperty('visibility', 'hidden', 'important');
    n++;
  }
  return n;
}
"""

# Снять off-zone: убрать наши инлайновые visibility/transition со всех элементов (binodex инлайн
# ни то, ни другое не использует, поэтому чужого не затираем).
_CLEAR_OFFZONE_JS = ("() => { for (const el of document.querySelectorAll('body *')) {"
                     " if (!el.style) continue;"
                     " if (el.style.visibility) el.style.removeProperty('visibility');"
                     " if (el.style.transition) el.style.removeProperty('transition'); } }")


async def _apply_offzone(page: Page) -> None:
    """Скрыть off-zone UI (CPU ~40→~22%), оставив в белом списке детект кук и ярлык пары."""
    try:
        await _eval(page, _HIDE_OFFZONE_JS, screen_zone_otc)
    except (Exception,) as err:
        logger.debug(f"OTC off-zone apply: {err}")


async def _clear_offzone(page: Page) -> None:
    """Вернуть весь UI (на время выбора пары — модалка выбора под off-zone не кликается)."""
    try:
        await _eval(page, _CLEAR_OFFZONE_JS)
    except (Exception,) as err:
        logger.debug(f"OTC off-zone clear: {err}")


async def _restore_frame_layers(page: Page) -> None:
    """Вернуть слои кадра после выбора пары: подложка (её мог снести reload) + off-zone.
    Зовётся из select_otc_pair на ЛЮБОМ исходе — порядок важен только тем, что off-zone
    белым списком оставляет подложку видимой (см. _HIDE_OFFZONE_JS)."""
    await apply_bg_layer(page)
    await _apply_offzone(page)


async def screenshot_otc(page: Page, asset: str = None, qr=None):
    """Кадр зоны графика binodex (клип по боксу канваса: подложка #bino_bg + свечи/оси/часы/ценник
    + ярлык пары) + QR оверлеем, и цена графика (медиана чтений window.chartData.price вокруг
    кадра). chartData.price — то значение, что движок рисует на ярлыке; точнее WS-тика, который
    опережает график на ~150 мс (см. docs/BINODEX_PRICE.md). Если chartData недоступен — фолбэк на
    WS-цену по моменту кадра. Подложку рисует браузер нашим слоем (apply_bg_layer), штатный фон
    binodex выключен за аккаунтом.
    :return: (success, price|error_text, screenshot_path|'')."""
    symbol = symbol_key(asset)
    last_error = 'нет цены графика OTC'
    for attempt in range(1, MAX_SCREENSHOT_ATTEMPTS + 1):
        try:
            element = page.locator(screen_zone_otc).first
            await element.wait_for(state='visible', timeout=TIMEOUT_LONG)
            # Защита: модалка выбора пары иногда осталась открытой (select_otc_pair не дозакрыл) —
            # она перекрывает график. Закрываем перед кадром, чтобы не попала в пост. В норме
            # (модалка закрыта) _close_pair_modal выходит сразу на первой проверке — без кликов.
            await _close_pair_modal(page)
            box = await element.bounding_box()
            if not box:  # элемент невидим/отсоединён → bounding_box=None (иначе TypeError на box['x'])
                last_error = 'нет bounding_box зоны графика OTC'
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: {last_error} для {asset}")
                continue
            clip = {'x': round(box['x']), 'y': round(box['y']),
                    'width': round(box['width']), 'height': round(box['height'])}
            # Защита от пустого канваса: после переключения пары канвас ~1-3с пустой (свечи не
            # дорисованы) — не постим голый кадр. Ждём отрисовку до CANVAS_READY_SECONDS (wall-clock
            # по time.monotonic — каждая проба это _eval до EVAL_TIMEOUT). Проба дешёвая (даунсэмпл
            # 64×40 без PNG-энкода), поэтому она отдельно от кадра, а ценовой брекет (reads_before →
            # t_shot → кадр → reads_after) идёт ПОСЛЕ готовности — так медиана синхронна с кадром.
            deadline = time.monotonic() + CANVAS_READY_SECONDS
            ready = False
            while True:
                fill = await _eval(element, _CANVAS_FILL_JS)
                if isinstance(fill, (int, float)) and fill >= CANVAS_MIN_OPAQUE:
                    ready = True
                    break
                if time.monotonic() >= deadline:
                    break  # свечи так и не появились за бюджет → ретрай попытки
                await asyncio.sleep(0.4)
            if not ready:   # свечи так и не появились → ретрай попытки (редкий труло-стак)
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: канвас пуст "
                               f"{CANVAS_READY_SECONDS:.0f}с (свечи не отрисованы) для {asset}")
                continue
            reads = await _read_chart_prices(page, symbol, CHART_READS_BEFORE)
            t_shot = time.time()
            shot_buf = await _shot(page, clip=clip)
            reads += await _read_chart_prices(page, symbol, CHART_READS_AFTER)
            if reads:
                price = statistics.median(reads)
            else:
                # chartData не отдал ни одного чтения — кадр снят, но цену берём из WS-фолбэка.
                # Логируем: в пост-мортеме видно, что источник цены кадра — WS, а не ярлык графика.
                price = get_price_tracker().get_price_at(asset, t_shot)
                logger.debug(f"OTC {asset}: chartData пуст на кадре — цена из WS-фолбэка ({price})")
            if price is None:  # ни chartData, ни WS не дали цену
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: нет цены графика OTC для {asset}")
                await asyncio.sleep(0.5)
                continue
            frame = Image.open(BytesIO(shot_buf)).convert('RGB')
            if qr:
                paste_overlay(frame, qr[0], otc_qr_x, otc_qr_y)  # на OTC один QR (qr110)
            frame.save(screenshot_path)
            return True, price, screenshot_path
        except (Exception,) as error:
            last_error = str(error)
            logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS} скриншота OTC: {error}")
    return False, f'Ошибка записи скриншота OTC - {last_error}', ''


async def open_otc_browser(manager: "BrowserManager") -> OperationResult:
    """Открытие binodex для OTC."""
    return OperationResult(success=bool(await init_otc(manager=manager)))


async def _verify_otc_ready(page: Page) -> None:
    """Авторизация + готовность торгового UI на /trade. Возвращается при успехе; иначе raises:
    CookiesExpired (нужен релогин: нет токена / Demo / форма логина), FeedOutage (аутэйдж фида),
    SetupError (front-end аутэйдж binodex — в т.ч. редирект/boot-recovery при ЖИВОМ токене — либо
    сменившиеся селекторы). Редирект с /trade разводит _raise_ui_dead ПО ЖИВОСТИ privy:token, а не
    безусловно как отвал кук. WS-фид для BinoOptions НЕ критичен (цена из chartData, WS — фолбэк/
    liveness): не поднялся → лог деградации, БЕЗ raise."""
    # authed читаем ПЕРВОЙ — от неё зависит трактовка редиректа (куки vs аутэйдж фронта binodex).
    authed = await _authed_safe(page)
    if not on_trade(page.url):
        # binodex увёл с /trade. Сперва — backend: auth-API 5xx браузер-фри → это НЕ куки и НЕ
        # front-end-аутэйдж, а падение бэкенда binodex (Privy-логин на 502); релогин/прокси не
        # помогут → FeedOutage (браузер-фри ожидание). Грабли 2026-07-23.
        await _raise_if_backend_down(f'редирект с /trade на {page.url}')
        # Токен ЖИВ → апп-шелл не поднялся и фронт САМ сбросил на лендинг/?boot-recovery=… (их само-
        # восстановление) при живой сессии = аутэйдж их фронта, НЕ куки: релогин бесполезен →
        # SetupError(mounted=False) (прокси-фолбэк + переподъём). Токена нет → storage_state реально
        # протух → CookiesExpired. Грабли 2026-07: boot-recovery.
        if authed:
            raise SetupError(f'binodex OTC: редирект с /trade на {page.url} при живой авторизации — '
                             f'аутэйдж фронта binodex (boot-recovery), не куки', mounted=False)
        raise CookiesExpired(f'binodex OTC: редирект с /trade на {page.url}, нет privy:token — сессия протухла')
    # Ранний гейт «сессии нет вовсе» (чистый контекст). На ПРОТУХШЕЙ (но присутствующей) сессии
    # токен только что восстановлен из storage_state → ранний гейт пропустит; Privy очистит его на
    # буте → ловит авторитетная перепроверка ниже.
    if not authed:
        raise CookiesExpired('binodex OTC: нет privy:token (нет сессии) — нужен логин')
    # SPA не обязательно доехала: при сплеше чарт виснет, кнопка выбора пары не появляется.
    # _raise_ui_dead разводит: форма/Demo/error → CookiesExpired; фид мёртв → FeedOutage; токен жив,
    # UI не поднялся → SetupError.
    try:
        await page.locator(otc_select_pair).first.wait_for(state='visible', timeout=TIMEOUT_LONG)
    except (Exception,):
        await _raise_ui_dead(page, 'кнопка выбора пары не появилась')
    if not await _ui_loaded(page, UI_READY_TIMEOUT):
        await _raise_ui_dead(page, 'нет кнопки настроек аккаунта (завис на сплеше)')
    # Авторитетная перепроверка ПОСЛЕ оседания UI: Privy за время загрузки мог очистить протухший
    # токен (ранний гейт видел его свежевосстановленным) → апп в Demo.
    if not await _privy_authenticated(page):
        raise CookiesExpired('binodex OTC: UI поднялся, но privy:token очищен (Demo) — сессия протухла')
    # Масштабы графика и индикатор сбрасываются/отсутствуют при каждом запуске браузера (новый
    # контекст из storage_state) — выставляем на каждом старте, ДО off-zone: их модалки вне зоны
    # канваса и под off-zone не кликаются.
    await apply_chart_scale(page)
    await apply_indicator(page)
    # Подложка кадра своим слоем (штатный фон binodex выключен за аккаунтом ради CPU).
    await apply_bg_layer(page)
    # off-zone оптимизация CPU (~40→~22%): прячем UI вне зоны скрина (детект кук/ярлык/подложка — в белом списке).
    await _apply_offzone(page)
    # WS-котировки — мягко (источник цены chartData, WS = фолбэк/liveness). Не пошёл → деградация, БЕЗ raise.
    tracker = get_price_tracker()
    for _ in range(20):
        if tracker.ws_connected and tracker.prices:
            logger.report("✅ binodex: WS котировок подключён")
            return
        await asyncio.sleep(0.5)
    logger.warning("binodex: WS котировок не поднялся за 10с — работаю на chartData, "
                   "feed_dead-детект деградирован")


async def _relogin_inline(manager: "BrowserManager", page: Page) -> bool:
    """Inline-релогин binodex В ТЕКУЩЕМ браузере (без подпроцесса/холодного браузера): почта+app-pass
    и селекторы из БД → otc_login.otc_inline_login над живым page. Успех → свежий storage_state в БД
    (переживёт рестарт, чтобы не логиниться OTP каждый старт). True/False (любой сбой — лог + False)."""
    creds = await database.get_mail_creds(cookies_pocket_id)
    if not creds or creds is False or not creds['mail'] or not creds['mail_app_pass']:
        logger.error('OTC inline-релогин: нет mail/app-password (telegram.telegram) — логин невозможен')
        return False
    rows = await database.binodex_selectors()
    if not rows or rows is False:
        logger.error('OTC inline-релогин: нет селекторов binodex_settings')
        return False
    sel = {r['par_name']: r['par_value'] for r in rows}
    if not await otc_inline_login(page, manager.context, creds['mail'], creds['mail_app_pass'], sel):
        return False
    # Свежую сессию — в БД (переживёт рестарт). Сбой сохранения не критичен: работаем на live-сессии.
    try:
        if await database.save_otc_cookies(cookies_pocket_id, await manager.context.storage_state()) is False:
            logger.warning('OTC inline-релогин: storage_state не сохранён в БД (сбой) — продолжаю на live-сессии')
    except (Exception,) as err:
        logger.warning(f'OTC inline-релогин: сохранение storage_state не удалось ({err}) — продолжаю')
    return True


GOTO_RETRIES = 3          # попыток goto при транзиентном NS_BINDING_ABORTED
GOTO_RETRY_PAUSE = 1.5    # сек между ретраями goto


async def _goto_otc(page: Page, url: str, timeout: int = TIMEOUT_LONG) -> None:
    """page.goto с ретраями ТОЛЬКО на транзиентном NS_BINDING_ABORTED — binodex/Privy во время
    загрузки сам инициирует редирект → Firefox обрывает навигацию (гонка, не реальный сбой).
    Прочие ошибки goto пробрасываем сразу; исчерпали попытки — пробрасываем последнюю."""
    last_error = None
    for attempt in range(1, GOTO_RETRIES + 1):
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return
        except (Exception,) as error:
            if 'NS_BINDING_ABORTED' not in str(error):
                raise
            last_error = error
            logger.warning(f'OTC: goto {url} → NS_BINDING_ABORTED (попытка {attempt}/{GOTO_RETRIES}), повтор')
            if attempt < GOTO_RETRIES:
                await asyncio.sleep(GOTO_RETRY_PAUSE)
    raise last_error


async def init_otc(manager: "BrowserManager") -> bool:
    """Загрузка binodex.app/trade: WS-перехват → страница из cookies.pages → goto →
    _verify_otc_ready (авторизация + UI; WS мягко). При «нужен релогин» (CookiesExpired) — INLINE-
    логин в ЭТОМ ЖЕ браузере (apps/otc_login), без подпроцесса/двойной загрузки, и перепроверка. Не
    вышло → CookiesExpired наверх (main: счётчик RECOVER_ATTEMPTS → плановый выход)."""
    page = manager.pages['main']
    setup_websocket_tracker(page)  # подписка ДО навигации — поймать поток с самого старта

    url = await _otc_page_url()
    if not url:
        await close_program(manager=manager, status=1, text="Нет OTC-страницы в binodex.cookies.pages")
        return False

    try:
        await _goto_otc(page, url)
        await page.set_viewport_size({'width': win_x_otc, 'height': win_y_otc})
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f"Не загрузился binodex - {error}")
        return False

    try:
        relogged = False
        while True:
            try:
                await _verify_otc_ready(page)
                return True
            except CookiesExpired as err:
                # «Нужен релогин». Логинимся INLINE в ЭТОМ ЖЕ браузере — один раз за init_otc.
                # Уже логинились и снова CookiesExpired → релогин не помог → наверх: main считает
                # попытки (RECOVER_ATTEMPTS) → плановый выход. Так нет вечного inline-цикла.
                if relogged:
                    raise
                logger.warning(f'OTC: {err} → inline-релогин в текущем браузере')
                if not await _relogin_inline(manager, page):
                    raise  # inline не удался → наверх (счётчик RECOVER_ATTEMPTS → выход)
                relogged = True
                await _goto_otc(page, url)
    except (CookiesExpired, FeedOutage, SetupError):
        raise  # наружу → init_load → _init_with_retry (счётчик релогина / ожидание фида / setup-ретраи)
    except (Exception,) as error:
        await close_program(manager=manager, status=1, text=f'Ошибка загрузки OTC binodex - {error}')
        return False


async def _reload_otc_once(page: Page) -> bool:
    """Одна попытка reload + та же лестница готовности, что в init_otc, но мягкая (bool вместо
    CookiesExpired). False — UI не поднялся; чаще всего это транзиентный зависший сплеш binodex
    (Privy/SPA не достроился, #root пуст), который лечится повторным reload (см. reload_otc_page)."""
    try:
        await page.reload(wait_until='domcontentloaded', timeout=TIMEOUT_LONG)
    except (Exception,) as error:
        logger.warning(f'OTC: reload страницы перед опционом не удался - {error}')
        return False
    # networkidle НЕ ждём: постоянный WS-поток binodex не даёт ему сойтись — вырабатывался весь
    # TIMEOUT_LONG (15с) вхолостую на каждом reload. Готовность даёт лестница ниже (on_trade → gate).
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
    return True


async def reload_otc_page(manager: "BrowserManager") -> bool:
    """Перезагрузка binodex перед каждым новым опционом (вызов из main_app). binodex
    периодически выкатывает новую версию фронта и показывает баннер «Доступна новая версия.
    Обновите страницу», зависая на сплеше при ЖИВЫХ URL (/trade держится), UI и WS — отвал-кук-
    детект (on_trade/_ui_loaded/feed_dead) такое НЕ ловит. Регулярный reload подхватывает новую
    версию заранее, до того как чарт зависнет. WS-перехват НЕ переустанавливаем: page.on('websocket')
    переживает reload (повторная подписка задвоила бы хендлеры), старый WS закроется → новый
    откроется → трекер сам перецепится.

    Зависший загрузочный сплеш транзиентен (~3% reload Privy/SPA не достраивается, следующий reload
    рендерится нормально), поэтому повторяем САМ reload до RELOAD_RETRIES раз перед тем, как отдать
    False — иначе бот зря уходит в пересоздание браузера (ложный «отвал cookies») / «нет пар».
    :return: True — UI снова готов к скрину; False — не поднялся после всех ретраев (вызывающий
    уйдёт в exit_main → main-цикл по otc_session_dead пересоздаст браузер)."""
    page = manager.pages.get('main')
    if page is None:
        return False
    for attempt in range(1, RELOAD_RETRIES + 1):
        if await _reload_otc_once(page):
            break
        if attempt < RELOAD_RETRIES:
            logger.warning(f'OTC: UI не поднялся после reload ({attempt}/{RELOAD_RETRIES}) — '
                           f'повторяю reload (транзиентный зависший сплеш)')
            await asyncio.sleep(RELOAD_RETRY_PAUSE)
    else:
        return False  # все попытки впустую — реальный отвал/сплеш, наверх (пересоздание браузера)
    tracker = get_price_tracker()
    for _ in range(20):  # ждём переподключения WS-котировок (до 10 сек), как в init_otc
        if tracker.ws_connected and tracker.prices:
            break
        await asyncio.sleep(0.5)
    else:
        logger.warning("binodex: WS котировок не переподключился за 10с после reload")
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
