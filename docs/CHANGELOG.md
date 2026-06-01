# Changelog

## [Unreleased]

### Ревью кода (аудит ошибок/зависаний/дублей/мёртвого кода)
- Fail-fast для PG-кред: `DATABASE`/`PG_USER`/`PG_PASSWORD`/`PG_HOST` читаются через `_require`
  (понятная ошибка на старте вместо криптичного `asyncpg.connect(None)`).
- `config`: `option_setting.translocation` валидируется (список ≥2) — иначе был бы
  `TypeError`/`IndexError` на импорте (краш до подъёма логгера).
- Дедуп отправки в Telegram: единый `my_exeptions.send_photo_safe` (таймаут + восстановление);
  `main_app._try_send`, `app.check_plus`, `app.dop_plus_message` сведены к нему (было 3 копии).
- `check_plus`: `asyncio.sleep(CHECK_PLUS_DELAY)` перенесён внутрь ветки поста-вехи (не тратится
  в каждом плюсовом цикле).
- `find_point`: мёртвый `while i_color == 0` → `while True`, убран недостижимый `return`.
- Убраны неиспользуемые импорты (`channel_id` в main_app, `lost_connection_photo` в app).

### Приведение к семейному стандарту BinoDex (lifecycle-standard)
- §10 OTC-цена ↔ график: трекер хранит историю тиков `(recv_wall, price)`; `screenshot_otc`
  фиксирует `t_shot` ДО скрина и берёт `get_price_at` (тик до кадра, а не свежий после) —
  убирает «забег вперёд». Разбор и эмпирика — `docs/BINODEX_PRICE.md`.
- §3.1/§3.2 session-детект: `session_failed()` = `Unauthorized` + строковые `_SESSION_FAIL_MARKERS`
  (раньше только `isinstance`); старт юзербота двумя ветками (мёртвый ключ → сразу выход;
  transient → 3 попытки → плановый выход); рантайм-детект в `my_exeptions` через `session_failed`.
- §3.3 выделенный канал session-алертов: уровень логгера `SESSION` (37), `logger.session`,
  env `SESSION_CHANNEL` (фоллбэк на `error_channel`) — больше не уходит в шумный cookies-канал.
- §1.1 запись `status=false` не глотается: `write_status_offline()` (таймаут + `logger.error`
  при сбое), все плановые выходы переведены на неё.
- §4.1/§4.3 cookies: детект по URL-редиректу (TV `/signin`, OTC уход с `/trade`), эвристика
  цены — вторичная. Политика **Survive**: `CookiesExpired` → анти-спам backoff (120с×5, далее
  300с) + пересоздание браузера, куки перечитываются из БД на каждом init (`get_tv_cookies`/
  `get_otc_cookies`), процесс не выходит. Backoff прерывается SIGTERM.
- §4.4 усиления детекта: runtime WS-liveness OTC (`ws.on('close')` + `feed_dead`). Проактивный
  TTL TV — НЕ внедряли: проверка на живых данных показала, что у TV-кук `expires` уже в прошлом
  (−229 дней), а сессия рабочая — TradingView держит её server-side, игнорируя срок куки (тот же
  капкан, что у Privy). TTL давал ложное «истекла» → реальную смерть TV-кук ловит реактивный
  `/signin`-детект (§4.1). `cookie_health.py` удалён.
- §7 пауза между циклами — в `.env` (`MAIN_CYCLE_PAUSE_MIN`/`MAX`, дефолты 100/120, для OTC +30).

### Аудит и оптимизация
- env: дефолты/валидация (`OVERLAP`/`OVERLAP_RANDOM`/`PG_PORT`, обязательные каналы) —
  понятная ошибка вместо `TypeError` на отсутствующем env; добавлен `.env.example`.
- OTC `select_otc_pair`: слепые `asyncio.sleep` → auto-wait (ожидание поля ввода и
  нужного пункта списка); `_close_pair_modal` — поллинг до закрытия вместо фикс-пауз.
  Проверено на живом binodex (выбор пары ~1.15с против ~4с, скриншот + цена ок).
- `Option`: убран вводящий в заблуждение `@dataclass` при ручном `__init__`
  (поля → обычные атрибуты-дефолты, мутабельный `dogon_par` только в `__init__`).
