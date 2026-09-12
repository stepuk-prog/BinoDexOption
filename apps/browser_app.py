
import asyncio
import os
import re
import time

from playwright.async_api import async_playwright, BrowserContext, Page

from classes.browser_manager import BrowserManager
from classes.exceptions import CookiesExpired, FeedOutage, SetupError
from apps.exit_app import close_program
from apps.otc_app import open_otc_browser
from apps.browser_io import eval_js
from logs import init_logger
from settings import win_x, win_y
from settings.browser_set import browser_launch_options, context_options, chromium_launch_options
from settings.browser_config import tf_menu, tf_link, search_val, symbol, \
    tf_link_price, panel_toggle, panel_wrap
from settings.config import (cookies, database, binary, browser_engine, prog_key, cookies_tv_id,
                             cookies_pocket_id)
from apps.cookie_utils import add_cookies_to_context
from settings.timing import (
    POPUP_SETTLE_DELAY, EVAL_TIMEOUT,
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


# Чужой оверлей ловим ПО ФАКТУ, а не по имени класса. Грабли 10-09-2026: TV показал окно
# в своей универсальной модальной оболочке (`container-<хеш>` — прозрачный контейнер во весь
# экран с вертикальным центрированием), клик по тулбару весь прогон отбивался «intercepts
# pointer events», а прежняя чистка искала закрывашку по префиксам `navButton-` и
# `toast-group-close-button` и не находила ничего — обе программы отдали за день НОЛЬ записей.
# Спрашиваем у браузера, что РЕАЛЬНО лежит в точке, куда мы целимся: так ловится любой оверлей,
# включая те, которых ещё нет, и на любой площадке (TV, binodex) — узел помехи ищется и в
# #overlap-manager-root, и напрямую в body.
_AT_POINT_JS = """
(selector) => {
  const target = document.querySelector(selector);
  if (!target) return {state: 'no-target'};
  const r = target.getBoundingClientRect();
  if (!r.width || !r.height) return {state: 'no-target'};
  const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  if (!hit) return {state: 'no-hit'};
  if (hit === target || target.contains(hit) || hit.contains(target)) return {state: 'free'};
  const root = document.getElementById('overlap-manager-root');
  let top = hit;
  while (top.parentElement && top.parentElement !== document.body && top.parentElement !== root) {
    top = top.parentElement;
  }
  const tr = top.getBoundingClientRect();
  return {state: 'blocked', cls: (top.className || '').toString(),
          x: tr.x, y: tr.y, w: tr.width, h: tr.height,
          html: top.outerHTML.slice(0, 1200)};
}
"""

# Закрывашку ищем обобщённо: data-name / aria-label / title / класс / подпись кнопки. Хеши TV
# проворачивает при каждой выкатке фронта, а эти признаки — нет. Жмём через el.click() прямо в
# DOM: сама кнопка закрытия может быть накрыта соседним оверлеем, и обычный клик до неё не дойдёт.
_DISMISS_JS = """
(selector) => {
  const target = document.querySelector(selector);
  if (!target) return {state: 'no-target'};
  const r = target.getBoundingClientRect();
  const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  if (!hit || hit === target || target.contains(hit) || hit.contains(target)) return {state: 'free'};
  const root = document.getElementById('overlap-manager-root');
  let top = hit;
  while (top.parentElement && top.parentElement !== document.body && top.parentElement !== root) {
    top = top.parentElement;
  }
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight);
  const BY_ATTR = ['[data-name*="close" i]',
                   'button[aria-label*="\u0417\u0430\u043a\u0440" i]', 'button[aria-label*="close" i]',
                   'button[title*="\u0417\u0430\u043a\u0440" i]', 'button[title*="close" i]',
                   'button[class*="close" i]', 'button[class*="navButton-"]'];
  for (const sel of BY_ATTR) {
    for (const btn of top.querySelectorAll(sel)) {
      if (!visible(btn)) continue;
      try { btn.click(); return {state: 'closed', by: sel}; } catch (e) {}
    }
  }
  const WORDS = ['\u0437\u0430\u043a\u0440\u044b\u0442\u044c', 'close', '\u043d\u0435 \u0441\u0435\u0439\u0447\u0430\u0441',
                 '\u043f\u043e\u0437\u0436\u0435', '\u043e\u0442\u043c\u0435\u043d\u0430', '\u043f\u043e\u043d\u044f\u0442\u043d\u043e', 'ok', '\u043e\u043a'];
  for (const btn of top.querySelectorAll('button,[role="button"]')) {
    if (!visible(btn)) continue;
    if (WORDS.includes((btn.innerText || '').trim().toLowerCase())) {
      try { btn.click(); return {state: 'closed', by: '\u043f\u043e\u0434\u043f\u0438\u0441\u044c'}; } catch (e) {}
    }
  }
  return {state: 'no-button', cls: (top.className || '').toString(),
          x: top.getBoundingClientRect().x, y: top.getBoundingClientRect().y,
          html: top.outerHTML.slice(0, 1200)};
}
"""

# DOM помехи пишем в лог ОДИН раз за прогон: следующий такой случай надо разбирать по логу, а не
# поднимать разведку заново (10-09-2026 на это ушло полдня). Тем же флагом глушим и рассказ об
# успешном закрытии: оверлей лезет на КАЖДОЙ валюте (21 валюта × 3 страницы), а logger.report
# уходит в Telegram — без глушилки это 63 сообщения за цикл. Первый случай — в канал, остальные
# тем же текстом в info-лог.
_overlay_reported = False


def _overlay_log(text: str) -> None:
    """Первый оверлей за прогон — в канал, дальше только в файл."""
    global _overlay_reported
    if _overlay_reported:
        logger.info(text)
    else:
        _overlay_reported = True
        logger.report(text)


async def _probe_point(page: Page, selector: str) -> dict:
    """Что лежит в точке, куда целится клик. Пустой dict — спросить не вышло."""
    try:
        return await eval_js(page, _AT_POINT_JS, selector) or {}
    except (Exception,):
        return {}


async def _close_overlay(page: Page, selector: str) -> bool:
    """Снять чужой оверлей, накрывший точку клика по `selector`.

    Лесенка от дешёвого к грубому, каждый шаг проверяется тем же замером точки:
      1. кнопка закрытия внутри самого оверлея (обобщённо, без привязки к хешам);
      2. Escape — берёт диалоги, у которых закрывашки нет вовсе;
      3. клик в угол оверлея — «мимо окна», штатный способ закрыть модалку с backdrop.
    :return: True — точка освободилась, клик имеет смысл повторить.
    """
    info = await _probe_point(page, selector)
    if info.get('state') != 'blocked':
        return False

    if not _overlay_reported:
        # DOM пишем в warning.log (в Telegram уровень WARNING не уходит) — по нему и опознаем
        # окно в следующий раз, без разведки.
        logger.warning(f'Клик по {selector} перекрыт оверлеем '
                       f'{info.get("cls") or "(без класса)"} '
                       f'{round(info.get("w", 0))}x{round(info.get("h", 0))}; '
                       f'DOM: {info.get("html", "")}')

    try:
        result = await eval_js(page, _DISMISS_JS, selector) or {}
    except (Exception,):
        result = {}
    if result.get('state') == 'closed':
        await page.wait_for_timeout(300)
        if (await _probe_point(page, selector)).get('state') != 'blocked':
            _overlay_log(f'Оверлей закрыт кнопкой ({result.get("by")}) — повторяю клик')
            return True

    try:
        await page.keyboard.press('Escape')
        await page.wait_for_timeout(300)
    except (Exception,):
        pass
    if (await _probe_point(page, selector)).get('state') != 'blocked':
        _overlay_log('Оверлей закрыт по Escape — повторяю клик')
        return True

    # Угол оверлея: у модалки с backdrop сама коробка стоит по центру, край — «мимо окна».
    # Координаты берём свежие: после Escape оверлей мог перерисоваться.
    info = await _probe_point(page, selector)
    if info.get('state') == 'blocked' and info.get('w', 0) > 0:
        try:
            await page.mouse.click(info['x'] + 4, info['y'] + 4)
            await page.wait_for_timeout(300)
        except (Exception,):
            pass
        if (await _probe_point(page, selector)).get('state') != 'blocked':
            _overlay_log('Оверлей закрыт кликом мимо окна — повторяю клик')
            return True

    _overlay_log(f'Оверлей {info.get("cls") or "(без класса)"} не закрылся ни кнопкой, '
                 f'ни Escape, ни кликом мимо — иду в обход hit-testing')
    return False


async def click_guarded(page: Page, selector: str, timeout: int = 10000) -> None:
    """Клик, который доводит дело до конца, даже если сверху лёг чужой оверлей.

    Обычный клик → снять оверлей → повтор → и, как последний довод, dispatch_event: он
    отдаёт событие самому элементу, минуя проверку «кто лежит сверху». Обработчик React
    отработает независимо от того, что нарисовано поверх. Исключение наружу пробрасываем
    только если не помогло вообще ничего.
    """
    try:
        await page.locator(selector).first.click(timeout=timeout)
        return
    except (Exception,) as first_error:
        if await _close_overlay(page, selector):
            await page.locator(selector).first.click(timeout=timeout)
            return
        try:
            await page.locator(selector).first.dispatch_event('click')
            await page.wait_for_timeout(300)
        except (Exception,):
            raise first_error


async def _sweep_close_buttons(page: Page) -> None:
    """Best-effort крестик на страницах БЕЗ точки-ориентира (вкладка binodex и т.п.).
    Прежнее поведение close_dom_popups: жмём видимый крестик, ошибку глотаем."""
    for css in ('button[class*="closeButton" i]', '[data-name*="close" i]',
                'button[aria-label*="\u0417\u0430\u043a\u0440" i]'):
        try:
            item = page.locator(css).first
            if await item.is_visible():
                await item.click(timeout=1500)
                return
        except (Exception,):
            continue


# Оверлеи, попавшие В КАДР. Отличие от _AT_POINT_JS: там вопрос «что накрыло точку клика», а
# кадру мешает окно, которое клику не мешает вовсе — TV-онбординг («Теперь можно перемещать
# таблицы индикаторов…») висит над графиком, кнопка поиска символа свободна, и прежняя проверка
# честно отвечала «free», пока модалка уезжала в канал (скрин 11-09-2026).
#
# Ищем не по именам классов (TV крутит хеши на каждой выкатке), а по геометрии: берём попапы из
# `#overlap-manager-root` — штатный контейнер TV для модалок/тултипов — и жмём кнопку закрытия
# у тех, чей прямоугольник пересекает зону кадра. Зона приходит XPath'ом (так она лежит в БД),
# поэтому селектор резолвим и через document.evaluate.
_ZONE_CLEAR_JS = """
(selector) => {
  const resolve = (sel) => {
    if (sel.startsWith('/') || sel.startsWith('(')) {
      const r = document.evaluate(sel, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
      return r.singleNodeValue;
    }
    return document.querySelector(sel);
  };
  const zone = resolve(selector);
  if (!zone) return {state: 'no-zone'};
  const z = zone.getBoundingClientRect();
  if (!z.width || !z.height) return {state: 'no-zone'};
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight);
  const hits = (r) => r.width && r.height &&
      r.x < z.x + z.width && r.x + r.width > z.x &&
      r.y < z.y + z.height && r.y + r.height > z.y;
  const WORDS = ['\u043f\u043e\u043d\u044f\u0442\u043d\u043e', 'got it', 'ok', '\u043e\u043a',
                 '\u0437\u0430\u043a\u0440\u044b\u0442\u044c', 'close',
                 '\u043d\u0435 \u0441\u0435\u0439\u0447\u0430\u0441', '\u043f\u043e\u0437\u0436\u0435',
                 '\u043e\u0442\u043c\u0435\u043d\u0430', '\u043f\u0440\u043e\u043f\u0443\u0441\u0442\u0438\u0442\u044c'];
  const label = (el) => ((el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '')
                         .trim().toLowerCase());

  // 1) Прямой путь: видимая кнопка ВНУТРИ зоны кадра с подписью «Понятно»/«Got it»/крестиком.
  // Контейнер при этом не важен — TV кладёт онбординг то в overlap-manager-root, то отдельным
  // слоем, и привязка к контейнеру уже один раз промахнулась (11-09-2026).
  for (const btn of document.querySelectorAll('button,[role="button"],[data-name*="close" i]')) {
    if (!visible(btn) || !hits(btn.getBoundingClientRect())) continue;
    // Кнопка ВНУТРИ самого чарта — не наша: легенда индикаторов TV живёт там же, и стоит
    // переименоваться её aria-label, как мы начали бы удалять индикаторы вместо попапа.
    // Окно-онбординг лежит НАД зоной, а не в ней, поэтому гард его не задевает.
    if (zone.contains(btn)) continue;
    const txt = label(btn);
    const isClose = WORDS.includes(txt) ||
        /close|\u0437\u0430\u043a\u0440/i.test(btn.getAttribute('data-name') || '') ||
        /close|\u0437\u0430\u043a\u0440/i.test(btn.getAttribute('aria-label') || '');
    if (!isClose) continue;
    // Не жмём то, что лежит в самом графике (панель инструментов, легенда): берём только
    // кнопки с плавающим предком. ВАЖНО: position:fixed засчитываем БЕЗ требования z-index —
    // онбординг TV 11-09-2026 висел именно так (fixed + z-index:auto), и прежнее условие
    // «z-index > 0» его отбрасывало. Для absolute z-index всё же требуем: абсолютных блоков
    // внутри самого чарта много, и они не всплывают над ним.
    let el = btn, floating = false;
    for (let i = 0; el && i < 10; el = el.parentElement, i++) {
      const cs = getComputedStyle(el);
      if (cs.position === 'fixed' ||
          (cs.position === 'absolute' && (parseInt(cs.zIndex) || 0) > 0)) {
        floating = true; break;
      }
    }
    if (!floating) continue;
    try { btn.click(); return {state: 'closed', by: txt || 'close-attr'}; } catch (e) {}
  }

  // 2) Диагностика: чего не увидели. Верхние элементы в пяти точках зоны — по ним видно,
  // что реально лежит поверх графика, без гадания по скриншоту.
  const pts = [[0.5, 0.5], [0.25, 0.25], [0.75, 0.25], [0.25, 0.75], [0.75, 0.75]];
  const seen = [];
  for (const [fx, fy] of pts) {
    const el = document.elementFromPoint(z.x + z.width * fx, z.y + z.height * fy);
    if (!el) continue;
    let top = el;
    while (top.parentElement && top.parentElement !== document.body) top = top.parentElement;
    const tag = (el.tagName || '').toLowerCase();
    const cls = (el.className || '').toString().slice(0, 60);
    const txt = (el.innerText || '').trim().slice(0, 40).replace(/\s+/g, ' ');
    seen.push(`${fx},${fy}:${tag}.${cls}${txt ? '|' + txt : ''}`);
  }
  return {state: 'clean', probe: seen.join(' ;; ').slice(0, 700)};
}
"""


async def clear_zone_overlays(page: Page, zone_selector: str, attempts: int = 3) -> None:
    """Снять окна, накрывшие ЗОНУ КАДРА (для точки клика — close_dom_popups).

    Ищем саму кнопку закрытия в границах кадра, а не контейнер: TV кладёт онбординг то в
    `#overlap-manager-root`, то отдельным слоем, и привязка к контейнеру уже промахнулась
    (11-09-2026 — «Теперь можно перемещать таблицы индикаторов…» уехало в канал). Кнопку жмём
    только если она ВНЕ зоны-контейнера и у неё есть плавающий предок (position:fixed — без
    требования z-index, либо absolute с z-index > 0), иначе можно попасть по кнопке самого
    графика.

    Не нашли — пишем в лог, ЧТО лежит поверх зоны (проба в пяти точках): иначе следующий такой
    случай снова придётся разбирать по скриншоту. Кадр снимаем в любом случае: лучше кадр с
    модалкой, чем пропущенный опцион."""
    for _ in range(attempts):
        try:
            res = await eval_js(page, _ZONE_CLEAR_JS, zone_selector) or {}
        except (Exception,) as error:
            _overlay_log(f'Проба зоны кадра не выполнилась: {type(error).__name__}: {error}')
            return
        state = res.get('state')
        if state == 'closed':
            # info, а не канал: TV показывает онбординг на каждом подъёме браузера, то есть
            # сообщение приходило бы после каждого рестарта у каждого инстанса. Сам факт, что
            # окно нашлось и снято, — рутина; в тему ошибок ему незачем (11-09-2026).
            logger.info(f'Снято окно в зоне кадра (кнопка: {res.get("by")!r})')
            await page.wait_for_timeout(200)
            continue
        if state == 'no-zone':
            _overlay_log(f'Зона кадра не найдена по селектору {zone_selector!r} — пропускаю чистку')
            return
        # Кнопок нет — это НОРМА (чистый кадр), в канал такое слать незачем: чистка идёт на
        # каждом кадре. Пробу точек пишем в файл и только когда поверх графика лежит что-то
        # кроме самого холста — иначе строка бессмысленна.
        probe = res.get('probe', '')
        if probe and any(':canvas' not in part for part in probe.split(';;')):
            logger.info(f'В зоне кадра кнопок закрытия нет, но поверх графика что-то есть: {probe}')
        return


async def close_dom_popups(page: Page, target: str = None):
    """Снять оверлеи, накрывшие точку клика (по умолчанию — кнопка поиска символа).

    Переписано 10-09-2026. Прежняя версия жала по списку известных классов
    (`pop_up2`/`pop_up3` из БД, `closeButton`, `toastCommonBase`) — то есть закрывала
    только те окна, чьи имена мы знали заранее. TV проворачивает хеши при каждой
    выкатке фронта, и в это утро обе программы Quiz отдали НОЛЬ записей: окно висело
    в универсальной модальной оболочке TV, под известные имена не подходило, и клик
    по тулбару весь прогон отбивался «intercepts pointer events».

    Теперь ориентир — сама точка клика: спрашиваем у браузера, что в ней лежит, и
    снимаем помеху, какой бы она ни была. На страницах, где точки-ориентира нет
    (вкладка binodex), остаётся прежний best-effort по видимому крестику.
    """
    goal = target or f"#{symbol}"
    if (await _probe_point(page, goal)).get('state') == 'no-target':
        await _sweep_close_buttons(page)
        return
    for _ in range(3):
        if not await _close_overlay(page, goal):
            return
# Static-именованные entry-файлы binodex (app.js/app.css) на любом поддомене binodex.app.
_BINODEX_APPJS_RE = re.compile(r"^https?://(?:[a-z0-9-]+\.)?binodex\.app/assets/app\.(?:js|css)")

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

    # info, НЕ report: report уходит в служебную TG-тему, а это рутинная строка успеха —
    # она повторяется на каждом подъёме браузера (старт, fall, ротация прокси) и в канале
    # только зашумляет настоящие события. В файле info.log остаётся.
    logger.info("✅ open_tv_browser завершён, страницы: %s", list(manager.pages.keys()))
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
                    await eval_js(loc, 'el => el.click()')
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

            # Открыть поиск символа (устойчиво к перехвату клика оверлеем)
            # 10-09-2026: три попытки с фолбэком click(force=True) заменены на
            # click_guarded. force от оверлея НЕ спасает — проверено замером: он лишь
            # снимает проверку кликабельности, а событие всё равно уходит по координатам,
            # то есть в оверлей. Исключения при этом нет, и программа шла дальше, считая
            # что нажала кнопку. click_guarded снимает помеху, а если не вышло — отдаёт
            # событие самому элементу мимо hit-testing.
            await click_guarded(page, f"#{symbol}", timeout=TIMEOUT_MEDIUM)

            # Сброс категории на «Все» (sticky-фильтр TV иначе ломает поиск).
            # Диалог дождётся через wait_for внутри — фиксированный sleep не нужен.
            await _reset_search_category(page)

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
        # Закрываем браузер, как и в ветке исключений выше: без этого Firefox остаётся
        # осиротевшим и держит lock в общем кэше Playwright. Путь сейчас почти недостижим
        # (всё уходит через close_program → sys.exit), но латентная утечка от этого не
        # перестаёт быть утечкой — а стоит она одну строку.
        await manager.close()
        return False

    return manager
