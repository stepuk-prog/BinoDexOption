# Миграция с Selenium на Playwright

## Обзор

Проект UniversalOption мигрирован с Selenium 4.26.1 на Playwright для автоматизации браузера Firefox.

---

## Алгоритм миграции

### 1. Обновить зависимости

```bash
# requirements.txt
# Удалить:
selenium==4.26.1

# Добавить:
playwright==1.57.0

# Установить браузеры:
playwright install firefox
```

### 2. Маппинг Selenium → Playwright

| Selenium | Playwright |
|----------|------------|
| `webdriver.Firefox(options)` | `playwright.firefox.launch()` |
| `driver.get(url)` | `page.goto(url)` |
| `driver.find_element(By.CSS_SELECTOR, s)` | `page.locator(s)` |
| `driver.find_element(By.ID, id)` | `page.locator(f"#{id}")` |
| `driver.find_element(By.CLASS_NAME, c)` | `page.locator(f".{c}")` |
| `driver.find_element(By.XPATH, x)` | `page.locator(f"xpath={x}")` |
| `WebDriverWait(driver, t).until(EC.*)` | Встроенные auto-wait или `page.wait_for_selector()` |
| `ActionChains(driver).move_to_element(e).click()` | `locator.click()` или `page.mouse` |
| `element.screenshot(path)` | `locator.screenshot(path=path)` |
| `driver.execute_script(js)` | `page.evaluate(js)` |
| `driver.add_cookie(cookie)` | `context.add_cookies([cookie])` |
| `driver.switch_to.new_window('tab')` | `context.new_page()` или `window.open()` через JS |
| `driver.switch_to.window(handle)` | `pages['name']` (словарь страниц) |
| `element.get_attribute("textContent")` | `locator.text_content()` |
| `element.value_of_css_property(p)` | `locator.evaluate("el => getComputedStyle(el).property")` |
| `element.send_keys(text)` | `locator.fill(text)` |
| `element.clear()` | `locator.clear()` |
| `driver.refresh()` | `page.reload()` |
| `driver.quit()` | `browser.close()` |
| `driver.set_window_size(w, h)` | `page.set_viewport_size({'width': w, 'height': h})` |

### 3. Архитектура: BrowserManager

Вместо передачи `WebDriver` создать класс-менеджер:

```python
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import Browser, BrowserContext, Page

@dataclass
class BrowserManager:
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    pages: dict[str, Page] = field(default_factory=dict)
    playwright: Optional[object] = None

    async def close(self):
        for page in self.pages.values():
            if not page.is_closed():
                await page.close()
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
```

### 4. Инициализация браузера

```python
from playwright.async_api import async_playwright

async def init_browser() -> tuple[bool, BrowserManager | str]:
    try:
        pw = await async_playwright().start()
        browser = await pw.firefox.launch(headless=False)
        context = await browser.new_context(
            user_agent='Mozilla/5.0...',
            ignore_https_errors=True
        )
        page = await context.new_page()
        await page.set_viewport_size({'width': 1480, 'height': 1015})

        manager = BrowserManager(
            browser=browser,
            context=context,
            pages={'main': page},
            playwright=pw
        )
        return True, manager
    except Exception as error:
        return False, f"Ошибка: {error}"
```

### 5. Открытие новых вкладок

```python
# Через JavaScript + expect_page (надёжный способ)
async with manager.context.expect_page() as new_page_info:
    await current_page.evaluate(f"window.open('{url}')")
page = await new_page_info.value
await page.wait_for_load_state('domcontentloaded')
manager.pages['tab_name'] = page
```

### 6. Работа с cookies

```python
# Добавление cookies
cookies_to_add = []
for cookie in cookies:
    cookie_copy = cookie.copy()
    cookie_copy.pop("expiry", None)  # Playwright не использует expiry
    # Приводим sameSite к правильному формату
    if 'sameSite' in cookie_copy:
        ss = cookie_copy['sameSite']
        if ss and ss.lower() in ['strict', 'lax', 'none']:
            cookie_copy['sameSite'] = ss.capitalize()
        else:
            cookie_copy.pop('sameSite', None)
    cookies_to_add.append(cookie_copy)

await context.add_cookies(cookies_to_add)
```

