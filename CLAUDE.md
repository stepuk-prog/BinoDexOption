# BinoOptions — контекст проекта (для Claude Code)

Async-бот торговых сигналов по опционам. Два режима: **FIN** (TradingView/FXCM) и **OTC**
(binodex.app). Снимает график через **Playwright (Firefox)**, постит сигналы в Telegram-канал
через **Pyrogram**-юзербота; служебные логи — через **aiogram**. Данные сигналов готовит
внешний сервис **BinoOptionData**.

GitHub: `git@github.com-stepuk:stepuk-prog/BinoDexOption.git`.

## Запуск
- Один экземпляр = пара env **`TIMEFRAME`** (1m/3m/5m/10m/15m) + **`BINARY`** (1=FIN, 0=OTC) + `PROG_KEY`.
  Пример: `TIMEFRAME=1m BINARY=0 .venv/bin/python main.py`.
- На сервере — systemd-юниты **`binodex-{tf}-{bin|otc}.service`** (5 шт. в `systemd/`), путь `/home/vova/Binodex/BinoOptions`, под `User=vova`, venv = **`venv`**.
- `main.py` — вход (asyncio loop, graceful по SIGTERM, старт/выходные-посты для FIN).

## Две БД (через PgBouncer, asyncpg)
- **Program** (`DATABASE`) — настройки браузера/cookies, юзербот-креды (`telegram.telegram`), статус (`program.programdata`), `cookies.pages`, `settings.tv_settings`/`pocket_settings`.
- **binodex** (`DATABASE_FIN`) — сигналы (`option_data.binary_data_view`/`otc_data_view`), счётчики (`option_data.counter`), настройки экземпляра (`settings.option_setting`), `settings.binodex_settings` (OTC-селекторы), cookies binodex.
- Старт читается синхронно (`settings/_bootstrap.py`) до подъёма пулов; рантайм — `database/postgres.py` (`Database`, пулы program+binodex). Подробно: **`docs/DATABASE.md`**.

## env (.env — gitignored; пример в `.env.example`)
`DATABASE`, `DATABASE_FIN`, `PG_*` (порт 6442), `ERROR_CHANNEL`/`MESSAGE_CHANNEL`/`COOKIES_CHANNEL`, `TOKEN`, `PROG_KEY`, `OVERLAP`, `OVERLAP_RANDOM`. `TIMEFRAME`/`BINARY` — задаёт диспетчер per-instance (для локального можно в .env). Тест-оверрайды: `TEST`, `CHANNEL`, `TEST_API_ID/HASH/SESSION_FILE`, `COOK_OTC`, `SIGNAL_CHANNEL`.

## Структура
- `apps/` — процедурная логика: `app.py` (FIN: цена/скрин/точка входа), `otc_app.py` (OTC: выбор пары/скрин/WS-цена), `browser_app.py` (init Playwright/TV), `main_app.py` (главный цикл + посты), `exit_app.py` (завершение/алерты), `my_exeptions.py`, `cookie_utils.py`.
- `classes/` — `Option_class.py` (`Option` — данные опциона; обычный класс, НЕ dataclass), `browser_manager.py`, `price_tracker.py` (WS-цены OTC), `result_types.py`.
- `database/postgres.py`, `messages/message.py` (тексты постов), `settings/` (config, _bootstrap, constant, browser_*, screenshot_set, timing, image_paths, logger_config), `logs/log_init.py` (по-уровневые файлы + TG-хендлер), `pictures/`, `scripts/`, `systemd/`, `docs/`.

## OTC / binodex (важное)
- Логин binodex — через **Privy**: сессия в `localStorage`, поэтому нужен **`storage_state`** (не только cookies); контекст создаётся `new_context(storage_state=...)`. См. `COOKIES_BINODEX.md`.
- Цена OTC — из **WebSocket** `api-coins.binodex.io` (на странице она в `<canvas>`), трекер `classes/price_tracker.py`.
- Выбор пары — модалка binodex по селекторам из `settings.binodex_settings`; **auto-wait вместо sleep** (проверено на живом сайте, ~1.15с).

## Конвенции / гочи
- Классы — в `classes/` отдельными файлами; **предпочитать Playwright auto-wait вместо `asyncio.sleep`**; таймауты на всех внешних вызовах (БД/Playwright/Pyrogram); по-уровневые логи.
- Все обязательные env-int читаются с дефолтом/понятной ошибкой (не `TypeError`).
- В постах серии плюсов **кириллические «О» вместо нулей в суммах — анти-модерация, НЕ нормализовать**.
- `option_data.name` мутируется в `otc_app` до `'EUR/USD OTC'` для скрина; для текста берут голый `'EUR/USD'`.
- Под Dispatcher в юнитах `Restart` должен быть **выключен** (иначе systemd мешает failover); деплоит/проверяет это **DeployManager** (`../../../Helpers/DeployManager`).
- Картинки постов — в `pictures/`; рабочие скрины (`shot_*`, `screenshot_*`) gitignored. Стартовое/выходное фото: `pictures/start_week.png` / `end_week.png`.

## Скрипты
- `scripts/check_messages.py` — отправка всех постов с картинками в форум-тему (вычитка вёрстки), юзербот OTC 1m. Запуск: `PYTHONPATH=. .venv/bin/python scripts/check_messages.py`.
- `scripts/place_qr.py` — наложение QR на скрин; `scripts/probe_otc.py` — диагностика OTC-флоу на живом binodex; `scripts/binodex_settings.sql` — DDL селекторов.

## Доки
`docs/DATABASE.md` (схема БД), `docs/DEPLOY.md` (деплой на сервер), `docs/BINODEX_PRICE.md` (как правильно снимать цену OTC: WS-источник + синхронизация с кадром), `docs/CHANGELOG.md`. Деплой/управление на нодах — инструментом **DeployManager**.
