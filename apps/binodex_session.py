"""Воркер логина binodex.app (Privy email-OTP) — БЕЗ доступа к БД.

Бот (async) запускает его ПОДПРОЦЕССОМ (sync-Playwright нельзя крутить в asyncio-loop) и
передаёт всё нужное на stdin; БД читает/пишет сам бот через asyncpg (см. apps/cookie_refresh.py).
Так не нужен второй (sync) драйвер БД в проекте.

Контракт:
  stdin  (JSON): {"mail","app_pass","selectors":{login_*/setup_*},"do_setup":bool}
  stdout (JSON): {"storage_state": {...}}   при успехе, exit 0
  stderr + exit 1                            при ошибке

Делает: открыть binodex → ввести e-mail → код Privy по IMAP (Gmail app-password) → (опц.)
прокликать настройку сайта → снять storage_state. После входа чистит письма Privy (Gmail Trash).

Зависимости: playwright (sync) + stdlib (imaplib, json). Браузер-опции (вкл. mute) — внутри.
"""
import email
import imaplib
import json
import os
import re
import sys
import time
from email.header import decode_header, make_header

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Браузер (самодостаточно) ─────────────────────────────────────────────────
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0"
HEADLESS = os.getenv("BINODEX_HEADLESS", "1") != "0"
LAUNCH_OPTIONS = {
    "headless": HEADLESS,
    "firefox_user_prefs": {
        "general.useragent.override": USER_AGENT,
        "dom.webdriver.enabled": False,
        "media.volume_scale": "0.0",       # mute (на нагрузку сайта не влияет)
        "media.autoplay.default": 5,
        "toolkit.telemetry.enabled": False,
        "datareporting.healthreport.uploadEnabled": False,
        "ui.systemUsesDarkTheme": 1,
    },
}
CONTEXT_OPTIONS = {
    "user_agent": USER_AGENT,
    "viewport": {"width": int(os.getenv("BINODEX_VW", "1280")),
                 "height": int(os.getenv("BINODEX_VH", "800"))},
    "color_scheme": "dark",
}

URL_LANDING = "https://binodex.app/"
URL_TRADE = "https://binodex.app/trade"

# Privy шлёт код с РАЗНЫХ адресов (no-reply@privy.io И no-reply@mail.privy.io) — фильтруем по
# домену (подстрока в FROM матчит оба), иначе на части аккаунтов код «не находится» и воркер виснет.
PRIVY_FROM = "privy.io"
PRIVY_SUBJECT_HINT = "login code"   # тема: "Your login code for BinoDex"
CODE_WAIT_SECONDS = 120
CODE_POLL_EVERY = 3

# Обязательные селекторы логина (без них воркер не сможет войти — падаем понятно, а не KeyError).
REQUIRED_LOGIN_SELECTORS = ("login_open", "login_email", "login_submit", "login_code_inputs")

# Шаги настройки (par_name «открыть» → «выбрать»). После всех — повторный клик по
# setup_settings_open закрывает окно. Настройки персистят за аккаунтом.
SETUP_STEPS = [
    ("setup_candle_scale", "setup_candle_scale_item"),
    ("setup_chart_scale",  "setup_chart_scale_item"),
]


# ── IMAP / код Privy ─────────────────────────────────────────────────────────
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
            except (Exception,) as err:
                print(f"decode письма Privy: {err}", file=sys.stderr)
                continue
            m = re.search(r"\b(\d{6})\b", txt)
            if m:
                return m.group(1)
    return None


def _wait_for_code(imap: imaplib.IMAP4_SSL, baseline: set[int]) -> str:
    """Первое письмо с кодом ПОСЛЕ запроса (uid не из baseline) — старые коды игнорируем."""
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
    """Удалить все письма Privy (одноразовые коды). Gmail: ярлык \\Trash."""
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


# ── Playwright ────────────────────────────────────────────────────────────────
def _wait_code_step(page, sel: dict, timeout: int) -> None:
    page.wait_for_function(
        "s => document.querySelectorAll(s).length >= 6",
        arg=sel["login_code_inputs"], timeout=timeout)


def _enter_code(page, sel: dict, code: str) -> None:
    cells = page.locator(sel["login_code_inputs"])
    if cells.count() < 6:
        raise RuntimeError(f"ожидал 6 ячеек кода, нашёл {cells.count()}")
    cells.first.click()
    page.keyboard.type(code, delay=60)          # OTP-виджет сам раскидает цифры
    if cells.first.input_value() != code[0]:    # фолбэк: по цифре в ячейку
        for i, ch in enumerate(code):
            cells.nth(i).fill(ch)


