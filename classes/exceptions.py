"""Доменные исключения, общие для browser_app/otc_app (нейтральный модуль без импортов —
чтобы оба могли импортировать без кольца импортов)."""


class CookiesExpired(Exception):
    """Сервис редиректнул на страницу логина (TV → /signin, binodex → ушёл с /trade):
    куки / Privy storage_state протухли. Ловится в main.py::_init_with_retry →
    cookies-backoff (сообщение + пауза + пересоздание браузера, куки перечитываются из
    БД). Программа НЕ выходит (политика Survive). См. docs/lifecycle-standard.md §4.3."""
    pass
