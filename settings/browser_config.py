from apps.setting_app import find_par
from settings.config import timeframe
from settings._bootstrap import bootstrap_fetch

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
# включение выбора валюты
symbol = find_par(data=vib_all_kat, par='symbol')
# вторичный чип биржи (FXCM-scope) в диалоге поиска — снимаем перед вводом, иначе
# TV отсеивает поиск по формату EXCHANGE:SYMBOL
scope_chip = find_par(data=vib_all_kat, par='scope_chip')
# установка таймфрейма в 1 минуту для страницы с ценой
tf_link_price = find_par(data=vib_all_kat, par=f'tf_link_1')
# установка таймфрейма (только FIN/TV; OTC tf_link не использует)
if timeframe == '3m':
    # 3m-вариант: график настроен на 1 минуту (tf_link_1), время опциона рандомится в коде
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_1')
elif timeframe in ['1m', '5m', '10m']:
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_2')
elif timeframe == '15m':
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_3')
else:
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_2')  # дефолт — чтобы не словить NameError на импорте

#---------- Настройки для OTC (binodex) --------------------------------------------------------------------------------
# Селекторы сайта binodex.app из binodex.settings.binodex_settings (подобраны scripts/binodex_selectors.py).
# Страница OTC берётся из binodex.cookies.pages (bino_option/otc), не хардкодим.
otc_setting = bootstrap_fetch('binodex', "SELECT * FROM settings.binodex_settings")
# Открытие/закрытие окна выбора актива (одна кнопка-переключатель)
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
