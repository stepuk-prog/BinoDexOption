"""Inline-логин binodex.app (Privy email-OTP) В ОСНОВНОМ браузере бота — async, без подпроцесса.

Раньше релогин жил отдельным sync-подпроцессом `binodex_session.py` с СВОИМ холодным браузером:
основной браузер доходил до страницы авторизации, закрывался, подпроцесс невидимо (headless)
заново грузил тот же сайт, логинился, писал в БД, и основной браузер пересоздавался. Двойная
загрузка + невидимый флоу + лишние точки отказа. Теперь логин — это набор async-функций над уже
открытым `page` основного (видимого) браузера: пришли на страницу авторизации → залогинились
прямо тут. Забор OTP-кода с почты (блокирующий `imaplib`) уводим в `asyncio.to_thread`.

Контракт: `otc_inline_login(page, context, mail, app_pass, sel) -> bool` (True — вошли, в
localStorage есть privy:token и мы на /trade). Селекторы/URL — из binodex_settings (sel dict).
"""
import asyncio
import email
import imaplib
import re
import time
from email.header import decode_header, make_header

from playwright.async_api import Page, BrowserContext, TimeoutError as PWTimeout

from logs import init_logger

logger = init_logger(__name__)

URL_LANDING = "https://binodex.app/"          # дефолт; рантайм берёт landing_url из sel
URL_TRADE = "https://binodex.app/trade"       # дефолт; рантайм берёт trade_url из sel

# Privy шлёт код с РАЗНЫХ адресов (no-reply@privy.io и no-reply@mail.privy.io) — фильтр по домену.
PRIVY_FROM = "privy.io"
PRIVY_SUBJECT_HINT = "login code"             # тема: "Your login code for BinoDex"
CODE_WAIT_SECONDS = 120
CODE_POLL_EVERY = 3

REQUIRED_LOGIN_SELECTORS = ("login_open", "login_email", "login_submit", "login_code_inputs")


