import random
from dataclasses import dataclass, field
from typing import List

from settings.Data_set import spr_timeframe, find_timeframe
from settings.browser_constant import link1, link2


@dataclass  # Класс структуры хранения данных в списке
class Option:
    # параметры программы
    binary: bool = field(default=False)  # True - стандартные опционы, False - опционы OTC
    timeframe: str = field(default='')  # рабочий таймфрейм
    search_tf: str = field(default='')  # обозначение таймфрейма для поиска
    name_tf: str = field(default='')  # обозначение таймфрейма для постов
    link_val: str = field(default='')  # ссылка на валютную пару в TradingView
    price_screen: int = field(default=0)  # номер окна с ценой
    start_random: int = field(default=0)  # стартовая позиция рандома для поиска параметров уровней ПС
    end_random: int = field(default=0)  # финишная позиция рандома для поиска параметров уровней ПС
    option_time: int = field(default=0)  # время опциона в сек
    dogon_par: List[int] = field(default_factory=list)  # значения догонов
    # настройки валютной пары
    name: str = field(default='')  # название валюты
    id_val: int = field(default=0)  # id валюты
    browser_name: str = field(default='')  # название валюты для поиска в браузере
    name_emoji: str = field(default='')  # название валюты с эмодзи
    round: int = field(default=0)  # параметры округления
    coefficient: float = field(
        default=0.0)  # коэффициент для перерасчета параметров, если таймфреймы для поиска совпадают
    # параметры индикаторов
    movement_potential: str = field(default='')  # параметр Потенциал движения
    long_trend: str = field(default='')  # параметр Долгосрочный тренд
    volume_profile: str = field(default='')  # параметр Объемный профиль
    interest: str = field(default='')  # параметр Усредненный интерес
    paritet: str = field(default='')  # параметр Паритет объёмного баланса
    paritet_str: str = field(default='')  # параметр Паритет объёмного баланса для резюме
    direction_force: str = field(default='')  # параметр Сила направления движения
    kol_ind: int = field(default=0)  # параметр Количество индикаторов
    prop_ind: int = field(default=0)  # параметр Вероятность по индикаторам
    price_reversal: str = field(default='')  # параметр Вероятность разворота цены
    potential_change: str = field(default='')  # параметр Вероятность изменения потенциала
    itog_stat: str = field(default='')  # параметр Итоговая статистика успешного исхода сделки
    dogon: str = field(default='')  # параметр Вероятность использования перекрытий
    kol_dogon: int = field(default=0)  # параметр Количество догонов (для режима infinity)
    # параметры опциона
    resume: str = field(default='')  # направление опциона

    buy: bool = field(default=False)  # если опцион на покупку
    sell: bool = field(default=False)  # True, если опцион на продажу
    trade_emoji: str = field(default='')  # эмодзи для отражения направления опциона
    support: str = field(default='')  # уровень поддержки
    resistance: str = field(default='')  # уровень сопротивления
    price: float = field(default=0)  # цена входа
    itg_price: float = field(default=0)  # цена итога опциона пятизначная
    plus: bool = field(default=False)  # True, если опцион в плюс
    minus: bool = field(default=False)  # True, если опцион в минус
    vozvrat: bool = field(default=False)  # True, если опцион возврат
    dgn: bool = field(default=False)  # True, если нужен догон

    price_difference: str = field(default='')  # разница в цене на начало и конец опциона
    difference_point: int = field(default=0)  # разница в цене на начало и конец опциона в пунктах
    dgn_time: int = field(default=0)  # Время догона в секундах
    dgn_time_str: str = field(default='')  # Время догона строкой
    start_message_id: int = field(default=0)  # Id стартового сообщения для пересылки и записи в БД
    itog_message_id: int = field(default=0)  # Id стартового сообщения для пересылки и записи в БД
    message_forecast: str = field(default='')  # строка с направлением опциона для второго сообщения
    message_emoji_quotation: str = field(default='')  # эмодзи для котировки для второго сообщения
    success: str = field(default='')  # параметр Вероятность успешного исхода сделки

    def __init__(self, tf, dogon):
        self.timeframe = tf
        self.find_timeframe = next((item['find'] for item in find_timeframe if item['timeframe'] == tf), '5m')
        result = [item for item in spr_timeframe if item["timeframe"] == tf]
        if result is not None:
            self.search_tf = result[0]['search_tf']
            self.name_tf = result[0]['name_tf']
            self.option_time = int(tf.replace('m', '')) * 60 + 2
            self.dogon_par = dogon
            if result[0]['coefficient']:
                self.coefficient = self.find_coefficient()
            else:
                self.coefficient = 1

    def clear_data(self):
        if self.difference_point >= 50 and self.timeframe == 5:
            pass
        self.name = ''  # название валюты
        self.browser_name = ''  # название валюты для поиска в браузере
        self.name_emoji = ''  # название валюты с эмодзи
        self.round = 0  # параметры округления
        # параметры индикаторов
        self.movement_potential = ''  # параметр Потенциал движения
        self.long_trend = ''  # параметр Долгосрочный тренд
        self.volume_profile = ''  # параметр Объемный профиль
        self.interest = ''  # параметр Усредненный интерес
        self.paritet = ''  # параметр Паритет объёмного баланса
        self.direction_force = ''  # параметр Сила направления движения
        self.kol_ind = 0  # параметр Количество индикаторов
        self.prop_ind = 0  # параметр Вероятность по индикаторам
        self.price_reversal = ''  # параметр Вероятность разворота цены
        self.potential_change = ''  # параметр Вероятность изменения потенциала
        self.itog_stat = ''  # параметр Итоговая статистика успешного исхода сделки
        self.dogon = ''  # параметр Вероятность использования перекрытий
        # параметры опциона
        self.resume = ''  # направление опциона
        self.buy = False  # если опцион на покупку
        self.sell = False  # True, если опцион на продажу
        self.support = ''  # уровень поддержки
        self.resistance = ''  # уровень сопротивления
        self.price = 0.0  # цена входа
        self.itg_price = 0.0  # цена итога опциона пятизначная
        self.plus = False  # True, если опцион в плюс
        self.minus = False  # True, если опцион в минус
        self.vozvrat = False  # True, если опцион возврат
        self.dgn = False  # True, если нужен догон
        self.price_difference = ''  # разница в цене на начало и конец опциона
        self.difference_point = 0  # разница в цене на начало и конец опциона в пунктах
        self.dgn_time = 0  # Время догона в секундах
        self.dgn_time_str = ''  # Время догона строкой
        self.start_message_id = 0  # Id стартового сообщения для пересылки и записи в БД
        self.itog_message_id = 0  # Id итогового сообщения для пересылки и записи в БД
        self.message_forecast = ''  # строка с направлением опциона для второго сообщения
        self.message_emoji_quotation = ''  # эмодзи для котировки для второго сообщения
        self.success = ''  # параметр Вероятность успешного исхода сделки
        self.paritet_str = ''  # параметр Паритет объёмного баланса для резюме
        self.trade_emoji = ''  # эмодзи для отражения направления опциона
        self.kol_dogon = 0  # очистка кол-ва догонов

    def add_option_data(self, data: dict):
        if self.binary:
            self.fill_binary(data=data)
        else:
            self.fill_otc(data=data)

    def fill_binary(self, data):
        """
                # внесение данных для стандартного опциона
                :param data: данные по опциону из БД
                :return:
                """
        self.name = data['name_val']
        self.round = data['round']
        self.id_val = data['val_id']
        valname = self.name.split('/')
        self.link_val = link1 + valname[0] + valname[1] + link2 + valname[0] + valname[1]
        self.browser_name = self.name.replace('/', '')
        self.name_emoji = f"<b><i>{self.name} {data['base_emoji']}/{data['second_emoji']}</i></b>"
        if "ПОКУПКУ" in data['resume']:
            self.resume = "ПОКУПАТЬ"
            self.buy = True
            self.sell = False
            self.message_forecast = '<i><b>ВХОД ВВЕРХ</b></i> <emoji id="5269460053651366623">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5377799276548071161">🔹</emoji>'
            self.trade_emoji = '<emoji id="5449683594425410231">🔼</emoji>'
        else:
            self.resume = "ПРОДАВАТЬ"
            self.buy = False
            self.sell = True
            self.message_forecast = '<i><b>ВХОД ВНИЗ</b></i> <emoji id="5271811599785534382">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5398038979217989221">🔴</emoji>'
            self.trade_emoji = '<emoji id="5447183459602669338">🔽</emoji>'
        # параметр Потенциал движения
        self.movement_potential = (f"{data['move_potential_down']}% — {data['move_potential_up']}% "
                                   f"{self.post_emoji(data=data['move_potential_up'])}")
        # параметр Сила направления движения
        self.direction_force = f"{data['dir_force_down']}% — {data['dir_force_up']}%"
        # параметр Долгосрочный тренд
        self.long_trend = (f"{data['long_trend_down']}% — {data['long_trend_up']}% "
                           f"{self.post_emoji(data=data['long_trend_up'])}")
        # параметр Объемный профиль — всегда направленный эмодзи (📈/📉), не зависит от порога
        _vp_arrow = ('<emoji id="5269460053651366623">📈</emoji>' if self.buy
                     else '<emoji id="5271811599785534382">📉</emoji>')
        self.volume_profile = (f"{data['volume_profile_down']}% — {data['volume_profile_up']}% "
                               f"{_vp_arrow}")
        # параметр Усредненный интерес
        self.interest = (f"{data['average_interest_down']}% — {data['average_interest_up']:.0f}% "
                         f"{self.post_emoji(data=data['average_interest_up'])}")
        # параметр Паритет объёмного баланса
        self.paritet = (f"{data['volume_balance_down']}% — {(data['volume_balance_up'])}% "
                        f"{self.post_emoji(data=data['volume_balance_up'])}")
        self.paritet_str = f"{(data['volume_balance_down'] + (data['volume_balance_up'])) / 2:.1f}%"
        # параметр Вероятность по индикаторам
        self.prop_ind = data['prop_ind']
        # параметр Количество индикаторов
        self.kol_ind = data['kol_ind'] + 1
        # Параметр - Вероятность разворота цены
        self.price_reversal = f"{data['price_reversal_down']}% — {data['price_reversal_up']}% "
        # Параметр Вероятность изменения потенциала
        self.potential_change = f"{data['potential_change_down']}% — {data['potential_change_up']}% "
        # Параметр Итоговая статистика успешного исхода сделки
        self.itog_stat = f"{data['itog_stat_down']}% — {data['itog_stat_up']}% "

    def fill_otc(self, data):
        """
        # внесение данных для опциона OTC
        :param data: данные по опциону из БД
        :return:
        """
        self.name = data['name_val']
        self.round = data['round']
        self.id_val = data['val_id']
        self.browser_name = self.name.replace('/', '')
        self.name_emoji = f"<b><i>{self.name} {data['base_emoji']}/{data['second_emoji']}</i></b>"
        if data['buy']:
            self.resume = "ПОКУПАТЬ"
            self.buy = True
            self.sell = False
            self.message_forecast = '<i><b>ВХОД ВВЕРХ</b></i> <emoji id="5269460053651366623">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5377799276548071161">🔹</emoji>'
            self.trade_emoji = '<emoji id="5449683594425410231">🔼</emoji>'
        else:
            self.resume = "ПРОДАВАТЬ"
            self.buy = False
            self.sell = True
            self.message_forecast = '<i><b>ВХОД ВНИЗ</b></i> <emoji id="5271811599785534382">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5398038979217989221">🔴</emoji>'
            self.trade_emoji = '<emoji id="5447183459602669338">🔽</emoji>'
        # параметр Объемный профиль — всегда направленный эмодзи (📈/📉), не зависит от порога
        _vp_arrow = ('<emoji id="5269460053651366623">📈</emoji>' if self.buy
                     else '<emoji id="5271811599785534382">📉</emoji>')
        self.volume_profile = (f"{data['volume_profile_down']}% — {data['volume_profile_up']}% "
                                   f"{_vp_arrow}")
        # параметр Усредненный интерес
        self.interest = (f"{data['average_interest_down']}% — {data['average_interest_up']}% "
                         f"{self.post_emoji(data=data['average_interest_up'])}")
        # параметр Паритет объёмного баланса
        self.paritet = (f"{data['volume_balance_down']}% — {data['volume_balance_up']}% "
                        f"{self.post_emoji(data=data['volume_balance_up'])}")
        self.paritet_str = f"{(data['volume_balance_down'] + data['volume_balance_up']) / 2:.1f}%"
        # параметр Сила направления движения
        self.direction_force = f"{data['dir_force_down']}% — {data['dir_force_up']}%"
        self.kol_ind = data['kol_ind']
        self.prop_ind = data['prop_ind']
        self.price_reversal = f"{data['price_reversal_down']:.1f}% — {data['price_reversal_up']:.1f}% "
        # Параметр Вероятность изменения потенциала
        self.potential_change = f"{data['potential_change_down']:.1f}% — {data['potential_change_up']:.1f}% "
        # Параметр Итоговая статистика успешного исхода сделки
        self.itog_stat = f"{data['itog_stat_down']:.1f}% — {data['itog_stat_up']:.1f}%"

    def random_dogon(self):
        """
        выбор рандомного направления для догона
        :return:
        """
        self.buy = random.choice([False, True])
        if self.buy:
            self.sell = False
            self.message_forecast = '<i><b>ВХОД ВВЕРХ</b></i> <emoji id="5269460053651366623">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5377799276548071161">🔹</emoji>'
            self.trade_emoji = '<emoji id="5449683594425410231">🔼</emoji>'
        else:
            self.sell = True
            self.message_forecast = '<i><b>ВХОД ВНИЗ</b></i> <emoji id="5271811599785534382">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5398038979217989221">🔴</emoji>'
            self.trade_emoji = '<emoji id="5447183459602669338">🔽</emoji>'

    # определение уровней поддержки и сопротивления
    def levels(self):
        level1_1 = self.price - random.uniform(self.start_random, self.end_random) * 0.1 ** self.round
        level2_1 = self.price + random.uniform(self.start_random, self.end_random) * 0.1 ** self.round
        level1_2 = level1_1 - random.uniform(self.start_random / 2, self.end_random / 2) * 0.1 ** self.round
        level2_2 = level2_1 + random.uniform(self.start_random / 2, self.end_random / 2) * 0.1 ** self.round
        self.resistance = f'{level2_1:.{self.round}f} — {level2_2:.{self.round}f}'
        self.support = f'{level1_1:.{self.round}f} — {level1_2:.{self.round}f}'

    def comparing_lists(self):  # расчет итога опциона
        itog_price = self.itg_price - self.price
        if itog_price == 0:
            self.vozvrat = True
            self.price_difference = f"{itog_price:.{self.round}}"
            self.difference_point = int(round(itog_price * 10 ** self.round))
        elif self.buy:
            if itog_price > 0:
                self.plus = True
                self.price_difference = f"{itog_price:.{self.round}}"
                self.difference_point = int(round(itog_price * 10 ** self.round))
            elif itog_price < 0:
                self.dgn = True
        elif self.sell:
            if itog_price < 0:
                self.plus = True
                self.price_difference = f"{abs(itog_price):.{self.round}}"
                self.difference_point = int(round(abs(itog_price) * 10 ** self.round))
            elif itog_price > 0:
                self.dgn = True

    def comparing_lists_dogon(self) -> bool:  # расчет итога
        itog_price = self.itg_price - self.price
        if self.buy == 1:
            if itog_price > 0:
                self.plus = True
                self.price_difference = f"{itog_price:.{self.round}}"
                self.difference_point = int(round(itog_price * 10 ** self.round))
                return True
            else:  # itog_price <= 0
                return False
        else:
            if itog_price < 0:
                self.plus = True
                self.price_difference = f"{abs(itog_price):.{self.round}}"
                self.difference_point = int(round(abs(itog_price) * 10 ** self.round))
                return True
            else:  # itog_price >= 0
                return False

    # настройки времени для догона
    def dogon_settings(self, dogon_par):
        self.dgn_time = dogon_par * 60 + 2
        self.dgn_time_str = f'{dogon_par} {self.minuts(kol=dogon_par)}'

    # определяет коэффициент для расчета параметров
    @staticmethod
    def find_coefficient():
        while True:
            number = round(random.uniform(-2.5, 2.5), 1)
            if number != 0:
                return number

    # добавляет эмодзи в зависимости от значения параметра
    def post_emoji(self, data):
        if data >= 80:
            if self.buy:
                return '<emoji id="5021905410089550576">✅</emoji>'
            else:
                return '<emoji id="5019523782004441717">❌</emoji>'
        else:
            if self.buy:
                return '<emoji id="5269460053651366623">📈</emoji>'
            else:
                return '<emoji id="5271811599785534382">📉</emoji>'

    @staticmethod
    def different_price(start_price, itog_price, rnd) -> dict:  # расчет итоговой разницы
        result = {}
        dif_price = abs(start_price - itog_price)
        point = 1
        for i in range(0, rnd):
            point *= 10
        dif_point = round(dif_price * point, 0)
        dif_price_str = f"{dif_price:.{rnd}f}"
        result.update(point=dif_point, dif_price=dif_price_str)
        return result

    @staticmethod
    def minuts(kol: int):
        # Делаем падежи для слова минута
        if 11 <= kol % 100 <= 14:
            return 'минут'
        last_digit = kol % 10
        if last_digit == 1:
            return 'минута'
        elif last_digit in [2, 3, 4]:
            return 'минуты'
        else:
            return 'минут'

    @staticmethod
    def random_otc(par) -> dict:
        # поиск рандомных параметров для ОТС
        result_dict = {}
        if par == 1:
            mean = random.randint(65, 70)
        elif par == 2:
            mean = random.randint(70, 83)
        else:
            mean = random.randint(84, 92)
        mean2 = mean - random.randint(2, 4)
        result_dict.update(par1=mean2, par2=mean)
        return result_dict
