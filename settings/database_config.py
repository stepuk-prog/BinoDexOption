import os
from dotenv import load_dotenv

load_dotenv()
pg_name = os.getenv("DATABASE")
pg_user = os.getenv("PG_USER")
pg_password = os.getenv("PG_PASSWORD")
pg_host = os.getenv("PG_HOST")
pg_port = int(os.getenv("PG_PORT"))
# Отдельная база с данными опционов (сигналы FIN/OTC). Те же host/port/логин,
# отличается только именем базы. Настройки браузера и cookies остаются в pg_name.
pg_name_fin = os.getenv("DATABASE_FIN", "binodex")


