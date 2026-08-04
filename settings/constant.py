import os

qr110_path = f'{os.getcwd()}/pictures/qr-code_110.png'
qr85_path = f'{os.getcwd()}/pictures/qr-code_85.png'
# OTC использует собственный QR (один на скрине) — остальное (позиция otc_qr_x/y и т.д.) без изменений
otc_qr110_path = f'{os.getcwd()}/pictures/otc_qr-code_110.png'
# Подложка кадра OTC. Её рисует БРАУЗЕР: otc_app.apply_bg_layer заливает этим цветом свой слой
# #bino_bg по боксу канваса, кадр снимается element.screenshot (штатный фон binodex .wrap_bg
# выключен за аккаунтом — docs/BINODEX_CPU.md). Сплошной цвет, а не картинка: у binodex свой фон
# тёмно-синий, а постам нужен чистый чёрный. Вернуть картинку — см. docs/CHANGELOG.md 2026-08-04.
bg_otc_color = '#000000'
bear_color = '225'  # цвет медвежьей свечи
bull_color = '219'  # цвет бычьей свечи.
find_time = 2  # максимальное время поиска точки входа в минутах

# Коды выхода процесса — их читает диспетчер (WD/systemd) и принимает решения. ВАЖНО (инвариант):
# programdata.status=false (write_status_offline) выставляет ТОЛЬКО плановый weekend-выход binary;
# сбойные коды ниже status НЕ трогают — диспетчер сам решает по коду.
#   0  — штатная остановка извне (SIGTERM/SIGINT): диспетчер ничего не делает;
#   1  — непредвиденный краш: рестарт;
#   10 — браузер не поднялся BROWSER_MAX_ATTEMPTS раз подряд → failover на другую ноду;
#   11 — куки протухли и авто-релогин (RECOVER_ATTEMPTS) не помог → рефреш куков / рестарт;
#   12 — сайт binodex не настроился при живых куках (вероятно сменились селекторы) → нужен человек;
#   13 — отвал session Pyrogram-юзербота → реавторизация (scripts/reauth_userbot.py).
EXIT_BROWSER = 10
EXIT_COOKIES = 11
EXIT_SETUP = 12
EXIT_USERBOT = 13
BROWSER_MAX_ATTEMPTS = 3    # подъёмов браузера подряд; больше биться смысла нет → exit(EXIT_BROWSER)

# Таблицы таймфреймов (перенесены из Data_set.py)
spr_timeframe = [{'timeframe': '1m', 'search_tf': '60', 'name_tf': '1 минута'},
                 {'timeframe': '3m', 'search_tf': '300', 'name_tf': '3 минуты'},
                 {'timeframe': '5m', 'search_tf': '300', 'name_tf': '5 минут'},
                 {'timeframe': '10m', 'search_tf': '900', 'name_tf': '10 минут'},
                 {'timeframe': '15m', 'search_tf': '900', 'name_tf': '15 минут'},
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

