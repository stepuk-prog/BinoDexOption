import os
from dotenv import load_dotenv

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm


def parse_bool(value: str | None) -> bool:
    """Парсинг bool из строки (поддержка 1/0, true/false, yes/no)"""
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


error_channel = int(os.getenv("ERROR_CHANNEL"))
message_channel = int(os.getenv("MESSAGE_CHANNEL"))
cookies_channel = int(os.getenv("COOKIES_CHANNEL"))
token = os.getenv("TOKEN")
timeframe = os.getenv("TIMEFRAME", "unknown")
binary = parse_bool(os.getenv("BINARY"))
if binary:
    prog_name = '🎢 Smoke FX FIN'
else:
    prog_name = '🎲 Smoke FX OTC'
frame = f"{prog_name} — {timeframe} "
