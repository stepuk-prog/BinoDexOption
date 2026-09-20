"""Строка пула прокси из `settings.proxy_data` (БД binodex).

Доменный тип, поэтому живёт в `classes/`, а не рядом с политикой подбора (§1: один класс — свой
файл в наиболее когерентном месте; §2: classes/ — доменные классы и типы). Вынесен из
`settings/proxy.py` 17-09-2026 вместе с переездом самого релея в `classes/local_proxy.py`.

Только :50100 (HTTP): Playwright-Firefox не умеет socks5-auth и ненадёжно жуёт http-auth
напрямую, поэтому авторизация подставляется локальным релеем (`classes/local_proxy`), а браузеру
отдаётся адрес релея без логина и пароля.
"""
from dataclasses import dataclass


@dataclass
class ProxyData:
    """Данные прокси из settings.proxy_data (:50100 HTTP)."""
    ip: str
    port: int
    login: str
    password: str