# ── IMAP / код Privy (sync — зовём через asyncio.to_thread) ───────────────────────────────────
def _imap_connect(mail: str, app_pass: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
    imap.login(mail, app_pass)
    imap.select("INBOX")
    return imap


def _privy_uids(imap: imaplib.IMAP4_SSL) -> list[int]:
    # noinspection PyTypeChecker
    typ, data = imap.uid("search", None, f'(FROM "{PRIVY_FROM}")')  # None — charset (валидно для IMAP)
    return [int(x) for x in data[0].split()] if data and data[0] else []


def _extract_code(imap: imaplib.IMAP4_SSL, uid: int) -> str | None:
    typ, md = imap.uid("fetch", str(uid), "(RFC822)")
    if not md or not md[0]:
        return None
    msg = email.message_from_bytes(md[0][1])
    if PRIVY_SUBJECT_HINT not in str(make_header(decode_header(msg.get("Subject", "")))).lower():
        return None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() in ("text/plain", "text/html"):
            body = part.get_payload(decode=True)
            if not body:
                continue
            try:
                txt = body.decode(part.get_content_charset() or "utf-8", "ignore")
            except (Exception,):
                continue
            m = re.search(r"\b(\d{6})\b", txt)
            if m:
                return m.group(1)
    return None


def _wait_for_code(imap: imaplib.IMAP4_SSL, baseline: set[int]) -> str:
    """Первое письмо с кодом ПОСЛЕ запроса (uid не из baseline) — старые коды игнорируем.
    Блокирующий поллинг IMAP до CODE_WAIT_SECONDS — вызывать через asyncio.to_thread."""
    deadline = time.monotonic() + CODE_WAIT_SECONDS
    while time.monotonic() < deadline:
        imap.noop()
        for uid in sorted(set(_privy_uids(imap)) - baseline, reverse=True):
            code = _extract_code(imap, uid)
            if code:
                return code
        time.sleep(CODE_POLL_EVERY)
    raise RuntimeError(f"код Privy не пришёл за {CODE_WAIT_SECONDS}с")


def _purge_privy(imap: imaplib.IMAP4_SSL) -> None:
    """Удалить письма Privy (одноразовые коды). Gmail: ярлык \\Trash."""
    uids = _privy_uids(imap)
    if not uids:
        return
    uid_set = ",".join(str(u) for u in uids)
    for store in (("+X-GM-LABELS", "\\Trash"), ("+FLAGS", "\\Deleted")):
        try:
            imap.uid("STORE", uid_set, *store)
        except (Exception,):
            pass
    try:
        imap.expunge()
    except (Exception,):
        pass


def _safe_logout(imap: imaplib.IMAP4_SSL) -> None:
    try:
        imap.logout()
    except (Exception,):
        pass


# ── Playwright (async, над живым page) ─────────────────────────────────────────────────────────
async def _clear_session(page: Page, context: BrowserContext) -> None:
    """Сбросить старую (битую) сессию из контекста перед логином — чтобы протухшие privy:*
    не путали Privy SDK. Логинимся «как с чистого листа», но в том же браузере."""
    try:
        await context.clear_cookies()
    except (Exception,):
        pass
    try:
        await page.evaluate("() => { try { localStorage.clear(); sessionStorage.clear(); } catch(e){} }")
    except (Exception,):
        pass


async def _wait_code_inputs(page: Page, sel: str, timeout: int) -> None:
    """Дождаться появления ≥6 полей ввода OTP-кода (виджет Privy)."""
    await page.wait_for_function(
        "s => document.querySelectorAll(s).length >= 6", arg=sel, timeout=timeout)


async def _enter_code(page: Page, sel: str, code: str) -> None:
    cells = page.locator(sel)
    if await cells.count() < 6:
        raise RuntimeError(f"ожидал 6 ячеек кода, нашёл {await cells.count()}")
    await cells.first.click()
    await page.keyboard.type(code, delay=60)            # OTP-виджет сам раскидает цифры
    if await cells.first.input_value() != code[0]:      # фолбэк: по цифре в ячейку
        for i, ch in enumerate(code):
            await cells.nth(i).fill(ch)


async def _goto_retry(page: Page, url: str, attempts: int = 3, pause: float = 1.5) -> None:
    """page.goto с ретраями ТОЛЬКО на NS_BINDING_ABORTED: binodex/Privy во время загрузки сам
    инициирует редирект → Firefox обрывает навигацию (гонка, не реальный сбой). Прочие ошибки —
    сразу наверх; исчерпали попытки — пробрасываем последнюю."""
    last = None
    for i in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return
        except (Exception,) as err:
            if 'NS_BINDING_ABORTED' not in str(err):
                raise
            last = err
            logger.warning(f'OTC inline-логин: goto {url} → NS_BINDING_ABORTED ({i}/{attempts}), повтор')
            if i < attempts:
                await asyncio.sleep(pause)
    raise last


async def otc_inline_login(page: Page, context: BrowserContext,
                           mail: str, app_pass: str, sel: dict) -> bool:
    """Залогиниться в binodex.app по email-OTP прямо в текущем (живом) браузере.
    True — вход удался (privy:token в localStorage, мы на /trade). False — любой сбой (лог + откат).
    Шаги: чистим сессию → страница авторизации → login_open → e-mail → код Privy (IMAP) → ввод →
    ждём токен → /trade. Селекторы/URL — из sel (binodex_settings)."""
    missing = [k for k in REQUIRED_LOGIN_SELECTORS if not sel.get(k)]
    if missing:
        logger.error(f'OTC inline-логин: нет обязательных селекторов {missing}')
        return False
    landing = sel.get('landing_url') or URL_LANDING
    trade = sel.get('trade_url') or URL_TRADE
    try:
        imap = await asyncio.to_thread(_imap_connect, mail, app_pass)
    except (Exception,) as err:
        logger.error(f'OTC inline-логин: не подключиться к почте (IMAP) — {err}')
        return False
    try:
        baseline = set(await asyncio.to_thread(_privy_uids, imap))   # старые коды — игнор
        await _goto_retry(page, landing)
        await _clear_session(page, context)
        await _goto_retry(page, landing)  # перезагрузка начисто
        await page.click(sel["login_open"], timeout=15000)
        await page.fill(sel["login_email"], mail, timeout=15000)
        await page.locator(sel["login_email"]).first.press("Enter")  # отправка надёжнее через Enter
        try:
            await _wait_code_inputs(page, sel["login_code_inputs"], 8000)
        except PWTimeout:
            await page.locator(sel["login_submit"]).first.click(timeout=8000)
            await _wait_code_inputs(page, sel["login_code_inputs"], 15000)
        code = await asyncio.to_thread(_wait_for_code, imap, baseline)
        await _enter_code(page, sel["login_code_inputs"], code)
        await page.wait_for_function(
            "() => !!window.localStorage.getItem('privy:token')", timeout=30000)
        await _goto_retry(page, trade)
        if not page.url.rstrip("/").endswith("/trade"):
            logger.warning(f'OTC inline-логин: после входа редирект с /trade на {page.url}')
            return False
        await asyncio.to_thread(_purge_privy, imap)
        logger.report('OTC: inline-релогин binodex успешен')
        return True
    except (Exception,) as err:
        logger.warning(f'OTC inline-логин не удался: {err}')
        return False
    finally:
        await asyncio.to_thread(_safe_logout, imap)
