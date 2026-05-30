# Changelog

## [Unreleased]

### БД: единый async-интерфейс (миграция с psycopg2)
- Синхронный `database/postgres.py` (psycopg2) заменён единым async-классом `Database`
  на asyncpg: два пула (`program` + `binodex`), json/jsonb-codec, retry и
  PgBouncer-recovery. `database/async_postgres.py` удалён (слит в `postgres.py`).
- `settings/_bootstrap.py` — синхронное чтение конфига/кред/cookies на import-time
  (одноразовые `asyncpg.connect`, свой event loop) до подъёма пулов.
- `settings/config.py` и `settings/browser_config.py` читают настройки через
  `bootstrap_fetch`; единый `database = Database()`, пулы поднимаются в `main.py`
  (`await database.connect()`). Убраны дублирующие `get_database()` в `apps/app.py`/
  `apps/otc_app.py`; `close_program`/`pages` переведены на `await`.

### Надёжность и чистка (по итогам аудита)
- `open_tv_browser`: guard на пустой/ошибочный ответ `database.pages` (был краш
  `enumerate(False)` и осиротевший браузер).
- `BrowserManager.close`: пошаговое best-effort закрытие — ошибка на page/context
  больше не мешает `browser.close()`/`playwright.stop()` (нет утечки процессов).
- Таймауты на всех прямых Pyrogram-отправках (`asyncio.wait_for`, `TG_SEND_TIMEOUT`/
  `TG_RECONNECT_TIMEOUT`); `_try_send` — таймаут по умолчанию; `my_exeptions`:
  явный возврат после `session_dead_shutdown` + таймаут на restart+resend.
- `Option`: `if result:` вместо неверного `is not None` (был латентный `IndexError`);
  исправлен fallback-глиф 📉 на сигнале «ВХОД ВНИЗ»; блок направления вынесен в
  `_apply_direction()`. Удалены 12 мёртвых полей и мёртвая ветка `clear_data`.
- `price_tracker`: удалены `last_message`/`_debug_mode`, проглатывания логируются.
- `screenshot(...)`: булев `take_shot` вместо игнорируемого `screen='...'`.
- `messages`: подпись актива вынесена в `_asset_label()`.

### Серия плюсов
- Множитель суммы заработка зависит от длины серии: 5–25 плюсов → ×1000,
  от 30 → ×10000 (`plus_message`).

## [2.1.0] - 2026-05-29

### Рефакторинг и реорганизация
- Доменные классы вынесены в `classes/`: `Option`, `BrowserManager`, `WebSocketPriceTracker`, result-типы.
- `cookie_utils.py` → `apps/`; таблицы таймфреймов (`Data_set` → `constant.py`) и `pl_mes` (→ `messages/message.py`) перенесены.
- Удалён мёртвый код: `retry.py`, `tf_config.py`, `Data_set.py`, `big_plus`, неиспользуемые тайминги/типы/функции и async-методы БД.
- Дедуп: хелперы `_try_send` (отправка постов) и `paste_overlay` (вставка QR); `place_qr`/`place_qr_otc` объединены в один инструмент.

### Сообщения
- Все посты с картинками отправляются через `send_photo` (плюс-серия, старт/выходные/сбой).
- Единый шаблон плюс-серии 5–50, сумма = число плюсов × 10 000; FIN/OTC больше не различаются.
- Терминология «торговый актив» (OTC — «торговый актив OTC») вместо «валютная пара».
- Новые тексты: сообщение о сбое сервера, начало/завершение торговой недели.
- Инструмент сверки вёрстки постов: `check_messages.py`.

### Надёжность
- Таймауты на всех внешних вызовах: БД (`command_timeout`/`connect_timeout`/`acquire`), Playwright (`goto`/`reload`/`wait_for*`/клики), Pyrogram (через `_try_send`).
- Отвал session юзербота (на старте или при отправке) → штатный стоп без перезапуска + алерт + запись в БД; прочие сбои запуска — до 5 попыток.
- `init_browser`: очистка при сбое (нет осиротевшего Firefox).
- Guard'ы от крашей: `check_plus` (False/None), цикл догонов (IndexError); единый контракт `execute_query` → `False`; `return` после `close_program`.
- Тише на штатной остановке: потеря драйвера логируется как warning, а не error.

### Отбор пар
- Окно неповтора актива — 4 рынка подряд.

### Логи
- По-уровневые файлы (`report/warning/cookies/error.log`) в `logs/option_{tf}_{bin|otc}/`; `init_logger` идемпотентен, `propagate=False`.

### Документация
- README обновлён под текущую структуру; добавлен `docs/DATABASE.md` (схема двух БД).

---

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
