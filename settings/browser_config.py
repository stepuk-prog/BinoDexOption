from apps.setting_app import find_par
from settings.config import database, timeframe

vib_all_kat = database.tv_setting()

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
# установка таймфрейма в 1 минуту для страницы с ценой
tf_link_price = find_par(data=vib_all_kat, par=f'tf_link_1')
# установка таймфрейма
if timeframe in ['1m', '3m', '5m', '10m']:
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_2')
elif timeframe == '15m':
    tf_link = find_par(data=vib_all_kat, par=f'tf_link_3')

#---------- Настройки для OTC-------------------------------------------------------------------------------------------
otc_setting = database.otc_setting()
# открытие списка валют OTC
otc_val_list_open = find_par(data=otc_setting, par='list_open')
# Выбор места на сайте для отключения списка валют
otc_val_list_close = find_par(data=otc_setting, par='list_close_header')
# Класс окна поиска валют (input)
input_otc = find_par(data=otc_setting, par='input_otc')
# tooltip с ценой
otcprice = find_par(data=otc_setting, par='price_css')
# Зона для скриншота
screen_zone_otc = find_par(data=otc_setting, par='screen_zone_class')
# страница в Pocket для торговли OTC
otc_screen = 'https://pocketoption.com/ru/cabinet/quick-high-low/'
# Текущая выбранная валюта
tek_val = find_par(data=otc_setting, par='tek_val_css')
# Кнопка выбора таймфрейма
timeframe_otc = find_par(data=otc_setting, par='timeframe')
# Элемент H4 в списке таймфремов
change_tf = find_par(data=otc_setting, par='change_tf')
# Включение окна выбора масштаба свечи
chart_type = find_par(data=otc_setting, par='chart_type')
# Выбор масштаба свечи
s30 = find_par(data=otc_setting, par='s30_css')
# Список валютных пар
list_valute_css = find_par(data=otc_setting, par='list_valute_css')
# Имя валютной пары в списке
name_valute_list_css = find_par(data=otc_setting, par='name_valute_list_css')
# Процент по выплате по валютной паре
percent_value = find_par(data=otc_setting, par='percent_value')
# Кнопка Google - проверка на загрузку Cookies
check_google = find_par(data=otc_setting, par='check_google')
