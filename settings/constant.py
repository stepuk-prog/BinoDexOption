import os

qr110_path = f'{os.getcwd()}/pictures/qr-code_110.png'
qr85_path = f'{os.getcwd()}/pictures/qr-code_85.png'
# OTC использует собственный QR (один на скрине) — остальное (позиция otc_qr_x/y и т.д.) без изменений
otc_qr110_path = f'{os.getcwd()}/pictures/otc_qr-code_110.png'
bear_color = '225'  # цвет медвежьей свечи
bull_color = '219'  # цвет бычьей свечи.
find_time = 2  # максимальное время поиска точки входа в минутах

# Таблицы таймфреймов (перенесены из Data_set.py)
spr_timeframe = [{'timeframe': '1m', 'search_tf': '60', 'name_tf': '1 минута', 'coefficient': False},
                 {'timeframe': '3m', 'search_tf': '300', 'name_tf': '3 минуты', 'coefficient': True},
                 {'timeframe': '5m', 'search_tf': '300', 'name_tf': '5 минут', 'coefficient': False},
                 {'timeframe': '10m', 'search_tf': '900', 'name_tf': '10 минут', 'coefficient': True},
                 {'timeframe': '15m', 'search_tf': '900', 'name_tf': '15 минут', 'coefficient': False},
                 ]

# Варианты времени экспирации (мин) для рандомизации (FIN и OTC одинаково): график
# настроен на один таймфрейм, а реальное время опциона выбирается случайно из набора
# (анти-однообразие). 3m — экспирация рандомно 2/3 мин; 5m — экспирация 4/5 мин.
# Таймфреймы без записи рандома не имеют (берётся номинал из spr_timeframe).
option_time_variants = [
    {'timeframe': '3m', 'variants': [2, 3]},
    {'timeframe': '5m', 'variants': [4, 5]},
]

# Маппинг рабочего tf → tf для поиска данных опциона
find_timeframe = [
    {'timeframe': '1m', 'find': '1m'},
    {'timeframe': '3m', 'find': '1m'},
    {'timeframe': '5m', 'find': '5m'},
    {'timeframe': '10m', 'find': '5m'},
    {'timeframe': '15m', 'find': '15m'}]

