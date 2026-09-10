"""Статичные картинки-оверлеи: единый кэш и вставка с альфой.

До выделения в пакет эта логика жила в двенадцати программах в пяти разных формах —
модульные глобалы (`_globe_asset`/`_globe_resized`/`_globe_scaled`, последний в двух
несовместимых видах), `Image.open` на каждый вызов, `_load_asset(path, cache_name)` через
`globals()`, свой кэш у OTC-QR и своё чтение файла в `load_overlay`. Одна и та же правка
(кэш ресайза) была независимо сделана трижды разными способами.

Кэш живёт в процессе и по ключу (путь, размер): под None лежит исходный файл, под
конкретным размером — ресайзнутая копия.
"""
import logging

from PIL import Image

# Логгер и текст предупреждения программа подменяет через configure(): у семьи свой
# init_logger (доп. уровни, файлы по уровням, отправка в Telegram), а язык строк логов
# в английских форках местами английский.
_logger = logging.getLogger(__name__)
_warn_template = 'Не загружена картинка оверлея {path}: {error}'

_IMAGE_CACHE: dict = {}


def configure(logger=None, warn_template: str = None) -> None:
    """Подключить логгер программы и, при необходимости, свой текст предупреждения.

    Зовётся один раз на старте (обычно из settings/screenshot_set.py программы). Без вызова
    пакет пишет в стандартный logging.getLogger('binocore.images') — ничего не падает,
    просто запись не попадёт в файлы/Telegram программы."""
    global _logger, _warn_template
    if logger is not None:
        _logger = logger
    if warn_template is not None:
        _warn_template = warn_template


def clear_cache() -> None:
    """Сбросить кэш. Нужен тестам и утилитам, боевой код это не зовёт."""
    _IMAGE_CACHE.clear()


def load_rgba(path: str, size=None):
    """PNG → Image(RGBA) с кэшем в памяти. None — файла нет/не читается (вызывающий
    решает, критично это или кадр просто соберётся без оверлея).

    `size` — вернуть картинку под указанный размер. Ресайз кэшируется ВМЕСТЕ с файлом, по
    ключу (путь, размер): у глобуса размер файла (~1470x870) не совпадает с канвасом binodex
    (1452x870), и без кэша LANCZOS по RGBA такого размера гонялся бы НА КАЖДЫЙ кадр, хотя
    канвас за жизнь процесса не меняется. Размер совпал — отдаём исходник, ничего не считая.

    Конверсия в RGBA безопасна и для непрозрачных PNG (QR/водяные знаки лежат в RGB): альфа
    выставляется в 255, а вставка с полностью непрозрачной маской даёт побитово тот же кадр,
    что вставка без маски — проверено попиксельно."""
    size = tuple(size) if size else None
    key = (path, size)
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]
    # Файл читаем (и логируем сбой) один раз на путь, независимо от того, сколько размеров спросят.
    if (path, None) not in _IMAGE_CACHE:
        try:
            _IMAGE_CACHE[(path, None)] = Image.open(path).convert('RGBA')
        except (Exception,) as error:
            _logger.warning(_warn_template.format(path=path, error=error))
            _IMAGE_CACHE[(path, None)] = None
    img = _IMAGE_CACHE[(path, None)]
    if img is not None and size and img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    _IMAGE_CACHE[key] = img
    return img


def load_overlay(path: str):
    """Загружает оверлей → (rgba_image, mask|None). Общий хелпер для боевого пайплайна и
    dev-утилиты подбора координат (place_qr.py) — чтобы логика split-маски не расходилась.
    Файла нет → (None, None), вызывающий уже это переживает.

    Маска — альфа-канал, но ТОЛЬКО если он не сплошь непрозрачный: у PNG без альфы (QR лежат
    в RGB) load_rgba выставляет альфу в 255, а исторически здесь отдавался None. Разница
    косметическая — вставка с полностью непрозрачной маской побитово равна вставке без маски,
    проверено, — но контракт вызывающих держим прежним."""
    img = load_rgba(path)
    if img is None:
        return None, None
    alpha = img.getchannel('A')
    return img, (alpha if alpha.getextrema() != (255, 255) else None)


def paste_overlay(img, overlay, x, y) -> None:
    """Вставить оверлей (водяной знак/QR) на изображение с учётом альфа-канала (RGBA/LA → mask)."""
    img.paste(overlay, (x, y), mask=overlay if overlay.mode in ('RGBA', 'LA') else None)
