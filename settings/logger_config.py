import os
from dotenv import load_dotenv

from settings.env import parse_bool, req_int  # parse_bool ре-экспортируется (импортируют отсюда)

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm


error_channel = req_int("ERROR_CHANNEL")
message_channel = req_int("MESSAGE_CHANNEL")
cookies_channel = req_int("COOKIES_CHANNEL")
# Отвал session юзербота — в ВЫДЕЛЕННЫЙ канал (не в шумный cookies-канал, иначе поток
# cookies-сообщений похоронит единственный критичный алерт). Опционален: фоллбэк на error.
_session_channel_raw = os.getenv("SESSION_CHANNEL")
session_channel = int(_session_channel_raw) if _session_channel_raw else error_channel
token = os.getenv("TOKEN")
timeframe = os.getenv("TIMEFRAME", "unknown")
binary = parse_bool(os.getenv("BINARY"))
# Единый суффикс экземпляра {tf}_{bin|otc} — ОДИН источник для config (пути файлов) и
# log_init (папка логов); раньше формула дублировалась в трёх местах.
file_suffix = f"{timeframe}_{'bin' if binary else 'otc'}"
if binary:
    prog_name = '⚡️ Bimodex Smoke FX FIN'
else:
    prog_name = '⚡️ Bimodex Smoke FX OTC'
frame = f"{prog_name} — {timeframe} "
