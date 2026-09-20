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

from PIL import Image, ImageChops
from playwright.async_api import Page, WebSocket, FloatRect, Position

from classes.Option_class import Option
from classes.price_tracker import WebSocketPriceTracker, symbol_key
from classes.result_types import OperationResult
from binocore.binodex import (SESSION_PROBE_JS as BINODEX_SESSION_PROBE_JS,
                              apply_chart_background as binodex_chart_background,
                              close_modal_button as binodex_close_modal,
                              LoginRateLimited, RATE_LIMIT_PAUSE,
                              rate_limit_sleep as binodex_rate_limit_sleep,
                              has_session as binodex_has_session,
                              watch_session_refresh as binodex_watch_refresh)
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from apps.browser_io import eval_js as _eval, shot as _shot
from apps.otc_login import otc_inline_login
from binocore.shutdown import shutdown_event
from apps.page_nav import goto_retry, on_trade
from logs import init_logger
from settings.config import screenshot_path, database, cookies_pocket_id
from settings.constant import globe_otc_path
from settings.timing import (EVAL_TIMEOUT, TIMEOUT_SHORT, TIMEOUT_MEDIUM, TIMEOUT_LONG,
                             MAX_SCREENSHOT_ATTEMPTS)
from settings.screenshot_set import win_x_otc, win_y_otc, otc_qr_x, otc_qr_y, load_rgba, paste_overlay
from settings.browser_config import (otc_trade_url, otc_select_pair, otc_category_valute, otc_input_pair,
                                     otc_modal_pair_item, screen_zone_otc, otc_settings_btn, otc_login_email,
                                     otc_candle_scale, otc_candle_scale_item,
                                     otc_chart_scale, otc_chart_scale_item, otc_indicators,
                                     otc_theme_open, otc_theme_toggle, otc_wrap_bg, otc_session_keys)

if TYPE_CHECKING:
    from binocore.browser import BrowserSession

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

# Пауза между чтениями chartData — порядка одного кадра отрисовки. Задаётся ЗДЕСЬ, в Python, а НЕ
# внутри страницы: setTimeout подчинён троттлингу таймеров браузера (Chromium зажимает фоновые и
# вложенные цепочки), и серия чтений могла бы молча растянуться на секунды внутри EVAL_TIMEOUT.
# Разносить чтения во времени обязательно: ярлык анимируется, и сэмплы, снятые вплотную, дают
# ОДНО значение — медиана по [X,X,X,Y,Y,Y] вырождается в (X+Y)/2, то есть в среднее двух групп,
# а анимационный выброс, попавший в свою группу, уже не отсекается, а усредняется — на выходе
# цена, которой на ярлыке не было ни в один момент (ревизия 12-09-2026).
CHART_READ_GAP = 0.016   # сек
# Канвас на ~97% прозрачный даже с графиком (свечи/оси/часы ≈ 3% непрозрачных пикселей). Сразу
# после переключения пары канвас бывает пустым (свечи не дорисованы) — такой кадр не постим.
# Порог доли непрозрачных пикселей: ниже = «пусто» → ждём отрисовку (норм. график проходит с запасом).
CANVAS_MIN_OPAQUE = 0.005
CANVAS_READY_SECONDS = 6.0   # сколько ждать отрисовки свечей внутри попытки (отдельно от MAX_SCREENSHOT_ATTEMPTS)

# Потолок на ВСЁ снятие кадра (все попытки вместе). CANVAS_READY_SECONDS ограничивает только
# ожидание отрисовки внутри одной итерации, а сама итерация — это семь _eval по EVAL_TIMEOUT
# каждый (3 чтения цены + toDataURL + 3 чтения) плюс wait_for(TIMEOUT_LONG) и bounding_box;
# при подвисшем рендерере три попытки складывались в минуты. Кадр, снятый после экспирации,
# бесполезен — лучше честно вернуть ошибку и пропустить опцион.
# Бюджет уходит ВНУТРЬ этих шагов (потолок каждого = min(штатный, остаток) через _left_s/_left_ms),
# а не только проверяется между попытками: иначе он оставался номинальным — замер на боевой
# функции давал 99.9с при объявленных 45, и кадр всё равно уходил, только с ценой чужого момента.
SHOT_TOTAL_BUDGET = 45.0   # сек

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
        tracker.ws_ever_connected = True   # см. feed_dead: отличает «отвалился» от «не было вовсе»

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


PAIR_SWITCH_WAIT = 2.0   # сек-потолок ожидания, пока chartData переключится на выбранную пару


async def _wait_chart_symbol(page: Page, symbol: str | None, timeout: float) -> bool:
    """Дождаться, пока движок binodex переключит график на `symbol` (window.chartData.symbol).
    Ранний выход, как только символ совпал; по истечении потолка — False (не критично: вызывающий
    дальше ждёт WS-котировку этой пары). Заменяет прежнюю слепую паузу после клика по паре."""
    if not symbol:
        return False
    deadline = time.monotonic() + timeout
    while True:
        # Потолок на ОДНО чтение — остаток бюджета, а не общий EVAL_TIMEOUT (10с): иначе
        # подвисший evaluate растянул бы «ожидание на 2с» до десяти.
        left = deadline - time.monotonic()
        try:
            data = await _eval(page, CHART_DATA_JS, cap=max(0.2, left))
        except (Exception,):
            data = None
        if isinstance(data, dict) and data.get('symbol') == symbol:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)


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
        # Position — TypedDict Playwright; аннотируем, а не глушим инспекцию (как рядом с FloatRect).
        corner: Position = {'x': 8, 'y': 8}
        await page.locator(screen_zone_otc).first.click(timeout=TIMEOUT_SHORT, position=corner)

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


async def _overlay_backdrop_visible(page: Page) -> bool:
    """Висит ли поверх страницы бэкдроп MUI (промо/онбординг binodex)."""
    try:
        backdrop = page.locator(MODAL_BACKDROP).first
        return bool(await backdrop.count()) and await backdrop.is_visible()
    except (Exception,):
        return False


async def _open_pair_modal(page: Page) -> None:
    """Открыть модалку выбора пары, не упираясь в чужой оверлей.

    Обычный click перед нажатием ждёт, пока элемент перестанут перекрывать, и на модалке
    binodex выжигает ВЕСЬ таймаут — по 10с на КАЖДУЮ пару (11-09-2026: промо-модалка после
    reload, пять пар подряд у трёх программ сразу; пары в итоге выбирались, но опцион уходил
    на минуту позже, а в warning.log ложились простыни ретраев Playwright).

    Поэтому: модалка уже открыта — не трогаем; бэкдроп висит — гасим; не погас — открываем
    меню DOM-событием (dispatch_event проверку перекрытия пропускает).
    """
    if await _pair_modal_open(page):
        return
    await dismiss_modal_backdrop(page)      # дёшево: бэкдропа нет — мгновенный выход
    if await _overlay_backdrop_visible(page):
        logger.warning('OTC: бэкдроп binodex не погас — открываю выбор пары DOM-событием')
        await page.locator(otc_select_pair).first.dispatch_event('click')
        return
    try:
        await page.click(otc_select_pair, timeout=TIMEOUT_MEDIUM)
    except (Exception,):
        await page.locator(otc_select_pair).first.dispatch_event('click')


async def select_otc_pair(page: Page, pair: str) -> bool:
    """Выбрать '<pair> OTC' в модалке binodex (pair вида 'EUR/USD').
    Открыть выбор → категория Валюты → ввести пару → клик по элементу '<pair> ... OTC' →
    закрыть модалку → дождаться, пока сайт прогрузит пару (WS отдаст котировку). True при успехе."""
    try:
        # Снять off-zone на время выбора: модалка выбора пары — вне зоны скрина, под off-zone
        # (visibility:hidden) её пункты не кликаются. off-zone ВОЗВРАЩАЕТСЯ в finally на любом исходе
        # (иначе для нерабочих пар и в 10-мин сне бот бы крутился на полном CPU).
        await _clear_offzone(page)
        await _open_pair_modal(page)
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

        # Ждём, пока движок переключит график на выбранную пару, — по факту (chartData.symbol),
        # а не слепой секундной паузой: в норме выходим за ~0.1–0.3с, потолок PAIR_SWITCH_WAIT.
        # Подтверждение №1 — сам график: chartData.symbol переключился на нужную пару. Раньше
        # результат этого ожидания выбрасывался, и подтверждением служил ТОЛЬКО WS.
        chart_ok = await _wait_chart_symbol(page, symbol_key(pair), PAIR_SWITCH_WAIT)
        await _close_pair_modal(page)        # закрыть модалку (иначе перекрывает график и блокирует прогрузку)

        # Подтверждение №2 — WS-котировка пары (до 8с; рабочие пары приходят за 1–3с). Тик
        # обязан быть СВЕЖЕЕ момента клика: трекер process-global, и цена прошлой сессии/пары
        # иначе подтверждала бы загрузку мгновенно.
        tracker = get_price_tracker()
        target = pair + ' OTC'
        clicked_at = time.time()
        for _ in range(32):
            # Именно НОВЫЙ тик по этому символу, пришедший ПОСЛЕ клика (см. has_tick_since):
            # прежняя проверка через get_price_at(back_ms=...) смотрела в обратную сторону и
            # подтверждалась ценой ДО клика, а tracker.last_tick — флаг по всему фиду, не по паре.
            if tracker.has_tick_since(target, clicked_at):
                await _build_label_cutout(page, target)   # запечь вырезку ярлыка, пока off-zone снят
                return True
            await asyncio.sleep(0.25)

        # WS не подтвердил. Если график УЖЕ показывает нужную пару — работаем: _verify_otc_ready
        # сознательно допускает, что WS не поднимется вовсе (домен фида переезжал .io → .app), и
        # цена кадра всё равно берётся из chartData. Без этой ветки одна промашка перехвата WS
        # означала бы, что НИ ОДНА пара не выбирается: parce_otc перебирает весь список →
        # 'no_pairs' → реинит по кругу, юнит зелёный, в канале тишина и ни одного алерта.
        if chart_ok:
            logger.warning(f"OTC: пара '{pair}' — WS-котировки нет, но график переключился; "
                           f"работаю по chartData (детект фида деградирован)")
            await _build_label_cutout(page, target)
            return True
        logger.warning(f"OTC: пара '{pair}' не прогрузилась на binodex "
                       f"(ни WS-котировки, ни переключения графика за 8с) — пропускаю")
        return False
    except (Exception,) as error:
        logger.warning(f"OTC: ошибка выбора пары {pair} — {error}")
        return False
    finally:
        await apply_offzone(page)   # off-zone восстанавливается на ЛЮБОМ исходе (успех/неудача/ошибка)


async def parce_otc(log_data: Option, session: "BrowserSession", valute: list,
                    deadline: float | None = None) -> bool:
    """Подобрать активную OTC-пару из БД и выбрать её на binodex.
    Сначала берём активные пары, исключая последние использованные (valute) — чтобы актив не
    повторялся в окне. Если после исключения кандидатов не осталось (узкий пул активных OTC на
    этом ТФ сузился до недавно использованных), повторяем запрос БЕЗ исключения — разрешаем
    повтор пары. Иначе бот ложно решил бы «пар нет» и ушёл бы в ожидание-простой, хотя пары
    на сайте есть (просто все недавно крутились). :return: True при успешном выборе.

    `deadline` (monotonic) — потолок на ПЕРЕБОР, от вызывающего (main_app._acquire_otc_pair).
    Перебор идёт по ВСЕМУ списку активных пар, а одна неудачная пара стоит до ~70с (открытие
    модалки, ожидание пункта, ожидание WS-котировки) — на двух десятках пар это десятки минут
    при формально живом юните. Бюджет вышел — отдаём False, вызывающий переждёт штатно."""
    page = session.page('main')
    # Один запрос вместо двух: тянем ПОЛНЫЙ список по ТФ, а недавние пары исключаем в памяти —
    # порядок вьюхи срез сохраняет, а прежний второй SELECT (на «пары исчерпаны исключением»)
    # был ровно тем же запросом без фильтра.
    full = await database.option_data_pocket(exclude_ids=[], tf=log_data.find_timeframe)
    if full is False:  # ошибка пула (контракт execute_query) — не «нет пар»
        return False
    if not full:  # нет активных пар на этом ТФ
        return False
    recent = set(valute)
    active_otc_list = [row for row in full if row['val_id'] not in recent]
    if not active_otc_list:  # все активные пары — недавние → разрешаем повтор
        logger.info('OTC: активные пары исчерпаны исключением недавних — разрешаю повтор')
        active_otc_list = full
    for idx, otc in enumerate(active_otc_list):
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(f'OTC: бюджет подбора пары исчерпан — перебрал {idx} из '
                           f'{len(active_otc_list)} активных пар, прекращаю')
            return False
        log_data.add_option_data(otc)  # log_data.name = 'EUR/USD' (из БД)
        if not await select_otc_pair(page, log_data.name):  # сам ждёт прогрузку пары (WS)
            logger.warning(f"OTC-пара {log_data.name} не выбралась, пробую следующую")
            continue
        log_data.name = log_data.name + ' OTC'
        return True
    return False


