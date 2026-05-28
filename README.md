# UniversalOption

Автоматизированная система управления опционами Binary и OTC.

## Возможности

- Работа с Binary и OTC опционами
- Автоматический парсинг данных с TradingView и PocketOption
- Отправка сигналов в Telegram-канал
- Поддержка нескольких таймфреймов (1m, 3m, 5m)
- Система догонов с настраиваемыми параметрами
- Асинхронная работа с PostgreSQL

## Технологии

- **Python 3.11+**
- **Playwright** — автоматизация браузера Firefox
- **asyncpg** — асинхронная работа с PostgreSQL
- **Pyrogram** — отправка сообщений в Telegram
- **Pillow** — обработка скриншотов

## Установка на сервер

Целевой каталог: **`/home/vova/Binidex/BinoOptions`**. Заливка — копированием с dev-машины (rsync/scp), **не** через git.

```bash
# 1. Каталог на сервере
mkdir -p /home/vova/Binidex/BinoOptions

# 2. Копия проекта с dev-машины (исключая venv/.git/логи/сессии)
rsync -av --exclude 'venv' --exclude '.venv' --exclude '.git' \
    --exclude 'logs/*' --exclude 'files/*' \
    ./ vova@SERVER:/home/vova/Binidex/BinoOptions/

# 3. Виртуальное окружение (Python 3.11) — на сервере именно venv
cd /home/vova/Binidex/BinoOptions
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
| [DEPLOY.md](docs/DEPLOY.md)                                 | Инструкция по деплою на сервер |
| [CHANGELOG.md](docs/CHANGELOG.md)                           | История изменений              |
| [MIGRATION.md](docs/MIGRATION_SELENIUM_TO_PLAYWRIGHT.md)    | Миграция Selenium → Playwright |

## Структура проекта

```
UniversalOption/
├── apps/                   # Основная логика приложения
│   ├── app.py              # Функции для Binary
│   ├── otc_app.py          # Функции для OTC
│   ├── browser_app.py      # Управление браузером
│   ├── main_app.py         # Главный цикл
│   └── exit_app.py         # Завершение работы
├── database/               # Работа с БД
│   ├── postgres.py         # Синхронный клиент
│   └── async_postgres.py   # Асинхронный клиент
├── settings/               # Конфигурация
│   ├── config.py           # Основные настройки
│   ├── browser_set.py      # Настройки браузера
│   └── timing.py           # Таймауты и задержки
├── messages/               # Шаблоны сообщений
├── logs/                   # Логи приложения
├── systemd/                # Service-файлы для systemd
├── docs/                   # Документация
├── main.py                 # Точка входа
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

Unit-файлы в папке `systemd/` (`WorkingDirectory=/home/vova/Binidex/BinoOptions`, запуск `venv/bin/python3.11 main.py`, параметры экземпляра — в `Environment=`):

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
