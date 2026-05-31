import os
from dotenv import load_dotenv

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm


def parse_bool(value: str | None) -> bool:
    """Парсинг bool из строки (поддержка 1/0, true/false, yes/no)"""
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _req_int(name: str) -> int:
    """Обязательная int-переменная окружения с понятной ошибкой вместо TypeError на None."""
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"Не задана обязательная переменная окружения {name}")
    return int(value)


error_channel = _req_int("ERROR_CHANNEL")
message_channel = _req_int("MESSAGE_CHANNEL")
cookies_channel = _req_int("COOKIES_CHANNEL")
token = os.getenv("TOKEN")
timeframe = os.getenv("TIMEFRAME", "unknown")
binary = parse_bool(os.getenv("BINARY"))
if binary:
    prog_name = '⚡️ Bimodex Smoke FX FIN'
else:
    prog_name = '⚡️ Bimodex Smoke FX OTC'
frame = f"{prog_name} — {timeframe} "