### 7. Strict mode — добавлять .first

Playwright в strict mode выдаёт ошибку если найдено несколько элементов:

```python
# Ошибка: strict mode violation
element = page.locator(".some-class")

# Правильно:
element = page.locator(".some-class").first
```

### 8. Viewport vs Window Size

**Важно!** В Selenium `set_window_size()` — размер окна (включая заголовок).
В Playwright `set_viewport_size()` — размер контента.

Если скриншоты стали меньше — увеличить viewport на ~80px по высоте.

Playwright даёт **одинаковый viewport** в headless и headed режимах (в отличие от Selenium).

---

## Дополнительные изменения в проекте

### Логирование: python-telegram-bot → aiogram

Синхронный `python-telegram-bot` блокировал async event loop. Заменён на `aiogram`:

```python
from aiogram import Bot

# Синглтон для бота
_telegram_bot: Bot | None = None

def get_telegram_bot() -> Bot:
    global _telegram_bot
    if _telegram_bot is None:
        _telegram_bot = Bot(token=token)
    return _telegram_bot

class TelegramBotHandler(Handler):
    def __init__(self):
        super().__init__()
        self.bot = get_telegram_bot()

    async def _send_message(self, chat_id: int, text: str):
        await self.bot.send_message(chat_id=chat_id, text=text)

    def emit(self, record: LogRecord):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send_message(...))
        except RuntimeError:
            pass  # Нет event loop
```

### Pyrogram Client: ленивая инициализация

Pyrogram Client нельзя создавать при импорте модуля — привязывается к неправильному event loop:

```python
# Плохо:
app = Client(name=session_file, api_id=api_id, api_hash=api_hash)

# Хорошо:
_app: Client | None = None

def get_app() -> Client:
    global _app
    if _app is None:
        _app = Client(name=session_file, api_id=api_id, api_hash=api_hash)
    return _app

# Использование внутри async функции:
async def bot():
    app = get_app()
    await app.start()
```

### Параметризация файлов для мульти-инстанс

Для запуска нескольких экземпляров с разными настройками:

```python
# config.py
timeframe = os.getenv("TIMEFRAME")
binary = parse_bool(os.getenv("BINARY", "0"))
file_suffix = f"{timeframe}_{'bin' if binary else 'otc'}"

shot_path = f"pictures/shot_{file_suffix}.png"
screenshot_path = f"pictures/screenshot_{file_suffix}.png"
log_path = f"logs/option_{file_suffix}.log"
```

---

## Systemd service для мульти-инстанс

```ini
# /etc/systemd/system/option-1m-bin.service
[Unit]
Description=Option Bot 1m Binary
After=network.target

[Service]
Type=simple
User=vlad
WorkingDirectory=/path/to/project
Environment="TIMEFRAME=1m"
Environment="BINARY=true"
Environment="TEST=false"
EnvironmentFile=/path/to/project/.env
ExecStart=/path/to/project/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Чеклист миграции

- [ ] Обновить requirements.txt (selenium → playwright)
- [ ] Установить браузеры: `playwright install firefox`
- [ ] Создать BrowserManager dataclass
- [ ] Переписать инициализацию браузера (async)
- [ ] Заменить все find_element на page.locator
- [ ] Добавить .first к локаторам где возможны множественные результаты
- [ ] Заменить ActionChains на page.mouse
- [ ] Обновить работу с cookies (убрать expiry, исправить sameSite)
- [ ] Заменить window handles на словарь pages
- [ ] Использовать context.expect_page() для новых вкладок
- [ ] Проверить размеры viewport (добавить ~80px если нужно)
- [ ] Сделать Pyrogram Client ленивым (get_app())
- [ ] Заменить синхронные Telegram библиотеки на async (aiogram)
- [ ] Параметризовать пути к файлам для мульти-инстанс
- [ ] Протестировать все сценарии
