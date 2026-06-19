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
import base64
import re
import statistics
import time
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page, WebSocket, FloatRect

from classes.Option_class import Option
from classes.price_tracker import WebSocketPriceTracker, symbol_key
from classes.result_types import OperationResult
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from apps.exit_app import close_program
from apps.otc_login import otc_inline_login
from logs import init_logger
from settings.config import screenshot_path, database, prog_key, cookies_pocket_id
from settings.constant import globe_otc_path
from settings.timing import TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_LONG, MAX_SCREENSHOT_ATTEMPTS
from settings.screenshot_set import win_x_otc, win_y_otc, otc_qr_x, otc_qr_y, paste_overlay
from settings.browser_config import (otc_trade_url, otc_select_pair, otc_category_valute, otc_input_pair,
                                     otc_modal_pair_item, screen_zone_otc, otc_settings_btn, otc_login_email,
                                     otc_candle_scale, otc_candle_scale_item,
                                     otc_chart_scale, otc_chart_scale_item)

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
                await _build_label_cutout(page, target)   # запечь вырезку ярлыка, пока off-zone снят
                return True
            await asyncio.sleep(0.25)
        logger.warning(f"OTC: пара '{pair}' не прогрузилась на binodex (нет WS-котировки за 8с) — пропускаю")
        return False
    except (Exception,) as error:
        logger.warning(f"OTC: ошибка выбора пары {pair} — {error}")
        return False
    finally:
        await _apply_offzone(page)   # off-zone восстанавливается на ЛЮБОМ исходе (успех/неудача/ошибка)


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


async def _raise_ui_dead(page: Page, detail: str) -> None:
    """UI не поднялся — развести причину на классы (канон, docs/lifecycle-standard §4.5).
    Всегда бросает:
      • видна форма логина → CookiesExpired (отвал кук → релогин);
      • формы нет, market-WS молчит браузер-фри (feed_alive=False) → FeedOutage (аутэйдж фида);
      • формы нет, фид ЖИВ, нет privy:token (Privy очистил → Demo) → CookiesExpired (сессия мертва, релогин);
      • формы нет, фид ЖИВ, токен ЕСТЬ, error-boundary «Something went wrong» → SetupError(mounted=False):
        front-end аутэйдж (JS-бандл/чанк не загрузился, напр. отравленный CDN-кэш) — релогин бесполезен,
        выживаем с бэкоффом, без выхода;
      • формы нет, фид ЖИВ, токен ЕСТЬ, апп-шелл СМОНТИРОВАН → SetupError(mounted=True): сменились
        селекторы → N ретраев → плановый выход;
      • формы нет, фид ЖИВ, токен ЕСТЬ, апп-шелл НЕ смонтировался (сплеш) → SetupError(mounted=False):
        front-end аутэйдж binodex → выживаем с бэкоффом, без выхода."""
    if await _login_modal_open(page):
        raise CookiesExpired(f'binodex OTC: {detail} + всплыла форма логина — куки протухли')
    from apps.binodex_feed import feed_alive  # лениво: модуль тянет browser_config (bootstrap)
    if not await feed_alive():
        raise FeedOutage(f'binodex OTC: {detail} + market-WS молчит браузер-фри — аутэйдж binodex')
    # Токен очищен (Privy сбросил протухшую сессию на буте) → реальная смерть сессии → релогин.
    # Проверяем ДО error-boundary: иначе «Something went wrong» поверх мёртвой сессии увёл бы в
    # выживание-без-релогина вместо восстановления кук.
    if not await _privy_authenticated(page):
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


# ── Композит кадра OTC (глобус-файл + прозрачный канвас + ярлык пары + QR) ────────────────────────────
# Глобус (`.wrap_bg`) на binodex ВЫКЛЕН за аккаунтом (главный потребитель CPU headless-рендера,
# docs/BINODEX_CPU.md), поэтому график грузится на тёмном фоне (~40% CPU вместо ~90%). Сам глобус
# в пост подкладываем композитом из статичного файла под прозрачный канвас — кадр выглядит как
# раньше, но браузер глобус не рендерит. Слои: глобус(файл, низ) + канвас(toDataURL, прозрачные
# свечи/оси/часы/ценник) + ярлык пары(вырезка, см. ниже) + QR.