async def _read_chart_prices(page: Page, symbol: str | None, count: int,
                             deadline: float | None = None) -> list[float]:
    """`count` чтений window.chartData.price с паузой CHART_READ_GAP между ними. Если symbol задан
    — берём только тики этой пары (chartData.symbol == symbol), чтобы не схватить цену чужой пары
    сразу после переключения.

    Сбой ОДНОГО чтения (страница моргнула) серию НЕ рвёт — собираем что успели.

    `deadline` (monotonic) — бюджет кадра от screenshot_otc. Каждое чтение ждёт не дольше
    ОСТАТКА бюджета: без него серия из трёх чтений на подвисшем рендерере выбирала по
    EVAL_TIMEOUT=10с каждое, то есть один брекет (два вызова) стоил минуту ПОВЕРХ объявленного
    потолка снятия кадра — см. SHOT_TOTAL_BUDGET.

    Логи здесь — ТОЛЬКО debug: функция зовётся ДВАЖДЫ за каждую итерацию ожидания отрисовки
    канваса в screenshot_otc, то есть в сценарии «канвас пуст / символ ещё не переключился»
    (а он ровно тот, где серия и оказывается пустой) на INFO выходило до ~60 строк на один
    опцион. Про неполный брекет один раз сообщает сам screenshot_otc — там это видно целиком,
    и именно там односторонняя медиана превращается в цену поста."""
    out: list[float] = []
    for i in range(count):
        if i:
            await asyncio.sleep(CHART_READ_GAP)
        try:
            data = await _eval(page, CHART_DATA_JS, cap=_left_s(deadline, EVAL_TIMEOUT))
        except (Exception,) as err:
            # type(err).__name__ обязателен: самый вероятный сбой здесь — таймаут _eval, а у
            # asyncio.TimeoutError пустой str(), и строка выродилась бы в «не удалось () —».
            logger.debug(f"OTC: чтение chartData не удалось "
                         f"({type(err).__name__}: {err}) — продолжаю серию")
            continue
        if not isinstance(data, dict):
            continue
        if symbol and data.get('symbol') != symbol:
            continue
        price = data.get('price')
        if isinstance(price, (int, float)):
            out.append(float(price))
    if not out:
        logger.debug(f"OTC: серия чтений chartData пуста (symbol={symbol}, count={count})")
    return out


async def ui_loaded(page: Page, timeout: float) -> bool:
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


async def _privy_token_alive(page: Page, *, on_error: bool) -> bool:
    """privy:token присутствует в localStorage = сессия Privy жива.

    Privy на буте САМ удаляет privy:token, если access-JWT протух, а обновить по
    privy:refresh_token не вышло → апп тихо уходит в Demo (без формы логина). Проверять ПОСЛЕ
    оседания UI: ранний гейт видит токен, только что восстановленный из storage_state, ещё до
    того как Privy его провалидирует и очистит.

    `on_error` — что вернуть, когда прочитать не удалось (страница навигирует, eval бросает).
    Это ЕДИНСТВЕННОЕ, чем различались две прежние функции:
      * False — гейт готовности: не смогли подтвердить сессию, значит не пускаем дальше;
      * True  — трактовка редиректа с /trade: сбой чтения ≠ «токена нет», и винить куки
                (гнать релогин впустую) на нём нельзя.
    """
    try:
        return bool(await _eval(page, BINODEX_SESSION_PROBE_JS, list(otc_session_keys), cap=5))
    except (Exception,):
        return on_error


async def _error_boundary_shown(page: Page) -> bool:
    """binodex показал React error-boundary («Something went wrong») — апп упал на буте. На
    битой/протухшей сессии Privy/инициализация бросает исключение → boundary, причём privy:token
    может ОСТАТЬСЯ (апп упал до его очистки), поэтому token-чек такой случай не ловит. Чистый
    контекст грузится без этого → трактуем как мёртвую сессию → релогин."""
    try:
        return bool(await _eval(
            page, "() => (document.body.innerText || '').includes('Something went wrong')", cap=5))
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


async def _raise_off_trade(detail: str, authed: bool, check_backend: bool = True) -> None:
    """binodex увёл с /trade — развести причину. ОДНА реализация на оба места (_raise_ui_dead и
    _verify_otc_ready): развязка тут нетривиальная и копий у неё быть не должно — расходятся
    молча, а цена расхождения — релогин вместо ожидания (или наоборот) на живой сессии.

    Порядок и смысл (docs/lifecycle-standard §4.5):
      • auth-API (api.binodex.app) 5xx браузер-фри → FeedOutage: падение бэкенда доминирует,
        релогин/прокси/движок бесполезны, пока API лежит (грабли 2026-07-23);
      • токен ЖИВ → апп-шелл не поднялся и фронт САМ сбросил на лендинг/?boot-recovery= при живой
        сессии: аутэйдж ИХ фронта, не куки → SetupError(mounted=False), прокси-фолбэк + переподъём;
      • токена нет → storage_state реально протух → CookiesExpired (релогин).
    `check_backend=False` — когда вызывающий уже проверил бэкенд (не ходить по сети дважды).
    Всегда бросает."""
    if check_backend:
        await _raise_if_backend_down(detail)
    if authed:
        raise SetupError(f'binodex OTC: {detail} при живой авторизации — аутэйдж фронта binodex '
                         f'(boot-recovery), не куки', mounted=False)
    raise CookiesExpired(f'binodex OTC: {detail}, нет признака сессии — она протухла')


