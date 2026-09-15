"""Единый разбор переменных окружения — без дублей в config/logger_config/database_config.

Чистые функции (dotenv грузят сами модули-потребители перед использованием). Контракт:
обязательные env при отсутствии падают понятной ошибкой (а не криптичным TypeError/None глубже).

Выход — через settings.fatal.fatal_exit, а НЕ через ValueError: это import-time, лупа ещё нет,
и обычный путь алертов молчит по построению. Голый ValueError давал код 1 «краш, рестартани
меня» — диспетчер честно поднимал процесс заново, тот падал снова, и так по кругу без единого
сообщения оператору. fatal_exit шлёт синхронно и отдаёт код 12 («нужен человек»).
Реестр BinoCore: fatal-config-alert.
"""
import os

from settings.fatal import fatal_exit


def parse_bool(value: str | None) -> bool:
    """bool из строки (1/0, true/false, yes/no, on/off). None → False."""
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def req_str(name: str) -> str:
    """Обязательная str-переменная — понятная ошибка вместо падения глубже (asyncpg(None) и т.п.)."""
    value = os.getenv(name)
    if not value:
        fatal_exit(f"Не задана обязательная переменная окружения {name}")
    return value


def _to_int(name: str, value: str) -> int:
    """int из строки env с понятной ошибкой (а не криптичный `int('')`/`int('abc')`)."""
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        fatal_exit(f"Некорректное целое в переменной окружения {name}={value!r}")


def req_int(name: str) -> int:
    """Обязательная int-переменная — понятная ошибка вместо TypeError на None / ValueError на blank."""
    value = os.getenv(name)
    if not value or not value.strip():   # None ИЛИ пустая/пробельная строка
        fatal_exit(f"Не задана обязательная переменная окружения {name}")
    return _to_int(name, value)


def opt_int(name: str, default: int) -> int:
    """int с дефолтом (env не задан ИЛИ пуст → default; задан мусором → понятная ошибка)."""
    value = os.getenv(name)
    if not value or not value.strip():
        return default
    return _to_int(name, value)
