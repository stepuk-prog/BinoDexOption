import os
from dotenv import load_dotenv

from settings.env import opt_int, parse_bool, req_int, req_str  # parse_bool ре-экспортируется (импортируют отсюда)

load_dotenv(override=False)  # Не перезаписывать переменные окружения из системы/PyCharm


error_channel = req_int("ERROR_CHANNEL")
message_channel = req_int("MESSAGE_CHANNEL")
cookies_channel = req_int("COOKIES_CHANNEL")
# Отвал session юзербота — в ВЫДЕЛЕННЫЙ канал (не в шумный cookies-канал, иначе поток
# cookies-сообщений похоронит единственный критичный алерт). Опционален: фоллбэк на error.
_session_channel_raw = os.getenv("SESSION_CHANNEL")
session_channel = int(_session_channel_raw) if _session_channel_raw else error_channel
token = req_str("TOKEN")  # обязателен: иначе Bot(token=None) падает глубокой ошибкой aiogram до подъёма логов
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

# КУДА ШЛЁМ ЛОГИ (2026-08-17). Адрес назначения — пара (chat_id, message_thread_id): у форума
# тема обязательна, у обычного канала thread=None. Ветвление живёт ЗДЕСЬ, а не в горячем emit:
# хендлер просто разворачивает готовый адрес.
#
# Ошибки переезжают из плоского канала в ФОРУМ с темой по режиму — чтобы FIN и OTC не
# перемешивались в одной ленте. Фича-флаг ровно как у MODERATOR/FORUM/TOPICS: задан ERROR_FORUM
# (и тема режима) → шлём в форум; не задан → прежний ERROR_CHANNEL, старые деплои не ломаются.
error_forum = opt_int("ERROR_FORUM", 0) or None
error_topic = opt_int("ERROR_TOPIC_FIN" if binary else "ERROR_TOPIC_OTC", 0) or None
error_dest = (error_forum, error_topic) if error_forum and error_topic else (error_channel, None)

# REPORT-сообщения — свой форум, тоже с темой по режиму. Форум ОТДЕЛЬНЫЙ от форума ошибок:
# смешивать рабочий поток с алертами нельзя, иначе алерт тонет. Флаг устроен так же.
message_forum = opt_int("MESSAGE_FORUM", 0) or None
message_topic = opt_int("MESSAGE_TOPIC_FIN" if binary else "MESSAGE_TOPIC_OTC", 0) or None
message_dest = ((message_forum, message_topic) if message_forum and message_topic
                else (message_channel, None))

# Отвал session — в свой канал, если задан; иначе туда же, куда ошибки (включая форум-тему):
# иначе при переезде ошибок в форум единственный критичный алерт остался бы в опустевшем канале.
session_dest = (session_channel, None) if _session_channel_raw else error_dest
# Куки — намеренно ОСТАЮТСЯ каналом: поток шумный и самостоятельной ценности в теме форума не
# имеет; переносить есть смысл только вместе с решением, что с ним вообще делать.
cookies_dest = (cookies_channel, None)
