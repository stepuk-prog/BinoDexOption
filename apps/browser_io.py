"""Низкоуровневый ввод-вывод браузера: evaluate/screenshot с верхней границей по времени.

У `page.evaluate` и `page.screenshot` встроенного таймаута НЕТ: зависший рендерер вешает await
навсегда — и это уже стоило простоя. Страховка была, но копиями: своя `_eval` в otc_app и четыре
ручных `asyncio.wait_for(page.evaluate(...))` в browser_app, то есть в части мест её просто
забывали. Здесь одна реализация на программу (ревизия 12-09-2026, §5).
"""
import asyncio

from settings.timing import EVAL_TIMEOUT


async def eval_js(target, js, *args, timeout: float | None = None):
    """page/element.evaluate с потолком по времени. `timeout` — свой предел, когда общий
    EVAL_TIMEOUT не подходит (например, остаток бюджета у вызывающего)."""
    return await asyncio.wait_for(target.evaluate(js, *args), timeout=timeout or EVAL_TIMEOUT)


async def shot(target, *, timeout: float | None = None, **kwargs):
    """page/element.screenshot с тем же потолком (встроенного таймаута тоже нет)."""
    return await asyncio.wait_for(target.screenshot(**kwargs), timeout=timeout or EVAL_TIMEOUT)
