import random

from settings.config import option_data, binary
from settings.constant import minus_fraze, bug_pic, weekend, start_trade, pl_mes


def first_message():
    """
    # стартовое сообщение опциона
    :return: текст поста
    """
    # f_message = f'<a href="{first_pic}">&#8205;</a>'
    f_message = f'<b><i>Подготовьте торговый актив: {option_data.name_emoji} </i></b>\n\n'
    if binary:
        f_message += (f'<i>Ссылка на валютную пару: <a href="{option_data.link_val}"><b>Жми сюда</b></a></i> '
                      f'<emoji id="5021905410089550576">✅</emoji>\n\n')
    f_message += ('<emoji id="5472146462362048818">💡</emoji>'
                  '<b><i>Важные рекомендации перед отработкой:</i></b>\n\n')
    f_message += ('<blockquote>'
                  '<emoji id="5021712394259268143">🟡</emoji> <i>Соблюдайте риск и мани менеджмент, не рискуйте более '
                  '5% от суммы вашего баланса!</i>\n'
                  '<emoji id="5021712394259268143">🟡</emoji> <i>Не копируйте вход в рынок слепо, всегда используйте '
                  'свой собственный технический анализ!</i>\n'
                  '<emoji id="5021712394259268143">🟡</emoji> <i>Данные прогнозы выступают в качестве рекомендации для'
                  ' входа, но не являются сигналом!</i>'
                  '</blockquote>')
    return f_message


def second_message():
    """
    # второе сообщение - пост с аналитикой и прогнозом
    :return: текст поста
    """
    asset_label = 'Торговый актив' if binary else 'Торговый актив OTC'
    sila = '<emoji id="5292142381531931487">🐻</emoji>'
    txt_str = 'продажу'
    if 'ПОКУПАТЬ' in option_data.resume:  # формирование строки вывода для пары Активно покупать
        txt_str = 'покупку'
        sila = '<emoji id="5289500525673325056">🐂</emoji>'
    # option_data.name мутируется в otc_app.py до 'AUD/CAD OTC' для скриншота; для текста нужен голый "AUD/CAD"
    name_clean = option_data.name.replace(' OTC', '')
    s_message = f'<i>{asset_label}: </i>{option_data.name_emoji}\n\n'
    s_message += f'<i>Прогноз:</i> {option_data.message_forecast}\n'
    s_message += (f'<i>Время экспирации: </i><b><i>{option_data.name_tf.upper()}</i></b> '
                  f'<emoji id="5433825729060018456">🧭</emoji>\n')
    s_message += (f'<i>Текущая котировка: </i><b><i>{option_data.price:.{option_data.round}f}</i></b> '
                  f'{option_data.message_emoji_quotation}\n\n')
    s_message += '<b><i>Автоматический технический анализ от Smoke FX AI:</i></b>\n\n'
    # volume_profile / interest / paritet уже содержат завершающий эмодзи (post_emoji в Option_class),
    # который зависит от направления и порога ≥80 — дополнительных эмодзи не добавляем.
    s_message += (f'<blockquote>'
                  f'<i>Объёмный профиль: </i><b><i>{option_data.volume_profile}</i></b>\n'
                  f'<i>Усредненный интерес на {txt_str}: </i><b><i>{option_data.interest}</i></b>\n'
                  f'<i>Паритет объёмного баланса: </i><b><i>{option_data.paritet}</i></b>'
                  f'</blockquote>\n\n')
    s_message += (f'<b><i>Сила движения {name_clean} от объёма: '
                  f'{option_data.direction_force}</i></b> {sila}\n\n')
    s_message += (f'<blockquote>'
                  f'<emoji id="5375129357373165375">🔗</emoji> <i>Область поддержки: </i>'
                  f'<b><i>{option_data.support}</i></b>\n'
                  f'<emoji id="5375129357373165375">🔗</emoji> <i>Область сопротивления: </i>'
                  f'<b><i>{option_data.resistance}</i></b>'
                  f'</blockquote>\n\n')
    s_message += (f'<emoji id="5397569414738485822">🟡</emoji> <i>Итоговая статистика успешного исхода сделки по '
                  f'Smoke FX AI: </i><b><i>{option_data.itog_stat}</i></b>\n\n')
    s_message += ('<emoji id="5472146462362048818">💡</emoji><b><i>ВАЖНО:</i></b> '
                  '<i>Данная информация не является торговым сигналом, и выступает только в качестве '
                  'дополнительного источника анализа торгового актива!</i>')
    return s_message


