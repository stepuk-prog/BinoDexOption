"""Inline-логин binodex.app (email-OTP) В ОСНОВНОМ браузере бота — тонкая обёртка над ядром.

Сам флоу входа живёт в `binocore.binodex`: модалка (у binodex их ДВЕ — виджет Privy и
собственная, выбор за флагом `my.ownAuth` из api.binodex.app/config), письмо с кодом (Privy шлёт
с privy.io и код в теле, binodex — с mail.binodex.io и код в ТЕМЕ), ожидание признака сессии
(`privy:token` либо `ownAuthSession`) и чтение отказа модалки. Раньше всё это лежало двенадцатью
копиями по программам, и правка 18-09-2026 под новую модалку означала бы двенадцать одинаковых
диффов — теперь она вносится в ядре и раскладывается `sync.py`.

Здесь остаётся ровно то, что у программы СВОЁ: навигация с ретраями, `evaluate` под нашим
потолком, прерываемая пауза ожидания кода (`apps.shutdown.wait_stop`) и логгер модуля.

Контракт прежний: `otc_inline_login(page, context, mail, app_pass, sel) -> bool` (True — вошли,
в localStorage есть признак сессии и мы на /trade). Селекторы/URL — из binodex_settings (sel).
"""
from playwright.async_api import Page, BrowserContext

from binocore.binodex import inline_login
from apps.browser_io import eval_js
from apps.page_nav import goto_retry, on_trade
from apps.shutdown import wait_stop
from logs import init_logger

logger = init_logger(__name__)

EVAL_CAP = 15                # сек на evaluate внутри логина (чистка сессии, чтение отказа модалки)
LOGIN_GOTO_TIMEOUT = 30000   # мс на одну навигацию в логин-флоу


async def _goto(page: Page, url: str) -> None:
    """Навигация логина: общий goto-с-ретраями (apps/page_nav) с коротким таймаутом."""
    await goto_retry(page, url, timeout=LOGIN_GOTO_TIMEOUT, label='OTC inline-логин')


async def _eval(page: Page, js: str, *args):
    """evaluate под потолком программы — у ядра свой, но терять наш незачем."""
    return await eval_js(page, js, *args, cap=EVAL_CAP)


async def otc_inline_login(page: Page, context: BrowserContext,
                           mail: str, app_pass: str, sel: dict) -> bool:
    """`stop_wait` обязателен, хотя у ядра он необязательный: без него ожидание кода из письма —
    блокирующий `time.sleep` в рабочем потоке (до `CODE_WAIT_SECONDS`=120с), который про SIGTERM
    не знает и досиживает своё окно, а `asyncio.run` в 3.11 ждёт потоки дефолтного пула БЕЗ
    таймаута. Сигнал, пришедший в это окно, сдвигал НАЧАЛО уборки почти на две минуты, а её
    собственный бюджет (SHUTDOWN_TOTAL_BUDGET=120с) оставляет от TimeoutStopSec=150 меньше
    тридцати секунд запаса — systemd приходил с SIGKILL посреди закрытия браузера и оставлял
    висячий Playwright-lock в общем кэше ноды (лечит его _heal_stale_pw_lock на следующем старте).
    С прерываемой паузой поток уходит на первом же такте."""
    return await inline_login(page, context, mail=mail, app_pass=app_pass, sel=sel,
                              goto=_goto, eval_js=_eval, logger=logger, on_trade=on_trade,
                              stop_wait=wait_stop)
