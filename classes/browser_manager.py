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
        """Закрытие браузера и всех страниц"""
        try:
            for page in self.pages.values():
                if not page.is_closed():
                    await page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except (Exception,) as e:
            # «Connection closed/lost» при закрытии = драйвер уже мёртв (штатная остановка/краш) — не сбой
            if 'Connection closed' in str(e) or 'Connection lost' in str(e):
                logger.warning(f"Браузер уже закрыт (соединение с драйвером потеряно): {e}")
            else:
                logger.error(f"Ошибка при закрытии браузера: {e}")
