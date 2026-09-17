import asyncio
import random
import time
from typing import TYPE_CHECKING

from apps.app import exit_main, screenshot, find_point, find_option_data, check_cookies_price, sleep_or_stop
from apps.my_exeptions import send_photo_safe
from apps.otc_app import (parce_otc, screenshot_otc, reload_otc_page, select_otc_pair,
                          ensure_chart_setup, ui_loaded, apply_offzone, UI_DEAD_CONFIRM)
from logs import init_logger
from messages.message import (first_message, second_message, dogon_message, third_message, prepare_dogon_message,
                              dop_dogon_message, minus_dogon_message)
from settings.config import option_data, binary, overlap, overlap_random, screenshot_path
from settings.timing import BETWEEN_MESSAGES_DELAY, POST_SCREENSHOT_DELAY, TG_SEND_TIMEOUT
from settings.image_paths import DOGON_IMAGES, NEW_FORECAST_IMAGES

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

used_val = [0]
prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
count_price = 0  # счетчик количества одинаковой цены подряд

# Сколько заходов подряд без заведённой пары признаём отказом БРАУЗЕРА, а не выдачи TV.
# Счётчик МОДУЛЬНЫЙ: локальный обнулялся бы на каждом опционе, и три промаха кряду никогда
# бы не накопились — ровно те грабли, на которых стояли счётчики в init_with_retry.
FIN_INIT_MAX_FAILS = 3
_init_fails = 0
logger = init_logger(__name__)

# OTC: binodex периодически (тест-режим) висит БЕЗ единой торговой пары — модалка пар пуста,
# хотя сессия/UI/WS живы. Это не краш и не повод рестартить (единый принцип: «сайт не даёт
# работать» → ждём, а не сбрасываем процесс). Делаем NO_PAIRS_RELOADS быстрых reload+выбор пары
# (пауза NO_PAIRS_RELOAD_PAUSE) — на случай, если пары вернёт простой reload. Не помогло —
# отдаём управление главному циклу (result=False, fall=False): тот при мёртвом фиде уйдёт в
# браузер-фри ожидание (выгрузка браузера + слушание WS, без релогина/спама), а при живом —
# просто повторит цикл через time_sleep. Никаких длинных таймер-снов с удержанием браузера здесь.
NO_PAIRS_RELOADS = 3
NO_PAIRS_RELOAD_PAUSE = 5      # сек между быстрыми reload
# Потолок на ВЕСЬ подбор пары (reload'ы + перебор активных пар в parce_otc). Без него три круга
# reload_otc_page (до ~90с) плюс перебор всех пар по ~70с на неудачную давали десятки минут —
# юнит зелёный, постов нет, в логе одни warning'и. Исчерпали — отдаём главному циклу ('timeout'),
# он переждёт штатно (браузер-фри при мёртвом фиде / повтор при живом), БЕЗ рестарта процесса.
ACQUIRE_TOTAL_BUDGET = 120     # сек

# OTC: binodex сам может свалиться на сплеш В ТЕЧЕНИЕ опциона (новая версия/переинициализация
# Privy — без нашего reload, см. memory binodex-stuck-splash). Чтобы опцион не прерывался, за
# HEALTH_LEAD сек до КАЖДОЙ фиксации результата (итог И каждый шаг догона) проверяем живость UI
# и при сплеше поднимаем reload+переселект ТОЙ ЖЕ пары — результат снимется с опозданием, а не
# потеряется. Лид прячется в хвосте ожидания экспирации, поэтому в норме задержки нет.
HEALTH_LEAD = 15              # сек до фиксации результата — упреждающая проверка/восстановление UI
# Потолок на сам ремонт (reload + переселект пары + оформление). Он идёт за HEALTH_LEAD до
# фиксации результата, а без потолка растягивался на минуты — итоговая котировка снималась бы
# сильно ПОСЛЕ экспирации, то есть была бы неверной. Лучше не снять итог, чем снять чужой.
ALIVE_REPAIR_BUDGET = 45      # сек


