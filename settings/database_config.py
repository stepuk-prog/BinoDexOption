import os
from dotenv import load_dotenv

load_dotenv()
pg_name = os.getenv("DATABASE")
pg_user = os.getenv("PG_USER")
pg_password = os.getenv("PG_PASSWORD")
pg_host = os.getenv("PG_HOST")
pg_port = int(os.getenv("PG_PORT"))