async def _raise_ui_dead(page: Page, detail: str) -> None:
    """UI не поднялся ИЛИ редирект с /trade — развести причину на классы (канон, docs/lifecycle-
    standard §4.5). Работает и на лендинге/boot-recovery (localStorage тот же origin, feed_alive
    браузер-фри, апп-шелл там не смонтирован → mounted=False). Всегда бросает:
      • видна форма логина → CookiesExpired (отвал кук → релогин);
      • формы нет, market-WS молчит браузер-фри (feed_alive=False) → FeedOutage (аутэйдж фида);
      • формы нет, фид ЖИВ, нет признака сессии (binodex очистил → Demo) → CookiesExpired (сессия мертва, релогин);
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
    authed = await _privy_token_alive(page, on_error=True)
    if not on_trade(page.url):
        # backend уже проверен выше по функции — второй раз по сети не ходим.
        await _raise_off_trade(f'{detail} + редирект с /trade на {page.url}', authed,
                               check_backend=False)
    # Токен очищен (Privy сбросил протухшую сессию на буте) → реальная смерть сессии → релогин.
    # Проверяем ДО error-boundary: иначе «Something went wrong» поверх мёртвой сессии увёл бы в
    # выживание-без-релогина вместо восстановления кук.
    if not authed:
        raise CookiesExpired(f'binodex OTC: {detail} + нет признака сессии (Demo) — она протухла')
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


# Модалка binodex (онбординг/промо) поверх страницы: её бэкдроп ест pointer events, и клик по
# кнопке масштаба 5 секунд ретраится впустую. Локатор при этом РЕЗОЛВИТСЯ — по логу не видно, что
# мешает именно оверлей. Класс MUI (`MuiBackdrop-root`) стабилен: это имя компонента, а не хеш
# сборки, в отличие от соседнего суффикса `css-3j5o61`.
MODAL_BACKDROP = '.MuiBackdrop-root'


async def dismiss_modal_backdrop(page: Page) -> None:
    """Погасить модалку binodex, если она открыта.

    ЗАЩИТА, а не исправление живого дефекта: у текущего аккаунта модалка уже погашена и лежит в
    storage_state. Но стоит обновить куки с чистого логина — и она вернётся, а сломает молча: клик
    по кнопке масштаба будет ретраиться впустую, локатор при этом резолвится, и в логе не видно,
    что мешает оверлей (ровно так 20-08-2026 масштаб перестал применяться у английских
    OTC-программ семьи). Проверка дешёвая: нет бэкдропа — выходим сразу.

    Лесенка от дешёвого к грубому, каждый шаг проверяется исчезновением бэкдропа: Escape
    (штатный путь MUI) → крестик самой модалки (ядро; только он убирает окно ИЗ КАДРА) → клик
    в КРАЙ бэкдропа (центр перекрывает картинка анонса) → клик по бэкдропу DOM-событием, которое
    перекрытие не проверяет вовсе. Не закрылась — не падаем: вызывающий код всё равно кликает
    пункты через dispatch_event."""
    backdrop = page.locator(MODAL_BACKDROP).first
    try:
        if not await backdrop.count() or not await backdrop.is_visible():
            return
        await page.keyboard.press('Escape')
        await backdrop.wait_for(state='hidden', timeout=2000)
        logger.info('OTC: модалка binodex закрыта (Escape) перед настройкой графика')
        return
    except (Exception,):
        pass
    # Крестик самой модалки. Единственный путь, который убирает её ИЗ КАДРА, а не просто
    # перестаёт мешать кликам: Escape и клик по бэкдропу берут лишь те окна, что закрываются
    # снаружи, а модалку-анонс binodex рисует картинкой ПОВЕРХ бэкдропа. Ядро ищет закрывашку
    # по служебным признакам (aria-label/data-testid/класс close, значок ×) и НЕ трогает кнопки
    # с подписями вроде «OK» — такая в промо увела бы бота с /trade.
    closed_by = await binodex_close_modal(page)
    if closed_by:
        try:
            await backdrop.wait_for(state='hidden', timeout=2000)
            logger.info(f'OTC: модалка binodex закрыта крестиком ({closed_by})')
            return
        except (Exception,):
            pass
    # Клик в КРАЙ бэкдропа, а не в центр: поверх него binodex рисует модалку-анонс
    # (`announcement-images/*.webp`), и её картинка перехватывает pointer events ровно по центру —
    # Playwright ретраит клик до таймаута и пишет полотно «subtree intercepts pointer events»
    # (EnglishGold, ночь 18-09-2026: так не выбрались пять пар подряд). Угол бэкдропа свободен.
    try:
        edge: Position = {'x': 4, 'y': 4}
        await backdrop.click(position=edge, timeout=1500)
        await backdrop.wait_for(state='hidden', timeout=2000)
        logger.info('OTC: модалка binodex закрыта кликом по краю бэкдропа')
        return
    except (Exception,):
        pass
    # Последний путь: клик DOM-событием. dispatch_event не проверяет перекрытие вовсе, то есть
    # проходит и сквозь картинку анонса; MUI слушает onClick на самом бэкдропе, событие всплывает.
    try:
        await backdrop.dispatch_event('click')
        await backdrop.wait_for(state='hidden', timeout=2000)
        logger.info('OTC: модалка binodex закрыта DOM-событием по бэкдропу')
    except (Exception,) as error:
        # Первая строка: Playwright кладёт в сообщение весь call log (десятки строк на каждый
        # ретрай клика), и в warning.log это полотно топит вокруг себя всё остальное.
        logger.warning(f'OTC: модалка binodex не закрылась ({str(error).splitlines()[0]}) — '
                       f'настройки графика выставляем через DOM-события')


# Остаток бюджета для ОДНОЙ операции. Без него потолок был номинальным: бюджет проверялся
# только МЕЖДУ шагами, а каждый шаг тянул свой штатный таймаут (click 5с, wait_for 5с, _eval до
# EVAL_TIMEOUT=10с) — на залипшем UI ремонт выбирал ~90–100с против объявленных 25с. Теперь
# таймаут каждой операции = min(штатный, остаток), то есть потолок соблюдается по-настоящему.
# Пол в 0.2с намеренный: ноль/отрицательное Playwright понял бы как «ждать бесконечно», а гейты
# перед шагами всё равно выходят раньше, чем остаток дойдёт до пола.
_OP_FLOOR = 0.2   # сек


# Потолок УБОРКИ (вернуть скрытый ярлык). Он намеренно НЕ режется остатком бюджета: уборка идёт
# после того, как кадр уже снят, и урезание её до _OP_FLOOR на исчерпанном бюджете оставило бы
# страницу с inline-скрытым ярлыком пары до следующей пересборки вырезки.
_CLEANUP_CAP = 2.0   # сек


def _left_s(deadline: float | None, cap: float) -> float:
    """Сколько СЕКУНД можно ждать: не дольше штатного потолка и не дольше остатка бюджета."""
    if deadline is None:
        return cap
    return max(_OP_FLOOR, min(cap, deadline - time.monotonic()))


def _left_ms(deadline: float | None, cap_ms: float) -> float:
    """То же в МИЛЛИСЕКУНДАХ — для таймаутов Playwright (click/wait_for)."""
    return _left_s(deadline, cap_ms / 1000) * 1000


async def apply_chart_scale(page: Page, deadline: float | None = None) -> None:
    """Выставить масштабы графика: свеча '30S' → график 'H1'. binodex сбрасывает их на дефолт
    при КАЖДОМ запуске браузера (новый контекст из storage_state → M30; reload в рамках сессии
    значение держит — проверено), а раньше штатный setup шёл только на холодном
    релогине. Поэтому применяем здесь, в init_otc, на каждом старте браузера — и вдобавок перед
    каждым опционом через ensure_chart_setup (сброс случается и в течение суток, без нашего
    рестарта: новая версия фронта / переинициализация чарта). Порядок важен: смена
    масштаба свечи сбрасывает масштаб графика, поэтому график (H1) ставим ПОСЛЕДНИМ. Пункты —
    по тексту (порядок списков binodex плавает). Ошибки не критичны для запуска (масштаб — оформление
    кадра, не данные) — логируем и продолжаем.

    `deadline` (monotonic) — общий бюджет ремонта от ensure_chart_setup. На залипшем UI одна пара
    масштабов стоит до 30с (на каждый: click 5 + wait visible 5 + wait hidden 5), а вызывается это
    перед каждым опционом. None (холодный старт из _verify_otc_ready) — без ограничения: там мы
    никуда не спешим и оформление нужно выставить целиком.

    Бюджет проверяется ОДИН раз, ДО цикла — «оба масштаба или ни одного». Проверять перед каждым
    шагом нельзя: смена масштаба свечи СБРАСЫВАЕТ масштаб графика (см. выше про порядок), поэтому
    выход по бюджету между двумя шагами оставил бы график на дефолтном M30 — кадр уехал бы с чужим
    таймфреймом, то есть хуже, чем если вообще не трогать."""
    if deadline is not None and time.monotonic() >= deadline:
        logger.warning('OTC: бюджет ремонта оформления исчерпан до масштабов — не трогаю их '
                       '(частичная правка сбила бы масштаб графика), оставляю следующему опциону')
        return
    await dismiss_modal_backdrop(page)
    for opener, item, name in ((otc_candle_scale, otc_candle_scale_item, 'свеча 30S'),
                               (otc_chart_scale, otc_chart_scale_item, 'график H1')):
        try:
            try:
                await page.locator(opener).first.click(timeout=_left_ms(deadline, TIMEOUT_SHORT))
            except (Exception,):
                # Бэкдроп модалки не ушёл: открываем дропдаун DOM-событием, как и его пункт ниже.
                # Модалку binodex рисует ПОРТАЛОМ в body, backdrop (MuiBackdrop-root) растянут на
                # весь вьюпорт и забирает pointer events — обычный клик выжигает таймаут и не
                # доходит, оба масштаба дают «0 из 2» → SetupError при живых куках, сайте и WS.
                # Не теория: 11-09-2026 в 20:21–20:31 так погасли 25 пар BinodexScreens на шести
                # нодах разом (восстановились сами через ~13 мин). Реестр: scale-opener-guard.
                await page.locator(opener).first.dispatch_event('click')
            item_loc = page.locator(item).first
            await item_loc.wait_for(state='visible', timeout=_left_ms(deadline, TIMEOUT_SHORT))
            # Контейнер-дропдаун binodex (.profile_add_wrap_selected_wrap_options) перехватывает
            # pointer events на своём же пункте (overlay/стэкинг) — обычный .click() ловит «intercepts
            # pointer events». Кликаем напрямую DOM-событием: пункт уже зарезолвлен и видим, оверлей
            # при dispatch_event не помеха (проверка перекрытия пропускается).
            await item_loc.dispatch_event('click')
            # Дропдаун закрывается сам — ждём именно этого, а не фиксированные полсекунды.
            # Не закрылся (редкий залипший оверлей) — не страшно: следующий шаг открывает своё меню.
            try:
                await item_loc.wait_for(state='hidden', timeout=_left_ms(deadline, TIMEOUT_SHORT))
            except (Exception,):
                pass
        except (Exception,) as error:
            logger.warning(f"OTC: не удалось выставить масштаб ({name}): {error}")


# ── Оформление графика: индикаторы + проверка, что масштаб/индикаторы не сбились ──────────────────
# Индикаторы графика для OTC-кадра: (пункт меню #setup_indicators, текст чипа-легенды на графике).
# Whale Absorption — оверлей (профиль объёма по правому краю поверх свечей), своей панели снизу не
# заводит, поэтому идёт первым и на расклад панелей не влияет. Чип легенды — 'Whale' (рядом binodex
# рисует параметры '150, 28, 2'). Дальше порядок = порядок панелей: Volume включаем ПОСЛЕДНИМ,
# чтобы его панель осела НИЖНЕЙ (под Stochastic).
OTC_CHART_INDICATORS = (('Whale Absorption', 'Whale'), ('Stochastic', 'Stoch'), ('Volume', 'VOL'))


async def _indicators_menu_open(page: Page, cap: float | None = None) -> bool:
    """Открыто ли меню индикаторов (видимы пункты button.chart_indicator).

    `cap` — потолок на ЭТО чтение (секунды). Нужен, когда проверку зовут под общим бюджетом
    ремонта: без него один evaluate на подвисшей странице ждёт EVAL_TIMEOUT=10с, и «ожидание
    1.5с» в _wait_menu_open растягивалось до десяти — ровно та гранулярность, из-за которой
    потолок SETUP_TOTAL_BUDGET оставался номинальным."""
    try:
        return await _eval(page,
            "() => [...document.querySelectorAll('button.chart_indicator')].some(b => b.offsetParent !== null)",
            cap=cap)
    except (Exception,):
        return False


# Где искать чипы легенды: 'box' — только контейнер графика, 'document' — вся страница.
# None — ещё не калибровали (сравниваем оба скана, см. ниже).
# Модульная переменная, а не аргумент: вёрстка binodex в пределах ОДНОЙ загрузки страницы не
# меняется, а функцию зовут из нескольких мест. Сбрасывается в init_otc вместе с прочими кэшами
# страницы: новая версия фронта могла перенести легенду, а залипшая область означала бы
# «чипов нет» → клик по пункту меню ВЫКЛЮЧИЛ бы работающие индикаторы (клик ТОГГЛИТ).
_legend_scope: str | None = None


def reset_legend_scope() -> None:
    """Забыть откалиброванную область поиска чипов легенды — DOM пересоздан.

    Зовут из двух мест: новая страница (init_otc) и reload перед опционом (reload_otc_page).
    Метка _LEGEND_ROOT_ATTR живёт на узле СТАРОГО дерева и исчезает вместе с ним, а залипший
    scope='box' сканировал бы по ней и возвращал null — то есть «чипы не прочитались», и
    ensure_chart_setup на ЭТОМ опционе оформление не проверял. На живых логах 15-09 это дало
    калибровку ЧЕРЕЗ опцион: половина опционов прошла без проверки масштабов и индикаторов."""
    global _legend_scope
    _legend_scope = None


# Метка контейнера легенды: при калибровке помечаем найденный узел атрибутом и дальше
# сканируем по нему. Держать ссылку на узел между вызовами нельзя (каждый evaluate — свой
# контекст), а атрибут переживает вызовы и исчезает вместе с перерисовкой узла — тогда
# scope='box' вернёт null, и Python перекалибруется (см. _missing_indicators).
_LEGEND_ROOT_ATTR = 'data-legend-root'

# Сколько уровней вверх от канваса пробуем при поиске контейнера легенды. Родитель канваса не
# подошёл на живом binodex (14-09: калибровка на всех трёх инстансах дала 'document'), а вот
# общий предок графика и легенды — на несколько уровней выше. Потолок нужен, чтобы не дойти до
# body: «контейнер» размером со страницу не экономит ничего.
_LEGEND_ROOT_MAX_UP = 6

# Чип легенды — это span с ТОЧНЫМ именем индикатора внутри бейджа, у которого есть кнопки
# действий (глаз/карандаш/крестик — картинки /img/chart/*.svg). Признак по картинке, а не по
# классу: CSS-хэши binodex (_badge_1sh1r_92) плавают между сборками, а пути картинок стабильны.
# Берём САМЫЙ ВНУТРЕННИЙ элемент (без вложенных span/div), иначе один чип считался бы дважды —
# за себя и за обёртку с тем же textContent.
_LEGEND_SCAN_JS = (
    "(arg) => { const scan = (root) => { const counts = {};"
    "   for (const b of arg.badges) counts[b] = 0;"
    "   for (const el of root.querySelectorAll('span')) {"
    "     const t = (el.textContent || '').trim();"
    "     if (!arg.badges.includes(t)) continue;"
    "     if (el.querySelector('span,div')) continue;"
    "     const chip = el.closest('div');"
    "     if (!chip || !chip.querySelector('img[src*=\"/img/chart/\"]')) continue;"
    "     counts[t]++; }"
    "   return counts; };"
    " const full = (c) => arg.badges.every(b => c[b] > 0);"
    " if (arg.scope === 'box') {"
    "   const root = document.querySelector('[' + arg.rootAttr + ']');"
    "   return {near: root ? scan(root) : null}; }"
    " if (arg.scope === 'document') return {all: scan(document)};"
    # Калибровка: документ сканируем всегда (его результат и есть ответ), контейнер ищем
    # ТОЛЬКО при полном наборе — по неполному о раскладке судить нельзя, а лишний обход в
    # сценарии ремонта сделал бы хуже, чем было до оптимизации.
    " const all = scan(document);"
    " if (!full(all)) return {all, near: null};"
    # Ищем МИНИМАЛЬНОГО предка канваса, внутри которого видны все чипы: родитель канваса на
    # живом binodex их не содержит, легенда лежит выше. Найденный узел метим атрибутом.
    " for (const el of document.querySelectorAll('[' + arg.rootAttr + ']'))"
    "   el.removeAttribute(arg.rootAttr);"
    " let node = document.querySelector(arg.zone);"
    " for (let i = 0; node && i <= arg.maxUp; i++, node = node.parentElement) {"
    "   const near = scan(node);"
    "   if (full(near)) {"
    "     node.setAttribute(arg.rootAttr, '');"
    "     return {all, near, up: i}; } }"
    " return {all, near: null}; }")

# Снять ОДНУ лишнюю копию индикатора: крестик на последнем его чипе. По одной за вызов —
# после клика React перестраивает легенду, и пачка кликов по устаревшим узлам не проходит.
# Выключить индикатор через меню НЕЛЬЗЯ (см. apply_chart_indicators): там клик добавляет.
_LEGEND_DROP_JS = (
    "(arg) => { const chips = [];"
    "   for (const el of document.querySelectorAll('span')) {"
    "     if ((el.textContent || '').trim() !== arg.badge) continue;"
    "     if (el.querySelector('span,div')) continue;"
    "     const chip = el.closest('div');"
    "     if (chip && chip.querySelector('img[src*=\"/img/chart/\"]')) chips.push(chip); }"
    "   if (chips.length < 2) return 'no-dup';"
    "   const btn = chips[chips.length - 1]"
    "     .querySelector('button img[src*=\"cross\"]');"
    "   if (!btn) return 'no-button';"
    "   btn.closest('button').click();"
    "   return 'clicked'; }")


async def _indicator_counts(page: Page, cap: float | None = None) -> dict[str, int] | None:
    """СКОЛЬКО копий каждого индикатора сейчас на графике — по чипам легенды.

    Считаем именно количество, а не факт наличия: клик по пункту меню binodex ДОБАВЛЯЕТ ещё
    один экземпляр индикатора (проверено 20-09-2026 на живой странице: 1 → 2 → 3), поэтому
    каждый ложный «ремонт» — когда легенда ещё не отрисована после reload или смены пары и
    скан вернул пусто — навешивал вторую копию поверх рабочей. К вечеру на боевых кадрах было
    по ТРИ Stochastic: свечи сжаты в половину кадра, и это уходило подписчикам.

    `None` — прочитать НЕ удалось (страница моргнула/evaluate бросил): «не знаю» НЕЛЬЗЯ
    трактовать как «выключено всё», иначе ремонт добавит копии там, где всё было в порядке.
    Вызывающий на None просто ничего не трогает — следующий опцион перечитает."""
    global _legend_scope
    badges = [badge for _, badge in OTC_CHART_INDICATORS]
    scope = _legend_scope or 'calibrate'
    try:
        res = await _eval(page, _LEGEND_SCAN_JS,
                          {'badges': badges, 'zone': screen_zone_otc, 'scope': scope,
                           'rootAttr': _LEGEND_ROOT_ATTR, 'maxUp': _LEGEND_ROOT_MAX_UP}, cap=cap)
    except (Exception,) as err:
        logger.debug(f'OTC: чипы индикаторов не прочитались ({err}) — состояние неизвестно')
        return None
    if not isinstance(res, dict):
        return None

    near, whole = res.get('near'), res.get('all')
    if scope == 'box':
        # Контейнер уже признан валидным; пропал (сменилась вёрстка) — считаем, что не знаем.
        if near is None:
            _legend_scope = None      # перекалибруемся на следующем вызове
            return None
        counts = near
    elif scope == 'document':
        counts = whole
    else:
        counts = whole
        # Только ПОЛНЫЙ набор: по одному-двум горящим чипам судить о раскладке нельзя (см.
        # докстринг). Неполное наблюдение оставляет scope=None — перекалибруемся на след. вызове.
        # А вот при полном наборе решение принимается ВСЕГДА, в том числе когда контейнер не
        # нашёлся (near=None: канваса нет в DOM либо легенда лежит дальше _LEGEND_ROOT_MAX_UP).
        if isinstance(whole, dict) and all(whole.get(b, 0) > 0 for b in badges):
            same = isinstance(near, dict) and all(near.get(b, 0) == whole[b] for b in badges)
            _legend_scope = 'box' if same else 'document'
            up = res.get('up')
            logger.info(f'OTC: область поиска чипов легенды — {_legend_scope}'
                        + (f' (предок канваса +{up})' if _legend_scope == 'box' and up is not None else ''))
    if not isinstance(counts, dict):
        return None
    return {badge: int(counts.get(badge, 0) or 0) for badge in badges}


# Пауза перед ПОДТВЕРЖДАЮЩИМ чтением легенды. Нужна ровно в одном случае — когда первое
# чтение дало ВСЕ НУЛИ: «индикаторов действительно нет» и «легенда ещё не отрисована» дают
# одинаковый ответ, а решения у них противоположные. По ложному нулю ремонт добавит копии;
# снимутся они следующим проходом, но это лишний цикл добавить-снять прямо перед опционом.
# 0.6с хватает: чипы появляются вместе с первой отрисовкой чарта, а платим мы эту паузу только
# в редком «всё по нулям», не на каждом опционе.
_LEGEND_CONFIRM_PAUSE_MS = 600


async def indicator_counts_confirmed(page: Page, cap: float | None = None) -> dict[str, int] | None:
    """Счётчики чипов с подтверждением пустоты (приём тот же, что у confirm_stable для фида).

    Ноль по ВСЕМ индикаторам перечитываем через паузу: это единственное показание, которое
    нельзя отличить от «страница ещё не дорисовала легенду», и именно по нему принимается
    решение КЛИКАТЬ. Непустой ответ подтверждения не требует — лишняя копия видна сразу."""
    counts = await _indicator_counts(page, cap=cap)
    if counts is None or any(counts.values()):
        return counts
    await page.wait_for_timeout(_LEGEND_CONFIRM_PAUSE_MS)
    second = await _indicator_counts(page, cap=cap)
    if second is None:
        return None                      # «не знаю» — вызывающий не трогает UI
    if any(second.values()):
        logger.debug('OTC: первое чтение легенды дало пусто, подтверждение нашло чипы — '
                     'легенда просто не была отрисована')
    return second


def _missing_from(counts: dict[str, int]) -> list[tuple[str, str]]:
    """Индикаторы, которых на графике НЕТ ВОВСЕ (их и только их добавляем через меню)."""
    return [(name, badge) for name, badge in OTC_CHART_INDICATORS if counts.get(badge, 0) == 0]


def _extra_from(counts: dict[str, int]) -> dict[str, int]:
    """Сколько ЛИШНИХ копий висит сверх одной у каждого индикатора."""
    return {badge: counts[badge] - 1 for _, badge in OTC_CHART_INDICATORS if counts.get(badge, 0) > 1}


async def drop_extra_indicators(page: Page, extra: dict[str, int],
                                deadline: float | None = None) -> None:
    """Снять лишние копии индикаторов — крестиком на чипе легенды.

    Через меню это невозможно: там клик ДОБАВЛЯЕТ копию (см. _indicator_counts). Удаляем по
    одной за вызов и перечитываем: после клика binodex перестраивает легенду, и пачка кликов
    по устаревшим узлам не проходит. Потолок попыток — сумма лишних плюс запас на промах."""
    for badge, count in extra.items():
        for _ in range(count + 2):
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning('OTC: бюджет ремонта исчерпан — лишние копии индикаторов '
                               'оставляю следующему опциону')
                return
            try:
                verdict = await _eval(page, _LEGEND_DROP_JS, {'badge': badge},
                                      cap=_left_s(deadline, EVAL_TIMEOUT))
            except (Exception,) as err:
                logger.debug(f'OTC: снятие лишней копии {badge} не удалось ({err})')
                break
            if verdict != 'clicked':
                if verdict == 'no-button':
                    logger.warning(f'OTC: у чипа {badge} нет кнопки удаления — вёрстка легенды '
                                   f'изменилась, лишние копии снять не могу')
                break
            await page.wait_for_timeout(300)


async def apply_chart_indicators(page: Page, missing: list[tuple[str, str]] | None = None,
                                 deadline: float | None = None) -> None:
    """Включить индикаторы графика (OTC_CHART_INDICATORS) для OTC-кадра — рисуются binodex на том же
    канвасе, что и свечи, поэтому попадают в toDataURL-кадр (screenshot_otc) без отдельного слоя.
    Меню #setup_indicators, пункты button.chart_indicator выбираются ПО ТЕКСТУ (порядок списка
    binodex плавает). Клик по пункту ДОБАВЛЯЕТ индикатор и закрывает модалку, поэтому: (1) меню
    переоткрываем перед каждым; (2) включаем только те, которых НЕТ ВОВСЕ. Раньше здесь стояло
    «клик ТОГГЛИТ» — так было у прежнего фронта; сейчас (проверено 20-09-2026 на живой странице)
    повторный клик не выключает индикатор, а вешает ВТОРУЮ копию: 1 → 2 → 3. Отсюда и брались
    три Stochastic на боевых кадрах — каждый ложный «ремонт» добавлял панель. Выключить
    индикатор через меню нельзя вообще, только крестиком на чипе (drop_extra_indicators). binodex сбрасывает индикаторы на дефолт (выкл) при новом
    контексте, как и масштаб (в сохранённом storage_state ключей `indicators/*` нет) → на холодном
    старте (_verify_otc_ready) включаем с нуля. Порядок важен: Volume ПОСЛЕДНИМ (нижняя панель);
    Whale Absorption — оверлей, панели не заводит. Ошибки не критичны (индикатор — оформление кадра,
    не данные) — лог.

    `missing` — что именно включать, список (name, badge) от вызывающего (ensure_chart_setup).
    `None` означает ХОЛОДНЫЙ СТАРТ: индикаторы заведомо выключены, включаем все. Никогда не
    вычисляем список сами: «не смог прочитать» и «выключено всё» — разные вещи, развести их может
    только вызывающий (см. _missing_indicators).

    После кликов ОДИН раз перечитываем чипы и добираем не появившиеся: клик мог не дойти (модалка
    не открылась / пункт перерисовался), а без проверки индикатор оставался бы выключенным до
    следующего опциона.

    `deadline` (monotonic) — общий бюджет ремонта от ensure_chart_setup. Передаётся и ВНУТРЬ
    проходов (_click_indicators проверяет его перед каждым индикатором): гейта только на повторном
    проходе не хватало — один залипший индикатор стоит до ~40с (меню не открывается, чип не
    зажигается), и первый же проход по трём выбирал минуты против объявленных секунд."""
    if not otc_indicators:  # старая БД без строки setup_indicators — тихо пропускаем
        return
    if missing is None:
        missing = list(OTC_CHART_INDICATORS)
    await _click_indicators(page, missing, deadline=deadline)
    # cap обязателен: без него контрольное чтение ждёт общий EVAL_TIMEOUT ПОВЕРХ бюджета
    # ремонта, и на подвисшей SPA это +10с молчания ленты перед первым постом опциона.
    counts = await _indicator_counts(page, cap=_left_s(deadline, EVAL_TIMEOUT))
    left = None if counts is None else _missing_from(counts)
    retry = [item for item in missing if left is not None and item in left]
    if retry and deadline is not None and time.monotonic() >= deadline:
        logger.warning(f"OTC: индикаторы не включились ({', '.join(n for n, _ in retry)}), "
                       f"бюджет ремонта исчерпан — повтор пропускаю, кадр уйдёт без них")
    elif retry:
        logger.warning(f"OTC: индикаторы не включились с первого раза "
                       f"({', '.join(n for n, _ in retry)}) — повторяю")
        await _click_indicators(page, retry, deadline=deadline)
        counts = await _indicator_counts(page, cap=_left_s(deadline, EVAL_TIMEOUT))
        left = None if counts is None else _missing_from(counts)
        still = [item for item in retry if left is not None and item in left]
        if still:
            logger.warning(f"OTC: индикаторы так и не включились "
                           f"({', '.join(n for n, _ in still)}) — кадр уйдёт без них")


async def _wait_menu_open(page: Page, timeout: float = 1.5) -> bool:
    """Дождаться открытия меню индикаторов ТЕМ ЖЕ предикатом, каким открытость определяется
    везде в файле (_indicators_menu_open).

    Ждать видимости 'button.chart_indicator' локатором нельзя: закрытое меню оставляет свои
    кнопки в DOM, и первый матч может оказаться именно невидимым узлом — предикаты разошлись бы,
    а мы выжигали бы TIMEOUT_SHORT на КАЖДЫЙ индикатор. При пропавшем селекторе меню проход
    вырос бы с ~18 до ~39 с, и это перед каждым опционом (ревизия 12-09-2026)."""
    deadline = time.monotonic() + timeout
    while True:
        if await _indicators_menu_open(page, cap=max(_OP_FLOOR, deadline - time.monotonic())):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)


async def _wait_indicator_on(page: Page, name: str, timeout: float = 3.0) -> bool:
    """Дождаться, что индикатор реально включился — по чипу легенды на графике.

    Вместо слепой паузы: клик по пункту меню ТОГГЛИТ индикатор, и раньше мы просто ждали 700 мс
    на каждый, то есть ~2.1 с за проход и при этом без всякой гарантии. Проверку делает
    _missing_indicators — он и читает чипы легенды (один обход DOM), а `name` тут только ключ
    записи; отдельный аргумент под чип был лишним и не использовался.

    `None` от _missing_indicators — «прочитать не удалось»: не трактуем как готовность, просто
    пробуем ещё раз до потолка."""
    deadline = time.monotonic() + timeout
    while True:
        counts = await _indicator_counts(page, cap=max(_OP_FLOOR, deadline - time.monotonic()))
        missing = None if counts is None else _missing_from(counts)
        if missing is not None and not any(item_name == name for item_name, _ in missing):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.15)


async def _click_indicators(page: Page, missing: list[tuple[str, str]],
                            deadline: float | None = None) -> None:
    """Один проход кликов по пунктам меню индикаторов.

    `deadline` (monotonic) — бюджет ремонта от ensure_chart_setup, проверяется ПЕРЕД каждым
    индикатором. Это и есть место, где бюджет реально ограничивает работу: один залипший
    индикатор стоит до ~40с (открытие меню до 10 + click 5 + _wait_menu_open 1.5 + клик-eval
    до 10 + _wait_indicator_on до 13), а проход идёт по трём. None — без ограничения (холодный
    старт из _verify_otc_ready: там оформление надо выставить целиком).

    Прерваться ПОСЕРЕДИНЕ здесь безопасно (в отличие от пары масштабов, где частичная правка
    сбивает масштаб графика): `missing` всегда идёт в порядке OTC_CHART_INDICATORS, поэтому
    break оставляет включённым ПРЕФИКС списка, а следующий проход добирает хвост — Volume
    по-прежнему окажется включён последним, и его панель осядет нижней."""
    await dismiss_modal_backdrop(page)
    for menu_name, _badge in missing:
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(f"OTC: бюджет ремонта оформления исчерпан — индикатор '{menu_name}' "
                           f"не включён, оставляю следующему опциону")
            break
        try:
            if not await _indicators_menu_open(page, cap=_left_s(deadline, EVAL_TIMEOUT)):
                try:
                    await page.locator(otc_indicators).first.click(timeout=_left_ms(deadline, TIMEOUT_SHORT))
                except (Exception,):
                    # Бэкдроп модалки-анонса binodex ест pointer events, и обычный клик выжигает
                    # весь таймаут — по КАЖДОМУ индикатору, ДВА прохода (замер: 20с на проход,
                    # кадр уходит без индикаторов, пока модалка висит). dispatch_event перекрытие
                    # не проверяет вовсе; тот же фолбэк давно стоит в apply_chart_scale.
                    await page.locator(otc_indicators).first.dispatch_event('click')
                # Ждём факт открытия, а не «полсекунды на всякий случай» — и тем же предикатом,
                # что и остальной файл (см. _wait_menu_open).
                if not await _wait_menu_open(page, timeout=_left_s(deadline, 1.5)):
                    logger.warning(f"OTC: меню индикаторов не открылось — пропускаю '{menu_name}'")
                    continue
            clicked = await _eval(page,
                "(name) => { const b = [...document.querySelectorAll('button.chart_indicator')]"
                ".find(x => (x.innerText || '').trim() === name); if (!b) return false; b.click(); return true; }",
                menu_name, cap=_left_s(deadline, EVAL_TIMEOUT))
            if not clicked:
                logger.warning(f"OTC: индикатор '{menu_name}' не найден в меню")
            elif not await _wait_indicator_on(page, menu_name,
                                              timeout=_left_s(deadline, 3.0)):
                logger.warning(f"OTC: индикатор '{menu_name}' не зажёгся на графике за отведённое "
                               f"время — проверю следующим проходом")
        except (Exception,) as error:
            logger.warning(f"OTC: не удалось включить индикатор '{menu_name}': {error}")
    # На случай ошибки (пункт не найден → модалка осталась открытой) закрываем меню, чтобы не мешало.
    try:
        if await _indicators_menu_open(page, cap=_left_s(deadline, EVAL_TIMEOUT)):
            try:
                await page.locator(otc_indicators).first.click(timeout=_left_ms(deadline, TIMEOUT_SHORT))
            except (Exception,):
                await page.locator(otc_indicators).first.dispatch_event('click')
    except (Exception,):
        pass


def _scale_label(selector: str | None) -> str:
    """Ожидаемое значение масштаба из селектора пункта в БД (`… >> text="30S"` → `30S`). Значение
    живёт в ОДНОМ месте (строка binodex_settings) — своей константы в коде не заводим, иначе правка
    селектора в БД молча разъезжалась бы с проверкой. Не распарсили → '' (проверка вырождается в
    «перевыставить вслепую», см. _scale_drifted)."""
    match = re.search(r'text=[\'"]([^\'"]+)[\'"]', selector or '')
    return match.group(1) if match else ''


CANDLE_SCALE_LABEL = _scale_label(otc_candle_scale_item)   # '30S'
CHART_SCALE_LABEL = _scale_label(otc_chart_scale_item)     # 'H1'

# Текст кнопки-открывашки (#setup_candle_scale/#setup_chart_scale) = выбранное сейчас значение.
# textContent, а НЕ innerText: под off-zone (visibility:hidden) innerText отдаёт пусто, и проверка
# всегда считала бы масштаб сбитым.
_SCALE_TEXT_JS = ("(sel) => { const el = document.querySelector(sel);"
                  " return el ? (el.textContent || '').replace(/\\s+/g, ' ').trim() : null; }")


async def _scale_drifted(page: Page, cap: float | None = None) -> bool:
    """Сбились ли масштабы графика (свеча/график) — read-only: читаем текст открывашек, ничего не
    кликая и не открывая. Прочитать не удалось (нет кнопки / БД без значения / кнопка без текста) →
    True: перевыставим вслепую, это безопасно (выбор уже выбранного значения ничего не меняет).

    `cap` — потолок на ОДНО чтение (секунды), от бюджета ремонта. Проверка read-only и в норме
    занимает единицы миллисекунд, но на подвисшей странице БЕЗ него каждое чтение ждёт
    EVAL_TIMEOUT=10с — и «дешёвые проверки» съедали бюджет целиком, ещё до ремонта."""
    for opener, want, name in ((otc_candle_scale, CANDLE_SCALE_LABEL, 'свеча'),
                               (otc_chart_scale, CHART_SCALE_LABEL, 'график')):
        if not want:
            return True
        try:
            current = await _eval(page, _SCALE_TEXT_JS, opener, cap=cap)
        except (Exception,):
            return True
        if not current:
            logger.info(f'OTC: масштаб ({name}) не прочитать с кнопки — перевыставляю вслепую')
            return True
        if want.lower() not in current.lower():
            logger.warning(f"OTC: масштаб ({name}) сбился: '{current}' вместо '{want}' — возвращаю")
            return True
    return False


# Потолок на ВЕСЬ ремонт оформления перед опционом (проверки + клики + повторный проход).
# Сами проверки read-only и дёшевы, но ремонт на залипшем UI (меню не открывается, чипы не
# зажигаются) складывался в десятки секунд, а вызывается это ПЕРЕД каждым опционом — и общего
# потолка не было (ревизия 12-09-2026).
SETUP_TOTAL_BUDGET = 25.0   # сек


async def ensure_chart_setup(session: "BrowserSession") -> None:
    """Проверить оформление графика и вернуть сбитое: масштабы (свеча 30S / график H1) + индикаторы.

    binodex сбрасывает их не только при новом контексте браузера, но и сам по себе в течение суток —
    новая версия фронта, переинициализация чарта после reload/смены пары. Кадр опциона уходил тогда
    с чужим таймфреймом и без индикаторов. Поэтому зовём перед КАЖДЫМ опционом (main_app, после
    выбора пары и ДО первого кадра) и после аварийного reload в течение опциона (_ensure_otc_alive).

    В норме дёшево: обе проверки read-only (текст кнопки масштаба + чипы легенды индикаторов), UI
    трогаем ТОЛЬКО при реальном сбросе. На время кликов снимаем off-zone — под ним
    (visibility:hidden) кнопки настроек не кликаются; возвращаем в finally на любом исходе.
    Ошибки не критичны (оформление кадра, не данные) — внутри логируются.

    Общий БЮДЖЕТ на ремонт — SETUP_TOTAL_BUDGET: проверки дёшевы, а вот ремонт (клики по меню,
    ожидание чипов, второй проход) на залипшем UI складывался в десятки секунд ПЕРЕД каждым
    опционом, и потолка у этого не было вовсе. Бюджет исчерпан — выходим с тем, что успели
    поправить: кадр без индикатора хуже опоздавшего кадра, но лучше пропущенного опциона.

    Дедлайн уходит ВНУТРЬ обоих ремонтников (apply_chart_scale / _click_indicators), а не только
    проверяется между ними: внешних гейтов не хватало — на залипшем UI сама пара масштабов стоит
    до 30с, а проход по трём индикаторам до ~2 мин, то есть первый же шаг выбирал больше всего
    бюджета и ограничивать было уже нечего (ревизия 12-09-2026)."""
    page = session.find('main')
    if page is None:
        return
    deadline = time.monotonic() + SETUP_TOTAL_BUDGET
    # Бюджет уходит и в САМИ проверки: на живой странице это единицы миллисекунд, а на подвисшей
    # каждое чтение без cap ждёт EVAL_TIMEOUT=10с — три таких чтения плюс два переключения
    # off-zone выбирали 40с при объявленных 25, то есть потолок оставался номинальным даже после
    # того, как его прокинули в ремонт (замер на модели: ×1.6 → ×1.0).
    scale_drifted = await _scale_drifted(page, cap=_left_s(deadline, EVAL_TIMEOUT))
    # None — прочитать не удалось: НЕ трогаем
    counts = await indicator_counts_confirmed(page, cap=_left_s(deadline, EVAL_TIMEOUT))
    missing = None if counts is None else _missing_from(counts)
    extra = {} if counts is None else _extra_from(counts)
    if missing:
        logger.warning(f"OTC: индикаторы графика сбились ({', '.join(n for n, _ in missing)}) — "
                       f"включаю заново")
    if extra:
        # Лишние копии кадр ПОРТЯТ: каждая заводит свою панель, и свечи сжимаются в верхнюю
        # половину. 20-09-2026 у боевых инстансов так и уходило подписчикам — по три Stochastic.
        logger.warning('OTC: лишние копии индикаторов на графике ('
                       + ', '.join(f'{b}×{n + 1}' for b, n in extra.items()) + ') — снимаю')
    if not scale_drifted and not missing and not extra:
        return
    await _clear_offzone(page, cap=_left_s(deadline, EVAL_TIMEOUT))
    # Бэкдроп гасим ЗДЕСЬ, а не внутри apply_chart_scale: она вызывается только при дрейфе
    # масштаба, и когда сбились ОДНИ индикаторы (частый случай), модалку никто не закрывал —
    # клики по меню индикаторов выжигали таймаут. Вызов дешёвый: бэкдропа нет — мгновенный выход.
    await dismiss_modal_backdrop(page)
    try:
        if scale_drifted:
            await apply_chart_scale(page, deadline=deadline)
        if missing and time.monotonic() >= deadline:
            logger.warning(f'OTC: бюджет ремонта оформления {SETUP_TOTAL_BUDGET:.0f}с исчерпан на '
                           f'масштабе — индикаторы оставляю следующему опциону')
        elif missing:
            if scale_drifted:
                # Масштаб перерисовал чарт — замер ДО кликов мог устареть. Перечитываем; не
                # прочиталось (None) — идём по прежнему замеру, это лучшее, что у нас есть.
                # ЧТЕНИЕ С ПОДТВЕРЖДЕНИЕМ, и здесь оно нужнее, чем на входе функции: окно
                # «легенда ещё не отрисована» создаёт ровно та перерисовка, которую мы только
                # что вызвали сменой масштаба. По ложному нулю решение здесь — КЛИКАТЬ, а клик
                # по пункту меню ДОБАВЛЯЕТ копию (см. _indicator_counts), то есть две лишние
                # панели поверх работающих индикаторов.
                refreshed = await indicator_counts_confirmed(page, cap=_left_s(deadline, EVAL_TIMEOUT))
                if refreshed is not None:
                    missing = _missing_from(refreshed)
                    extra = _extra_from(refreshed)
            if missing:
                await apply_chart_indicators(page, missing, deadline=deadline)
        if extra:
            # Снимаем ПОСЛЕ добавления недостающих: apply_chart_indicators сама перечитывает
            # чипы и могла добрать ещё копию, если клик всё-таки дошёл со второго раза.
            # Тоже с подтверждением: по ложному нулю `_extra_from` даст пусто, и лишние копии
            # доживут до конца опциона — то есть кадр уйдёт подписчику с тремя панелями и
            # сжатыми свечами, ровно с тем, ради чего снятие копий и заводили.
            fresh = await indicator_counts_confirmed(page, cap=_left_s(deadline, EVAL_TIMEOUT))
            await drop_extra_indicators(page, _extra_from(fresh) if fresh else extra,
                                        deadline=deadline)
    finally:
        # Восстановление off-zone — ОБЯЗАТЕЛЬНАЯ уборка (без него остаток опциона рендерится на
        # полном CPU), поэтому идёт и при исчерпанном бюджете. Но тоже под потолком: остаток, а
        # при нулевом остатке — пол _OP_FLOOR. На живой странице это один evaluate за единицы
        # миллисекунд, так что 0.2с хватает с запасом; на мёртвой off-zone уже ничего не решает.
        await apply_offzone(page, cap=_left_s(deadline, EVAL_TIMEOUT))


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

_label_cutout_cache: dict = {}                  # {asset: (cutout RGBA, (dx, dy))} — вырезка ярлыка на пару


def _load_globe(size) -> Image.Image | None:
    """Глобус-файл (RGBA) под размер канваса. И файл, и ресайз кэширует общий load_rgba —
    своего кэша здесь не нужно. None — файла нет (кадр соберётся без подложки)."""
    return load_rgba(globe_otc_path, size=size)


def _decode_canvas(d: dict) -> Image.Image:
    """base64 → RGBA + приведение к CSS-боксу. Синхронная часть _canvas_alpha, вынесена
    отдельной функцией, чтобы уходить в поток (см. ниже)."""
    img = Image.open(BytesIO(base64.b64decode(d['url'].split(',', 1)[1]))).convert('RGBA')
    if (d['w'], d['h']) != (d['cssw'], d['cssh']):
        img = img.resize((d['cssw'], d['cssh']), Image.Resampling.LANCZOS)
    return img


async def _canvas_alpha(element, deadline: float | None = None) -> Image.Image:
    """Пиксели канваса с альфой (toDataURL), приведённые к CSS-боксу (ресайз при DPR).

    Декод — в отдельном потоке: base64 кадра плюс возможный LANCZOS-ресайз держали event loop
    по 100–300 мс, а кадров несколько на опцион. В эти окна не разбирались WS-фреймы котировок
    (страдал last_tick, по которому считается feed_dead) и не отрабатывал обработчик SIGTERM.
    Реестр BinoCore: frame-io-thread (14-09-2026).

    `deadline` (monotonic) — бюджет кадра от screenshot_otc, как в _read_chart_prices: снятие
    канваса ждёт не дольше остатка, а не полного EVAL_TIMEOUT."""
    d = await _eval(element, _CANVAS_ALPHA_JS, cap=_left_s(deadline, EVAL_TIMEOUT))
    return await asyncio.to_thread(_decode_canvas, d)


def _matte_label(crop_a: Image.Image, crop_b: Image.Image, k: int = 3, thr: int = 10) -> Image.Image:
    """Вырезка ярлыка по разнице: A (ярлык виден) − B (фон). Альфа = clamp(|A−B|*k).
    Так фон (где A==B) становится прозрачным, остаётся только сам ярлык — без «короба».

    Векторно средствами PIL, а не попиксельным циклом на Python: результат тот же, но работа идёт
    на C. Прежний двойной for по ~100×30 px и был единственной причиной, по которой вырезка
    уходила в asyncio.to_thread. ImageChops.add клампит сумму каналов на 255 — на итог это не
    влияет: при d ≥ 85 альфа и так равна 255 (min(255, d*k) при k=3), а срезаются только
    значения выше этого потолка."""
    if crop_a.size != crop_b.size:
        # ImageChops.difference ТРЕБУЕТ одинаковых размеров (бросает ValueError), а попиксельный
        # цикл, что был здесь раньше, разные размеры терпел. Два _shot одного clip обычно дают
        # одинаковый кадр, но DPR/скролл между ними могут развести их на пиксель — и ярлык МОЛЧА
        # перестал бы накладываться (except в _label_cutout → «кадр без ярлыка»).
        #
        # Обрезаем оба кадра до общего размера, а НЕ ресайзим B под A: ресайз пересчитывает все
        # пиксели (bicubic), фон перестаёт быть побитово равным — и вся идея матирования («где
        # A == B, там альфа 0») рассыпается. Замер на фоне графика (сетка + свечи, т.е. высокие
        # частоты): после resize 53% фоновых пикселей получают ненулевую альфу, вплоть до 255,
        # то есть в пост уезжает прямоугольный кусок графика вместо прозрачности. При crop —
        # ровно 0%. Обрезка теряет полоску в 1 px по краю, что на вырезку ярлыка не влияет.
        w = min(crop_a.width, crop_b.width)
        h = min(crop_a.height, crop_b.height)
        crop_a, crop_b = crop_a.crop((0, 0, w, h)), crop_b.crop((0, 0, w, h))
    a = crop_a.convert('RGB')
    r, g, b = ImageChops.difference(a, crop_b.convert('RGB')).split()
    d = ImageChops.add(ImageChops.add(r, g), b)          # |Δr| + |Δg| + |Δb|, clamp 255
    out = a.copy()
    out.putalpha(d.point([0 if v < thr else min(255, v * k) for v in range(256)]))
    return out


async def _label_cutout(page: Page, asset, clip, rebuild: bool = False,
                        deadline: float | None = None):
    """Вырезка ярлыка пары (с прозрачным фоном) и её позиция относительно бокса канваса.
    Глобус НЕ включаем — снимаем регион дважды при выкл глобусе: ярлык виден (A) и скрыт (B),
    вычитаем фон. None — если не собрать. Строить нужно при СНЯТОМ off-zone (полный UI) — иначе
    ярлык не захватится; поэтому пересборка (rebuild=True) идёт из select_otc_pair до off-zone.
    rebuild=True — пересобрать с нуля (актуальный payout на каждый выбор пары); rebuild=False
    (из screenshot_otc) — взять готовое из кэша, собранного на этом же выборе пары.

    `deadline` (monotonic) — бюджет кадра от screenshot_otc (у пересборки из select_otc_pair его
    нет: там мы не в кадре и не спешим). Сборка стоит трёх evaluate и двух скринов, то есть на
    подвисшей странице — десятки секунд ПОВЕРХ бюджета, уже ПОСЛЕ того как цена снята: кадр
    уезжал бы подписчику тем позже, чем хуже отвечает рендерер. Возврат ярлыка в finally идёт
    под ОТДЕЛЬНЫМ потолком _CLEANUP_CAP, а не под остатком бюджета: на исчерпанном бюджете
    уборке доставался бы пол _OP_FLOOR=0.2с, она не успевала бы, и ярлык остался бы скрытым —
    вырезка на следующем кадре вышла бы полностью прозрачной."""
    key = symbol_key(asset)
    if not rebuild and key in _label_cutout_cache:
        return _label_cutout_cache[key]
    try:
        lb = await _eval(page, _LABEL_BOX_JS, screen_zone_otc, cap=_left_s(deadline, EVAL_TIMEOUT))
        if not lb:
            return None
        lx, ly, lw, lh = round(lb['x']), round(lb['y']), round(lb['w']), round(lb['h'])
        region: FloatRect = {'x': lx, 'y': ly, 'width': lw, 'height': lh}
        a_buf = await _shot(page, clip=region, cap=_left_s(deadline, EVAL_TIMEOUT))  # A: ярлык виден
        await _eval(page, "() => { const e=document.querySelector('[data-otc-lbl]');"
                          " if (e) e.style.setProperty('visibility','hidden','important'); }",
                    cap=_left_s(deadline, EVAL_TIMEOUT))
        try:
            await page.wait_for_timeout(150)
            b_buf = await _shot(page, clip=region, cap=_left_s(deadline, EVAL_TIMEOUT))  # B: фон без ярлыка
        finally:                                                          # вернуть ярлык в любом случае
            await _eval(page, "() => { const e=document.querySelector('[data-otc-lbl]');"
                              " if (e) e.style.removeProperty('visibility'); }", cap=_CLEANUP_CAP)
        # Матирование векторное (ImageChops) — доли миллисекунды на C, отдельный поток не нужен.
        cutout = _matte_label(Image.open(BytesIO(a_buf)), Image.open(BytesIO(b_buf)))
        result = (cutout, (lx - clip['x'], ly - clip['y']))
        _label_cutout_cache[key] = result
        return result
    except (Exception,) as err:
        logger.info(f"OTC {asset}: вырезка ярлыка не удалась ({err}) — кадр без ярлыка")
        return None


# ── off-zone оптимизация CPU (~40→~22%): скрыть UI вне зоны скрина ─────────────────────────────────
# Весь UI вне канваса (правое торговое меню, аккаунт-бар, сайдбар) рендерится зря (в кадр через
# toDataURL не попадает) — прячем `visibility:hidden`, экономия ~17 пт. В БЕЛОМ СПИСКЕ остаются
# видимыми #setup_settings_open (по нему ui_loaded детектит отвал кук в рантайме — НЕЛЬЗЯ прятать!)
# и ярлык пары (нужен для вырезки + это кнопка открытия модалки). Применяем после выбора пары и в
# init_otc; СНИМАЕМ на время select_otc_pair (модалка выбора — вне зоны, под off-zone не кликается).
_OFFZONE_STYLE_ID = '__offzone_style'
_OFFZONE_KEEP_ATTR = 'data-offzone-keep'

# Реализация — ОДНО инжектируемое правило CSS, а не обход DOM. Раньше здесь был
# `document.querySelectorAll('body *')` с inline-стилем на КАЖДОМ узле (и такой же обход на
# снятии) — три прохода по тяжёлой SPA за опцион. Теперь помечаем атрибутом ровно те узлы, что
# должны остаться видимыми, а всё остальное гасим одним правилом на body: visibility наследуется,
# поэтому видимый потомок скрытого предка рисуется — на этом весь приём и держится.
_HIDE_OFFZONE_JS = r"""
({zone, settingsSel, pairSel, styleId, keepAttr}) => {
  const cv = document.querySelector(zone);
  if (!cv) return -1;
  for (const el of document.querySelectorAll('[' + keepAttr + ']')) el.removeAttribute(keepAttr);
  const keep = (el) => { if (el) el.setAttribute(keepAttr, ''); return el; };
  keep(cv);
  // Селекторы белого списка приходят из БД (settings.binodex_settings) — теми же значениями,
  // по которым работают ui_loaded и выбор пары. Раньше они были зашиты здесь литералами:
  // второй источник истины, и смена id в БД (как при переезде на #id) оставила бы кнопку
  // настроек скрытой → ui_loaded=False → otc_session_dead на каждом цикле → бесконечное
  // пересоздание браузера при исправном сайте.
  keep(document.querySelector(settingsSel));               // детект кук (ui_loaded) — обязательно видим
  const pl = keep(document.querySelector(pairSel));        // ярлык пары (вырезка + кнопка модалки)
  if (pl && pl.parentElement) keep(pl.parentElement);      // обрамление ярлыка — нужно для вырезки
  let style = document.getElementById(styleId);
  if (!style) {
    style = document.createElement('style');
    style.id = styleId;
    (document.head || document.documentElement).appendChild(style);
  }
  style.textContent = 'body{visibility:hidden!important}'
                    + '[' + keepAttr + '],[' + keepAttr + '] *{visibility:visible!important}';
  return 1;
}
"""

# Снятие off-zone: убрать наш <style> и пометки. Обхода DOM тут тоже нет — querySelectorAll идёт
# по атрибуту (это индексируемый поиск по считаным узлам), а не по 'body *'.
_CLEAR_OFFZONE_JS = r"""
({styleId, keepAttr}) => {
  const style = document.getElementById(styleId);
  if (style) style.remove();
  for (const el of document.querySelectorAll('[' + keepAttr + ']')) el.removeAttribute(keepAttr);
  // Единственное место, где inline-visibility ставим МЫ САМИ — ярлык пары в _label_cutout
  // (скрыть на кадр B, вернуть в finally). Если тот возврат не отработает (таймаут зависшего
  // рендерера, Target closed), ярлык остался бы скрытым: A == B, вырезка полностью прозрачная,
  // и остаток опциона идёт без ярлыка молча. Поэтому лечим ТОЧЕЧНО, по своему маркеру.
  for (const el of document.querySelectorAll('[data-otc-lbl]')) el.style.removeProperty('visibility');
  // Чужие инлайновые visibility не трогаем: сметать всё подряд на каждом снятии — значит
  // регулярно стирать собственный inline-стиль binodex (MUI-переходы, легенда чарта).
}
"""


async def apply_chart_background(page: Page) -> None:
    """Выключить фоновую подложку чарта — вызов ядра с нашими селекторами из БД.

    Сам флоу (идемпотентность, путь в UI, тексты) живёт в `binocore.binodex`: он одинаков у всей
    семьи, и держать его одиннадцатью копиями не за чем. Отсюда приходит только то, что у
    программы своё: селекторы из binodex_settings, потолок клика и гашение модалки binodex,
    бэкдроп которой перехватывает клики у английских аккаунтов."""
    await binodex_chart_background(
        page, settings_btn=otc_settings_btn, theme_open=otc_theme_open,
        theme_toggle=otc_theme_toggle, wrap_bg=otc_wrap_bg, logger=logger,
        click_timeout=TIMEOUT_SHORT, dismiss=dismiss_modal_backdrop)
async def apply_offzone(page: Page, cap: float | None = None) -> None:
    """Скрыть off-zone UI (CPU ~40→~22%), оставив в белом списке детект кук и ярлык пары.

    `cap` — потолок (секунды) для вызова под бюджетом ремонта. На живой странице это один
    evaluate за единицы миллисекунд, поэтому даже пол в 0.2с — с огромным запасом; на мёртвой
    off-zone всё равно ничего не спасёт (опцион уже не снимется), и ждать её десять секунд
    незачем."""
    try:
        await _eval(page, _HIDE_OFFZONE_JS,
                    {'zone': screen_zone_otc, 'settingsSel': otc_settings_btn,
                     'pairSel': otc_select_pair,
                     'styleId': _OFFZONE_STYLE_ID, 'keepAttr': _OFFZONE_KEEP_ATTR},
                    cap=cap)
    except (Exception,) as err:
        logger.info(f'OTC: скрытие off-zone UI не применилось: {err}')


async def _clear_offzone(page: Page, cap: float | None = None) -> None:
    """Вернуть весь UI (на время выбора пары — модалка выбора под off-zone не кликается).
    `cap` — как в apply_offzone: потолок для вызова под бюджетом ремонта."""
    try:
        await _eval(page, _CLEAR_OFFZONE_JS,
                    {'styleId': _OFFZONE_STYLE_ID, 'keepAttr': _OFFZONE_KEEP_ATTR},
                    cap=cap)
    except (Exception,) as err:
        logger.info(f'OTC: возврат off-zone UI не выполнился: {err}')


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
        logger.info(f"OTC {asset}: подготовка вырезки ярлыка не удалась — {err}")


async def screenshot_otc(page: Page, asset: str = None, qr=None):
    """Кадр графика binodex композитом (глобус-файл + прозрачный канвас + ярлык пары + QR) +
    цена графика (медиана чтений window.chartData.price вокруг кадра). chartData.price — то
    значение, что движок рисует на ярлыке; точнее WS-тика, который опережает график на ~150 мс
    (см. docs/BINODEX_PRICE.md). Если chartData недоступен — фолбэк на WS-цену по моменту кадра.
    Глобус НЕ рендерится браузером (выкл за аккаунтом, экономия CPU) — подкладывается из файла.
    :return: (success, price|error_text) — как у FIN-варианта app.screenshot.
    Третий элемент (путь кадра) убран: он всегда SCREENSHOT_PATH и никем не читался,
    а разная арность одного контракта путала вызывающих."""
    symbol = symbol_key(asset)
    last_error = 'нет цены графика OTC'
    shot_deadline = time.monotonic() + SHOT_TOTAL_BUDGET
    for attempt in range(1, MAX_SCREENSHOT_ATTEMPTS + 1):
        if time.monotonic() >= shot_deadline:
            logger.warning(f'OTC: бюджет снятия кадра {SHOT_TOTAL_BUDGET:.0f}с исчерпан на '
                           f'попытке {attempt} — прекращаю (кадр после экспирации бесполезен)')
            break
        try:
            element = page.locator(screen_zone_otc).first
            # Потолки шагов — не штатные, а min(штатный, ОСТАТОК бюджета): см. про бюджет ниже.
            await element.wait_for(state='visible', timeout=_left_ms(shot_deadline, TIMEOUT_LONG))
            # _close_pair_modal здесь НЕ зовём (снято 12-09-2026): кадр собирается из
            # canvas.toDataURL, куда DOM-оверлей физически не попадает, а под активным off-zone
            # модалка скрыта (visibility:hidden) — _pair_modal_open возвращал False всегда, то
            # есть «страховка» ничего не проверяла и просто ходила в DOM на каждой попытке.
            box = await element.bounding_box(timeout=_left_ms(shot_deadline, TIMEOUT_MEDIUM))
            if not box:  # элемент невидим/отсоединён → bounding_box=None (иначе TypeError на box['x'])
                last_error = 'нет bounding_box зоны графика OTC'
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: {last_error} для {asset}")
                continue
            clip = {'x': round(box['x']), 'y': round(box['y']),
                    'width': round(box['width']), 'height': round(box['height'])}
            # Защита от пустого канваса: после переключения пары канвас ~1-3с пустой (свечи не
            # дорисованы) — не постим голый кадр. Ждём отрисовку до CANVAS_READY_SECONDS (wall-clock
            # по time.monotonic — каждый _canvas_alpha это _eval до EVAL_TIMEOUT). Кадр канваса
            # СНИМАЕМ ОДИН раз за итерацию и им же проверяем непустоту — убрали двойной toDataURL
            # (было probe+захват = 2 PNG-энкода/кадр в стационаре). Ценовой брекет (reads_before →
            # t_shot → канвас → reads_after) держим ВНУТРИ итерации, чтобы медиана оставалась
            # синхронной с кадром; пустой кадр НЕ постим (ждём/ретраим до бюджета).
            # Ожидание отрисовки не может пережить общий бюджет: иначе последняя попытка
            # растягивала снятие далеко за него.
            #
            # Бюджет уходит и ВНУТРЬ шагов итерации (потолок каждого = min(штатный, остаток)), а
            # не только проверяется между ними. Без этого он был номинальным: одна итерация —
            # это wait_for(15с) + bounding_box(10с) + семь _eval по EVAL_TIMEOUT=10с, и на
            # подвисшем рендерере она выбирала под сотню секунд при объявленных 45. Проверено
            # прогоном боевой функции (рендерер отвечает по 9.5с на вызов): 99.9с против 45с,
            # причём кадр УХОДИЛ — с ценой, снятой на полторы минуты позже момента, ради
            # которого его снимали, то есть исход опциона считался по чужой котировке.
            # Тот же приём уже стоит в ensure_chart_setup (ревизия 12-09-2026).
            deadline = min(time.monotonic() + CANVAS_READY_SECONDS, shot_deadline)
            canvas_img = None
            # Только объявление типа, без значения: обе заполняет ПЕРВАЯ же строка тела цикла, а
            # `while True` гарантирует хотя бы один проход. Прежняя инициализация выглядела
            # страховкой «на случай, если цикл не выполнится», хотя такого случая нет: выход до
            # присваивания возможен лишь через исключение, а там обе переменные уже не читаются.
            reads: list[float]
            t_shot: float
            while True:
                reads = await _read_chart_prices(page, symbol, CHART_READS_BEFORE,
                                                 deadline=shot_deadline)
                t_shot = time.time()
                candidate = await _canvas_alpha(element, deadline=shot_deadline)
                reads += await _read_chart_prices(page, symbol, CHART_READS_AFTER,
                                                  deadline=shot_deadline)
                if sum(candidate.getchannel('A').histogram()[16:]) >= candidate.width * candidate.height * CANVAS_MIN_OPAQUE:
                    canvas_img = candidate  # непустой кадр — используем его же как снимок
                    break
                if time.monotonic() >= deadline:
                    break  # свечи так и не появились за бюджет → ретрай попытки
                await asyncio.sleep(0.4)
            if canvas_img is None:   # свечи так и не появились → ретрай попытки (редкий труло-стак)
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: канвас пуст "
                               f"{CANVAS_READY_SECONDS:.0f}с (свечи не отрисованы) для {asset}")
                continue
            if reads:
                # median_low, а НЕ median: серия чтений ЧЁТНАЯ (CHART_READS_BEFORE + _AFTER),
                # и обычная медиана вернула бы среднее двух центральных — цену, которой на ярлыке
                # не было ни в один момент. А она уходит в пост и участвует в определении исхода
                # (возврат ловится ТОЧНЫМ равенством цен). median_low всегда отдаёт реально
                # прочитанное значение, и это верно при любой длине серии — часть чтений отсеивает
                # фильтр по symbol. Реестр BinoCore: frame-price-median-low (14-09-2026).
                price = statistics.median_low(reads)
                # Неполный брекет = часть сэмплов не собралась. Опасен не сам недобор, а
                # ПЕРЕКОС: если целиком отвалилась серия «до кадра», медиана считается только
                # по пост-кадровым чтениям, то есть по цене, которой на ярлыке ещё не было.
                # Сообщаем ОДИН раз здесь, а не в _read_chart_prices (та зовётся дважды за
                # каждую итерацию ожидания канваса — на INFO это были десятки строк на опцион).
                expected = CHART_READS_BEFORE + CHART_READS_AFTER
                if len(reads) < expected:
                    logger.info(f"OTC {asset}: неполный ценовой брекет — {len(reads)}/{expected} "
                                f"чтений chartData, медиана может быть односторонней (цена {price})")
            else:
                # chartData не отдал ни одного чтения — кадр снят, но цену берём из WS-фолбэка.
                # Логируем: в пост-мортеме видно, что источник цены кадра — WS, а не ярлык графика.
                price = get_price_tracker().get_price_at(asset, t_shot)
                logger.info(f"OTC {asset}: chartData пуст на кадре — цена из WS-фолбэка ({price})")
            if price is None:  # ни chartData, ни WS не дали цену
                logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS}: нет цены графика OTC для {asset}")
                await asyncio.sleep(0.5)
                continue
            # Сэндвич: глобус(файл) → канвас(прозрачный) → ярлык пары(вырезка) → QR.
            # Глобуса может не быть (сбой выкатки — load_rgba отдаёт None, и докстринг
            # _load_globe это обещает): собираем кадр без подложки, а не падаем. Раньше
            # alpha_composite(None, ...) давал AttributeError, все попытки скриншота сгорали
            # и программа уходила в бесконечный рестарт-цикл — картинки в канале при этом нет,
            # а в логах только общий «Ошибка скриншота».
            globe = _load_globe(canvas_img.size)
            comp = Image.alpha_composite(globe, canvas_img) if globe else canvas_img.copy()
            cut = await _label_cutout(page, asset, clip, deadline=shot_deadline)
            if cut:
                comp.alpha_composite(cut[0], dest=(max(0, cut[1][0]), max(0, cut[1][1])))
            comp = comp.convert('RGB')
            if qr:
                paste_overlay(comp, qr[0], otc_qr_x, otc_qr_y)  # на OTC один QR (qr110)
            comp.save(screenshot_path)
            return True, price
        except (Exception,) as error:
            # С типом, а не голым str(): теперь самый частый сбой здесь — обрыв шага по остатку
            # бюджета, то есть asyncio.TimeoutError, а у неё str() ПУСТОЙ. Без имени типа и лог, и
            # bug_text вырождались в «Ошибка записи скриншота OTC - » без единого слова о причине
            # (тот же приём уже стоит в _read_chart_prices).
            last_error = f'{type(error).__name__}: {error}'.rstrip(': ')
            logger.warning(f"Попытка {attempt}/{MAX_SCREENSHOT_ATTEMPTS} скриншота OTC: {last_error}")
    return False, f'Ошибка записи скриншота OTC - {last_error}'


async def open_otc_browser(session: "BrowserSession") -> OperationResult:
    """Открытие binodex для OTC.

    `error` заполняем ОБЯЗАТЕЛЬНО: init_load логирует его через logger.error, то есть текст
    уходит алертом в канал ошибок. С дефолтным '' это был алерт «‼️Сбой …» с пустым телом —
    и до 12-09-2026 оно никому не мешало только потому, что ветка была недостижима (init_otc
    выходил через close_program). Теперь это штатная реакция на транзиентный сбой подъёма,
    поэтому у алерта должен быть смысл. Конкретную причину init_otc уже положил в warning.log."""
    if await init_otc(session=session):
        return OperationResult(success=True)
    return OperationResult(success=False,
                           error='OTC: binodex не поднялся (причина — warning.log выше) — '
                                 'отдаю неуспех в политику подъёма')


# Ожидание первой котировки по WS: 20 проб по 0.5с = 10 секунд.
WS_WAIT_ATTEMPTS = 20
WS_WAIT_STEP = 0.5


async def _wait_quotes_ws(timeout_text: str) -> bool:
    """Подождать первую цену от WS котировок. True — дождались, False — вышло время (текст
    таймаута уже записан в лог).

    ОДНА реализация на программу. Цикл стоял дважды — в `_verify_otc_ready` (подъём браузера) и
    в `reload_otc_page` (reload перед опционом): механика одинаковая, тексты разные, и правка
    одного места другое молча не чинила (ревизия 17-09-2026, п.3.3).

    Ждём НЕ соединения, а первой ЦЕНЫ: `ws_connected` без `prices` означает, что сокет открыт, а
    данных ещё нет, — по такому признаку фид считался бы живым раньше времени.
    """
    tracker = get_price_tracker()
    for _ in range(WS_WAIT_ATTEMPTS):
        if tracker.ws_connected and tracker.prices:
            return True
        await asyncio.sleep(WS_WAIT_STEP)
    logger.warning(timeout_text)
    return False


async def _verify_otc_ready(page: Page) -> None:
    """Авторизация + готовность торгового UI на /trade. Возвращается при успехе; иначе raises:
    CookiesExpired (нужен релогин: нет токена / Demo / форма логина), FeedOutage (аутэйдж фида),
    SetupError (front-end аутэйдж binodex — в т.ч. редирект/boot-recovery при ЖИВОМ токене — либо
    сменившиеся селекторы). Редирект с /trade разводит _raise_ui_dead ПО ЖИВОСТИ privy:token, а не
    безусловно как отвал кук. WS-фид для BinoOptions НЕ критичен (цена из chartData, WS — фолбэк/
    liveness): не поднялся → лог деградации, БЕЗ raise."""
    # authed читаем ПЕРВОЙ — от неё зависит трактовка редиректа (куки vs аутэйдж фронта binodex).
    authed = await _privy_token_alive(page, on_error=True)
    if not on_trade(page.url):
        # binodex увёл с /trade. Сперва — backend: auth-API 5xx браузер-фри → это НЕ куки и НЕ
        # front-end-аутэйдж, а падение бэкенда binodex (Privy-логин на 502); релогин/прокси не
        # помогут → FeedOutage (браузер-фри ожидание). Грабли 2026-07-23.
        await _raise_off_trade(f'редирект с /trade на {page.url}', authed)
    # Ранний гейт «сессии нет вовсе» (чистый контекст). На ПРОТУХШЕЙ (но присутствующей) сессии
    # токен только что восстановлен из storage_state → ранний гейт пропустит; Privy очистит его на
    # буте → ловит авторитетная перепроверка ниже.
    if not authed:
        raise CookiesExpired('binodex OTC: нет признака сессии — нужен логин')
    # SPA не обязательно доехала: при сплеше чарт виснет, кнопка выбора пары не появляется.
    # _raise_ui_dead разводит: форма/Demo/error → CookiesExpired; фид мёртв → FeedOutage; токен жив,
    # UI не поднялся → SetupError.
    try:
        await page.locator(otc_select_pair).first.wait_for(state='visible', timeout=TIMEOUT_LONG)
    except (Exception,):
        await _raise_ui_dead(page, 'кнопка выбора пары не появилась')
    if not await ui_loaded(page, UI_READY_TIMEOUT):
        await _raise_ui_dead(page, 'нет кнопки настроек аккаунта (завис на сплеше)')
    # Авторитетная перепроверка ПОСЛЕ оседания UI: Privy за время загрузки мог очистить протухший
    # токен (ранний гейт видел его свежевосстановленным) → апп в Demo.
    if not await _privy_token_alive(page, on_error=False):
        raise CookiesExpired('binodex OTC: UI поднялся, но признак сессии очищен (Demo) — она протухла')
    # Масштабы графика и индикаторы сбрасываются на дефолт при каждом запуске браузера (новый
    # контекст из storage_state) — выставляем на каждом старте, ДО off-zone (под ним кнопки не кликаются).
    await apply_chart_scale(page)
    await apply_chart_indicators(page)
    # off-zone оптимизация CPU (~40→~22%): прячем UI вне зоны скрина (детект кук/ярлык — в белом списке).
    # Подложку гасим ДО off-zone: off-zone прячет её от отрисовки, но настройка аккаунта при этом
    # остаётся включённой, и в следующем контексте всё повторится. Здесь выключаем по-настоящему.
    await apply_chart_background(page)
    await apply_offzone(page)
    # WS-котировки — мягко (источник цены chartData, WS = фолбэк/liveness). Не пошёл → деградация, БЕЗ raise.
    if await _wait_quotes_ws("binodex: WS котировок не поднялся за 10с — работаю на chartData, "
                             "feed_dead-детект деградирован"):
        # info, а не report: рутинный успех подъёма браузера (старт, пересоздание,
        # восстановление сессии) — в служебную тему это шумело на каждом рестарте, как
        # ранее open_tv_browser finished. Провал подъёма WS остаётся warning (внутри).
        logger.info("✅ binodex: WS котировок подключён")


async def _save_session_snapshot(session: "BrowserSession") -> None:
    """Положить ТЕКУЩИЙ storage_state в БД. Зовётся после каждой ротации сессии binodex
    (подписка `watch_session_refresh` в init_otc).

    Зачем не только после релогина. refresh-токен собственной авторизации binodex одноразовый:
    фронт меняет его примерно раз в 15 минут, и снимок, снятый при входе, к этому моменту уже
    потрачен — холодный старт с него получает 401 REFRESH_REUSED, то есть «куки протухли» →
    релогин → письмо с кодом. Держим в БД текущий экземпляр, и рестарт снова стоит ноль писем.

    Гард на признак сессии тот же, что у релогина: снимок без ключа в БД не пишем, прежние куки
    целее."""
    state = await session.context.storage_state()
    if not binodex_has_session(state, otc_session_keys):
        logger.warning('OTC: в снимке после ротации нет признака сессии — в БД НЕ пишу')
        return
    if await database.save_otc_cookies(cookies_pocket_id, state) is False:
        logger.warning('OTC: снимок после ротации сессии не сохранён в БД (сбой) — работа продолжается')
        return
    logger.info('OTC: снимок сессии обновлён в БД после ротации токена binodex')


async def _relogin_inline(session: "BrowserSession", page: Page) -> bool:
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
    try:
        if not await otc_inline_login(page, session.context, creds['mail'], creds['mail_app_pass'], sel):
            return False
    except LoginRateLimited as limit:
        # Лимит запросов кода у binodex: повторять вход нельзя — КАЖДАЯ попытка его продлевает
        # (18-09 ForumTradeEnglish просила код каждые 2.5 минуты три часа подряд, 19-09 в ту же
        # яму сползли 3m/5m OTC). Держим паузу прямо здесь, как это уже делает ветка front-end
        # аутэйджа, и возвращаем неуспех: наверху сработает обычный счётчик.
        logger.warning(f'OTC inline-релогин: binodex ограничил запросы кода («{limit}») — '
                       f'пауза {RATE_LIMIT_PAUSE // 60} мин, вход сейчас не пробую')
        # Пауза ПРЕРЫВАЕМАЯ: голый sleep не давал начаться уборке по SIGTERM, и systemd добивал
        # процесс SIGKILL по TimeoutStopSec (19-09-2026: Result=timeout, ExecMainStatus=9 — для
        # диспетчера это краш, то есть лишний повод к рестартам).
        if not await binodex_rate_limit_sleep(RATE_LIMIT_PAUSE, stop=shutdown_event()):
            logger.info('OTC inline-релогин: пауза по лимиту прервана остановкой процесса')
        return False
    # Свежую сессию — в БД (переживёт рестарт). Сбой сохранения не критичен: работаем на live-сессии.
    # Подложку гасим ДО снятия storage_state, а не после. Настройка живёт в localStorage
    # (`isChartBgVisible`), то есть уезжает в БД ВМЕСТЕ с куками — и тогда она приедет на все ноды
    # уже выключенной, без лишнего захода в UI на каждом подъёме. Если сделать это после, в БД
    # ляжет storage_state с включённой подложкой, и каждый холодный старт будет выключать её заново.
    await apply_chart_background(page)
    try:
        state = await session.context.storage_state()
        if not binodex_has_session(state, otc_session_keys):
            # Снимок БЕЗ признака сессии в БД не пишем: 18-09-2026 такой записался (вход прошёл,
            # но binodex погасил сессию сразу после него) и затёр рабочие куки служебными ключами.
            # И возвращаем FALSE, а не True: раз ключа нет в снимке — его нет и в браузере
            # (снимок снимается с ТОГО ЖЕ контекста, в котором живёт вкладка), то есть вход не
            # прижился и работать на «live-сессии» не на чем. Прежнее True стоило ночи 19-09-2026
            # на EnglishGold: init сразу за релогином видел Demo → CookiesExpired → счётчик гнал
            # ещё три входа, binodex слал ЧЕТЫРЕ письма с кодом подряд (лимит запросов — рядом),
            # и всё это заканчивалось выходом 11. False отдаёт решение наверх с первого раза.
            logger.warning('OTC inline-релогин: в снимке нет признака сессии — вход не прижился '
                           '(binodex погасил сессию сразу после входа); в БД НЕ пишу, '
                           'прежние куки целее')
            return False
        if await database.save_otc_cookies(cookies_pocket_id, state) is False:
            logger.warning('OTC inline-релогин: storage_state не сохранён в БД (сбой) — продолжаю на live-сессии')
    except (Exception,) as err:
        logger.warning(f'OTC inline-релогин: сохранение storage_state не удалось ({err}) — продолжаю')
    return True


async def _goto_otc(page: Page, url: str, timeout: int = TIMEOUT_LONG) -> None:
    """Навигация на binodex — общий `page_nav.goto_retry` (единый список транзиентных сбоев
    с inline-релогином: раньше здесь ретраился ТОЛЬКО NS_BINDING_ABORTED, и сетевой блип до
    binodex ронял init, хотя в релогине переживался)."""
    await goto_retry(page, url, timeout=timeout, label='OTC')


async def init_otc(session: "BrowserSession") -> bool:
    """Загрузка binodex.app/trade: WS-перехват → страница из cookies.pages → goto →
    _verify_otc_ready (авторизация + UI; WS мягко). При «нужен релогин» (CookiesExpired) — INLINE-
    логин в ЭТОМ ЖЕ браузере (apps/otc_login), без подпроцесса/двойной загрузки, и перепроверка. Не
    вышло → CookiesExpired наверх (main: счётчик RECOVER_ATTEMPTS → плановый выход)."""
    page = session.page('main')
    get_price_tracker().reset()   # новая сессия: цены/история/liveness прошлой — невалидны
    _label_cutout_cache.clear()    # новый браузер/страница → старые вырезки ярлыков невалидны
    # И область поиска чипов легенды: новая версия фронта могла перенести легенду,
    # а залипшая область означала бы «чипов нет» → клик по пункту меню ВЫКЛЮЧИЛ бы
    # работающие индикаторы (клик ТОГГЛИТ). Реестр: legend-scan-scope.
    reset_legend_scope()
    setup_websocket_tracker(page)  # подписка ДО навигации — поймать поток с самого старта
    # Ротация сессии: подписываемся ДО навигации по той же причине — первый refresh фронт может
    # сделать сразу после загрузки. Подписка идемпотентна (init зовут на каждом подъёме).
    binodex_watch_refresh(page, lambda: _save_session_snapshot(session), logger=logger)

    # URL — из binodex_settings.trade_url (browser_config.otc_trade_url) с дефолтом на уровне
    # чтения настроек, поэтому пустым быть не может: прежняя async-обёртка _otc_page_url() и
    # ветка «нет OTC-страницы» (недостижимый close_program) сняты 2026-08-15.
    url = otc_trade_url

    try:
        await _goto_otc(page, url)
        await page.set_viewport_size({'width': win_x_otc, 'height': win_y_otc})
    except (Exception,) as error:
        # НЕ close_program: init_otc зовётся из init_load → _init_with_retry, у которой своя
        # политика (пауза, пересоздание браузера, ротация прокси, счётчик BROWSER_MAX_ATTEMPTS →
        # EXIT_BROWSER). Выход прямо отсюда с кодом 1 обрывал транзиентный сбой навигации до
        # первого же ретрая и прятал его от этих счётчиков (ревизия 12-09-2026).
        logger.warning(f'OTC: не загрузился binodex ({error}) — отдаю неуспех в политику подъёма')
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
                if not await _relogin_inline(session, page):
                    raise  # inline не удался → наверх (счётчик RECOVER_ATTEMPTS → выход)
                relogged = True
                await _goto_otc(page, url)
    except (CookiesExpired, FeedOutage, SetupError):
        raise  # наружу → init_load → _init_with_retry (счётчик релогина / ожидание фида / setup-ретраи)
    except (Exception,) as error:
        # Как и выше: неуспех отдаём наверх, решение о выходе принимает _init_with_retry.
        logger.warning(f'OTC: ошибка загрузки binodex ({error}) — отдаю неуспех в политику подъёма')
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
    if not await ui_loaded(page, UI_READY_TIMEOUT):
        logger.warning('OTC: после reload нет кнопки настроек аккаунта (завис на сплеше)')
        return False
    return True


async def reload_otc_page(session: "BrowserSession") -> bool:
    """Перезагрузка binodex перед каждым новым опционом (вызов из main_app). binodex
    периодически выкатывает новую версию фронта и показывает баннер «Доступна новая версия.
    Обновите страницу», зависая на сплеше при ЖИВЫХ URL (/trade держится), UI и WS — отвал-кук-
    детект (on_trade/ui_loaded/feed_dead) такое НЕ ловит. Регулярный reload подхватывает новую
    версию заранее, до того как чарт зависнет. WS-перехват НЕ переустанавливаем: page.on('websocket')
    переживает reload (повторная подписка задвоила бы хендлеры), старый WS закроется → новый
    откроется → трекер сам перецепится.

    Зависший загрузочный сплеш транзиентен (~3% reload Privy/SPA не достраивается, следующий reload
    рендерится нормально), поэтому повторяем САМ reload до RELOAD_RETRIES раз перед тем, как отдать
    False — иначе бот зря уходит в пересоздание браузера (ложный «отвал cookies») / «нет пар».
    :return: True — UI снова готов к скрину; False — не поднялся после всех ретраев (вызывающий
    уйдёт в exit_main → main-цикл по otc_session_dead пересоздаст браузер)."""
    page = session.find('main')
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
    # DOM пересоздан — метка контейнера легенды ушла со старым деревом (почему это важно —
    # в reset_legend_scope). Реестр: legend-scope-reload.
    reset_legend_scope()
    await _wait_quotes_ws("binodex: WS котировок не переподключился за 10с после reload")
    logger.info('🔄 OTC: страница перезагружена перед опционом — UI готов')
    return True


OTC_WS_SILENCE_LIMIT = 30  # сек без тика при закрытом WS = мёртвый фид (внутренний тайминг)


async def otc_session_dead(session: "BrowserSession") -> tuple[bool, str]:
    """Рантайм-детект отвала OTC-сессии (§4.4). Три сигнала:
      (a) редирект с /trade — Privy storage_state протух (основной, URL-детект);
      (b) торговый UI пропал — нет кнопки настроек аккаунта при живом URL/WS (Privy-токен
          залип без редиректа: тулбар отрисован частично, /trade держится, котировок-WS стримит
          все пары → (a) и (c) молчат, но страница свалилась на сплеш);
      (c) WS-фид котировок мёртв — токен WS мог протухнуть без редиректа страницы
          (дополняет (a); точнее и раньше, чем ждать сбоя данных).
    Возвращает (dead, reason) — reason для лога вызывающим."""
    page = session.find('main')
    if page is not None:
        try:
            if not on_trade(page.url):
                return True, 'редирект с /trade (Privy storage_state протух)'
        except (Exception,):
            pass
        # На живом графике кнопка настроек видна сразу (нет ложняка); нет её весь
        # UI_DEAD_CONFIRM — страница реально свалилась на сплеш.
        if not await ui_loaded(page, UI_DEAD_CONFIRM):
            return True, 'торговый UI пропал — завис на сплеше (нет кнопки настроек, storage_state протух)'
    if get_price_tracker().feed_dead(OTC_WS_SILENCE_LIMIT):
        return True, f'WS-фид котировок мёртв (закрыт, нет тика > {OTC_WS_SILENCE_LIMIT}с)'
    return False, ''
