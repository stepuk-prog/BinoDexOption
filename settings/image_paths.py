"""
Константы путей к изображениям.
"""

# Сообщения о волатильности и ошибках
VOLATILITY_IMAGE = 'pictures/volat.jpg'
UNACTIVE_IMAGE = 'pictures/unactive.jpg'

# Прогнозы — рандомный выбор из 3 картинок на каждый новый прогноз.
NEW_FORECAST_IMAGES = [
    'pictures/new_prognoz/new_prognoz_1.png',
    'pictures/new_prognoz/new_prognoz_2.png',
    'pictures/new_prognoz/new_prognoz_3.png',
]

# Картинка для follow-up «оставь отзыв» (dop_plus_message)
PLUS_SERIES_IMAGE = 'pictures/new_seria.png'

# Догоны — выбираем рандомно без повторов внутри одного прогноза.
# В каждом цикле main() из этого списка берётся перетасованная копия,
# из неё по индексу догона достаётся картинка. 3 картинки = 3 догона = без повторов.
DOGON_IMAGES = [
    'pictures/dogon/dogon_1.png',
    'pictures/dogon/dogon_2.png',
    'pictures/dogon/dogon_3.png',
]
