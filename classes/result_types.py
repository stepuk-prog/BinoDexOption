"""
Типизированные результаты функций вместо tuple[bool, ...]
Улучшает читаемость: result.success вместо result[0]
"""

from typing import NamedTuple, Union
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager


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


class OperationResult(NamedTuple):
    """Общий результат операции"""
    success: bool
    error: str = ''


class MainResult(NamedTuple):
    """Результат одного прохода main() для главного цикла (вместо «магического» 5-tuple).
    result — опцион завершён; plus — закончился плюсом; fall — нужен перезапуск процесса;
    bug_text — текст ошибки; check_cookies — счётчик одинаковых цен подряд (эвристика отвала)."""
    result: bool
    plus: bool
    fall: bool
    bug_text: str = ''
    check_cookies: int = 0