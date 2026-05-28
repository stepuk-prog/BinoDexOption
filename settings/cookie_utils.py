"""Утилиты для работы с cookies в Playwright"""

from logs import init_logger

logger = init_logger(__name__)


def prepare_cookies_for_playwright(cookies: list[dict]) -> list[dict]:
    """
    Подготовка cookies для Playwright.
    Удаляет expiry, нормализует sameSite.

    :param cookies: список cookies из БД или файла
    :return: список cookies, готовых для add_cookies()
    """
    if not cookies:
        return []

    cookies_to_add = []
    for cookie in cookies:
        cookie_copy = cookie.copy()

        # Playwright не использует expiry
        cookie_copy.pop("expiry", None)

        # Нормализация sameSite
        if 'sameSite' in cookie_copy:
            ss = cookie_copy['sameSite']
            if ss and ss.lower() in ['strict', 'lax', 'none']:
                cookie_copy['sameSite'] = ss.capitalize()
            else:
                cookie_copy.pop('sameSite', None)

        cookies_to_add.append(cookie_copy)

    return cookies_to_add


async def add_cookies_to_context(context, cookies: list[dict]) -> tuple[int, int]:
    """
    Добавление cookies в контекст браузера.

    :param context: BrowserContext Playwright
    :param cookies: список cookies
    :return: (accepted, skipped) - количество добавленных и пропущенных
    """
    if not cookies:
        logger.warning("Нет cookies для установки")
        return 0, 0

    cookies_to_add = prepare_cookies_for_playwright(cookies)

    try:
        await context.add_cookies(cookies_to_add)
        logger.info("Загрузка cookies завершена: accepted=%d", len(cookies_to_add))
        return len(cookies_to_add), 0
    except (Exception,) as e:
        logger.warning(f"Ошибка добавления cookies: {e}")
        return 0, len(cookies_to_add)
