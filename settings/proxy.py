"""Подбор прокси для OTC-фолбэка (binodex).

Прокси берутся из Program.settings.proxy_data (общий пул со статистикой/банами). Используется
как фолбэк, когда прямой режим не поднял front-end binodex (напр. отравленный CDN-эдж отдаёт
index.html вместо JS-чанка — был инцидент на AMS-колокейшене Cloudflare). Авторизация — через
локальный релей (settings/local_proxy).

Перенос приёма из проекта Screens (Pocket Option OTC).
"""

import random
from dataclasses import dataclass
from typing import Optional

from logs import init_logger

logger = init_logger(__name__)


@dataclass
class ProxyData:
    """Данные прокси из settings.proxy_data."""
    ip: str
    port: int
    login: str
    password: str


# Активные прокси из БД (кэш на процесс; перечитываются load_proxies_from_db при ротации/банах).
proxy_list: list[ProxyData] = []
# Уже опробованные в этом процессе (чтобы ротация не возвращалась на тот же сразу).
used_proxies: set[str] = set()
# Текущий выбранный прокси — main по нему ведёт update_proxy_stats / ban_proxy.
current_proxy: Optional[ProxyData] = None


async def load_proxies_from_db(database) -> bool:
    """Загрузка/перечитка активных прокси из БД (исключает забаненные/long_ban). True при успехе."""
    global proxy_list
    rows = await database.get_active_proxies()
    if not rows:  # None/False/[] — пула нет или сбой
        logger.error("OTC-прокси: не удалось загрузить активные прокси из settings.proxy_data")
        proxy_list = []
        return False
    proxy_list = [ProxyData(ip=r['ip'], port=r['port'], login=r['login'], password=r['password'])
                  for r in rows]
    logger.info(f"OTC-прокси: загружено {len(proxy_list)} активных прокси из БД")
    return True


def get_unused_proxy() -> Optional[ProxyData]:
    """Случайный ещё не опробованный прокси (по кругу). Выставляет current_proxy. None — пул пуст."""
    global used_proxies, current_proxy
    if not proxy_list:
        logger.error("OTC-прокси: список пуст — вызовите load_proxies_from_db() сначала")
        current_proxy = None
        return None
    available = [p for p in proxy_list if p.ip not in used_proxies]
    if not available:  # все опробованы — начинаем круг заново
        logger.info("OTC-прокси: все опробованы, сбрасываю круг")
        used_proxies.clear()
        available = proxy_list.copy()
    proxy = random.choice(available)
    used_proxies.add(proxy.ip)
    current_proxy = proxy
    return proxy


def get_current_proxy() -> Optional[ProxyData]:
    """Текущий выбранный прокси (для stats/ban). Геттер — чтобы не ловить stale-binding модуля."""
    return current_proxy
