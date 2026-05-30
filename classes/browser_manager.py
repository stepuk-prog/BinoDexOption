"""Менеджер браузера Playwright (контекст, страницы, корректное закрытие)."""
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from logs import init_logger

logger = init_logger(__name__)


@dataclass
class BrowserManager:
    """Менеджер браузера Playwright"""
    browser: Optional[Browser] = None
    context: Optional[BrowserContext] = None
    pages: dict[str, Page] = field(default_factory=dict)
    playwright: Optional[Playwright] = None

    async def close(self):
        """Закрытие браузера и всех страниц.

        Каждый шаг — независимый best-effort: ошибка на раннем (page/context) НЕ должна
        помешать `browser.close()`/`playwright.stop()`, иначе утечёт Firefox-процесс и драйвер.
        «Connection closed/lost» = драйвер уже мёртв (штатная остановка/краш) — это не сбой.
        """
        steps = (
            *[(f"page[{name}]", page.close) for name, page in self.pages.items()],
            ("context", self.context.close if self.context else None),
            ("browser", self.browser.close if self.browser else None),
            ("playwright", self.playwright.stop if self.playwright else None),
        )
        for label, action in steps:
            if action is None:
                continue
            try:
                await action()
            except (Exception,) as e:
                if 'Connection closed' in str(e) or 'Connection lost' in str(e):
                    logger.warning(f"{label}: соединение с драйвером уже потеряно — {e}")
                else:
                    logger.error(f"Ошибка при закрытии ({label}): {e}")