async def _try_send(photo, caption, mes_type: str, timeout: float = TG_SEND_TIMEOUT) -> tuple[bool, str]:
    """Отправка поста с обработкой обрыва связи и таймаутом — тонкая обёртка над единым
    send_photo_safe (клиент берётся из get_app()-синглтона внутри send_photo_safe). Возврат (ok, err).

    Дефолт — из settings.timing, а не число: свой литерал здесь переопределял бы общий потолок
    (посты основного цикла шли бы мимо правки TG_SEND_TIMEOUT — так и было до 2026-08-15)."""
    ok, err = await send_photo_safe(photo, caption, mes_type, timeout)
    if ok:
        # Отмечаем ФАКТ публикации: по нему exit_main решает, слать ли баг-картинку. Сбой до
        # первого поста подписчики не видели вовсе — извиняться за него не за что, а картинка
        # «сбой программы» в ленте без единого прогноза выглядит как поломка на ровном месте.
        option_data.posted = True
    return ok, err


async def _repair_otc_ui(manager: "BrowserManager", page) -> None:
    """Собственно ремонт UI в течение опциона: reload → вернуть ТУ ЖЕ пару → вернуть оформление.
    Вынесено из _ensure_otc_alive, чтобы накрыть всё это ОДНИМ потолком по времени (см. там)."""
    if not await reload_otc_page(manager=manager):
        logger.warning('OTC: reload в течение опциона не поднял UI — результат может не сняться')
        return
    # reload сбрасывает выбранную пару → возвращаем ту же. option_data.name = '<pair> OTC',
    # select_otc_pair ждёт голую пару (сам добавит ' OTC' для проверки WS-котировки).
    bare = option_data.name[:-4] if option_data.name.endswith(' OTC') else option_data.name
    if not await select_otc_pair(page, bare):
        logger.warning(f'OTC: не вернул пару {bare} после reload в течение опциона — результат под вопросом')
        return
    # Аварийный reload сбрасывает и оформление графика (масштабы/индикаторы) — возвращаем, иначе
    # остаток опциона снимался бы чужим таймфреймом и без индикаторов.
    await ensure_chart_setup(manager)


async def _ensure_otc_alive(manager: "BrowserManager", stop_event):
    """OTC: перед фиксацией результата СНАЧАЛА дёшево проверить, жив ли UI — видна ли кнопка
    настроек аккаунта (точный маркер «не сплеш»). Видна → ничего не делаем, БЕЗ reload. И только
    если пропала (binodex сам свалился на сплеш в течение опциона, не наш reload) — поднять reload
    (он ретраит сплеш) и ВЕРНУТЬ ТУ ЖЕ пару (reload сбрасывает выбор пары). Best-effort: не вышло —
    результат снимется как раньше с ошибкой → exit_main. FIN не трогаем. SIGTERM пропускаем.

    Ремонт накрыт ЖЁСТКИМ потолком ALIVE_REPAIR_BUDGET. Зовётся он за HEALTH_LEAD (15с) до
    фиксации результата, а сам по себе мог идти минуты (reload до ~90с + переселект до ~70с +
    оформление) — то есть итоговая котировка снималась бы далеко ПОСЛЕ экспирации и была бы
    просто неверной. Лучше признать опцион несостоявшимся, чем опубликовать чужую цену."""
    if binary or stop_event.is_set():
        return
    page = manager.pages['main']
    if await ui_loaded(page, UI_DEAD_CONFIRM):   # кнопка настроек на месте → UI жив, reload не нужен
        return
    logger.warning('OTC: кнопка настроек пропала в течение опциона (сплеш) — reload+переселект, '
                   'не прерывая опцион')  # рутина → файл, не канал
    try:
        await asyncio.wait_for(_repair_otc_ui(manager, page), timeout=ALIVE_REPAIR_BUDGET)
    except asyncio.TimeoutError:
        logger.warning(f'OTC: ремонт UI не уложился в {ALIVE_REPAIR_BUDGET}с — прекращаю, '
                       f'итог снимется как есть (цена после экспирации была бы неверной)')
        # Отмена могла оборвать select_otc_pair на полуслове, вместе с его finally, который
        # возвращает off-zone. Без off-zone остаток опциона рендерится на полном CPU — ставим
        # его обратно явно (сама по себе неудача ремонта это не чинит, но CPU не жжёт).
        await apply_offzone(page)


