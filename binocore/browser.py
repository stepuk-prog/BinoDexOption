"""Сессия Playwright: владелец ресурсов (pw/browser/context/вкладки) и их корректное закрытие.

ЗАЧЕМ В ЯДРЕ. Класс один и тот же у всей семьи, а редакций к 20-09-2026 накопилось ВОСЕМЬ:
голый `BrowserSession` у Crypto (без ролей), он же с `main_page()` у Stock/Stoch, он же с
`pages_by_role` у форумных и Gold/AITrade, `page` вместо списка вкладок у Screens, dataclass
вообще без `close()` у квизов, и отдельный `BrowserManager` со словарём `pages[name]` у пары
BinoOptions. Различались не задачи, а слова: одно и то же закрытие было переписано восемь раз,
и правка жизненного цикла (таймаут на шаг, гашение релея, чистка ролей) доезжала до программ
годами — ровно та же болезнь, от которой в ядро уехали `shutdown` и `db`.

ЧТО ЗАКРЫВАТЬ, А ЧТО НЕТ. Вкладки отдельно НЕ закрываются: `context.close()` закрывает их сам,
а перебор страниц до контекста (редакция BinoManager) добавлял шагов ровно там, где счёт идёт
на секунды бюджета остановки. Каждый шаг идёт под своим `asyncio.wait_for`: зависший
`context.close()` не должен съесть весь бюджет и оставить `browser.close()` невыполненным —
иначе на ноде остаётся осиротевший Firefox, и следующий запуск упирается в его lock.

Ошибки закрытия НЕ поднимаются: close() — это уборка, и падать в ней означает пропустить
остальные шаги. Но они и не проглатываются молча (так было у шести редакций из восьми):
«объект уже закрыт» — debug (штатно при Ctrl+C, когда Firefox получил тот же сигнал), потеря
связи с драйвером — debug (штатно при крахе/остановке), всё прочее — warning.

`add_cleanup` — для того, что живёт РЯДОМ с браузером, но ядру не принадлежит: локальный
прокси-релей OTC-фолбэка (его daemon-поток иначе переживает браузер, который обслуживал).
Ядро не знает про `classes.local_proxy`, поэтому программа вешает свой хук явно. Синхронный
хук уходит в поток: `stop()` релея join'ит до 2с, а держать на этом event loop во время
остановки нельзя.
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:                     # playwright не импортируется в рантайме: ядро вендорится
    from playwright.async_api import Browser, BrowserContext, Page   # и в неброузерные программы

_logger = logging.getLogger(__name__)

# Таймаут на КАЖДЫЙ шаг закрытия. Зависший шаг не должен довести процесс до SIGKILL от systemd.
CLOSE_STEP_TIMEOUT = 15      # сек
CLEANUP_TIMEOUT = 5          # сек на один хук уборки


def configure(logger=None) -> None:
    """Логгер программы вместо `logging.getLogger('binocore.browser')`.

    Тот же приём, что у `binocore.db`: `init_logger` семьи вешает хендлеры на ИМЕНОВАННЫЙ
    логгер с `propagate=False`, поэтому записи ядра без этого вызова не дойдут ни до файлов
    уровней, ни до темы ошибок."""
    global _logger
    if logger is not None:
        _logger = logger


def _clean_err(error) -> str:
    """Сообщение Playwright без приклеенного спама консоли Firefox ('Browser logs:' и дальше)."""
    return str(error).split('Browser logs:')[0].strip()


def _expected(error) -> bool:
    """Ошибка закрытия, которая НЕ является сбоем: объект уже закрыт или драйвер уже мёртв.
    Первое штатно при Ctrl+C (Firefox получил тот же SIGINT), второе — при остановке и крахе."""
    text = str(error).lower()
    return any(mark in text for mark in
               ('has been closed', 'target closed', 'already closed',
                'connection closed', 'connection lost'))


@dataclass
class BrowserSession:
    """Ресурсы одной сессии Playwright. Создаётся init-логикой программы, закрывается явно.

    `expected_pages` — вкладки, которые создала init-логика, В ПОРЯДКЕ создания. Именно по ним
    отличаются «свои» вкладки от попапов рекламы (`close_unexpected_pages`).
    `pages_by_role` — карта 'main'/'price'/… → Page (у форумных заполняется по
    `cookies.pages.description`). Заменяет хардкодные индексы и словарь имён `BrowserManager`."""
    pw: Any = None
    browser: 'Browser | None' = None
    context: 'BrowserContext | None' = None
    expected_pages: list = field(default_factory=list)
    pages_by_role: dict = field(default_factory=dict)
    _cleanups: list = field(default_factory=list, repr=False)

    @property
    def pages(self) -> list:
        """Живые вкладки в порядке создания. Закрытые (popup-blocker, краш рендерера) отсеяны:
        обращение к закрытой Page бросает, а вызывающие ждут «есть вкладка / нет вкладки»."""
        return [p for p in self.expected_pages if not p.is_closed()]

    def main_page(self) -> 'Page':
        """Основная (первая) вкладка. RuntimeError, а не IndexError: отсутствие вкладки —
        это сигнал «пересоздать браузер», и вызывающие ловят именно его."""
        pages = self.pages
        if pages:
            return pages[0]
        raise RuntimeError('Нет открытой вкладки в сессии браузера (init её не создал)')

    def page(self, role: str) -> 'Page':
        """Вкладка по роли. KeyError с внятным текстом вместо `pages_by_role['main']`:
        промах по роли означает, что init не довёл страницу до готовности."""
        found = self.pages_by_role.get(role)
        if found is None or found.is_closed():
            known = ', '.join(sorted(self.pages_by_role)) or 'ни одной'
            raise KeyError(f'Нет живой вкладки с ролью {role!r} (известные роли: {known})')
        return found

    def add_cleanup(self, action: Callable) -> None:
        """Хук, который выполнится ПОСЛЕ закрытия браузера (см. про релей в докстринге модуля).
        Принимает и обычную функцию (уйдёт в поток), и корутинную."""
        self._cleanups.append(action)

    async def close(self) -> None:
        """Закрытие в порядке context → browser → pw, каждый шаг под своим таймаутом, затем
        хуки уборки. Ошибки не поднимаются: пропущенный шаг дороже любого из них."""
        steps = (('контекст', getattr(self.context, 'close', None)),
                 ('браузер', getattr(self.browser, 'close', None)),
                 ('playwright', getattr(self.pw, 'stop', None)))
        for name, closer in steps:
            if closer is None:                    # шага нет (сессия не доросла до него)
                continue
            await self._guarded(name, closer(), CLOSE_STEP_TIMEOUT)
        for action in self._cleanups:
            # Синхронный хук — В ПОТОК: `stop()` релея join'ит до 2с, и держать на этом
            # event loop во время остановки нельзя.
            running = action() if inspect.iscoroutinefunction(action) else asyncio.to_thread(action)
            await self._guarded('уборка', running, CLEANUP_TIMEOUT)
        # Ссылки на закрытые Page не должны пережить close(): задержавшийся объект сессии
        # иначе отдаёт мёртвую страницу по роли.
        self.pages_by_role.clear()

    async def close_unexpected_pages(self) -> None:
        """Закрывает вкладки, которых init не создавал (попапы рекламы binodex/TV)."""
        if self.context is None:
            return
        expected = set(self.expected_pages)
        for page in list(self.context.pages):
            if page not in expected and not page.is_closed():
                await self._guarded('лишняя вкладка', page.close(), CLOSE_STEP_TIMEOUT)

    @staticmethod
    async def _guarded(name: str, awaitable, timeout: float) -> None:
        """Один шаг уборки: под таймаутом и без права уронить остальные."""
        try:
            await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError:
            _logger.warning(f'Закрытие ({name}): не уложилось в {timeout:.0f}с')
        except (Exception,) as error:
            level = _logger.debug if _expected(error) else _logger.warning
            level(f'Закрытие ({name}): {_clean_err(error)}')