# toDataURL канваса → пиксели с альфой; w/h — бэкстор канваса, css* — его CSS-бокс (ресайз при DPR).
_CANVAS_ALPHA_JS = ("el => ({ url: el.toDataURL('image/png'), w: el.width, h: el.height,"
                    " cssw: Math.round(el.getBoundingClientRect().width),"
                    " cssh: Math.round(el.getBoundingClientRect().height) })")

# Ярлык пары (флаг+пара+OTC+payout) — HTML поверх канваса, в toDataURL он НЕ попадает, поэтому
# кладём отдельным слоем. Находим по содержимому+геометрии (у верх-левого угла бокса канваса,
# текст с 'OTC' и '%') — устойчиво к ротации классов binodex; ставим маркер data-otc-lbl.
_LABEL_BOX_JS = r"""
(sel) => {
  const cv = document.querySelector(sel);
  if (!cv) return null;
  const b = cv.getBoundingClientRect();
  let best = null, area = 0;
  for (const el of document.querySelectorAll('body *')) {
    const t = el.textContent || '';
    if (!t.includes('OTC') || !t.includes('%')) continue;
    const r = el.getBoundingClientRect();
    if (r.left < b.left + 24 && r.top < b.top + 44 && r.width > 0 && r.width < 360 && r.height < 80) {
      const a = r.width * r.height; if (a > area) { area = a; best = el; }
    }
  }
  if (!best) return null;
  best.setAttribute('data-otc-lbl', '1');
  const r = best.getBoundingClientRect();
  return {x: r.left, y: r.top, w: r.width, h: r.height};
}
"""

_globe_asset: Image.Image | None = None        # глобус-файл (RGBA), грузится один раз
_label_cutout_cache: dict = {}                  # {asset: (cutout RGBA, (dx, dy))} — вырезка ярлыка на пару


def _load_globe(size) -> Image.Image:
    """Глобус-файл (RGBA) под размер канваса; кэш в памяти (файл читаем один раз)."""
    global _globe_asset
    if _globe_asset is None:
        _globe_asset = Image.open(globe_otc_path).convert('RGBA')
    return _globe_asset if _globe_asset.size == tuple(size) else _globe_asset.resize(size, Image.Resampling.LANCZOS)


async def _canvas_alpha(element) -> Image.Image:
    """Пиксели канваса с альфой (toDataURL), приведённые к CSS-боксу (ресайз при DPR)."""
    d = await _eval(element, _CANVAS_ALPHA_JS)
    img = Image.open(BytesIO(base64.b64decode(d['url'].split(',', 1)[1]))).convert('RGBA')
    if (d['w'], d['h']) != (d['cssw'], d['cssh']):
        img = img.resize((d['cssw'], d['cssh']), Image.Resampling.LANCZOS)
    return img


def _matte_label(crop_a: Image.Image, crop_b: Image.Image, k: int = 3, thr: int = 10) -> Image.Image:
    """Вырезка ярлыка по разнице: A (ярлык виден) − B (фон). Альфа = clamp(|A−B|*k).
    Так фон (где A==B) становится прозрачным, остаётся только сам ярлык — без «короба»."""
    a, b = crop_a.convert('RGB').load(), crop_b.convert('RGB').load()
    out = Image.new('RGBA', crop_a.size)
    o = out.load()
    for y in range(crop_a.size[1]):
        for x in range(crop_a.size[0]):
            ra, ga, ba = a[x, y]; rb, gb, bb = b[x, y]
            d = abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            o[x, y] = (ra, ga, ba, 0 if d < thr else min(255, d * k))
    return out