async def _wait_result(manager: "BrowserManager", stop_event, seconds: float):
    """Дождаться экспирации перед фиксацией результата, но за HEALTH_LEAD сек до конца проверить
    живость OTC-UI и при сплеше восстановить (reload+переселект). Лид прячется в хвосте ожидания —
    в норме (UI жив, проверка ~мгновенна) задержки нет; при сплеше результат снимется с опозданием,
    но опцион не прервётся. stop_event прерывает паузы (после вызова проверять stop_event.is_set())."""
    lead = min(float(HEALTH_LEAD), seconds) if not binary else 0.0
    await sleep_or_stop(stop_event, seconds - lead)
    if lead and not stop_event.is_set():
        # Лид — это окно ДО экспирации, а не пауза ПОСЛЕ проверки. Восстановление UI может занять
        # весь свой потолок, и безусловный сон на весь лид добавлял бы это время ПОВЕРХ экспирации:
        # итоговый кадр снимался бы с ценой чужого момента — ровно то, от чего страхует бюджет
        # кадра. Поэтому досыпаем только ОСТАТОК лида (в норме проверка мгновенна и остаток полный).
        started = time.monotonic()
        await _ensure_otc_alive(manager, stop_event)
        await sleep_or_stop(stop_event, max(0.0, lead - (time.monotonic() - started)))


async def _capture(manager: "BrowserManager", qr, *, seek_point: bool):
    """Снять скрин текущего окна (единый код вместо 4 дублей if binary/else).
    FIN — окно price/main + опциональный поиск точки входа (seek_point); OTC — окно main.
    :return: кортеж (ok, price|error) от screenshot/screenshot_otc."""
    if binary:
        if seek_point:
            fp_ok, fp_err = await find_point(manager, option_data.buy)
            if not fp_ok:
                logger.warning("find_point не нашёл точку входа (%s) — продолжаю по текущей цене", fp_err)
        return await screenshot(manager=manager, take_shot=True, qr=qr)
    page = manager.pages['main']
    return await screenshot_otc(page=page, asset=option_data.name, qr=qr)


async def _acquire_otc_pair(manager: "BrowserManager", stop_event) -> str:
    """Подобрать OTC-пару с устойчивостью к тест-режиму binodex (периодически пар нет вовсе).
    Логика — см. константы NO_PAIRS_* выше. Развилки исходов:
      'ok'            — пара выбрана, можно работать дальше;
      'reload_failed' — reload не поднял UI (новая версия/сплеш/редирект) → отдаём штатному
                        otc_session_dead (пересоздание браузера/авто-рефреш кук), НЕ ждём пары;
      'no_pairs'      — после быстрых reload пар по-прежнему нет → отдаём главному циклу
                        (result=False, fall=False): тот переждёт браузер-фри/повтором, БЕЗ рестарта;
      'timeout'       — бюджет подбора исчерпан (UI отвечает, но каждый шаг залипает) → туда же,
                        куда 'no_pairs', но с честной причиной в логе и bug_text;
      'stopped'       — пришёл сигнал остановки (SIGTERM/SIGINT) во время пауз.
    Длинных таймер-снов здесь нет: кадэнс ожидания держит главный цикл (browser-free / time_sleep),
    чтобы не удерживать тяжёлый браузер впустую и не плодить рестарты на простое сайта.

    ОБЩИЙ БЮДЖЕТ — ACQUIRE_TOTAL_BUDGET. Раньше потолка не было вовсе: три круга по
    reload_otc_page (до ~90с каждый) плюс parce_otc, который перебирает ВСЕ активные пары по
    ~70с на неудачную, складывались в десятки минут — для диспетчера это неотличимо от
    зависания, а в логе только warning'и."""
    deadline = time.monotonic() + ACQUIRE_TOTAL_BUDGET

    def budget_spent() -> bool:
        """Бюджет вышел? Тогда пишем причину — вызывающий отдаёт 'timeout'.

        Проверка нужна дважды: в начале каждого круга и после цикла (последний круг мог
        доесть остаток). Раньше в обоих местах стоял ДОСЛОВНО повторённый warning — правка
        текста в одном месте оставляла второй как был (ревизия 17-09-2026, п.3.3).
        """
        if time.monotonic() < deadline:
            return False
        logger.warning(f'OTC: бюджет подбора пары {ACQUIRE_TOTAL_BUDGET:.0f}с исчерпан — '
                       f'отдаю главному циклу (без рестарта)')
        return True

    for _ in range(NO_PAIRS_RELOADS):
        if stop_event.is_set():
            return 'stopped'
        if budget_spent():
            return 'timeout'
        if not await reload_otc_page(manager=manager):
            return 'reload_failed'   # сессия/сплеш — не «нет пар», лечит otc_session_dead
        if await parce_otc(manager=manager, log_data=option_data, valute=used_val,
                           deadline=deadline):
            return 'ok'
        # Пауза перед следующим кругом — тоже под бюджетом. Иначе она спала полные
        # NO_PAIRS_RELOAD_PAUSE перед кругом, которого уже не будет (бюджет-то вышел), и
        # «потолок 120с» превращался в 120 + 5 на каждый оставшийся круг.
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if await sleep_or_stop(stop_event, min(NO_PAIRS_RELOAD_PAUSE, left)):
            return 'stopped'
    if stop_event.is_set():
        return 'stopped'
    if budget_spent():
        return 'timeout'
    logger.info('OTC: на binodex нет торговых пар после быстрых reload — отдаю главному циклу '
                '(браузер-фри ожидание при мёртвом фиде / повтор при живом), без рестарта')
    return 'no_pairs'


