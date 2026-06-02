# Структура БД

Документ описывает таблицы/представления PostgreSQL **в том виде, как их использует код** (по SQL-запросам в `database/postgres.py` + чтениям на старте в `settings/_bootstrap.py`). Это не авторитетный DDL — точные типы колонок смотрите в самой БД.

## Две базы

| База        | env                                  | Что хранит                                                                                     |
|-------------|--------------------------------------|------------------------------------------------------------------------------------------------|
| **Program** | `DATABASE`                           | Селекторы TV, TV-cookies, юзербот-креды + почта, статус программы                               |
| **binodex** | `DATABASE_FIN` (по умолч. `binodex`) | Сигналы опционов, счётчики, настройки экземпляра, селекторы и cookies binodex, `cookies.pages`  |

Доступ — **единый async-класс `Database`** (asyncpg, `database/postgres.py`) с двумя пулами; база выбирается параметром `db='program'|'binodex'` в `execute_query`. Соединение — через **PgBouncer**. Конфиг/креды/cookies на старте читаются синхронно через `settings/_bootstrap.py` (одноразовые `asyncpg.connect` до подъёма пулов). Сигнальные данные (`option_data.*`) наполняет внешний сервис **BinoOptionData** (OTC-данные синтетические).

---

## binodex (`DATABASE_FIN`)

### `option_data.binary_data_view` / `option_data.otc_data_view`
Представления с готовыми сигналами (уже только enabled-активы). Читаются `option_data_tv` / `option_data_pocket`.

- **Фильтр/сортировка:** `timeframe`, `val_id`; FIN — `ORDER BY strong DESC, binary_percent DESC, itog_stat_up DESC`; OTC — `ORDER BY otc_percent DESC, itog_stat_up DESC`.
- **Колонки, читаемые в `Option` (fill_binary/fill_otc):** `val_id`, `name_val`, `round`, `base_emoji`, `second_emoji`, `resume` (FIN) / `buy` (OTC), `dir_force_down/up`, `volume_profile_down/up`, `average_interest_down/up`, `volume_balance_down/up`, `itog_stat_down/up`.

### `option_data.counter`
Счётчики серий плюсов/минусов. `plus_counter` / `minus_counter`.

- Колонки: `plus`, `minus`; ключ — `program_id`.
- `UPDATE … SET plus = plus + 1, minus = 0 … RETURNING plus` (и зеркально для минусов).

### `settings.option_setting`
Базовые настройки экземпляра (на старте через `bootstrap_fetch`).

- **Фильтр:** `timeframe`, `"binary"`, `program` (ключ `PROG_KEY`).
- **Используемые колонки:** `program_id`, `channel_id`, `dogon`, `user_bot`, `cookies_tv`, `cookies_pocket`, `translocation`, `prog_name`, `session_file`.

### `settings.binodex_settings`
CSS/XPath-селекторы сайта binodex.app (`binodex_selectors`; на старте читается целиком, значение по `par_name` достаёт `apps/setting_app.find_par`).

- Колонки: `par_name`, `par_value`, `description`.
- Группы `par_name`: **OTC-флоу** (`select_pair_add`, `category_valute`, `input_pair`, `modal_pair_item`, `tek_val`, `screen_zone`), **логин** (`login_open`/`login_email`/`login_submit`/`login_code_inputs`), **настройка сайта** (`setup_indicators`/`setup_indicator_item`/`setup_candle_scale*`/`setup_chart_scale*`/`setup_settings_open`/`setup_theme`/`setup_theme_toggle`).

### `cookies.pages`
Страницы браузера по программе/режиму (`pages`).

- **Фильтр:** `program`, `mode` (`'tv'`/`'otc'`); `ORDER BY order_idx`.
- Колонки: `program`, `mode`, `description`, `url`, `order_idx`.

### `cookies.binodex_cookies`
OTC `storage_state` (Privy). Чтение — `get_otc_cookies`, запись (авто-рефреш) — `save_otc_cookies`.

- **Фильтр:** `user_id` (= `option_setting.cookies_pocket`).
- Колонки: `user_id` (PK), `cookies` (jsonb — `{cookies, origins}`), `updated_at`. Подробно: `docs/COOKIES_BINODEX.md`.

---

## Program (`DATABASE`)

### `settings.tv_settings`
Селекторы/параметры браузера TradingView (`tv_setting`; читается целиком, затем `find_par(data, par=...)` по имени параметра). Строки — пары «имя параметра → значение (селектор/класс/xpath)». *(OTC-селекторы — в `binodex.settings.binodex_settings`, не здесь.)*

### `telegram.telegram`
Креды Pyrogram-юзербота (`telegram_creds`) + почта для авто-рефреша binodex-кук.

- **Фильтр:** `id_telegram` (= `option_setting.user_bot`; для рефреша кук — `= option_setting.cookies_pocket`).
- Колонки: `api_id`, `api_hash`, `session_string`; `name` (имя владельца — в текстах cookies-алертов, §4.2); `mail` + **`mail_app_pass`** (16-символьный Gmail app-password для IMAP; читает `database.get_mail_creds` → воркер `apps/binodex_session.py`). Обычный пароль для IMAP не годится — нужен app-password (требует 2FA).

### `program.programdata`
Статус программы (для диспетчера). `close_program`: `UPDATE program.programdata SET status = false WHERE program_id = $1`.

- Колонки (используемые ботом в рантайме): `program_id`, `status`.
- Прочие (вне рантайма бота): `cookies_binodex` (bigint, UNIQUE, FK → `telegram.telegram(id_telegram)`); `phone_topup` (numeric(10,2)) / `phone_topup_date` (date) — учёт пополнения телефона аккаунта.

### `cookies.tv_cookies`
TV-cookies для авторизации TradingView (`get_tv_cookies`). Плоский `list[dict]` (через `add_cookies`).

- **Фильтр:** `user_id` (= `option_setting.cookies_tv`).
- Колонки: `user_id`, `cookies` (jsonb).

---

## Где какой запрос

| Метод (`func`/`Database.*`)             | База    | Объект                                              |
|-----------------------------------------|---------|-----------------------------------------------------|
| `option_data_tv` / `option_data_pocket` | binodex | `option_data.binary_data_view` / `otc_data_view`    |
| `plus_counter` / `minus_counter`        | binodex | `option_data.counter`                               |
| `option_setting` (bootstrap)            | binodex | `settings.option_setting`                           |
| `binodex_selectors` (+ bootstrap)       | binodex | `settings.binodex_settings`                         |
| `pages`                                 | binodex | `cookies.pages`                                     |
| `get_otc_cookies` / `save_otc_cookies`  | binodex | `cookies.binodex_cookies`                           |
| `tv_setting` (bootstrap)                | Program | `settings.tv_settings`                              |
| `telegram_creds` / `get_mail_creds`     | Program | `telegram.telegram`                                 |
| `get_tv_cookies`                        | Program | `cookies.tv_cookies`                                |
| `close_program`                         | Program | `program.programdata`                               |