async def _label_cutout(page: Page, asset, clip, rebuild: bool = False):
    """Вырезка ярлыка пары (с прозрачным фоном) и её позиция относительно бокса канваса.
    Глобус НЕ включаем — снимаем регион дважды при выкл глобусе: ярлык виден (A) и скрыт (B),
    вычитаем фон. None — если не собрать. Строить нужно при СНЯТОМ off-zone (полный UI) — иначе
    ярлык не захватится; поэтому пересборка (rebuild=True) идёт из select_otc_pair до off-zone.
    rebuild=True — пересобрать с нуля (актуальный payout на каждый выбор пары); rebuild=False
    (из screenshot_otc) — взять готовое из кэша, собранного на этом же выборе пары."""
    key = symbol_key(asset)
    if not rebuild and key in _label_cutout_cache:
        return _label_cutout_cache[key]
    try:
        lb = await _eval(page, _LABEL_BOX_JS, screen_zone_otc)
        if not lb:
            return None
        lx, ly, lw, lh = round(lb['x']), round(lb['y']), round(lb['w']), round(lb['h'])
        region: FloatRect = {'x': lx, 'y': ly, 'width': lw, 'height': lh}
        a_buf = await _shot(page, clip=region)                           # A: ярлык виден
        await _eval(page, "() => { const e=document.querySelector('[data-otc-lbl]');"
                          " if (e) e.style.setProperty('visibility','hidden','important'); }")
        try:
            await page.wait_for_timeout(150)
            b_buf = await _shot(page, clip=region)                       # B: фон без ярлыка
        finally:                                                          # вернуть ярлык в любом случае
            await _eval(page, "() => { const e=document.querySelector('[data-otc-lbl]');"
                              " if (e) e.style.removeProperty('visibility'); }")
        # Пиксельное матирование — синхронный CPU-цикл; уводим в поток, чтобы не блокировать event loop.
        cutout = await asyncio.to_thread(_matte_label, Image.open(BytesIO(a_buf)), Image.open(BytesIO(b_buf)))
        result = (cutout, (lx - clip['x'], ly - clip['y']))
        _label_cutout_cache[key] = result
        return result
    except (Exception,) as err:
        logger.debug(f"OTC {asset}: вырезка ярлыка не удалась ({err}) — кадр без ярлыка")
        return None


# ── off-zone оптимизация CPU (~40→~22%): скрыть UI вне зоны скрина ─────────────────────────────────
# Весь UI вне канваса (правое торговое меню, аккаунт-бар, сайдбар) рендерится зря (в кадр через
# toDataURL не попадает) — прячем `visibility:hidden`, экономия ~17 пт. В БЕЛОМ СПИСКЕ остаются
# видимыми #setup_settings_open (по нему _ui_loaded детектит отвал кук в рантайме — НЕЛЬЗЯ прятать!)
# и ярлык пары (нужен для вырезки + это кнопка открытия модалки). Применяем после выбора пары и в
# init_otc; СНИМАЕМ на время select_otc_pair (модалка выбора — вне зоны, под off-zone не кликается).
_HIDE_OFFZONE_JS = r"""
(sel) => {
  const cv = document.querySelector(sel);
  if (!cv) return -1;
  const keep = new Set();
  for (let e = cv; e; e = e.parentElement) keep.add(e);
  let n = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (keep.has(el) || el === cv || el.contains(cv)) continue;
    el.style.setProperty('visibility', 'hidden', 'important');
    n++;
  }
  const show = (el) => {                                   // вернуть видимость элементу + предкам + потомкам
    if (!el) return;
    for (let e = el; e; e = e.parentElement) e.style.setProperty('visibility', 'visible', 'important');
    for (const d of el.querySelectorAll('*')) d.style.setProperty('visibility', 'visible', 'important');
  };
  show(document.querySelector('#setup_settings_open'));    // детект кук (_ui_loaded) — обязательно видим
  const pl = document.querySelector('#select_pair_add');   // ярлык пары (вырезка + кнопка модалки)
  show(pl);
  if (pl && pl.parentElement) show(pl.parentElement);
  return n;
}
"""

# Снять off-zone: убрать наши инлайновые visibility со всех элементов (binodex inline-visibility не использует).
_CLEAR_OFFZONE_JS = ("() => { for (const el of document.querySelectorAll('body *'))"
                     " if (el.style && el.style.visibility) el.style.removeProperty('visibility'); }")


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


async def _build_label_cutout(page: Page, asset: str) -> None:
    """Запечь вырезку ярлыка пары, ПОКА off-zone снят (полный UI) — иначе ярлык не захватится.
    Вызывается из select_otc_pair при успехе ДО восстановления off-zone (finally). Ошибки не
    критичны — кадр соберётся и без вырезки (ярлык просто не ляжет)."""
    try:
        box = await page.locator(screen_zone_otc).first.bounding_box()
        if box:
            clip = {'x': round(box['x']), 'y': round(box['y']),
                    'width': round(box['width']), 'height': round(box['height'])}
            await _label_cutout(page, asset, clip, rebuild=True)   # пересобрать (актуальный payout), пока off-zone снят
    except (Exception,) as err:
        logger.debug(f"OTC {asset}: подготовка вырезки ярлыка не удалась — {err}")


