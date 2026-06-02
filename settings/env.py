"""Единый разбор переменных окружения — без дублей в config/logger_config/database_config.

Чистые функции (dotenv грузят сами модули-потребители перед использованием). Контракт:
обязательные env при отсутствии падают понятной ошибкой (а не криптичным TypeError/None глубже).
"""
import os


def parse_bool(value: str | None) -> bool:
    """bool из строки (1/0, true/false, yes/no, on/off). None → False."""
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def req_str(name: str) -> str:
    """Обязательная str-переменная — понятная ошибка вместо падения глубже (asyncpg(None) и т.п.)."""
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Не задана обязательная переменная окружения {name}")
    return value


def req_int(name: str) -> int:
    """Обязательная int-переменная — понятная ошибка вместо TypeError на None."""
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"Не задана обязательная переменная окружения {name}")
    return int(value)


def opt_int(name: str, default: int) -> int:
    """int с дефолтом (env не задан → default)."""
    value = os.getenv(name)
    return int(value) if value is not None else default
