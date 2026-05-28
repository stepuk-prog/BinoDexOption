# UniversalOption

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

## Быстрый старт

```bash
# Клонирование
git clone <repository_url>
cd /home/vova/Options

# Виртуальное окружение
python3.11 -m venv .venv
source .venv/bin/activate

# Зависимости
pip install -r requirements.txt
playwright install firefox

# Настройка
cp .env.example .env
nano .env

# Запуск
TIMEFRAME=1m BINARY=true python bot.py
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
├── bot.py                  # Точка входа
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

Готовые service-файлы находятся в папке `systemd/`:

- `option-1m-bin.service` — Binary 1 минута
- `option-1m-otc.service` — OTC 1 минута
- и другие...

Подробнее: [DEPLOY.md](docs/DEPLOY.md)

## Лицензия

Проприетарное ПО. Все права защищены.
