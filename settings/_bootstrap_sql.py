"""Тексты СТАРТОВЫХ SQL-запросов — одним реестром.

Правило проекта: все SQL в одном месте. Рантайм-запросы живут в `database/database.py`, но
чтения конфига идут на import-time, когда пулов ещё нет (их поднимает main внутри asyncio.run),
и потому не могут ходить через слой БД. Раньше эти запросы лежали литералами по месту вызова —
в `settings/config.py` и `settings/browser_config.py`, — то есть схема жила в трёх файлах.

Почему реестр здесь, а не в `database/`: `database/__init__.py` импортирует `database.database`,
а тот — `settings.config`. Любой импорт из `database/` внутри `settings/config.py` замкнул бы
круг ровно на старте. Так что адресов SQL остаётся два, и оба объяснимы одной строкой:
  • `database/database.py` — рантайм (пулы уже есть);
  • этот модуль — старт (пулов ещё нет), исполняется через `settings/_bootstrap`.

Зависимостей у модуля нет НАМЕРЕННО: только строки. Его импортируют и `config`, и
`browser_config`, то есть он обязан быть безопасен на любом порядке импорта.
"""

SQL_OPTION_SETTING = (
    'SELECT * FROM settings.option_setting '
    'WHERE timeframe = $1 AND "binary" = $2 AND program = $3'
)

SQL_COOKIES_BINODEX_COOKIES = 'SELECT cookies FROM cookies.binodex_cookies WHERE user_id = $1'

SQL_USERBOT_CREDS = (
    'SELECT api_id, api_hash, session_string FROM telegram.telegram WHERE id_telegram = $1'
)

SQL_COOKIES_TV_COOKIES = 'SELECT cookies FROM cookies.tv_cookies WHERE user_id = $1'

SQL_OWNER_NAME = 'SELECT name FROM telegram.telegram WHERE id_telegram = $1'

SQL_BINODEX_SETTINGS = 'SELECT * FROM settings.binodex_settings'

SQL_TV_SETTINGS = 'SELECT * FROM settings.tv_settings'
