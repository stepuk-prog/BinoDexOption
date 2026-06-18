from apps.setting_app import find_par
from settings.config import timeframe, binary
from settings._bootstrap import bootstrap_fetch

# TV/FIN-селекторы нужны только в FIN-режиме (BINARY=1). Для OTC (BINARY=0) не дёргаем
# settings.tv_settings (лишний синхронный запрос к БД на старте каждого OTC-инстанса) — имена
# определяем как None, чтобы импорт FIN-модулей не падал (их код в OTC-режиме не исполняется).
if binary:
    vib_all_kat = bootstrap_fetch('program', "SELECT * FROM settings.tv_settings")

    vib_kat = find_par(data=vib_all_kat, par='vib_kat')
    # Закрытие окна выбора котировок
    close_tool_win = find_par(data=vib_all_kat, par='close_tool_win')
    fxcm = find_par(data=vib_all_kat, par='fxcm')
    # поиск поля ввода котировок
    search_kat = find_par(data=vib_all_kat, par='search_kat')
    # Включение котировки
    find_kat = find_par(data=vib_all_kat, par='find_kat')
    # поиск поля ввода валют
    search_val = find_par(data=vib_all_kat, par='search_val')
    # включение выбранной валюты
    find_val = find_par(data=vib_all_kat, par='find_val')
    # Меню выбора таймфрейма
    tf_menu = find_par(data=vib_all_kat, par='tf_menu')
    # Поле для получения цены
    price_field = find_par(data=vib_all_kat, par='price_field')
    # поле для перемещения при имитации движения мыши
    move_field = find_par(data=vib_all_kat, par='move_field')
    # всплывающие окна
    pop_up = find_par(data=vib_all_kat, par='pop-up')
    pop_up2 = find_par(data=vib_all_kat, par='pop_up2')
    pop_up3 = find_par(data=vib_all_kat, par='pop_up3')
    # Зона скриншота
    screen_zone = find_par(data=vib_all_kat, par='screen_zone')
    # сворачивание правой widget-панели TV (в дефолте лэйаута раскрыта, сужает скрин;
    # состояние в лэйаут не персистится → сворачиваем в коде). panel_toggle — кнопка-тоггл,
    # panel_wrap — контейнер для проверки «реально ли открыта» (иначе тоггл её откроет).
    panel_toggle = find_par(data=vib_all_kat, par='panel_toggle')
    panel_wrap = find_par(data=vib_all_kat, par='panel_wrap')
    # включение выбора валюты
    symbol = find_par(data=vib_all_kat, par='symbol')
    # вторичный чип биржи (FXCM-scope) в диалоге поиска — снимаем перед вводом, иначе
    # TV отсеивает поиск по формату EXCHANGE:SYMBOL
    scope_chip = find_par(data=vib_all_kat, par='scope_chip')
    # установка таймфрейма в 1 минуту для страницы с ценой
    tf_link_price = find_par(data=vib_all_kat, par='tf_link_1')
    # установка таймфрейма графика (только FIN/TV; OTC tf_link не использует). Чарт показываем по
    # find_timeframe (гранулярности данных): 1m/3m → 1 минута (tf_link_1), 5m/10m → 5 минут (tf_link_2),
    # 15m → 15 минут (tf_link_3). Для 3m график 1 мин, а реальное время опциона рандомится в коде (2/3).
    if timeframe in ('1m', '3m'):
        tf_link = find_par(data=vib_all_kat, par='tf_link_1')   # 1 минута
    elif timeframe in ('5m', '10m'):
        tf_link = find_par(data=vib_all_kat, par='tf_link_2')   # 5 минут
    elif timeframe == '15m':
        tf_link = find_par(data=vib_all_kat, par='tf_link_3')   # 15 минут
    else:
        tf_link = find_par(data=vib_all_kat, par='tf_link_2')   # дефолт — чтобы не словить NameError на импорте