async def main(manager: "BrowserManager", qr, stop_event):
    """Тонкая обёртка над _run_option: ловит НЕПРЕДВИДЕННОЕ исключение середины опциона (после
    первого сообщения, до итогового) и шлёт баг-картинку в канал (channel_mess по option_data.posted),
    а не молчаливый краш/рестарт без пояснения подписчикам. Явные сбои покрыты в _run_option."""
    # Флаг публикации живёт в option_data (ставит _try_send на КАЖДОМ успешном посте,
    # снимает clear_data): раньше тут был свой модульный флаг ровно с тем же смыслом,
    # и два источника одной правды разъехались бы при первой же правке.
    option_data.posted = False
    try:
        return await _run_option(manager, qr, stop_event)
    except (Exception,) as error:
        logger.error(f'Непредвиденная ошибка в опционе: {error}')
        return await exit_main(channel_mess=option_data.posted, result=False,
                               bug_text=f'Непредвиденная ошибка - {error}', check_cookies=count_price)


async def _run_option(manager: "BrowserManager", qr, stop_event):
    global prev_price, count_price, _init_fails   # used_val только мутируем (append/del) — global не нужен
    prev_price = 0.0  # цена предыдущего цикла (для определения отвала cookies)
    count_price = 0  # счетчик количества одинаковой цены подряд

    logger.info("🔄 Начало main(), binary=%s", binary)
    logger.info("📑 Доступные страницы: %s", list(manager.pages.keys()))

    if binary:
        logger.info("🔍 Вызов find_option_data...")
        if not await find_option_data(manager=manager, log_data=option_data,
                                      used_val=used_val, stop_event=stop_event):
            # Ни одна пара не завелась. Один такой заход — не повод рестартить: выдача поиска
            # TV меняется, и пропустить опцион дешевле полного переподъёма браузера. А вот
            # FIN_INIT_MAX_FAILS подряд по РАЗНЫМ активам означают, что не отвечает браузер —
            # тогда просим рестарт (fall=True), как это делают квизы на MAX_INIT_FAILS.
            _init_fails += 1
            if _init_fails >= FIN_INIT_MAX_FAILS:
                _init_fails = 0
                return await exit_main(channel_mess=False, result=False, fall=True,
                                       bug_text=f'FIN: {FIN_INIT_MAX_FAILS} пары подряд не завелись '
                                                f'в браузере — он не отвечает',
                                       check_cookies=count_price)
            return await exit_main(channel_mess=False, result=False, fall=False,
                                   bug_text='FIN: пара не завелась — пропускаю опцион',
                                   check_cookies=count_price)
        _init_fails = 0
        logger.info("✅ find_option_data завершён")
        logger.info("📸 Вызов screenshot(screen=None)...")
        screen_shot = await screenshot(manager=manager, take_shot=False, qr=qr)
        logger.info("✅ screenshot завершён: %s", screen_shot[0])
    else:
        # Перед каждым опционом перезагружаем страницу binodex и подбираем пару. binodex
        # периодически (тест-режим) висит без единой пары — это НЕ краш: ждём, а не рестартим
        # (единый принцип «сайт не даёт работать → ждём»). Все исходы-«сайт не готов» уходят с
        # fall=False → главный цикл сам переждёт (браузер-фри при мёртвом фиде / повтор при живом).
        outcome = await _acquire_otc_pair(manager, stop_event)
        if outcome == 'stopped':  # SIGTERM во время ожидания пар — выходим без рестарта
            return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)
        if outcome == 'reload_failed':  # новая версия/сплеш/редирект → otc_session_dead пересоздаст браузер
            return await exit_main(channel_mess=False, result=False, fall=False,
                                   bug_text='binodex не поднялся после reload (новая версия/сплеш)',
                                   check_cookies=count_price)
        if outcome == 'no_pairs':  # пар нет → НЕ рестартим: главный цикл переждёт (browser-free/повтор)
            return await exit_main(channel_mess=False, result=False, fall=False,
                                   bug_text='На binodex нет торговых пар (тест-режим) — жду, не рестартю',
                                   check_cookies=count_price)
        if outcome == 'timeout':  # UI отвечает, но подбор залип → туда же, но причина честная
            return await exit_main(channel_mess=False, result=False, fall=False,
                                   bug_text=f'Подбор OTC-пары не уложился в '
                                            f'{ACQUIRE_TOTAL_BUDGET}с — жду, не рестартю',
                                   check_cookies=count_price)
        # Оформление графика (масштабы свеча/график + индикаторы) binodex периодически сбрасывает
        # сам — проверяем и возвращаем ПЕРЕД каждым опционом, до первого кадра. В норме read-only и
        # мгновенно; UI трогаем только при реальном сбросе. См. otc_app.ensure_chart_setup.
        await ensure_chart_setup(manager)
        page = manager.pages['main']
        screen_shot = await screenshot_otc(page=page, asset=option_data.name, qr=qr)

    if not screen_shot[0]:
        # OTC: первый кадр не снялся (нет цены графика / пустой канвас, частый транзиент тест-режима
        # binodex) — НЕ рестартим процесс. fall=False → возврат в главный цикл, где штатная браузер-
        # фри ветка (main.py: feed_alive → _await_binodex_feed) переждёт аутэйдж без релогина и спама;
        # при живом фиде — просто повтор следующего цикла с новой парой. FIN: браузер-фри ожидания
        # нет, поэтому там кадр-сбой по-прежнему уводит в рестарт (fall=True).
        return await exit_main(channel_mess=False, result=False, fall=bool(binary),
                               bug_text=f'Ошибка проверки скриншота - {screen_shot[1]}', check_cookies=count_price)

    message_text = first_message()
    new_prognoz_img = random.choice(NEW_FORECAST_IMAGES)  # рандомно из 3 в pictures/new_prognoz/
    ok, err = await _try_send(new_prognoz_img, message_text, 'первое сообщение')
    if not ok:
        return await exit_main(channel_mess=False, result=False, bug_text=err, check_cookies=count_price)

    used_val.append(option_data.id_val)
    if len(used_val) >= 4:  # держим последние 3 id → актив не повторяется в окне из 4 рынков подряд
        del used_val[0]

    await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

    screen_shot = await _capture(manager, qr, seek_point=True)

    if not screen_shot[0]:
        return await exit_main(channel_mess=True, result=False,
                               bug_text=f'Ошибка снятия скриншота для поста с тех. анализом - {screen_shot[1]}',
                               check_cookies=count_price)

    option_data.price = round(screen_shot[1], option_data.round)
    if not binary:  # Проверка на отвал cookies
        prev_price = option_data.price

    option_data.set_option_time()  # FIN/OTC 3m/5m: рандомное время экспирации + синхронизация name_tf
    option_data.levels()
    # Старт опциона в лог: пара / ТФ / направление / цена входа / экспирация. Первая половина
    # пары «старт → итог» (вторая — в app.exit_main): без неё по info.log виден только заход
    # в main(), а какой опцион ушёл в тему и чем кончился — нет. Место выбрано после
    # set_option_time: здесь ВСЕ поля уже заполнены, а впереди самое долгое ожидание.
    logger.info('▶️ Опцион %s %s %s: вход %s, экспирация %sс', option_data.name,
                option_data.timeframe, option_data.resume, option_data.price,
                option_data.option_time)
    message_text = second_message()

    ok, err = await _try_send(screenshot_path, message_text, 'второе сообщение')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

    await _wait_result(manager, stop_event, option_data.option_time)
    if stop_event.is_set():  # SIGTERM во время ожидания экспирации — выходим без постов
        return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

    screen_shot = await _capture(manager, qr, seek_point=False)

    if screen_shot[0]:
        option_data.itg_price = round(screen_shot[1], option_data.round)
    else:
        return await exit_main(channel_mess=True, result=False,
                               bug_text=f'Ошибка снятия скриншота для итогового поста - {screen_shot[1]}',
                               check_cookies=count_price)

    option_data.comparing_lists()

    if not binary:  # Проверка на отвал cookies
        count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                      new_price=option_data.itg_price,
                                                      round_par=option_data.round,
                                                      count=count_price)

    if not option_data.dgn:  # если опцион закончился без догона
        message_text = third_message()
        ok, err = await _try_send(screenshot_path, message_text, 'итоговое сообщение')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
        return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    # Перетасованный список картинок для перекрытий — без повторов в рамках одного прогноза.
    dogon_pool = random.sample(DOGON_IMAGES, len(DOGON_IMAGES))

    for index in range(min(overlap + 1, len(option_data.dogon_par))):
        dogon = option_data.dogon_par[index]
        option_data.dogon_settings(dogon_par=dogon)
        if index + 1 > overlap - overlap_random:
            option_data.random_dogon()

        text_message = prepare_dogon_message(idx=index)
        ok, err = await _try_send(screenshot_path, text_message, 'первое сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(BETWEEN_MESSAGES_DELAY)

        img = dogon_pool[index % len(dogon_pool)]
        text_message = dop_dogon_message()
        ok, err = await _try_send(img, text_message, 'доп. сообщение догона')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await asyncio.sleep(POST_SCREENSHOT_DELAY)

        screen_shot = await _capture(manager, qr, seek_point=True)

        if not screen_shot[0]:
            return await exit_main(channel_mess=True, result=False,
                                   bug_text=f'Ошибка снятия скриншота для поста с догоном - {screen_shot[1]}',
                                   check_cookies=count_price)

        option_data.price = round(screen_shot[1], option_data.round)
        if not binary:  # Проверка на отвал cookies
            count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                          new_price=option_data.price,
                                                          round_par=option_data.round,
                                                          count=count_price)

        text_message = dogon_message()
        ok, err = await _try_send(screenshot_path, text_message, 'Сообщение о догоне')
        if not ok:
            return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)

        await _wait_result(manager, stop_event, option_data.dgn_time)
        if stop_event.is_set():  # SIGTERM во время ожидания итога догона — выходим без постов
            return await exit_main(channel_mess=False, result=False, fall=False, check_cookies=count_price)

        screen_shot = await _capture(manager, qr, seek_point=False)

        if not screen_shot[0]:
            return await exit_main(channel_mess=True, result=False,
                                   bug_text=f'Ошибка снятия скриншота для итога догона - {screen_shot[1]}',
                                   check_cookies=count_price)

        option_data.itg_price = round(screen_shot[1], option_data.round)
        if not binary:  # Проверка на отвал cookies
            count_price, prev_price = check_cookies_price(old_price=prev_price,
                                                          new_price=option_data.itg_price,
                                                          round_par=option_data.round,
                                                          count=count_price)

        if option_data.comparing_lists_dogon():
            text_message = third_message()
            ok, err = await _try_send(screenshot_path, text_message, 'итоговое сообщение')
            if not ok:
                return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
            return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)

    option_data.minus = True
    option_data.plus = False
    text_message = minus_dogon_message()
    ok, err = await _try_send(screenshot_path, text_message, 'итоговое сообщение по последнему догону с минусом')
    if not ok:
        return await exit_main(channel_mess=True, result=False, bug_text=err, check_cookies=count_price)
    return await exit_main(channel_mess=False, result=True, fall=False, check_cookies=count_price)