- Дедуп: `init_json_codec`/`DB_NAMES` в `database_config` (общие для `postgres.py`
  и `_bootstrap.py`), единый `parse_bool`, `_close_popup` в `app.py`.
- `main.py`: прерываемый сон на выходных (SIGTERM не зависает), `datetime.now()` один раз.
- Логгер: ссылки на fire-and-forget задачи отправки + таймаут; `exit_app`:
  таймауты на закрытие браузера/юзербота; `screenshot_otc` отдаёт текст ошибки.
- Чистка: удалён неиспользуемый `psycopg2-binary` (БД на asyncpg), мёртвая
  `MAX_PRICE_ATTEMPTS`, невалидный pref `useragentoverride`, закомментированный код
  в `messages`; удалён устаревший `docs/MIGRATION_SELENIUM_TO_PLAYWRIGHT.md`.
- Доки: README (структура), DEPLOY (`PROG_KEY`, путь `Binodex`), service-файлы (путь).

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
- Таймауты на всех прямых Pyrogram-отправках (`asyncio.wait_for`, `TG_SEND_TIMEOUT`/да
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

### FIN-поиск актива (`apps/browser_app.py`)
- Перед вводом строки поиска, помимо сброса категории на «Все» (`_reset_search_category`),
  снимаем вторичный чип биржи (FXCM-scope) — `_remove_fxcm_chip()` по селектору
  `scope_chip` из `settings.tv_settings`. Иначе scope от прошлого выбора отсеивает
  поиск по формату `EXCHANGE:SYMBOL`. Выбор строки по-прежнему пиннингует FXCM
  (`_click_fxcm_pair`). Приведено к единому виду с проектом ForumTrade.

### Надёжность (2-й проход аудита)
- `settings/_bootstrap.py`: добавлен `command_timeout` — старт не зависнет навсегда
  на «живой, но не отвечающей» БД.
- `main_app.main`: ожидание экспирации/догона (`option_time`/`dgn_time`) теперь
  прерывается по SIGTERM (`_sleep_or_stop` + `stop_event`) — graceful-shutdown не
  ждёт минуты и не рискует SIGKILL.
- `price_tracker.get_price`: при заданном, но не найденном активе возвращается `None`
  (раньше отдавалась цена произвольного другого актива → неверная цена в посте).
- `exit_main`: при штатной остановке — ранний выход без постов и без инкремента
  счётчиков (раньше уходило в plus-ветку и слало `check_plus`/`dop_plus` в канал).
- `find_point`: неудача (таймаут/ошибка) логируется, а не проглатывается.
- `Database.connect`: идемпотентность + закрытие пулов при частичном сбое (нет утечки).
- `mouse_move`: ошибка через `logger.warning` вместо `logger.report` (не в TG-канал).
- OTC `design_customization`: `span.click(timeout=…)` — без 30-с зависания.

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

| Selenium                                   | Playwright                       |
|--------------------------------------------|----------------------------------|
| `webdriver.Firefox(options)`               | `playwright.firefox.launch()`    |
| `driver.get(url)`                          | `page.goto(url)`                 |
| `driver.find_element(By.CSS_SELECTOR, s)`  | `page.locator(s)`                |
| `driver.find_element(By.ID, id)`           | `page.locator(f"#{id}")`         |
| `driver.find_element(By.CLASS_NAME, c)`    | `page.locator(f".{c}")`          |
| `driver.find_element(By.XPATH, x)`         | `page.locator(f"xpath={x}")`     |
| `WebDriverWait().until(EC.*)`              | Встроенные auto-wait             |
| `ActionChains().move_to_element().click()` | `locator.click()` / `page.mouse` |
| `element.screenshot(path)`                 | `locator.screenshot(path=path)`  |
| `driver.execute_script(js)`                | `page.evaluate(js)`              |
| `driver.add_cookie(cookie)`                | `context.add_cookies([cookie])`  |
| `driver.switch_to.new_window('tab')`       | `context.new_page()`             |
| `element.get_attribute("textContent")`     | `locator.text_content()`         |
| `element.value_of_css_property(p)`         | `locator.evaluate()`             |
| `element.send_keys(text)`                  | `locator.fill(text)`             |
| `driver.refresh()`                         | `page.reload()`                  |
| `driver.quit()`                            | `browser.close()`                |

---

### Установка

```bash
pip install playwright asyncpg
playwright install firefox
```
