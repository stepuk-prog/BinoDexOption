import random

from settings.constant import spr_timeframe, find_timeframe, option_time_variants
from settings.browser_constant import link1, link2


class Option:  # Класс структуры хранения данных в списке
    # Классовые атрибуты-дефолты (общие fallback'и; экземпляр заполняют __init__ /
    # clear_data / fill_*). Все дефолты неизменяемые — общего мутабельного состояния нет.
    # dogon_par (изменяемый список) намеренно не объявлен здесь, а задаётся только в __init__.
    # параметры программы
    binary: bool = False  # True - стандартные опционы, False - опционы OTC
    timeframe: str = ''  # рабочий таймфрейм
    search_tf: str = ''  # обозначение таймфрейма для поиска
    name_tf: str = ''  # обозначение таймфрейма для постов
    link_val: str = ''  # ссылка на валютную пару в TradingView
    start_random: int = 0  # стартовая позиция рандома для поиска параметров уровней ПС
    end_random: int = 0  # финишная позиция рандома для поиска параметров уровней ПС
    option_time: int = 0  # время опциона в сек
    # настройки валютной пары
    name: str = ''  # название валюты
    id_val: int = 0  # id валюты
    browser_name: str = ''  # название валюты для поиска в браузере
    name_emoji: str = ''  # название валюты с эмодзи
    round: int = 0  # параметры округления
    # параметры индикаторов (рендерятся в постах)
    volume_profile: str = ''  # параметр Объемный профиль
    interest: str = ''  # параметр Усредненный интерес
    paritet: str = ''  # параметр Паритет объёмного баланса
    direction_force: str = ''  # параметр Сила направления движения
    itog_stat: str = ''  # параметр Итоговая статистика успешного исхода сделки
    dogon: str = ''  # параметр Вероятность использования перекрытий
    # параметры опциона
    resume: str = ''  # направление опциона

    buy: bool = False  # если опцион на покупку
    sell: bool = False  # True, если опцион на продажу
    trade_emoji: str = ''  # эмодзи для отражения направления опциона
    support: str = ''  # уровень поддержки
    resistance: str = ''  # уровень сопротивления
    price: float = 0.0  # цена входа
    itg_price: float = 0.0  # цена итога опциона пятизначная
    plus: bool = False  # True, если опцион в плюс
    minus: bool = False  # True, если опцион в минус
    vozvrat: bool = False  # True, если опцион возврат
    dgn: bool = False  # True, если нужен догон

    dgn_time: int = 0  # Время догона в секундах
    dgn_time_str: str = ''  # Время догона строкой
    start_message_id: int = 0  # Id стартового сообщения для пересылки и записи в БД
    itog_message_id: int = 0  # Id итогового сообщения для пересылки и записи в БД
    message_forecast: str = ''  # строка с направлением опциона для второго сообщения
    message_emoji_quotation: str = ''  # эмодзи для котировки для второго сообщения

    def __init__(self, tf, dogon):
        self.timeframe = tf
        self.find_timeframe = next((item['find'] for item in find_timeframe if item['timeframe'] == tf), '5m')
        result = [item for item in spr_timeframe if item["timeframe"] == tf]
        if not result:
            raise ValueError(f"Неизвестный таймфрейм '{tf}' — нет строки в spr_timeframe")
        self.search_tf = result[0]['search_tf']
        self.name_tf = result[0]['name_tf']
        self.option_time = int(tf.replace('m', '')) * 60 + 2
        self.dogon_par = dogon

    def clear_data(self):
        self.name = ''  # название валюты
        self.browser_name = ''  # название валюты для поиска в браузере
        self.name_emoji = ''  # название валюты с эмодзи
        self.round = 0  # параметры округления
        # параметры индикаторов
        self.volume_profile = ''  # параметр Объемный профиль
        self.interest = ''  # параметр Усредненный интерес
        self.paritet = ''  # параметр Паритет объёмного баланса
        self.direction_force = ''  # параметр Сила направления движения
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
        self.dgn_time = 0  # Время догона в секундах
        self.dgn_time_str = ''  # Время догона строкой
        self.start_message_id = 0  # Id стартового сообщения для пересылки и записи в БД
        self.itog_message_id = 0  # Id итогового сообщения для пересылки и записи в БД
        self.message_forecast = ''  # строка с направлением опциона для второго сообщения
        self.message_emoji_quotation = ''  # эмодзи для котировки для второго сообщения
        self.trade_emoji = ''  # эмодзи для отражения направления опциона

    def add_option_data(self, data: dict):
        if self.binary:
            self.fill_binary(data=data)
        else:
            self.fill_otc(data=data)

    def _apply_direction(self, buy: bool):
        """Единый блок направления опциона: buy/sell + эмодзи прогноза/котировки/итога.
        resume выставляется в fill_binary/fill_otc отдельно (для догона он не нужен)."""
        self.buy = buy
        self.sell = not buy
        if buy:
            self.message_forecast = '<i><b>ВХОД ВВЕРХ</b></i> <emoji id="5269460053651366623">📈</emoji>'
            self.message_emoji_quotation = '<emoji id="5377799276548071161">🔹</emoji>'
            self.trade_emoji = '<emoji id="5449683594425410231">🔼</emoji>'
        else:
            self.message_forecast = '<i><b>ВХОД ВНИЗ</b></i> <emoji id="5271811599785534382">📉</emoji>'
            self.message_emoji_quotation = '<emoji id="5398038979217989221">🔴</emoji>'
            self.trade_emoji = '<emoji id="5447183459602669338">🔽</emoji>'

    def fill_binary(self, data):
        """
        Внесение данных для стандартного опциона.
        :param data: данные по опциону из БД
        """
        self.name = data['name_val']
        self.round = data['round']
        self.id_val = data['val_id']
        valname = self.name.split('/')
        self.link_val = link1 + valname[0] + valname[1] + link2 + valname[0] + valname[1]
        self.browser_name = self.name.replace('/', '')
        self.name_emoji = f"<b><i>{self.name} {data['base_emoji']}/{data['second_emoji']}</i></b>"
        buy = "ПОКУПКУ" in data['resume']
        self.resume = "ПОКУПАТЬ" if buy else "ПРОДАВАТЬ"
        self._apply_direction(buy)
        # параметр Сила направления движения
        self.direction_force = f"{data['dir_force_down']}% — {data['dir_force_up']}%"
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
        # Параметр Итоговая статистика успешного исхода сделки
        self.itog_stat = f"{data['itog_stat_down']}% — {data['itog_stat_up']}% "

    def fill_otc(self, data):
        """
        Внесение данных для опциона OTC.
        :param data: данные по опциону из БД
        """
        self.name = data['name_val']
        self.round = data['round']
        self.id_val = data['val_id']
        self.browser_name = self.name.replace('/', '')
        self.name_emoji = f"<b><i>{self.name} {data['base_emoji']}/{data['second_emoji']}</i></b>"
        buy = bool(data['buy'])
        self.resume = "ПОКУПАТЬ" if buy else "ПРОДАВАТЬ"
        self._apply_direction(buy)
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
        # параметр Сила направления движения
        self.direction_force = f"{data['dir_force_down']}% — {data['dir_force_up']}%"
        # Параметр Итоговая статистика успешного исхода сделки
        self.itog_stat = f"{data['itog_stat_down']:.1f}% — {data['itog_stat_up']:.1f}%"

    def random_dogon(self):
        """Выбор рандомного направления для догона."""
        self._apply_direction(random.choice([False, True]))

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
        elif self.buy:
            if itog_price > 0:
                self.plus = True
            elif itog_price < 0:
                self.dgn = True
        elif self.sell:
            if itog_price < 0:
                self.plus = True
            elif itog_price > 0:
                self.dgn = True

    def comparing_lists_dogon(self) -> bool:  # расчет итога догона
        itog_price = self.itg_price - self.price
        if self.buy:
            if itog_price > 0:
                self.plus = True
                return True
            return False  # itog_price <= 0
        else:
            if itog_price < 0:
                self.plus = True
                return True
            return False  # itog_price >= 0

    # назначение времени экспирации опциона на текущий сигнал
    def set_option_time(self):
        """Время экспирации текущего сигнала (сек) + синхронизация name_tf для поста.
        Варианты 3m/5m (FIN и OTC одинаково): график настроен на фиксированный ТФ, а реальное
        время опциона выбирается рандомно (3m → 2/3 мин, 5m → 4/5 мин); name_tf обновляется
        под выбранное значение, чтобы пост не врал о времени экспирации. Прочие ТФ
        (1m/10m/15m) — номинал из таймфрейма, name_tf остаётся из spr_timeframe (как в __init__)."""
        variants = next((item['variants'] for item in option_time_variants
                         if item['timeframe'] == self.timeframe), None)
        if variants:
            minutes = random.choice(variants)
            self.name_tf = f'{minutes} {self.minuts(kol=minutes)}'
        else:
            minutes = int(self.timeframe.replace('m', ''))
        self.option_time = minutes * 60 + 2

    # настройки времени для догона
    def dogon_settings(self, dogon_par):
        self.dgn_time = dogon_par * 60 + 2
        self.dgn_time_str = f'{dogon_par} {self.minuts(kol=dogon_par)}'

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
        dif_point = round(dif_price * 10 ** rnd, 0)
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