else:
    # OTC: FIN/TV-селекторы не используются — заглушки, чтобы импорт не падал.
    vib_all_kat = None
    vib_kat = close_tool_win = fxcm = search_kat = find_kat = search_val = find_val = tf_menu = \
        price_field = move_field = pop_up = pop_up2 = pop_up3 = screen_zone = symbol = scope_chip = \
        tf_link_price = tf_link = panel_toggle = panel_wrap = None

#---------- Настройки для OTC (binodex) --------------------------------------------------------------------------------
# Селекторы сайта binodex.app из binodex.settings.binodex_settings (подобраны scripts/binodex_selectors.py).
# Страница OTC берётся из binodex.cookies.pages (bino_option/otc), не хардкодим.
otc_setting = bootstrap_fetch('binodex', "SELECT * FROM settings.binodex_settings")
# Открытие/закрытие окна выбора актива (одна кнопка-переключатель)
# URL/Origin binodex — единый источник (binodex_settings.trade_url/landing_url/ws_origin),
# меняется в одном месте. next()+дефолт, а НЕ find_par (тот sys.exit при отсутствии) — чтобы
# старая БД без этих строк не валила старт.
otc_trade_url = next((i['par_value'] for i in otc_setting if i['par_name'] == 'trade_url'), 'https://app.binodex.app/trade')
otc_landing_url = next((i['par_value'] for i in otc_setting if i['par_name'] == 'landing_url'), 'https://app.binodex.app/')
otc_ws_origin = next((i['par_value'] for i in otc_setting if i['par_name'] == 'ws_origin'), 'https://app.binodex.app')

otc_select_pair = find_par(data=otc_setting, par='select_pair_add')
# Кнопка категории «Валюты» в модалке выбора
otc_category_valute = find_par(data=otc_setting, par='category_valute')
# Поле ввода имени пары (div-обёртка; реальный input внутри)
otc_input_pair = find_par(data=otc_setting, par='input_pair')
# Элемент пары в списке модалки (текст: '<pair> OTC <payout>%')
otc_modal_pair_item = find_par(data=otc_setting, par='modal_pair_item')
# Текущая выбранная пара в сайдбаре (проверка на 'OTC')
otc_tek_val = find_par(data=otc_setting, par='tek_val')
# Зона графика для скриншота (canvas)
screen_zone_otc = find_par(data=otc_setting, par='screen_zone')
# Кнопка настроек аккаунта (тулбар) — есть ТОЛЬКО при полностью прогруженном UI; на сплеше
# её нет (хотя кнопка выбора пары присутствует). Маркер «не сплеш» для readiness-gate init_otc.
otc_settings_btn = find_par(data=otc_setting, par='setup_settings_open')
# Поле ввода почты формы логина Privy. При отвале кук binodex НЕ редиректит со /trade, а
# всплывает форма логина ПРЯМО на графике → видимость login_email = позитивный признак отвала
# кук (отличает его от транзиентного сплеша, где формы нет). next()+None: старая БД без строки
# не валит старт (детект тогда деградирует к token/UI-проверкам, без ложного рефреша).
otc_login_email = next((i['par_value'] for i in otc_setting if i['par_name'] == 'login_email'), None)
# Масштабы графика. binodex сбрасывает их на дефолт (свеча/график) при КАЖДОМ запуске браузера
# (новый контекст из storage_state → дефолт; reload в рамках сессии значение держит). Поэтому
# выставляются в init_otc на каждом старте, а не только в binodex_session._setup на релогине.
# Пункты выбираются ПО ТЕКСТУ (порядок списков на binodex плавает): свеча '30S', график 'H1'.
otc_candle_scale = find_par(data=otc_setting, par='setup_candle_scale')
otc_candle_scale_item = find_par(data=otc_setting, par='setup_candle_scale_item')
otc_chart_scale = find_par(data=otc_setting, par='setup_chart_scale')
otc_chart_scale_item = find_par(data=otc_setting, par='setup_chart_scale_item')
