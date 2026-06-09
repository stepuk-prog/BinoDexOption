# Changelog

## [Unreleased]

### 2026-06-08 — Транзиент-401 на постинге фото больше не хоронит юзербота
- **Дискриминатор «мёртвая сессия» vs «транзиент медиа-DC» (`apps/my_exeptions.py`).** 401
  (`Unauthorized`, «Auth key not found») на `send_photo` бывает двух видов: (а) мейн-сессия
  реально мертва; (б) сбой на ОТДЕЛЬНОЙ сессии к медиа-DC (`save_file → session.start → Ping
  timeout`) при ЖИВОМ ключе. Раньше `lost_connection_photo` через `session_failed()` любой
  `Unauthorized` трактовал как (а) → `session_dead_shutdown()` (🔒 в session-канал +
  `status=false` + выход) — транзиент ложно хоронил бота. Теперь новая проба `session_dead()`
  (бьёт `get_me()` в мейн-DC: `Unauthorized`→мёртв, таймаут/сеть→жив) различает: маркеры
  `AUTH_KEY_*` и реально мёртвый ключ → прежний штатный стоп; голый 401 при живом ключе →
  лечится как обрыв связи (`bot.restart()` + повтор отправки). Строковые маркеры по-прежнему
  однозначно мёртвые (пробу не зовём). Если транзиент не вылечился restart+повтором —
  ⚠️-алерт в session-канал (пост потерян, но бот продолжает: переавторизация не нужна).
- **Эскалация по счётчику невылеченных транзиент-401 (`apps/my_exeptions.py`, `settings/timing.py`).**
  Страховка от мёртвой session, ложно принятой за транзиент (если `get_me()`-проба сама
  таймаутила/висла — ключ молча считался живым). Глобальный счётчик наращивается на каждый
  транзиент-401, НЕ вылеченный restart+повтором, и сбрасывается любым успешным постом
  (`_reset_transient_strikes`). Дойдя до `TRANSIENT_401_MAX_STRIKES` (=3) ПОДРЯД — эскалация
  в `session_dead_shutdown` (🔒 + `status=false` + выход), чтобы бот не висел молча с
  `status=true`, теряя каждый пост. `'Connection lost'` (сетевой обрыв, не session-death)
  счётчик не наращивает.

### 2026-06-06 — Фикс авто-рефреша кук (флоу Privy) и тест-режим binodex без пар
- **Фикс авто-восстановления OTC-кук (`apps/binodex_session.py`).** binodex сменил флоу: после
  Privy email-OTP сайт больше **НЕ редиректит на `/trade`** (остаётся на лендинге). Ожидание
  `page.wait_for_url("**/trade**")` всегда падало по таймауту 30с → весь авто-рефреш кук валился
  («waiting for navigation to **/trade**» по всем ТФ), хотя вход реально проходил. Теперь после
  ввода кода ждём `privy:token` в `localStorage` (= вход завершён) → сами `goto('/trade')` →
  проверка, что остались на `/trade`. Диагностика — `scripts/probe_login_url.py`.
- **OTC: тест-режим binodex без пар — ожидание вместо рестарт-петли
  (`apps/main_app.py::_acquire_otc_pair`).** binodex периодически (тест-режим) висит без единой
  торговой пары (модалка пуста, при живых сессии/UI/WS) — это трактовалось как «Ошибка загрузки
  валюты на график» → `status=1` рестарт по кругу. Теперь: цикл из `NO_PAIRS_RELOADS`=3 быстрых
  reload+выбор пары (пауза 5с), не помогло — `logger.report` + пауза 10 мин и повтор; после
  `NO_PAIRS_MAX_CYCLES`=6 пустых циклов (~1 ч) — рестарт (`fall=True` → `status=1`). «reload не
  поднял UI» (сплеш/редирект) отделён от «нет пар» — уходит в штатный `otc_session_dead` сразу,
  без часового ожидания. Все паузы прерываются сигналом остановки.

### Авто-восстановление OTC-кук binodex (Privy email-OTP)
- Новый портативный DB-free воркер `apps/binodex_session.py`: получает на stdin
  `{mail, app_pass, selectors, do_setup}`, делает холодный вход binodex (Privy email-OTP),
  читает 6-значный код по IMAP (Gmail), при `do_setup` прокликивает настройку сайта
  (индикатор/масштабы/тема), отдаёт `storage_state` в stdout. Зависимости — только playwright+stdlib.
- Оркестратор `apps/cookie_refresh.py` (async): через asyncpg читает креды/селекторы, запускает
  воркер подпроцессом (`sys.executable`, sync-Playwright нельзя в asyncio-loop), пишет
  `storage_state` в БД. Один драйвер БД (asyncpg) — `psycopg2` не нужен.
- `main.py`: при отвале OTC-кук политика **Recover-3→Exit** (§4.3): до 3 попыток рефреша
  (`_recover_otc_cookies`) с алертами «Куки отвалились/восстановлены/не восстановить для {name}»,
  при неуспехе — `status=false` + выход (TV остаётся Survive).
- Новые методы `Database`: `get_mail_creds`, `binodex_selectors`, `save_otc_cookies`.
- БД: колонка `telegram.telegram.mail_app_pass` (16-симв. Gmail app-password); строки `login_*`/
  `setup_*` в `binodex.settings.binodex_settings`; FK+UNIQUE `programdata.cookies_binodex` →
  `telegram.telegram(id_telegram)`; колонки `programdata.phone_topup`(numeric)/`phone_topup_date`.
