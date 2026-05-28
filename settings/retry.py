"""
Декоратор для повторных попыток выполнения async функций.
"""

import asyncio
from functools import wraps
from typing import Callable, Type, Tuple

from logs import init_logger

logger = init_logger(__name__)


def retry_async(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 1.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[int, Exception], None] | None = None
):
    """
    Декоратор для повторных попыток выполнения async функции.

    :param max_attempts: максимальное количество попыток
    :param delay: начальная задержка между попытками (секунды)
    :param backoff: множитель задержки (1.0 = постоянная, 2.0 = экспоненциальная)
    :param exceptions: tuple исключений, которые вызывают повтор
    :param on_retry: callback функция при повторе (attempt, exception)

    Пример использования:
        @retry_async(max_attempts=3, delay=2, backoff=2.0)
        async def fetch_data():
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_attempts:
                        logger.error(
                            f"{func.__name__}: все {max_attempts} попыток исчерпаны. "
                            f"Последняя ошибка: {e}"
                        )
                        raise

                    wait_time = delay * (backoff ** (attempt - 1))
                    logger.warning(
                        f"{func.__name__}: попытка {attempt}/{max_attempts} не удалась. "
                        f"Повтор через {wait_time:.1f}с. Ошибка: {e}"
                    )

                    if on_retry:
                        on_retry(attempt, e)

                    await asyncio.sleep(wait_time)

            # Этот код не должен достигаться, но на всякий случай
            raise last_exception if last_exception else RuntimeError("Unreachable")

        return wrapper
    return decorator


def retry_on_timeout(max_attempts: int = 3, delay: float = 2.0):
    """
    Специализированный декоратор для повтора при таймаутах Playwright.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    return retry_async(
        max_attempts=max_attempts,
        delay=delay,
        exceptions=(PlaywrightTimeout, asyncio.TimeoutError)
    )


def retry_on_network_error(max_attempts: int = 3, delay: float = 5.0, backoff: float = 2.0):
    """
    Специализированный декоратор для повтора при сетевых ошибках.
    Использует экспоненциальную задержку.
    """
    return retry_async(
        max_attempts=max_attempts,
        delay=delay,
        backoff=backoff,
        exceptions=(ConnectionError, TimeoutError, OSError)
    )