async def screenshot_otc(page: Page, asset: str = None, qr=None):
    """Кадр графика binodex композитом (глобус-файл + прозрачный канвас + ярлык пары + QR) +
    цена графика (медиана чтений window.chartData.price вокруг кадра). chartData.price — то
    значение, что движок рисует на ярлыке; точнее WS-тика, который опережает график на ~150 мс
    (см. docs/BINODEX_PRICE.md). Если chartData недоступен — фолбэк на WS-цену по моменту кадра.
    Глобус НЕ рендерится браузером (выкл за аккаунтом, экономия CPU) — подкладывается из файла.
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
            # дорисованы) — не постим голый кадр. Ждём отрисовку до CANVAS_READY_SECONDS (отдельный
            # бюджет, не сжигаем MAX_SCREENSHOT_ATTEMPTS); пробные захваты канваса отбрасываем.
            # Бюджет — wall-clock (time.monotonic), НЕ сумма sleep'ов: каждый _canvas_alpha — это
            # _eval (до EVAL_TIMEOUT), поэтому «waited += 0.4» сильно недосчитывал реальное время
            # и бюджет CANVAS_READY_SECONDS растягивался в разы.
            deadline = time.monotonic() + CANVAS_READY_SECONDS
            while True:
                probe = await _canvas_alpha(element)
                if sum(probe.getchannel('A').histogram()[16:]) >= probe.width * probe.height * CANVAS_MIN_OPAQUE:
                    break
                if time.monotonic() >= deadline:
                    probe = None
                    break
                await asyncio.sleep(0.4)
            if probe is None:   # свечи так и не появились → ретрай попытки (редкий труло-стак)
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: канвас пуст "
                               f"{CANVAS_READY_SECONDS:.0f}с (свечи не отрисованы) для {asset}")
                continue
            # Канвас готов. Цена графика = медиана чтений chartData.price ВОКРУГ кадра; кадр =
            # toDataURL канваса (прозрачные свечи). t_shot фиксируем для фолбэка на WS.
            reads = await _read_chart_prices(page, symbol, CHART_READS_BEFORE)
            t_shot = time.time()
            canvas_img = await _canvas_alpha(element)
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
            # Сэндвич: глобус(файл) → канвас(прозрачный) → ярлык пары(вырезка) → QR.
            comp = Image.alpha_composite(_load_globe(canvas_img.size), canvas_img)
            cut = await _label_cutout(page, asset, clip)
            if cut:
                comp.alpha_composite(cut[0], dest=(max(0, cut[1][0]), max(0, cut[1][1])))
            comp = comp.convert('RGB')
            if qr:
                paste_overlay(comp, qr[0], otc_qr_x, otc_qr_y)  # на OTC один QR (qr110)
            comp.save(screenshot_path)
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
    CookiesExpired (нужен релогин: редирект / нет токена / Demo / форма логина / error-boundary),
    FeedOutage (аутэйдж фида), SetupError (UI/селекторы). WS-фид для BinoOptions НЕ критичен (цена
    из chartData, WS — фолбэк/liveness): не поднялся → лог деградации, БЕЗ raise."""
    if not on_trade(page.url):
        raise CookiesExpired(f'binodex OTC: вход слетел (редирект с /trade на {page.url})')
    # Ранний гейт «сессии нет вовсе» (чистый контекст). На ПРОТУХШЕЙ (но присутствующей) сессии
    # токен только что восстановлен из storage_state → ранний гейт пропустит; Privy очистит его на
    # буте → ловит авторитетная перепроверка ниже.
    if not await _privy_authenticated(page):
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
    # Масштабы графика сбрасываются на дефолт при каждом запуске браузера — выставляем на каждом старте.
    await apply_chart_scale(page)
    # off-zone оптимизация CPU (~40→~22%): прячем UI вне зоны скрина (детект кук/ярлык — в белом списке).
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
    _label_cutout_cache.clear()    # новый браузер/страница → старые вырезки ярлыков невалидны
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
    try:
        await page.wait_for_load_state('networkidle', timeout=TIMEOUT_LONG)
    except (Exception,):
        pass  # постоянный WS-поток может мешать networkidle — не критично (как в init_otc)
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
