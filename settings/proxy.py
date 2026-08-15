"""Подбор прокси для OTC-фолбэка (binodex).

Прокси берутся из settings.proxy_data в БД binodex (общий пул семейства ботов, со статистикой/
банами). BROWSER-бот (Playwright-Firefox) НЕ умеет socks5-auth и ненадёжно жуёт http-auth
напрямую, поэтому берём ТОЛЬКО :50100 (HTTP) и авторизуемся через локальный релей
(settings/local_proxy) — браузеру отдаём адрес релея без авторизации.

Используется как фолбэк, когда прямой режим не поднял front-end binodex (напр. отравленный
CDN-эдж отдаёт index.html вместо JS-чанка — был инцидент на AMS-колокейшене Cloudflare). Бан
РАЗДЕЛЬНЫЙ по рынку (scope): OTC-фолбэк банит прокси для binodex (поля *_binodex). scope
выводится из режима процесса (binary): FIN→'tv', OTC→'binodex'; BinoOptions включает прокси
только в OTC. Перенос приёма из проекта Screens.
"""

import random
from dataclasses import dataclass
from typing import Optional

from logs import init_logger
from settings.config import binary

logger = init_logger(__name__)

# Рынок бана: OTC → 'binodex' (поля *_binodex), FIN/binary (TradingView) → 'tv' (общие поля).
PROXY_SCOPE: str = 'tv' if binary else 'binodex'


@dataclass
class ProxyData:
    """Данные прокси из settings.proxy_data (:50100 HTTP)."""
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
    """Загрузка/перечитка активных :50100-прокси из БД для текущего scope (исключает
    забаненные/long_ban этого рынка). True при успехе."""
    global proxy_list
    rows = await database.get_active_proxies(PROXY_SCOPE)
    if not rows:  # None/False/[] — пула нет или сбой
        logger.error(f"Прокси({PROXY_SCOPE}): не удалось загрузить активные :50100 из settings.proxy_data")
        proxy_list = []
        return False
    proxy_list = [ProxyData(ip=r['ip'], port=r['port'], login=r['login'], password=r['password'])
                  for r in rows]
    logger.info(f"Прокси({PROXY_SCOPE}): загружено {len(proxy_list)} активных :50100-прокси из БД")
    return True


def get_unused_proxy() -> Optional[ProxyData]:
    """Случайный ещё не опробованный прокси (по кругу). Выставляет current_proxy. None — пул пуст."""
    global current_proxy   # used_proxies только мутируем (add/clear) — global не нужен
    if not proxy_list:
        logger.error(f"Прокси({PROXY_SCOPE}): список пуст — вызовите load_proxies_from_db() сначала")
        current_proxy = None
        return None
    available = [p for p in proxy_list if p.ip not in used_proxies]
    if not available:  # все опробованы — начинаем круг заново
        logger.info(f"Прокси({PROXY_SCOPE}): все опробованы, сбрасываю круг")
        used_proxies.clear()
        available = proxy_list.copy()
    proxy = random.choice(available)
    used_proxies.add(proxy.ip)
    current_proxy = proxy
    return proxy


def get_current_proxy() -> Optional[ProxyData]:
    """Текущий выбранный прокси (для stats/ban). Геттер — чтобы не ловить stale-binding модуля."""
    return current_proxy
