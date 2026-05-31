# Инструкция по деплою BinoOptions

**Целевая папка на сервере:** `/home/vova/Binodex/BinoOptions`

Заливка проекта — **копированием с dev-машины** (rsync/scp), не через git.

---

## 1. Подготовка сервера

### 1.1. Установка системных зависимостей

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
```

### 1.2. Установка Firefox (для Playwright)

Playwright использует собственные браузеры, но для стабильной работы рекомендуется установить системный Firefox (НЕ snap версия):

```bash
# Проверка текущей установки
firefox --version
snap list firefox

# Если Firefox установлен через snap — удаляем
sudo snap remove firefox
sudo apt purge firefox

# Установка deb-версии
sudo add-apt-repository ppa:mozillateam/ppa
sudo apt update

# Запрет snap-редиректа
echo '
Package: *
Pin: release o=LP-PPA-mozillateam
Pin-Priority: 1001
' | sudo tee /etc/apt/preferences.d/mozilla-firefox

sudo apt install -y firefox
```

---

## 2. Установка приложения

### 2.1. Создание структуры папок

```bash
mkdir -p /home/vova/Binodex/BinoOptions
```

### 2.2. Копирование файлов проекта

Заливаем с dev-машины (исключая venv/.git/логи/сессии):

```bash
rsync -av --exclude 'venv' --exclude '.venv' --exclude '.git' \
    --exclude 'logs/*' --exclude 'files/*' \
    ./ vova@server:/home/vova/Binodex/BinoOptions/

# либо scp:
# scp -r ./* vova@server:/home/vova/Binodex/BinoOptions/
```

### 2.3. Создание виртуального окружения

```bash
cd /home/vova/Binodex/BinoOptions
python3.11 -m venv venv
source venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

### 2.4. Установка браузеров Playwright

```bash
# Активируем venv если ещё не активирован
source /home/vova/Binodex/BinoOptions/venv/bin/activate

# Установка Firefox для Playwright + системные зависимости
playwright install firefox
playwright install-deps firefox
```

---

## 3. Настройка окружения

### 3.1. Создание файла .env

```bash
nano /home/vova/Binodex/BinoOptions/.env
```

`.env` содержит только общую базу (параметры экземпляра — `TIMEFRAME`/`BINARY`/`TEST` — передаются через systemd `Environment=`, не здесь):

```env
# Базы (через PgBouncer): Program — настройки/cookies/telegram, binodex — данные опционов
DATABASE=Program
DATABASE_FIN=binodex
PG_HOST=localhost
PG_PORT=6442
PG_USER=vova
PG_PASSWORD=your_password

# Каналы и бот для ERROR/REPORT-логов
ERROR_CHANNEL=-100...
MESSAGE_CHANNEL=-100...
COOKIES_CHANNEL=-100...
TOKEN=your_error_bot_token

# Ключ программы — фильтр своих строк в settings.option_setting (обязателен)
PROG_KEY=bino_option

# Настройки догонов
OVERLAP=3
OVERLAP_RANDOM=2
```

Сессии юзер ботов и cookies хранятся в БД (`telegram.telegram.session_string`, `cookies.*`), в `.env` их нет. `.env` в `.gitignore`, не коммитим.

---

## 4. Настройка systemd сервисов

### 4.1. Копирование service-файлов

```bash
sudo cp /home/vova/Binodex/BinoOptions/systemd/*.service /etc/systemd/system/
```

### 4.2. Пример unit-файла

Готовые юниты лежат в `systemd/`. Структура (параметры экземпляра — в `Environment=`):

```ini
[Unit]
Description=Option Bot 1m Binary
After=network.target

[Service]
Type=simple
User=vova
WorkingDirectory=/home/vova/Binodex/BinoOptions
Environment="TIMEFRAME=1m" "BINARY=true" "TEST=false"
ExecStart=/home/vova/Binodex/BinoOptions/venv/bin/python3.11 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4.3. Доступные конфигурации

| Сервис                  | Таймфрейм | Тип    |
|-------------------------|-----------|--------|
| `option-1m-bin.service` | 1 минута  | Binary |
| `option-1m-otc.service` | 1 минута  | OTC    |
| `option-3m-otc.service` | 3 минуты  | OTC    |
| `option-5m-bin.service` | 5 минут   | Binary |
| `option-5m-otc.service` | 5 минут   | OTC    |

Под другие ТФ/типы — скопировать юнит и поменять `Environment="TIMEFRAME=..." "BINARY=..."`.

### 4.4. Перезагрузка systemd

```bash
sudo systemctl daemon-reload
```

---

## 5. Запуск

### 5.1. Ручной запуск (для тестирования)

```bash
cd /home/vova/Binodex/BinoOptions
source venv/bin/activate

# Запуск с переменными окружения
TIMEFRAME=1m BINARY=true TEST=false python main.py
```

### 5.2. Запуск через systemd

```bash
# Запуск конкретного сервиса
sudo systemctl start option-1m-bin.service

# Проверка статуса
sudo systemctl status option-1m-bin.service

# Включение автозапуска
sudo systemctl enable option-1m-bin.service
```

> **Важно:** Если программой управляет GlobalDispatcher — НЕ включайте автозапуск (`enable`).

### 5.3. Управление сервисами

```bash
# Остановка
sudo systemctl stop option-1m-bin.service

# Перезапуск
sudo systemctl restart option-1m-bin.service

# Просмотр логов systemd
sudo journalctl -u option-1m-bin.service -f
```

---

## 6. Логирование

### 6.1. Расположение логов

Логи записываются в папку `logs/` с именами по шаблону:
```
/home/vova/Binodex/BinoOptions/logs/option_{timeframe}_{type}.log
```

Примеры:
- `/home/vova/Binodex/BinoOptions/logs/option_1m_bin.log`
- `/home/vova/Binodex/BinoOptions/logs/option_1m_otc.log`

### 6.2. Просмотр логов

```bash
# В реальном времени
tail -f /home/vova/Binodex/BinoOptions/logs/option_1m_bin.log

# Последние 50 строк
tail -n 50 /home/vova/Binodex/BinoOptions/logs/option_1m_bin.log

# Поиск ошибок
grep "ERROR" /home/vova/Binodex/BinoOptions/logs/option_1m_bin.log
```

---

## 7. Обновление

```bash
# Остановка сервисов
sudo systemctl stop option-1m-bin.service

# Перезаливка кода с dev-машины (rsync, как при установке)
rsync -av --exclude 'venv' --exclude '.venv' --exclude '.git' \
    --exclude 'logs/*' --exclude 'files/*' \
    ./ vova@server:/home/vova/Binodex/BinoOptions/

# Обновление зависимостей (если менялись)
cd /home/vova/Binodex/BinoOptions
source venv/bin/activate
pip install -r requirements.txt
playwright install firefox

# Запуск сервисов
sudo systemctl start option-1m-bin.service
```

---

## 8. Troubleshooting

### Ошибка "Browser closed unexpectedly"
```bash
# Переустановка браузеров Playwright
source /home/vova/Binodex/BinoOptions/venv/bin/activate
playwright install firefox --force
playwright install-deps firefox
```

### Ошибка подключения к БД
```bash
# Проверка доступности PgBouncer
pg_isready -h localhost -p 6442

# Проверка переменных окружения
cat /home/vova/Binodex/BinoOptions/.env | grep PG_
```

### Проверка работы Playwright
```bash
source /home/vova/Binodex/BinoOptions/venv/bin/activate
python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
```