- Гочи: Privy шлёт код с ДВУХ адресов (`no-reply@privy.io` и `no-reply@mail.privy.io`) — фильтр по
  домену `privy.io`; письма Privy чистятся после входа (Gmail Trash).
- §4.1.1 (стандарт): на init после URL-проверки — gate готовности UI (зависший `/trade` без
  редиректа = отвал cookies → `CookiesExpired`). Реализовано в `init_otc`.
- Фикс «залип на сплеше»: видимости кнопки выбора пары МАЛО — при залипшем Privy-токене `/trade`
  держится, тулбар рисуется частично, котировок-WS стримит все пары (`on_trade`/UI-gate по кнопке
  пары/`feed_dead` молчат), а чарт виснет на сплеше. Бот постил скрин сплеша (цена с WS-фолбэка —
  отсюда нереальные движения ярлыка) или рестартовал по кругу, НЕ запуская рефрешер. Маркер сплеша
  — отсутствие **кнопки настроек аккаунта** (`setup_settings_open`, та, через которую воркер
  ставит тему): на сплеше её нет. Добавлен `_ui_loaded` (ждёт видимость `otc_settings_btn`): в
  `init_otc` — gate readiness → `CookiesExpired` (→ Recover-3); в `otc_session_dead` — третий
  рантайм-сигнал (b) рядом с URL-детектом и `feed_dead`. `otc_settings_btn` выставлен в
  `browser_config` из уже загружаемого набора `binodex_settings`.

### Reload вкладки binodex перед каждым опционом (gap «новая версия фронта»)
- Новая проблема: binodex выкатывает новый фронт и показывает баннер «Доступна новая версия.
  Обновите страницу», SPA виснет на сплеше-спиннере. Капкан: `/trade` держится, тулбар (с кнопкой
  настроек) отрисован, котировок-WS стримит — **все три сигнала отвала молчат** (`on_trade`,
  `_ui_loaded`, `feed_dead`), а чарт не грузится. Ловить баннер по селектору ненадёжно.
- Митигация: `apps/otc_app.py::reload_otc_page(manager)` — `page.reload()` **перед каждым новым
  опционом** (вызов в начале OTC-ветки `main_app.main()`, до `parce_otc`) подхватывает новую
  версию заранее. После reload — та же readiness-лестница, что в `init_otc`, но мягкая (`bool`, не
  `CookiesExpired`): не поднялся UI → `exit_main(fall=False)` → `otc_session_dead` в main-цикле
  пересоздаёт браузер. WS-перехват после reload НЕ переустанавливаем (`page.on("websocket")`
  переживает reload — иначе задвоились бы хендлеры). Стандарт §4.4(в).

### Таймфрейм графика
- FIN 1m: фикс — график показывал **5 минут** вместо 1 минуты (`1m` ошибочно был в группе с
  `5m/10m`). Теперь чарт по `find_timeframe`: 1m/3m → 1 мин, 5m/10m → 5 мин, 15m → 15 мин.
- Рандомное время опциона распространено на OTC 3m/5m (как у FIN): `set_option_time` без гейта
  `binary`; константа `fin_option_time` → `option_time_variants`.

### Тексты постов
- «Разница пунктов» ВЕЗДЕ внутри цитаты (`third_message`, `prepare_dogon_message`,
  `minus_dogon_message`) — через пустую строку после котировок.
- Чистка вёрстки: лишние смежные спаны слиты (эмодзи-обёртки `<b><i>…</i></b>`), строка
  чат-бота отзывов в одном теге.

### Аудит/рефактор (3 прогона)
- Единый `settings/env.py` (`parse_bool/req_int/opt_int/req_str`) вместо трёх копий парсеров;
  единый `file_suffix` (logger_config) — убрано тройное дублирование формулы `{tf}_{bin|otc}`.
- `MainResult` (NamedTuple) вместо «магического» 5-tuple результата `main()`/`exit_main`.
- `main_app._capture` — дедуп 4 копий блока скриншота (FIN+find_point / OTC).
- `_recreate_pool`: `acquire(timeout=…)` (без зависания пула под локом); непредвиденная
  SQL-ошибка логируется с `exc_info`. `close_telegram_bot` ждёт отправку критичных алертов
  перед закрытием сессии. Убраны лишние `app.stop()` перед `close_program`.
- `config` через `init_logger` (раньше логи терялись в немом root). Мёртвый код
  (`log_path`, `BrowserInitResult.error`, `otc.get_price`, параметр `bot` в `_try_send`).
- Единый стиль `except (Exception,):`; правки по замечаниям PyCharm.
- `scripts/` убран из git (локальные диагностики); `.gitignore` — глобы рабочих скринов.

### OTC-цена кадра: window.chartData вместо WS-тика
- Цена кадра OTC берётся из **`window.chartData.price`** (значение, которое движок рисует на
  ярлыке графика), а не из WS-тика. Причина: график **отстаёт от WS на ~150 мс** (замер
  кросс-корреляцией, `scripts/probe_lag.py`), поэтому WS-цена «убегала вперёд» от картинки.
- `screenshot_otc` теперь делает медиану нескольких быстрых чтений `chartData` вокруг кадра
  (3 до + 3 после; гасит анимационный выброс ярлыка) с проверкой `chartData.symbol`; WS —
  фолбэк, если `chartData` недоступен. Сверка с ярлыком: медиана 9/10 против WS ~5/8.
- WS-трекер сохранён под liveness (подтверждение загрузки пары, `feed_dead`) и фолбэк.
- Доки `docs/BINODEX_PRICE.md` переписаны (chartData — главный источник; §7 закрыт).
  Диагностика — `scripts/probe_lag.py`, `scripts/probe_chartdata_median.py`.

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
