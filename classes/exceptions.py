"""Доменные исключения, общие для browser_app/otc_app (нейтральный модуль без импортов —
чтобы оба могли импортировать без кольца импортов)."""


class CookiesExpired(Exception):
    """Сервис редиректнул на страницу логина (TV → /signin, binodex → ушёл с /trade):
    куки / Privy storage_state протухли. Ловится в main.py::_init_with_retry →
    cookies-backoff (сообщение + пауза + пересоздание браузера, куки перечитываются из
    БД). Программа НЕ выходит (политика Survive). См. docs/lifecycle-standard.md §4.3."""
    pass


class FeedOutage(Exception):
    """OTC: сайт на /trade и сессия жива (privy:token есть, формы логина нет), но market-WS
    не отдаёт котировки — подтверждено браузер-фри пробой (apps/binodex_feed.feed_alive=False).
    Это аутэйдж binodex (рынок закрыт / сбой на стороне сайта), НЕ отвал кук. Ловится в
    main.py::_init_with_retry → выгрузка браузера + браузер-фри ожидание возврата фида, БЕЗ
    рефреша кук и БЕЗ выхода. См. docs/lifecycle-standard.md §4.5."""
    pass


class SetupError(Exception):
    """OTC: авторизованы (privy:token есть, формы логина нет) И фид ЖИВ (feed_alive=True), но
    торговый UI не прогрузился/не настроился. Причина НЕ в куках и НЕ в аутэйдже фида. Ловится в
    main.py::_init_with_retry; политика зависит от `mounted` (§4.5):

    :param mounted: True — торговый апп-шелл binodex смонтирован (кнопка выбора пары есть), но наш
        селектор не найден → сменились CSS-селекторы binodex: до SETUP_ATTEMPTS повторов → не
        помогло → ПЛАНОВЫЙ ВЫХОД (нужна ручная правка селекторов). False — фронт binodex не поднялся
        при живых /trade+WS+токене: либо завис на сплеше «лого+спиннер» (JS-бандл не смонтировался),
        либо упал в error-boundary «Something went wrong» (ленивый чанк не загрузился, напр.
        отравленный CDN-кэш). И то, и другое — front-end АУТЭЙДЖ binodex (релогин НЕ чинит):
        ВЫЖИВАЕМ с бэкоффом, без выхода и без счётчика (как FeedOutage, но фид тут жив — retry)."""
    def __init__(self, message: str = '', *, mounted: bool = True):
        super().__init__(message)
        self.mounted = mounted
