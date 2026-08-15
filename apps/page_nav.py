"""Единая навигация браузера с ретраями на ТРАНЗИЕНТНЫЕ сбои (binodex/Privy).

Раньше таких хелперов было два, с РАЗНЫМИ наборами «транзиентных» ошибок:
`otc_app._goto_otc` ретраил только `NS_BINDING_ABORTED`, а `otc_login._goto_retry` — ещё и
сетевые блипы/DNS/таймаут. Домен один и тот же, значит блип до binodex ронял init, но
переживался в релогине — расхождение без причины. Здесь один список и одна реализация.

Что считаем транзиентным: гонку редиректа Privy (Firefox рвёт навигацию на собственном
редиректе страницы) и короткий сбой сети/CDN/DNS до binodex.app. НЕ путать с реально протухшей
сессией — та проявляется уже ПОСЛЕ успешной навигации (нет кнопки пары / нет privy:token), а не
ошибкой goto. Принцип семейства «сбой сайта → переждать/повторить, а не выходить»: единичный
NS_ERROR_* не должен сжигать цикл восстановления сессии (Recover-3→Exit).
"""
import asyncio

from playwright.async_api import Page

from logs import init_logger

logger = init_logger(__name__)

# Маркеры транзиентных сбоев навигации (подстрока в тексте ошибки Playwright).
RETRYABLE_GOTO_ERRORS = (
    'NS_BINDING_ABORTED',                # Privy сам инициирует редирект при загрузке → Firefox рвёт навигацию
    'NS_ERROR_FAILURE',                  # generic network failure (наблюдался блип до binodex.app/Cloudflare)
    'NS_ERROR_NET_RESET',
    'NS_ERROR_NET_TIMEOUT',
    'NS_ERROR_NET_INTERRUPT',
    'NS_ERROR_CONNECTION_REFUSED',
    'NS_ERROR_PROXY_CONNECTION_REFUSED',
    'NS_ERROR_UNKNOWN_HOST',             # транзиентный сбой DNS
    'ERR_CONNECTION_RESET',              # те же классы в Chromium (OTC по умолчанию на нём)
    'ERR_CONNECTION_CLOSED',
    'ERR_NAME_NOT_RESOLVED',
    'ERR_ABORTED',
    'Timeout',                           # PWTimeout самого goto (домен не ответил за timeout)
)

GOTO_ATTEMPTS = 3        # попыток навигации
GOTO_RETRY_PAUSE = 1.5   # сек между попытками


def on_trade(url: str) -> bool:
    """binodex: авторизация активна, если остались на …/trade (Privy редиректит
    неавторизованных). Детерминированный детект отвала cookies (§4.1) — основной сигнал.

    Живёт здесь, а не в otc_app: то же условие было ещё дважды написано руками (в `otc_app`
    и в `otc_login`), а импортировать из `otc_app` логин не может — кольцо импортов."""
    return url.rstrip('/').endswith('/trade')


async def goto_retry(page: Page, url: str, timeout: int,
                     attempts: int = GOTO_ATTEMPTS,
                     pause: float = GOTO_RETRY_PAUSE,
                     label: str = 'OTC') -> None:
    """`page.goto` с ретраями на транзиентные сбои (см. RETRYABLE_GOTO_ERRORS).

    Прочие ошибки пробрасываются сразу; исчерпали попытки — пробрасываем последнюю.
    NB: таймаут самой навигации теперь тоже ретраится (раньше в init_otc он падал с первого
    раза) — до attempts×timeout на подъём страницы, зато блип не уводит в пересоздание браузера.

    :param timeout: потолок ОДНОЙ навигации (мс)
    :param label: префикс в логе — чей это поток (init/релогин)
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            return
        except (Exception,) as error:
            if not any(marker in str(error) for marker in RETRYABLE_GOTO_ERRORS):
                raise
            last_error = error
            tail = 'повтор' if attempt < attempts else 'попытки исчерпаны'
            logger.warning(f'{label}: goto {url} → транзиентный сбой ({attempt}/{attempts}), '
                           f'{tail}: {str(error).splitlines()[0]}')
            if attempt < attempts:
                await asyncio.sleep(pause)
    raise last_error