def third_message():
    """
    # итоговое сообщение (плюс или возврат)
    :return: текст поста
    """
    price_data = option_data.different_price(start_price=option_data.price, itog_price=option_data.itg_price,
                                             rnd=option_data.round)
    asset_label = 'Торговый актив' if binary else 'Торговый актив OTC'
    kat1 = '<emoji id="5373001317042101552">📈</emoji>'
    kat2 = '<emoji id="5361748661640372834">📉</emoji>'
    raznica = '<emoji id="5431577498364158238">📊</emoji>'
    if option_data.vozvrat:
        kat1 = '<emoji id="5373280361067322980">⏺️</emoji>'
        kat2 = '<emoji id="5373280361067322980">⏺️</emoji>'
        raznica = '<emoji id="5397569414738485822">🟡</emoji>'
    s_message = f'<i>{asset_label}: </i><b><i>{option_data.name_emoji}</i></b> {option_data.trade_emoji}\n\n'
    s_message += (f'<b><i>Котировка открытия: {option_data.price:.{option_data.round}f}</i></b> '
                  f'{kat1}\n')
    s_message += (f'<b><i>Котировка закрытия: {option_data.itg_price:.{option_data.round}f}</i></b> '
                  f'{kat2}\n\n')
    s_message += (f'<b><i>Разница пунктов: {price_data["dif_price"]}</i></b> {raznica}'
                  f'\n\n')
    if option_data.plus:
        s_message += '<i>Итог прогноза:</i><b><i> плюс</i></b> <emoji id="5021905410089550576">✅</emoji>'
    elif option_data.vozvrat:
        s_message += '<i>Итог прогноза: </i><b><i><u>ВОЗВРАТ</u></i></b> <emoji id="5382178536872223059">💫</emoji>\n'
        s_message += ('<blockquote>'
                      '<i><b>ВОЗВРАТ</b> — это, когда точка открытия, и точка закрытия совпадают вплоть до последней '
                      'цифры после запятой, такое происходит крайне редко, по этому робот фиксирует "возврат", т.е '
                      'цена вернулась к точке открытия на завершении времени экспирации</i> '
                      '<emoji id="5021712394259268143">🟡</emoji>'
                      '</blockquote>')
    return s_message


def prepare_dogon_message(idx: int):
    """
    # сообщение о первом догоне
    :param idx: индекс для определения поста
    :return: текст поста
    """
    asset_label = 'Торговый актив' if binary else 'Торговый актив OTC'
    trade_emoji = f'<b><i>{option_data.trade_emoji}</i></b>'
    if idx == 0:
        dop_str = (f'<b><i>Подготовьте перекрытие </i></b>{trade_emoji}<b><i> — </i></b>'
                   f'<a href="https://teletype.in/@smoke_fx/0IGYyrwdTYX"><b><i> </i></b></a>'
                   f'<b><i>поиск точки входа</i></b> '
                   f'<emoji id="5188217332748527444">🔍</emoji>\n\n')
    elif idx == 1:
        dop_str = (f'<b><i>Подготовьте второе перекрытие </i></b>{trade_emoji}'
                   f'<b><i> — поиск точки входа</i></b> '
                   f'<emoji id="5188217332748527444">🔍</emoji>\n\n')
    else:
        dop_str = (f'<b><i>Подготовьте следующее перекрытие </i></b>{trade_emoji}'
                   f'<b><i> — поиск точки входа</i></b> '
                   f'<emoji id="5188217332748527444">🔍</emoji>\n\n')
    price_data = option_data.different_price(start_price=option_data.price, itog_price=option_data.itg_price,
                                             rnd=option_data.round)
    dg_message = f'<i>{asset_label}: </i><b><i>{option_data.name_emoji} </i></b>\n\n' + dop_str
    dg_message += (f'<blockquote>'
                   f'<emoji id="5373001317042101552">📈</emoji> <b><i>Котировка открытия: '
                   f'{option_data.price:.{option_data.round}f}</i></b>\n'
                   f'<emoji id="5361748661640372834">📉</emoji> <b><i>Котировка закрытия: '
                   f'{option_data.itg_price:.{option_data.round}f}</i></b>'
                   f'</blockquote>\n\n')
    dg_message += (f'<emoji id="5431577498364158238">📊</emoji> <b><i>Разница пунктов: '
                   f'{price_data["dif_price"]}</i></b>')
    return dg_message


