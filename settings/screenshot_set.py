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
