# BinoOptions

Автоматизированная система управления опционами Binary и OTC.

## Возможности

- Работа с Binary и OTC опционами
- Автоматический парсинг данных с TradingView и PocketOption
- Отправка сигналов в Telegram-канал
- Поддержка нескольких таймфреймов (1m, 3m, 5m, 10m, 15m)
- Система догонов с настраиваемыми параметрами
- Асинхронная работа с PostgreSQL

## Технологии

- **Python 3.11+**
- **Playwright** — автоматизация браузера Firefox
- **asyncpg** — асинхронная работа с PostgreSQL
- **Pyrogram** — отправка сообщений в Telegram
- **Pillow** — обработка скриншотов

## Установка на сервер

Целевой каталог: **`/home/vova/Binodex/BinoOptions`**. Заливка — копированием с dev-машины (rsync/scp), **не** через git.

```bash
# 1. Каталог на сервере
mkdir -p /home/vova/Binodex/BinoOptions

# 2. Копия проекта с dev-машины (исключая venv/.git/логи/сессии)
rsync -av --exclude 'venv' --exclude '.venv' --exclude '.git' \
    --exclude 'logs/*' --exclude 'files/*' \
    ./ vova@SERVER:/home/vova/Binodex/BinoOptions/

# 3. Виртуальное окружение (Python 3.11) — на сервере именно venv
cd /home/vova/Binodex/BinoOptions
python3.11 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install firefox

# 4. .env — заполнить вручную (в git не коммитим)
nano .env

# 5. Запуск экземпляра вручную (параметры передаются через env)
TIMEFRAME=1m BINARY=true TEST=false venv/bin/python main.py
```

## Документация

| Документ                                                    | Описание                       |
|-------------------------------------------------------------|--------------------------------|
| [DATABASE.md](docs/DATABASE.md)                             | Структура БД (как используется кодом) |
| [DEPLOY.md](docs/DEPLOY.md)                                 | Инструкция по деплою на сервер |
| [CHANGELOG.md](docs/CHANGELOG.md)                           | История изменений              |
| [MIGRATION.md](docs/MIGRATION_SELENIUM_TO_PLAYWRIGHT.md)    | Миграция Selenium → Playwright |

## Структура проекта

```
BinoOptions/
├── classes/                # Доменные классы и типы
│   ├── Option_class.py     # Option — данные опциона/прогноза
│   ├── result_types.py     # Типизированные результаты (BrowserInitResult, OperationResult)
│   ├── browser_manager.py  # BrowserManager — браузер/контекст/страницы
│   └── price_tracker.py    # WebSocketPriceTracker — цены OTC по WebSocket
├── apps/                   # Процедурная логика
│   ├── app.py              # Функции Binary (скриншот, цена, точка входа)
│   ├── otc_app.py          # Функции OTC (выбор пары, скриншот)
│   ├── browser_app.py      # Инициализация/настройка браузера TradingView
│   ├── main_app.py         # Главный цикл прогноза + отправка постов
│   ├── exit_app.py         # Завершение/перезапуск, алерты
│   ├── my_exeptions.py     # Обработка обрывов связи Pyrogram
│   └── cookie_utils.py     # Подготовка cookies для Playwright
├── database/               # Слой БД
│   ├── postgres.py         # Синхронный клиент (бутстрап-конфиг)
│   └── async_postgres.py   # Асинхронный клиент (горячий путь, пул)
├── messages/message.py     # Тексты постов (прогнозы, итоги, догоны, плюсы)
├── settings/               # Конфигурация и константы
│   ├── config.py           # Сборка настроек экземпляра (БД + env)
│   ├── constant.py         # Константы + таблицы таймфреймов
│   ├── browser_config.py   # Селекторы браузера (из БД)
│   ├── browser_set.py      # Параметры запуска Playwright
│   ├── screenshot_set.py   # Геометрия скриншотов + paste_overlay
│   ├── image_paths.py      # Пути к картинкам
│   └── timing.py           # Таймауты и задержки
├── logs/                   # Логи (по уровням в logs/option_{tf}_{bin|otc}/)
├── pictures/               # Картинки постов (прогнозы, догоны, плюсы, QR)
├── systemd/                # Service-файлы systemd
├── docs/                   # Документация (DATABASE.md — схема БД)
├── place_qr.py             # Инструмент подбора координат QR (fin/otc)
├── check_messages.py       # Вычитка всех постов в форум-тему
├── main.py                 # Точка входа (главный цикл)
└── requirements.txt        # Зависимости
```

## Конфигурация

### Переменные окружения

| Переменная       | Описание                             | Пример            |
|------------------|--------------------------------------|-------------------|
| `TIMEFRAME`      | Таймфрейм опциона                    | `1m`, `5m`, `15m` |
| `BINARY`         | Тип опциона (true=Binary, false=OTC) | `true`            |
| `TEST`           | Тестовый режим                       | `false`           |
| `OVERLAP`        | Количество догонов                   | `2`               |
| `OVERLAP_RANDOM` | Рандомизация догонов                 | `1`               |

### Systemd сервисы

Unit-файлы в папке `systemd/` (`WorkingDirectory=/home/vova/Binodex/BinoOptions`, запуск `venv/bin/python3.11 main.py`, параметры экземпляра — в `Environment=`):

- `option-1m-bin.service`, `option-5m-bin.service` — Binary
- `option-1m-otc.service`, `option-3m-otc.service`, `option-5m-otc.service` — OTC

Установка:

```bash
sudo cp systemd/option-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now option-1m-bin.service
sudo systemctl status option-1m-bin.service
sudo journalctl -u option-1m-bin -f
```

Подробнее: [DEPLOY.md](docs/DEPLOY.md)

## Лицензия

Проприетарное ПО. Все права защищены.