def dop_dogon_message():
    """
    :return: текст поста
    """
    if option_data.buy:
        direction_word = 'ВВЕРХ'
        direction_arrow = '<emoji id="5269460053651366623">📈</emoji>'
    else:
        direction_word = 'ВНИЗ'
        direction_arrow = '<emoji id="5271811599785534382">📈</emoji>'
    dg_message = ('<b><i><emoji id="5472146462362048818">💡</emoji></i></b>'
                  '<b><i>Условия повторного входа в рынок:</i></b>\n\n')
    dg_message += (f'<emoji id="5337068216189464647">🤔</emoji> <i><b>Повторный</b> вход будет осуществляться: </i>'
                   f'<b><i>{direction_word}</i></b> {direction_arrow}\n\n')
    dg_message += ('<emoji id="5337068216189464647">🤔</emoji> <i>Поиск точки входа занимает </i>'
                   '<b><i>до 2-х минут времени</i></b>\n\n')
    dg_message += ('<emoji id="5337068216189464647">🤔</emoji> <i>Сейчас подготовьте сумму для перекрытия согласно '
                   'риск-мани менеджменту, </i><b><i>не более 5% от вашего баланса</i></b>')
    return dg_message


def dogon_message():
    """
    # сообщение о первом догоне в телеграм (данные по валютной паре, время догона, цена входа)
    :return: текст поста
    """
    if option_data.buy:
        dg_message = f'<i>Итог прогноза: <b>ПЕРЕКРЫТИЕ ВВЕРХ</b></i> {option_data.trade_emoji}\n'
    else:
        dg_message = f'<i>Итог прогноза: <b>ПЕРЕКРЫТИЕ ВНИЗ</b></i> {option_data.trade_emoji}\n'
    dg_message += f'<i>Время экспирации: <b>{option_data.dgn_time_str.upper()}</b></i> ' \
                  f'<emoji id="5451646226975955576">⌛️</emoji>\n'
    dg_message += f'<i>Котировка актива: <b>{option_data.price:.{option_data.round}f}</b></i> ' \
                  f'<emoji id="5231200819986047254">📊</emoji>'
    return dg_message


def minus_dogon_message():
    """
    # сообщение об итоге догона (входные данные, итоговые данные)
    :return: текст поста
    """
    s_message = f'<i>Валютная пара: <b>{option_data.name_emoji}</b></i> {option_data.trade_emoji}\n\n'
    s_message += (f'<b><i>Котировка открытия: {option_data.price:.{option_data.round}f}</i></b> '
                  f'<emoji id="5231200819986047254">📊</emoji>\n')
    s_message += (f'<b><i>Котировка закрытия: {option_data.itg_price:.{option_data.round}f}</i></b> '
                  f'<emoji id="5451882707875276247">🕯</emoji>\n\n')
    s_message += f'<i>Итог прогноза: <b><u>МИНУС</u></b></i> <emoji id="5390874368177873184">❌</emoji>\n\n'
    s_message += f'<i>{random.choice(minus_fraze)}</i> <emoji id="4927486932113425461">❗️</emoji>'
    return s_message


def main_bug_message():  # Сообщение о сбое сервера
    f_message = f'<a href="{bug_pic}">&#8205;</a>'
    f_message += '<emoji id="5388992695760854987">‼️</emoji><emoji id="5388773283061571233">🅱️</emoji>' \
                 '<emoji id="5386681118592343902">🅱️</emoji><emoji id="5386866759963781146">🅱️</emoji>' \
                 '<emoji id="5386558596060291152">🅱️</emoji><emoji id="5386742738488140832">🅱️</emoji>' \
                 '<emoji id="5386642292087989706">🅱️</emoji><emoji id="5386610041178563168">‼️</emoji>\n\n'
    f_message += '<b><i>Oooooops, system reboot</i></b> <emoji id="5276281088782638888">❗️</emoji>\n\n'
    f_message += ('<b><i>На сервере ведутся технические работы, по улучшению работы алгоритма, и стабильности подачи '
                  'данных</i></b> <emoji id="5021905410089550576">✅</emoji>\n\n')
    f_message += '<b><i>Обычно это занимает не так много времени, так, что можешь не переживать!</i></b>\n\n'
    f_message += ('<b><i>Почему так произошло? Мы всё время обновляем наши алгоритмы, что бы стать лучше, и время от '
                  'времени перезапускаем систему</i></b> <emoji id="5424972470023104089">🔥</emoji>\n\n')
    f_message += ('<b><i>Если этот сбой произошёл во время выхода прогноза, продолжи анализ и торговлю самостоятельно, '
                  'и главное помни про риск и мани менеджмент, счастье тебе в жизни дружок, обнял </i></b>🙏')
    return f_message


