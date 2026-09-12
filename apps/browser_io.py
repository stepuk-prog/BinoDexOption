"""Низкоуровневый ввод-вывод браузера: evaluate/screenshot с верхней границей по времени.

У `page.evaluate` и `page.screenshot` встроенного таймаута НЕТ: зависший рендерер вешает await
навсегда — и это уже стоило простоя. Страховка была, но копиями: своя `_eval` в otc_app и ручные
`asyncio.wait_for(page.evaluate(...))` в browser_app/app.py, то есть в части мест её просто
забывали. Здесь одна реализация на программу (ревизия 12-09-2026, §5).

Потолок называется `cap` и меряется в СЕКУНДАХ. Имя не `timeout` намеренно: у Playwright свой
`timeout` (в МИЛЛИСЕКУНДАХ) есть и у `Page.screenshot`, и у `Locator.screenshot`, и у
`Locator.evaluate` — одноимённый параметр здесь перехватывал бы его из `**kwargs`, то есть
`shot(el, timeout=5000)` дал бы внешнее ожидание на 5000 СЕКУНД и не выставил бы таймаут
Playwright вовсе. Теперь оба живут рядом: `cap` — наш, `timeout` уезжает в Playwright как обычно.
"""
import asyncio

from settings.timing import EVAL_TIMEOUT


async def eval_js(target, js, *args, cap: float | None = None):
    """page/element.evaluate с потолком по времени (секунды).

    `cap` — свой предел, когда общий EVAL_TIMEOUT не подходит (например, остаток бюджета у
    вызывающего). Проверка именно `is None`, а не `or`: `cap=0` — это «не ждать вовсе», и
    подменять его десятью секундами нельзя."""
    return await asyncio.wait_for(target.evaluate(js, *args),
                                  timeout=EVAL_TIMEOUT if cap is None else cap)


async def shot(target, *, cap: float | None = None, **kwargs):
    """page/element.screenshot с тем же потолком (встроенного таймаута тоже нет).

    `**kwargs` уходят в Playwright как есть — включая его собственный `timeout` (мс), если он
    нужен; наш потолок — `cap` (секунды). См. про имена в докстринге модуля."""
    return await asyncio.wait_for(target.screenshot(**kwargs),
                                  timeout=EVAL_TIMEOUT if cap is None else cap)
