"""
Типизированные результаты функций вместо tuple[bool, ...]
Улучшает читаемость: result.success вместо result[0]
"""

from typing import NamedTuple, Union
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.browser_app import BrowserManager


class BrowserInitResult(NamedTuple):
    """Результат инициализации браузера"""
    success: bool
    manager_or_error: Union['BrowserManager', str]

    @property
    def manager(self) -> 'BrowserManager':
        """Получить manager (только если success=True)"""
        if not self.success:
            raise ValueError(f"Cannot get manager: {self.manager_or_error}")
        return self.manager_or_error

    @property
    def error(self) -> str:
        """Получить сообщение об ошибке (только если success=False)"""
        if self.success:
            raise ValueError("No error: operation was successful")
        return self.manager_or_error


class ScreenshotResult(NamedTuple):
    """Результат снятия скриншота"""
    success: bool
    price_or_error: float | str

    @property
    def price(self) -> float:
        """Получить цену (только если success=True)"""
        if not self.success:
            raise ValueError(f"Cannot get price: {self.price_or_error}")
        return self.price_or_error

    @property
    def error(self) -> str:
        """Получить сообщение об ошибке (только если success=False)"""
        if self.success:
            raise ValueError("No error: operation was successful")
        return self.price_or_error


class OtcScreenshotResult(NamedTuple):
    """Результат снятия скриншота OTC"""
    success: bool
    price_or_error: float | str
    path: str = ''

    @property
    def price(self) -> float:
        if not self.success:
            raise ValueError(f"Cannot get price: {self.price_or_error}")
        return self.price_or_error

    @property
    def error(self) -> str:
        if self.success:
            raise ValueError("No error: operation was successful")
        return self.price_or_error


class OperationResult(NamedTuple):
    """Общий результат операции"""
    success: bool
    error: str = ''


class ExitMainResult(NamedTuple):
    """Результат выхода из main"""
    result: bool
    plus: bool
    fall: bool
    bug_text: str
    check_cookies: int


class MessageResult(NamedTuple):
    """Результат отправки сообщения"""
    success: bool
    error: str = ''


class PriceResult(NamedTuple):
    """Результат получения цены"""
    success: bool
    price_or_error: float | str

    @property
    def price(self) -> float:
        if not self.success:
            raise ValueError(f"Cannot get price: {self.price_or_error}")
        return self.price_or_error

    @property
    def error(self) -> str:
        if self.success:
            raise ValueError("No error: operation was successful")
        return self.price_or_error
