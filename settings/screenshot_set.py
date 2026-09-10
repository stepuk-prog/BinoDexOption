# Настройки для TradingView
# размеры окна для скрина (viewport в Playwright)
win_x = 1480
win_y = 1015  # было 935, добавлено ~80px для компенсации разницы Selenium vs Playwright
# координаты QR-оверлеев на FIN-скрине
qr110_x = 0     # QR110 по горизонтали
qr110_y = 640   # QR110 по вертикали
qr85_x = 1348   # QR85 по горизонтали
qr85_y = 850    # QR85 по вертикали
# Настройки для Pocket
# размеры окна для скрина (viewport в Playwright)
win_x_otc = 1712
win_y_otc = 990  # было 910, добавлено ~80px для компенсации
# координата QR на OTC-скрине (один QR — qr-code_110); подобрано под binodex-канвас 1452×870
otc_qr_x = 1360
otc_qr_y = 757


def paste_overlay(img, overlay, x, y):
    """Вставить оверлей (QR) на изображение с учётом альфа-канала (RGBA/LA → mask)."""
    img.paste(overlay, (x, y), mask=overlay if overlay.mode in ('RGBA', 'LA') else None)

# Кэш статичных картинок оверлеев (water/QR/глобус): файл читается один раз на процесс.
# Единая точка вместо россыпи ленивых загрузок по модулям. Ключ — (путь, размер): под None
# лежит исходный файл, под конкретным размером — ресайзнутая копия, см. load_rgba.
_IMAGE_CACHE: dict = {}


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
    from PIL import Image  # лениво: settings/* импортируется до тяжёлых зависимостей
    from logs import init_logger
    size = tuple(size) if size else None
    key = (path, size)
    if key in _IMAGE_CACHE:
        return _IMAGE_CACHE[key]
    # Файл читаем (и логируем сбой) один раз на путь, независимо от того, сколько размеров спросят.
    if (path, None) not in _IMAGE_CACHE:
        try:
            _IMAGE_CACHE[(path, None)] = Image.open(path).convert('RGBA')
        except (Exception,) as error:
            init_logger(__name__).warning(f'Не загружена картинка оверлея {path}: {error}')
            _IMAGE_CACHE[(path, None)] = None
    img = _IMAGE_CACHE[(path, None)]
    if img is not None and size and img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    _IMAGE_CACHE[key] = img
    return img
