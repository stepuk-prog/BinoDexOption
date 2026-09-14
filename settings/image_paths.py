"""
Константы путей к изображениям.
"""
from settings.constant import pic   # пути — от корня проекта (paths-from-root)

# Прогнозы — рандомный выбор из 3 картинок на каждый новый прогноз.
NEW_FORECAST_IMAGES = [
    pic('new_prognoz', 'new_prognoz_1.png'),
    pic('new_prognoz', 'new_prognoz_2.png'),
    pic('new_prognoz', 'new_prognoz_3.png'),
]

# Картинка для follow-up «оставь отзыв» (dop_plus_message → dop_plus10_message)
PLUS_SERIES_IMAGE = pic('seria_plus.png')

# Партнёрское сообщение (send_photo ботом-модератором в тему форума ПОСЛЕ вехи-форварда,
# apps/forum_forward.py). Бренд Smoke FX, из ForumTrade.
PARTNER_IMAGE = pic('partner_1.png')

# Серии плюсов: картинка по числу плюсов подряд — pictures/pluses/{N}.png (5, 10, … 50)
PLUS_IMAGE_DIR = pic('pluses')

# Догоны — выбираем рандомно без повторов внутри одного прогноза.
# В каждом цикле main() из этого списка берётся перетасованная копия,
# из неё по индексу догона достаётся картинка. 3 картинки = 3 догона = без повторов.
DOGON_IMAGES = [
    pic('dogon', 'dogon_1.png'),
    pic('dogon', 'dogon_2.png'),
    pic('dogon', 'dogon_3.png'),
]