def plus5_message():
    message = '<a href="https://i.ibb.co/gL9NGbv6/5.png">&#8205;</a>'
    message += '<b><i>5 прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>При заходе в каждый прогноз, вы могли бы заработать около — <b><u>5.ООО</u> ₽ чистыми</b>, при '
                'выплате по активу <b><u>от 75% — 85%</u></b>! Помните, что инвестирование небольших сумм и '
                'использование риск и мани менеджмента могут генерировать ваш стабильный доход <b><u>от 5.ООО</u> ₽ '
                '</b> — <b><u>1.ООО.ООО</u> ₽</b> ежедневно!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus10_message():
    message = '<a href="https://i.ibb.co/YFtr5tJF/10.png">&#8205;</a>'
    message += '<b><i>10 прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>При заходе в каждый прогноз, вы могли бы заработать около — <b><u>2O.ООО</u> ₽ чистыми</b>, при '
                'выплате по активу <b><u>от 75% — 85%</u></b>! Помните, что инвестирование небольших сумм и '
                'использование риск и мани менеджмента могут генерировать ваш стабильный доход <b><u>от 5.ООО</u> ₽ '
                '</b> — <b><u>1.ООО.ООО</u> ₽</b> ежедневно!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus15_message():
    message = '<a href="https://i.ibb.co/dsNQTpdv/15.png">&#8205;</a>'
    message += '<b><i>15 прогнозов в ряд закрываются в плюс </i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>При заходе в каждый прогноз, вы могли бы заработать около — <b><u>5O.ООО</u> ₽ чистыми</b>, при '
                'выплате по активу <b><u>от 75% — 85%</u></b>! Помните, что инвестирование небольших сумм и '
                'использование риск и мани менеджмента могут генерировать ваш стабильный доход <b><u>от 5.ООО</u> ₽ '
                '</b> — <b><u>1.ООО.ООО</u> ₽</b> ежедневно!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus20_message():
    message = '<a href="https://i.ibb.co/rfKY2g8P/20.png">&#8205;</a>'
    message += '<b><i>20 прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>При заходе в каждый прогноз, вы могли бы заработать около — <b><u>1OO.ООО</u> ₽ чистыми</b>, при '
                'выплате по активу <b><u>от 75% — 85%</u></b>! Помните, что инвестирование небольших сумм и '
                'использование риск и мани менеджмента могут генерировать ваш стабильный доход <b><u>от 5.ООО</u> ₽ '
                '</b> — <b><u>1.ООО.ООО</u> ₽</b> ежедневно!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus25_message():
    message = '<a href="https://i.ibb.co/27JpfwJ6/25.png">&#8205;</a>'
    message += '<b><i>25 прогнозов в ряд закрываются в плюс </i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>При заходе в каждый прогноз, вы могли бы заработать около — <b><u>15O.ООО</u> ₽ чистыми</b>, при '
                'выплате по активу <b><u>от 75% — 85%</u></b>! Помните, что инвестирование небольших сумм и '
                'использование риск и мани менеджмента могут генерировать ваш стабильный доход <b><u>от 5.ООО</u> ₽ '
                '</b> — <b><u>1.ООО.ООО</u> ₽</b> ежедневно!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus30_message():
    message = '<a href="https://i.ibb.co/7NWRYXkV/30.png">&#8205;</a>'
    message += '<b><i>3О прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>Мы сделали 3О прогнозов в ПЛЮС подряд - это доказывает тот факт, что алгоритм Смоука <u>нагибает '
                'рынок</u>, как нагибается бабка, когда сажает картошку! При <u>самых скромных</u> расчётах, каждый '
                'из Вас должен был сделать уже около <u>+- 15О.ООО₽</u> чистыми! Хочется сказать только одно, Smoke '
                'FX тащит!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus35_message():
    if binary:
        otc = ''
    else:
        otc = ' OTC'
    message = '<a href="https://i.ibb.co/tTp8QKzB/35.png">&#8205;</a>'
    message += '<b><i>35 прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += (f'<i>Мы сделали 35 прогнозов в ПЛЮС подряд, новый авторский алгоритм Smoke FX Binary{otc} AI сканирует '
                f'рынок на поиск лучших моментов, и выдаёт их Вам! Кто попал на эту серию плюсов, приблизился к '
                f'сумме <u>+-2ОО.ООО</u> ₽ чистыми! Smoke FX работает на благо всех трейдеров, аминь!</i> '
                f'<emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus40_message():
    message = '<a href="https://i.ibb.co/jkZ9b9Hm/40.png">&#8205;</a>'
    message += '<b><i>4О прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>Мы сделали 4О прогнозов в ПЛЮС подряд за счёт индивидуального подбора лучших ситуаций на рынке! '
                'Работает AI от Smoke FX, который сканирует все валютные пары для Binary Options! Ориентировочная сумма'
                ' прибыли составляет <u>275.ООО₽  чистыми</u>! Не забывайте о том, что, Smoke FX тащит как '
                'настоящий дед!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus45_message():
    if binary:
        otc = ''
    else:
        otc = ' OTC'
    message = '<a href="https://i.ibb.co/yn7z9H5g/45.png">&#8205;</a>'
    message += '<b><i>45 прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += (f'<i>Мы сделали 45 прогнозов в ПЛЮС подряд - это фактически один из лучших результатов в пространстве '
                f'СНГ-трейдинга, алгоритмы <u>Smoke FX Binary{otc} AI</u> работают как атомные-швейцарские часы! '
                f'Ваш доход мог приблизиться уже к <u>5ОО.ООO₽ чистыми!</u> С вас коммент на ютуб в формате: Smoke FX '
                f'тащит как батёк!</i> <emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def plus50_message():
    message = '<a href="https://i.ibb.co/wZSWQn7J/50.png">&#8205;</a>'
    message += '<b><i>5О прогнозов в ряд закрываются в плюс</i></b> <emoji id="5445284980978621387">🚀</emoji>\n\n'
    message += ('<i>Мы сделали 5О прогнозов в ПЛЮС подряд, это официальный рекорд всего трейдерского сообщества СНГ! '
                'Пишите всем своим друзьям, обращайтесь в СМИ, кричите с балкона, что мы это сделали! Фактический '
                'доход мог составить до 1.ООО.ООО₽  чистяковыми! Быть добру, миру мир, и Smoke FX ТАЩИТ!</i> '
                '<emoji id="5391240565679465844">⚡️</emoji>\n\n')
    message += pl_mes
    return message


