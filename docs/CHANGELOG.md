# Changelog

## [2.0.0] - 2026-01-15

### Миграция с Selenium на Playwright

#### Измененные файлы

**requirements.txt**
- Удален `selenium==4.26.1`
- Добавлен `playwright`
- Добавлен `asyncpg` для асинхронной работы с PostgreSQL

**settings/browser_set.py**
- Переписаны настройки браузера под Playwright API
- `FirefoxOptions` заменен на `browser_launch_options` и `context_options`

**apps/browser_app.py**
- Создан `BrowserManager` dataclass для управления браузером
- `init_browser()` — async инициализация Playwright
- `open_tv_browser()` — работа с cookies и страницами через Playwright
- `init_valute_browser()` — поиск элементов через `page.locator()`
- `setup_dialog_handler()` — автозакрытие JavaScript диалогов
- `setup_popup_blocker()` — автозакрытие неожиданных вкладок
- `close_dom_popups()` — закрытие DOM-модальных окон

**apps/app.py**
- Все `find_element` заменены на `page.locator()`
- `ActionChains` заменен на `page.mouse`
- `value_of_css_property()` заменен на `locator.evaluate()`
- `get_attribute("textContent")` заменен на `locator.text_content()`
- Добавлен `get_database()` для работы с async БД

**apps/otc_app.py**
- Переписан под Playwright API
- Удалена функция `wait()` — Playwright имеет встроенные ожидания
- Добавлен `get_database()` для работы с async БД

**apps/exit_app.py**
- Добавлена `_close_database()` для закрытия пула БД при выходе
- Закрытие браузера через `manager.close()`

#### Новые файлы

**database/async_postgres.py**
- `AsyncDatabase` класс с пулом соединений `asyncpg`
- Методы: `connect()`, `close()`, `execute_query()`
- Все методы работы с БД переписаны на async

**settings/result_types.py**
- `BrowserInitResult` — результат инициализации браузера
- `OperationResult` — результат операций

**settings/timing.py**
- Централизованные константы времени и таймаутов
- Задержки браузера, таймауты Playwright, retry константы

---

### Оптимизации

#### Исправления багов

**Race condition регистрации страниц** (`apps/browser_app.py`)
- Проблема: `handle_popup` закрывал новые страницы до регистрации
- Решение: страница регистрируется в `manager.pages` сразу после получения

**Busy loop в find_point** (`apps/app.py`)
- Проблема: `while` цикл без паузы нагружал CPU
- Решение: добавлен `await asyncio.sleep(0.1)`

#### Удаление неиспользуемого кода

**settings/browser_config.py**
- Удалены `pop_up2`, `pop_up3` — заменены универсальными селекторами

**settings/timing.py**
- Удалены `TEST_OPTION_TIME`, `TEST_DOGON_TIME`

**apps/main_app.py**
- Удален импорт `test` из config
- Тестовый режим теперь использует динамические тайминги из `option_data`

#### Оптимизация пауз

**apps/browser_app.py**
- Заменены `time.sleep()` на `await asyncio.sleep()`
- Удалены избыточные `PAGE_LOAD_DELAY`
- Оставлены необходимые паузы для TradingView UI (анимации, async загрузка)

---

### Маппинг API

| Selenium | Playwright |
|----------|------------|
| `webdriver.Firefox(options)` | `playwright.firefox.launch()` |
| `driver.get(url)` | `page.goto(url)` |
| `driver.find_element(By.CSS_SELECTOR, s)` | `page.locator(s)` |
| `driver.find_element(By.ID, id)` | `page.locator(f"#{id}")` |
| `driver.find_element(By.CLASS_NAME, c)` | `page.locator(f".{c}")` |
| `driver.find_element(By.XPATH, x)` | `page.locator(f"xpath={x}")` |
| `WebDriverWait().until(EC.*)` | Встроенные auto-wait |
| `ActionChains().move_to_element().click()` | `locator.click()` / `page.mouse` |
| `element.screenshot(path)` | `locator.screenshot(path=path)` |
| `driver.execute_script(js)` | `page.evaluate(js)` |
| `driver.add_cookie(cookie)` | `context.add_cookies([cookie])` |
| `driver.switch_to.new_window('tab')` | `context.new_page()` |
| `element.get_attribute("textContent")` | `locator.text_content()` |
| `element.value_of_css_property(p)` | `locator.evaluate()` |
| `element.send_keys(text)` | `locator.fill(text)` |
| `driver.refresh()` | `page.reload()` |
| `driver.quit()` | `browser.close()` |

---

### Установка

```bash
pip install playwright asyncpg
playwright install firefox
```
