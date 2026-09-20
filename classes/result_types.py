"""
Типизированные результаты функций вместо tuple[bool, ...]
Улучшает читаемость: result.success вместо result[0]
"""

from typing import NamedTuple, Union
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from binocore.browser import BrowserSession


class BrowserInitResult(NamedTuple):
    """Результат инициализации браузера"""
    success: bool
    session_or_error: Union['BrowserSession', str]

    @property
    def session(self) -> 'BrowserSession':
        """Получить session (только если success=True)"""
        if not self.success:
            raise ValueError(f"Cannot get session: {self.session_or_error}")
        return self.session_or_error


class OperationResult(NamedTuple):
    """Общий результат операции"""
    success: bool
    error: str = ''


class MainResult(NamedTuple):
    """Результат одного прохода main() для главного цикла (вместо «магического» 5-tuple).
    result — опцион завершён; plus — закончился ПЛЮСОМ (именно итог опциона, не «цикл без
    сбоя»: по этому флагу FIN решает, закрывать ли неделю); fall — нужен перезапуск процесса;
    bug_text — текст ошибки; check_cookies — счётчик одинаковых цен подряд (эвристика отвала)."""
    result: bool
    plus: bool
    fall: bool
    bug_text: str = ''
    check_cookies: int = 0