def minus_message():
    # message = f'<a href="{minus}">&#8205;</a>'
    message = f'<b><i>Высокая волатильность на рынке</i></b> <emoji id="5391240565679465844">⚡️</emoji>\n\n'
    message += (f'<i>Внимание, последние 2О минут волатильность на рынке резко повысилась, помни про использование '
                f'риск-мани менеджмента, и не рискуй большими суммами при торговле! Помни, что прогнозы в закрытой '
                f'группе созданы лишь как дополнительный инструмент для анализа, а не как основной источник принятия '
                f'решения, будь здравым и используй технический анализ</i> <emoji id="5021712394259268143">🟡</emoji>')
    return message


def dop_plus10_message():
    # message = f'<a href="https://i.ibb.co/YjfFQxd/photo-2023-06-16-22-15-02.jpg">&#8205;</a>'
    message = ('<b><i>Алгоритм Smoke FX AI выдаёт серию плюсов в ряд!</i></b> '
               '<emoji id="5431449001532594346">⚡️</emoji>\n\n')
    message += ('<i>Если ты принимал участие в торговле </i><i><b>и попал на эту серию плюсов</b>, '
                'отправь скриншот с результатами </i><b><i>в чат-бот с отзывами!</i></b>\n\n')
    message += ('<emoji id="5397569414738485822">🟡</emoji> <i>Ссылка на </i>'
                '<b><i>чат-бот для отзывов: </i></b>'
                '<a href="https://t.me/commentFX_bot"><b><i>Оставить свой отзыв</i></b></a>\n\n')
    message += '<i>Для чего я прошу Вас оставить отзывы про реальную торговлю?</i>\n\n'
    message += ('<blockquote>'
                '<emoji id="5021618274345943603">🟡</emoji> <i>Правдивая статистика нашего сообщества Smoke FX в '
                'открытом доступе для всех!</i>\n'
                '<emoji id="5021618274345943603">🟡</emoji> <i>Показатель эффективности бесплатного обучения от '
                'трейдера Smoke FX!</i>\n'
                '<emoji id="5021618274345943603">🟡</emoji> <i>Мотивация для начинающих трейдеров, которые только '
                'начинают свой путь!</i>'
                '</blockquote>\n\n')
    message += ('<emoji id="5472146462362048818">💡</emoji>'
                '<i><b>ВАЖНОЕ НАПОМИНАНИЕ:</b> закрепите свой результат в истории наших отзывов, своими положительными '
                'сделками и скриншотами с результатом! Это положительно повлияет на ваш личный рост как трейдера, так'
                ' и на мотивацию для начинающих трейдеров!</i>')
    return message


