# Структура БД

Документ описывает таблицы/представления PostgreSQL **в том виде, как их использует код** (по SQL-запросам в `database/postgres.py` и `database/async_postgres.py`). Это не авторитетный DDL — точные типы колонок смотрите в самой БД.

## Две базы

| База        | env                                  | Клиент в коде                                                 | Что хранит                                                   |
|-------------|--------------------------------------|---------------------------------------------------------------|--------------------------------------------------------------|
| **Program** | `DATABASE`                           | sync `Database` (`database`)                                  | Настройки браузера, юзербот-креды, cookies, статус программы |
| **binodex** | `DATABASE_FIN` (по умолч. `binodex`) | `database_fin` + async-пул `data_pool` (`use_data_pool=True`) | Сигналы опционов, счётчики плюсов, настройки экземпляра      |

Доступ к БД через **PgBouncer**. Сигнальные данные (`option_data.*`) наполняет внешний сервис **BinoOptionData** (OTC-данные синтетические).

---

## Binodex (сигналы и настройки экземпляра)

### `option_data.binary_data_view` / `option_data.otc_data_view`
Представления с готовыми сигналами (уже только enabled-активы). Читаются `option_data_tv` / `option_data_pocket`.

- **Фильтр/сортировка:** `timeframe`, `val_id`; FIN — `ORDER BY strong DESC, binary_percent DESC, itog_stat_up DESC`; OTC — `ORDER BY otc_percent DESC, itog_stat_up DESC`.
- **Колонки, читаемые в `Option` (fill_binary/fill_otc):** `val_id`, `name_val`, `round`, `base_emoji`, `second_emoji`, `resume` (FIN) / `buy` (OTC), `move_potential_down/up`, `dir_force_down/up`, `long_trend_down/up` (FIN), `volume_profile_down/up`, `average_interest_down/up`, `volume_balance_down/up`, `prop_ind`, `kol_ind`, `price_reversal_down/up`, `potential_change_down/up`, `itog_stat_down/up`.

### `option_data.counter`
Счётчики серий плюсов/минусов. `plus_counter` / `minus_counter`.

- Колонки: `plus`, `minus`, ключ — `program_id` (async-слой) либо `timeframe` + `otc` (sync-слой, легаси).
- `UPDATE … SET plus = plus + 1, minus = 0 … RETURNING plus` (и зеркально для минусов).

### `settings.option_setting`
Базовые настройки экземпляра (`option_setting_base`, через `database_fin`).

- **Фильтр:** `timeframe`, `"binary"`, `program` (ключ `PROG_KEY`).
- **Используемые колонки:** `program_id`, `channel_id`, `dogon`, `user_bot`, `cookies_tv`, `cookies_pocket`, `translocation`, `prog_name`, `session_file`.

---

## Program (браузер, cookies, юзербот, статус)

### `settings.tv_settings` / `settings.pocket_settings`
Селекторы/параметры браузера (TradingView / PocketOption). Читаются целиком (`tv_setting` / `otc_setting`), затем `find_par(data, par=...)` достаёт значение по имени параметра. Строки — пары «имя параметра → значение (селектор/класс/xpath)».

### `telegram.telegram`
Креды Pyrogram-юзербота (`telegram_creds`) + почта для авто-рефреша binodex-кук.

- **Фильтр:** `id_telegram` (= `option_setting.user_bot`; для рефреша кук — `= option_setting.cookies_pocket`).
- Колонки: `api_id`, `api_hash`, `session_string`; `name` (имя владельца — в текстах cookies-алертов, §4.2);
  `mail` + **`mail_app_pass`** (16-символьный Gmail app-password для IMAP; читает `database.get_mail_creds`
  → воркер `apps/binodex_session.py`). Обычный пароль для IMAP не годится — нужен app-password (требует 2FA).

### `program.programdata`
Статус программы (для диспетчера). `close_program`: `UPDATE program.programdata SET status = false WHERE program_id = %s`.

- Колонки (используемые ботом в рантайме): `program_id`, `status`.
- Прочие (вне рантайма бота): `cookies_binodex` (bigint, UNIQUE, FK → `telegram.telegram(id_telegram)`);
  `phone_topup` (numeric(10,2)) / `phone_topup_date` (date) — учёт пополнения телефона аккаунта.

### `cookies.pages`
Страницы браузера по программе/режиму (`pages`).

- **Фильтр:** `program`, `mode` (`'tv'`/`'otc'`); `ORDER BY order_idx`.
- Колонки: `program`, `mode`, `description`, `url`, `order_idx`.

### `cookies.tv_cookies` / `cookies.pocket_cookies`
Cookies для авторизации (`tv_cookies` / `get_pocket_cookies`).

- **Фильтр:** `user_id` (= `option_setting.cookies_tv` / `cookies_pocket`).
- Колонки: `user_id`, `cookies` (jsonb).

---

## Где какой запрос

| Метод                                   | База    | Объект                                              |
|-----------------------------------------|---------|-----------------------------------------------------|
| `option_data_tv` / `option_data_pocket` | binodex | `option_data.binary_data_view` / `otc_data_view`    |
| `plus_counter` / `minus_counter`        | binodex | `option_data.counter`                               |
| `option_setting_base`                   | binodex | `settings.option_setting`                           |
| `tv_setting` / `otc_setting`            | Program | `settings.tv_settings` / `settings.pocket_settings` |
| `telegram_creds`                        | Program | `telegram.telegram`                                 |
| `close_program`                         | Program | `program.programdata`                               |
| `pages`                                 | Program | `cookies.pages`                                     |
| `tv_cookies` / `get_pocket_cookies`     | Program | `cookies.tv_cookies` / `cookies.pocket_cookies`     |
