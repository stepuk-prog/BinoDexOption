# Инструкция по деплою UniversalOption

**Целевая папка на сервере:** `/home/vova/Options`

---

## 1. Подготовка сервера

### 1.1. Установка системных зависимостей

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev git
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
mkdir -p /home/vova/Options
cd /home/vova/Options
```

### 2.2. Копирование файлов проекта

```bash
# Вариант 1: через git
git clone <repository_url> /home/vova/Options

# Вариант 2: через scp (с локальной машины)
scp -r /path/to/UniversalOption/* vova@server:/home/vova/Options/
```

### 2.3. Создание виртуального окружения

```bash
cd /home/vova/Options
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
playwright install firefox
```

### 2.4. Установка браузеров Playwright

```bash
# Активируем venv если ещё не активирован
source /home/vova/Options/.venv/bin/activate

# Установка Firefox для Playwright
playwright install firefox

# Установка системных зависимостей для Playwright
playwright install-deps firefox
```

---

## 3. Настройка окружения

### 3.1. Создание файла .env

```bash
cp /home/vova/Options/.env.example /home/vova/Options/.env
nano /home/vova/Options/.env
```

Заполните необходимые переменные:
```env
# Database
PG_HOST=localhost
PG_PORT=5432
PG_NAME=options_db
PG_USER=options_user
PG_PASSWORD=your_password

# Telegram (для тестов)
TEST_API_ID=your_api_id
TEST_API_HASH=your_api_hash
TEST_SESSION_FILE=test_session
CHANNEL=your_test_channel_id

# Настройки догонов
OVERLAP=2
OVERLAP_RANDOM=1
```

---

## 4. Настройка systemd сервисов

### 4.1. Копирование service-файлов

```bash
sudo cp /home/vova/Options/systemd/*.service /etc/systemd/system/
```

### 4.2. Редактирование service-файлов

Отредактируйте пути в каждом service-файле:

```bash
sudo nano /etc/systemd/system/option-1m-bin.service
```

Пример содержимого:
```ini
[Unit]
Description=Option Bot 1m Binary
After=network.target

[Service]
Type=simple
User=vova
WorkingDirectory=/home/vova/Options
Environment="TIMEFRAME=1m" "BINARY=true" "TEST=false"
EnvironmentFile=/home/vova/Options/.env
ExecStart=/home/vova/Options/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4.3. Доступные конфигурации

| Сервис                   | Таймфрейм | Тип    |
|--------------------------|-----------|--------|
| `option-1m-bin.service`  | 1 минута  | Binary |
| `option-1m-otc.service`  | 1 минута  | OTC    |
| `option-3m-bin.service`  | 3 минуты  | Binary |
| `option-3m-otc.service`  | 3 минуты  | OTC    |
| `option-5m-bin.service`  | 5 минут   | Binary |
| `option-5m-otc.service`  | 5 минут   | OTC    |
| `option-10m-bin.service` | 10 минут  | Binary |
| `option-10m-otc.service` | 10 минут  | OTC    |
| `option-15m-bin.service` | 15 минут  | Binary |
| `option-15m-otc.service` | 15 минут  | OTC    |

### 4.4. Перезагрузка systemd

```bash
sudo systemctl daemon-reload
```

---

## 5. Запуск

### 5.1. Ручной запуск (для тестирования)

```bash
cd /home/vova/Options
source .venv/bin/activate

# Запуск с переменными окружения
TIMEFRAME=1m BINARY=true TEST=false python bot.py
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
/home/vova/Options/logs/option_{timeframe}_{type}.log
```

Примеры:
- `/home/vova/Options/logs/option_1m_bin.log`
- `/home/vova/Options/logs/option_1m_otc.log`

### 6.2. Просмотр логов

```bash
# В реальном времени
tail -f /home/vova/Options/logs/option_1m_bin.log

# Последние 50 строк
tail -n 50 /home/vova/Options/logs/option_1m_bin.log

# Поиск ошибок
grep "ERROR" /home/vova/Options/logs/option_1m_bin.log

# Поиск ошибок в последних 100 строках
tail -n 100 /home/vova/Options/logs/option_1m_bin.log | grep "ERROR"
```

---

## 7. Обновление

```bash
cd /home/vova/Options

# Остановка сервисов
sudo systemctl stop option-1m-bin.service

# Обновление кода
git pull

# Обновление зависимостей
source .venv/bin/activate
pip install -r requirements.txt

# Обновление Playwright (если нужно)
playwright install firefox

# Запуск сервисов
sudo systemctl start option-1m-bin.service
```

---

## 8. Troubleshooting

### Ошибка "Browser closed unexpectedly"
```bash
# Переустановка браузеров Playwright
source /home/vova/Options/.venv/bin/activate
playwright install firefox --force
playwright install-deps firefox
```

### Ошибка подключения к БД
```bash
# Проверка доступности PostgreSQL
pg_isready -h localhost -p 5432

# Проверка переменных окружения
cat /home/vova/Options/.env | grep PG_
```

### Проверка работы Playwright
```bash
source /home/vova/Options/.venv/bin/activate
python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
```