def weekend_message():
    message = f'<a href="{weekend}">&#8205;</a>'
    message += ('<b><i>Завершение торговой недели в закрытом сообществе трейдера Smoke FX</i></b> '
                '<emoji id="5021905410089550576">✅</emoji>\n\n')
    message += '<b><i>Что бы ваши выходные прошли с пользой, рекомендую:</i></b>\n\n'
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Пересматривайте обучающие видео на моём канале ютуб, '
                'так вы наберетесь больше полезной информации, и сможете проводить правильный разбор графика по '
                'прогнозам: https://www.youtube.com/@smoke_fx</i>\n\n')
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Проводите технический анализ на пустом графике, '
                'отмечайте уровни поддержки и сопротивления, проводите направление тренда на графике, делайте '
                'свечной анализ!</i>\n\n')
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Смотрите бесплатные обучающие курсы в открытой '
                'группе, чем больше полезной инфы, тем быстрее вы начнёте рубить капусточку!</i>\n\n')
    message += '<b><i>Желаю всем хороших выходных, отдыхайте и с понедельника снова в бой! Всех обнял</i></b>!'
    return message


def start_message():
    message = f'<a href="{start_trade}">&#8205;</a>'
    message += ('<b><i>Начало торговой недели в сообществе трейдера Smoke FX</i></b> '
                '<emoji id="5445284980978621387">🚀</emoji>\n\n')
    message += '<b><i>Чтобы ваша торговая неделя прошло удачно, рекомендую:</i></b>\n\n'
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Пересматривайте обучающие видео на моём канале ютуб, '
                'так вы наберетесь больше полезной информации, и сможете проводить правильный разбор графика по '
                'прогнозам: https://www.youtube.com/@smoke_fx</i>\n\n')
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Занимайтесь практикой, анализируйте график на '
                'финансовом рынке на сайте tradingview, рекомендую использовать таймфрейм 5 минут!</i>\n\n')
    message += ('<emoji id="5021712394259268143">🟡</emoji> <i>Смотрите бесплатные обучающие курсы в открытой группе,'
                ' чем больше полезной инфы, тем быстрее вы начнёте рубить капусточку!</i>\n\n')
    message += '<b><i>Желаю всем хорошей торговой недели, заряжайтесь только на прибыль, всех обнял!</i></b>'
    return message


def otc_unactive():
    o_mes = '<i><b>Данный ОТС-актив более недоступен для торговли!</b>\b\b'
    o_mes += '<emoji id="5021712394259268143">🟡</emoji> <b>Причины такой ситуации:</b>\n\n'
    o_mes += ('Со стороны брокера очень часто меняется ликвидность в активах ОТС, и для перезагрузки своих серверов, '
              'они вынуждены убирать часть активов для стабилизации работы всей системы! Мы как конечные пользователи,'
              ' остаемся просто с фактом о том, что его убрали, наш алгоритм подпирает другой актива ОТС для '
              'продолжения торговли, а ниже будут советы лично от меня!\n\n')
    o_mes += '<emoji id="4926956800005112527">🔴</emoji> <b>Не торгуйте на ОТС активах, где выплата менее 92%!</b>\n\n'
    o_mes += ('<emoji id="4926956800005112527">🔴</emoji> <b>Заходите в рынок только от областей ПС, учитывая общий '
              'тренд!</b>\n\n')
    o_mes += ('<emoji id="4926956800005112527">🔴</emoji> <b>Проводить анализ графика необходимо только на таймфрейме'
              ' 5 минут!</b>\n\n')
    o_mes += ('Напоминаю Вам, что активы ОТС максимально наполнены резкими движениями, но и выплата на них '
              'составляет в среднем 92%, отсюда соблюдаем риск и мани менеджмент! Smoke FX всегда с Вами на связи '
              'молодежь, быть добру! Обнял!</i>')
    return o_mes