def _login(page, mail: str, sel: dict, imap, baseline) -> None:
    page.goto(URL_LANDING, wait_until="domcontentloaded", timeout=30000)
    page.click(sel["login_open"], timeout=15000)
    page.fill(sel["login_email"], mail, timeout=15000)
    page.locator(sel["login_email"]).press("Enter")    # отправка надёжнее через Enter
    try:
        _wait_code_step(page, sel, timeout=8000)
    except PWTimeout:
        page.locator(sel["login_submit"]).first.click(timeout=8000)
        _wait_code_step(page, sel, timeout=15000)
    _enter_code(page, sel, _wait_for_code(imap, baseline))
    # Privy больше НЕ редиректит на /trade после входа — остаёмся на лендинге. Признак успешного
    # входа = токен Privy в localStorage; дождавшись его, сами идём на /trade и проверяем, что не
    # выбросило обратно (storage_state валиден). Раньше тут было wait_for_url('**/trade**'), которое
    # после смены флоу binodex всегда падало по таймауту и валило весь авто-рефреш.
    page.wait_for_function(
        "() => !!window.localStorage.getItem('privy:token')", timeout=30000)
    page.goto(URL_TRADE, wait_until="domcontentloaded", timeout=30000)
    if not page.url.rstrip("/").endswith("/trade"):
        raise RuntimeError(f"после логина редирект с /trade на {page.url}")


def _setup(page, sel: dict) -> None:
    # Разовая настройка после входа: слепые паузы здесь оправданы — ждём анимации меню настроек,
    # надёжного DOM-сигнала готовности у дропдаунов binodex нет (одноразовый флоу, не горячий путь).
    page.wait_for_timeout(2500)
    for open_key, item_key in SETUP_STEPS:
        try:
            page.locator(sel[open_key]).first.click(timeout=8000)
            page.locator(sel[item_key]).first.click(timeout=8000)
            page.wait_for_timeout(500)
        except (Exception,):
            pass
    # «Тема» = тумблер глобуса (фон `.wrap_bg`): inactive → глобус ВЫКЛ (фон вообще не в DOM),
    # active → ВКЛ. Глобус — главный потребитель CPU headless-рендера (полупрозрачный фон-оверлей
    # композитится софтом каждый кадр, ~90%→~38%, docs/BINODEX_CPU.md). У свежего аккаунта глобус
    # ВЫКЛ ПО УМОЛЧАНИЮ (проверено холодным логином) — то есть нужный нам режим уже стоит, трогать
    # не надо. РАНЬШЕ здесь был слепой клик тумблера — он наоборот ВКЛЮЧАЛ глобус (корень 90% CPU).
    # Теперь детерминированно: жмём тумблер ТОЛЬКО если фон реально присутствует (страховка на
    # случай, если binodex когда-нибудь включит глобус по умолчанию). Проверка по факту `.wrap_bg`
    # на странице (не по классу тумблера) — устойчиво к ротации классов.
    try:
        if page.locator(".wrap_bg").count() > 0:      # глобус включён (не дефолт) → выключаем
            page.locator(sel["setup_settings_open"]).first.click(timeout=8000)
            page.locator(sel["setup_theme"]).first.click(timeout=8000)
            page.locator(sel["setup_theme_toggle"]).first.click(timeout=8000)
            page.wait_for_timeout(500)
            page.locator(sel["setup_settings_open"]).first.click(timeout=8000)  # повторный клик = закрыть
    except (Exception,):
        pass


def run(mail: str, app_pass: str, selectors: dict, do_setup: bool) -> dict:
    missing = [k for k in REQUIRED_LOGIN_SELECTORS if not selectors.get(k)]
    if missing:
        raise RuntimeError(f"нет обязательных селекторов логина: {missing}")
    imap = _imap_connect(mail, app_pass)
    baseline = set(_privy_uids(imap))
    with sync_playwright() as p:
        browser = p.firefox.launch(**LAUNCH_OPTIONS)
        context = browser.new_context(**CONTEXT_OPTIONS)   # холодный вход, без storage_state
        page = context.new_page()
        try:
            _login(page, mail, selectors, imap, baseline)
            if do_setup:
                _setup(page, selectors)
            state = context.storage_state()
            _purge_privy(imap)
            return dict(state)
        finally:
            context.close()
            browser.close()
            try:
                imap.logout()
            except (Exception,):
                pass


if __name__ == "__main__":
    try:
        req = json.load(sys.stdin)
        result = run(req["mail"], req["app_pass"], req["selectors"], bool(req.get("do_setup")))
    except (Exception,) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    json.dump({"storage_state": result}, sys.stdout)
    sys.exit(0)
