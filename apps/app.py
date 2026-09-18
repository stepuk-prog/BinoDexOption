import asyncio
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from PIL import Image
from playwright.async_api import Page

from apps.browser_app import clear_zone_overlays, close_dom_popups, init_valute_browser
from apps.exit_app import close_program
from apps.forum_forward import forward_plus_milestone
from apps.my_exeptions import send_photo_safe
from apps.browser_io import eval_js, shot
# Остановка живёт в нейтральном apps/shutdown.py (его могут импортировать модули, которым
# apps.app тянуть нельзя — my_exeptions, binodex_feed). Здесь — реэкспорт: main.py и
# main_app.py берут request_shutdown/sleep_or_stop по прежнему адресу.
from apps.shutdown import (request_shutdown, shutdown_event,  # noqa: F401 (реэкспорт)
                           shutdown_requested, sleep_or_stop)
from logs import init_logger
from binocore.price import clean_price, lang_from_url
from classes.Option_class import Option
from classes.result_types import MainResult
from messages import main_bug_message, dop_plus10_message, plus_message
from settings import qr110_x, qr110_y, qr85_x, qr85_y, paste_overlay
from settings.screenshot_set import configure_images
from settings.browser_config import move_field, price_field, screen_zone
from settings.config import (option_data, binary, program_id, timeframe,
                            shot_path, screenshot_path, database,
                            main_cycle_pause_min, main_cycle_pause_max)
from settings.constant import qr110_path, qr85_path, otc_qr110_path, bear_color, bull_color, find_time, pic
from settings.timing import CHECK_PLUS_DELAY, POST_SCREENSHOT_DELAY, TIMEOUT_MEDIUM
from settings.image_paths import PLUS_SERIES_IMAGE, PLUS_IMAGE_DIR
from settings.screenshot_set import load_rgba

if TYPE_CHECKING:
    from classes.browser_manager import BrowserManager

logger = init_logger(__name__)

# Логгер семьи для общего пакета (свои уровни, файлы по уровням, отправка в Telegram):
# binocore о нём не знает и без этого писал бы в стандартный logging мимо наших файлов.
configure_images(logger=logger)



async def _close_popup(page):
    """Best-effort снятие оверлея, накрывшего тулбар (общий код для find_price/screenshot).

    10-09-2026: раньше жали `.{pop_up}` — конкретный класс из БД, который протух при
    выкатке фронта TV и не находил ничего. Теперь зовём общую гасилку: она смотрит, что
    реально лежит в точке клика, и снимает помеху без привязки к именам классов.
    """
    try:
        await close_dom_popups(page)
    except (Exception,) as error:
        logger.debug(f'Popup не закрылся (best-effort): {error}')


def get_water():
    """Загрузка QR-оверлеев. FIN — qr110+qr85; OTC — собственный otc_qr110 (на скрине один QR,
    используется только qr[0]). Позиция (otc_qr_x/y) и прочее без изменений."""
    # Кэш и лог сбоя — в общем settings.screenshot_set.load_rgba.
    qr110 = load_rgba(otc_qr110_path if not binary else qr110_path)
    qr85 = load_rgba(qr85_path) if binary else None  # OTC использует только qr[0]
    if qr110 is None or (binary and qr85 is None):
        return False, None
    return True, (qr110, qr85)


# Вехи серии плюсов: на каждой — пост-веха в канал (картинка pictures/pluses/{N}.png).
# ОДИН источник (было два списка в этом же файле — легко разъезжались при правке).
# Потолок на всё уведомление о сбое (bug-картинка): см. exit_main. Отдельно от
# TG_SEND_TIMEOUT — тот на ОДНУ попытку, а здесь их несколько подряд.
BUG_PHOTO_TOTAL_TIMEOUT = 60

PLUS_MILESTONES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50)
# Из них те, что дополнительно пересылаются ботом-модератором в случайную тему форума
# (forward_plus_milestone). Ниже 25 (5/10/15/20) — только пост в канал, без пересылки.
FORWARD_FROM = 25
FORWARD_MILESTONES = frozenset(m for m in PLUS_MILESTONES if m >= FORWARD_FROM)


async def check_plus():
    """Проверка количества плюсов"""
    kol_plus = await database.plus_counter(program_id=program_id, timeframe=timeframe, otc=not binary)
    # Различаем ДВА исхода, которые раньше сливались в «молча продолжаем»: False — сбой пула
    # (инкремент серии потерян, веха не сработает — об этом надо знать), None/пусто — теперь
    # аномалия: с 12-09-2026 запрос UPSERT'ит, то есть первый плюс сам заводит строку счётчика.
    if kol_plus is False:
        logger.warning('Счётчик плюсов не обновился (сбой БД) — серия и веха на этом цикле '
                       'потеряны; пост-веха, если он выпадал на этот плюс, не выйдет')
        return True, ''
    if not kol_plus:
        return True, ''
    count = kol_plus.get('plus')  # asyncpg.Record.get — None, если колонки нет (вместо KeyError)
    if count is None:
        logger.warning('Счётчик плюсов вернул строку без колонки plus — веха пропущена')
        return True, ''

    if count in PLUS_MILESTONES:
        await asyncio.sleep(CHECK_PLUS_DELAY)  # пауза перед постом-вехой (не в каждом плюсовом цикле)
        caption = plus_message(count)
        photo = f'{PLUS_IMAGE_DIR}/{count}.png'
        ok, err, msg_id = await send_photo_safe(photo, caption,
                                                mes_type=f'сообщение {count} плюс', return_message=True)
        if not ok:
            return False, f'Ошибка отправки сообщения {count} плюс! - {err}'
        # Пересылка вехи в случайную тему форума (вторичное действие — не рвёт цикл при сбое).
        if count in FORWARD_MILESTONES and msg_id:
            await forward_plus_milestone(msg_id, count)
        await asyncio.sleep(POST_SCREENSHOT_DELAY)
        bug_fix = await dop_plus_message()
        if bug_fix[0]:
            return True, ''
        else:
            return False, bug_fix[1]

    return True, ''


async def check_minus():
    """Сброс серии плюсов при минусе — инкремент счётчика минусов в БД."""
    # Результат ПРОВЕРЯЕМ: при сбое пула серия плюсов не обнулится, и следующий плюс догонит
    # веху с неверного числа — пост «N в ряд» уйдёт с завышенным счётом. Цикл на этом не рвём
    # (итог опциона уже опубликован), но в лог пишем.
    if await database.minus_counter(program_id=program_id, timeframe=timeframe, otc=not binary) is False:
        logger.warning('Счётчик минусов не обновился (сбой БД) — серия плюсов НЕ обнулена, '
                       'следующая веха может уйти с завышенным числом')
    return True, ''


async def exit_main(channel_mess: bool,
                    result: bool, bug_text='',
                    fall=True,
                    check_cookies: int = 0) -> MainResult:
    """
    Выход из main
    :param channel_mess: если True - отправлять в канал сообщение о сбое (баг-картинку).
        ИНВАРИАНТ вызывающих: True ставится только там, где в этом опционе УЖЕ был пост —
        подписчикам есть что объяснять. До первого поста выходы идут с False (ветка неуспеха
        самого первого сообщения), а обёртка main() передаёт сюда option_data.posted, то есть
        непредвиденный сбой до первого поста тоже приходит с False. Своей проверки здесь нет
        намеренно: она была бы недостижимой веткой, которую нельзя ни протестировать, ни
        сопровождать — а «сбой программы» в ленте без единого прогноза не появится потому,
        что его туда никто не отправляет.
    :param result: если True - опцион удачно завершился
    :param bug_text: текст ошибки
    :param fall: True - критическая ошибка - перезапуск программы
    :param check_cookies: если больше 2 - подозрение на отвал cookies - перезагрузка
    :return: result, plus - если окончился плюсом, fall - перезапуск
    """
    plus = False
    # Штатная остановка (SIGTERM/SIGINT): ничего не шлём в канал и не трогаем счётчики —
    # просто чистим состояние и выходим. Иначе ошибочный выход на shutdown ушёл бы
    # в plus-ветку (check_plus/dop_plus в канал + инкремент серии).
    if shutdown_requested():
        option_data.clear_data()
        return MainResult(result, plus, fall, bug_text, check_cookies)
    if channel_mess:
        # Через send_photo_safe (2026-08-15): прямой send_photo шёл мимо пробы доставки и
        # повтора — при потерях SYN сообщение о сбое просто не доходило (в инциденте оно
        # таймаутило вместе с постами). Сбой отправки здесь по-прежнему НЕ фатален: только лог,
        # выход продолжается штатно.
        # Общий потолок (2026-08-15): внутри send_photo_safe до трёх последовательных
        # ожиданий (отправка TG_SEND_TIMEOUT → проба истории → повтор TG_RECONNECT_TIMEOUT),
        # а при не-таймаутном повторе добавляется ещё restart в lost_connection_photo — это
        # больше двух минут. Для УВЕДОМЛЕНИЯ о сбое чересчур: прогноз уже потерян, держать
        # из-за картинки выход незачем.
        try:
            ok, err = await asyncio.wait_for(
                send_photo_safe(pic('bug.png'), main_bug_message(),
                                mes_type='сообщение о сбое программы'),
                timeout=BUG_PHOTO_TOTAL_TIMEOUT)
        except (Exception,) as error:   # в т.ч. TimeoutError общего потолка
            ok, err = False, repr(error)
        if not ok:
            logger.error(f'Ошибка отправки сообщения о сбое программы - {err}')
    else:
        # Исход опциона в лог — вторая половина пары «старт → итог» (первая в main_app._run_option).
        # Пишем ЗДЕСЬ, в единственной развязке всех трёх финалов (без догона / плюс на догоне /
        # минус после всех догонов), а не тремя строками по местам. Сбойные выходы приходят
        # сюда с result=False — у них исхода нет, и строки быть не должно.
        # До 13-09-2026 исход не логировался вовсе: в OTC-ветке в info.log попадал только факт
        # захода в main(), и по логу нельзя было ни посчитать опционы, ни разобрать минус.
        if result:
            outcome = 'ПЛЮС' if option_data.plus else ('ВОЗВРАТ' if option_data.vozvrat else 'МИНУС')
            # Направление и цена входа берутся из option_data, а её КАЖДЫЙ догон перезаписывает
            # (dogon_settings ставит своё buy/sell и свой price). Поэтому после догонов это
            # значения ПОСЛЕДНЕГО лега, а не исходного опциона — так и пишем. Иначе строка
            # врала: опцион начинался ПОКУПАТЬ по 0.6759, а итог сообщал «ПРОДАВАТЬ, вход
            # 0.67544» (живой пример 13-09-2026 в английской ветке).
            if option_data.dgn:
                logger.info('🏁 Опцион %s: %s — последний лег %s, вход %s, итог %s (после догонов)',
                            option_data.name, outcome, option_data.resume,
                            option_data.price, option_data.itg_price)
            else:
                logger.info('🏁 Опцион %s %s: %s — вход %s, итог %s', option_data.name,
                            option_data.resume, outcome, option_data.price, option_data.itg_price)
        # plus — именно «опцион закончился ПЛЮСОМ», а не «цикл прошёл без баг-картинки». Раньше
        # тут стояло plus = True, то есть флаг поднимался и на минусе; по нему FIN решает,
        # закрывать ли неделю (main.py: «неделю закрываем только на плюсовом опционе»), и
        # неделя закрывалась на первом же завершённом опционе, каким бы ни был итог.
        # В английской версии это уже было исправлено — паритет восстановлен 11-09-2026.
        plus = bool(option_data.plus)
        if option_data.plus:
            check = await check_plus()
            if not check[0]:
                return MainResult(result, plus, False, check[1], check_cookies)
        if option_data.minus:
            check = await check_minus()
            if not check[0]:
                return MainResult(result, False, False, check[1], check_cookies)
    option_data.clear_data()
    return MainResult(result, plus, fall, bug_text, check_cookies)


def check_cookies_price(old_price: float, new_price: float, round_par: int, count: int) -> tuple[int, float]:
    """
    Проверка повторения цены
    :param old_price: старая цена
    :param new_price: новая цена
    :param round_par: параметр округления
    :param count: счетчик повторов
    :return:
    """
    # Порог — ПОЛТИКА последнего знака, а не целый тик (2026-08-15). С EPS = 10**-round_par
    # минимальное реальное движение (1.13933 → 1.13934) попадало в «не изменилась» всякий раз,
    # когда float-разница оказывалась чуть меньше тика: перебор 4866 пар соседних котировок
    # (round=5/3/2) дал 1938 ложных срабатываний — 40%. Они копились в count_price и дёргали
    # вторичный детект отвала кук (main.py: check_cookies > 2 → пересоздание браузера) на ровном
    # месте. Полтика ловит только настоящий ноль: тем же перебором 0 ложных и 0 пропущенных.
    EPS = 0.5 * 10 ** (-round_par)
    if abs(new_price - old_price) <= EPS:
        new_count = count + 1
    else:
        new_count = 0
    return new_count, new_price


async def mouse_move(page: Page, element_xpath: str, move: int) -> bool:
    """
    Эмулятор движения мыши
    :param page: страница браузера
    :param element_xpath: xpath элемента для движения
    :param move: тип движения (1 - большое, иначе маленькое)
    :return: True при успехе
    """
    if move == 1:
        mv = 200
    else:
        mv = 5
    try:
        element = page.locator(f"xpath={element_xpath}").first
        box = await element.bounding_box()
        if box:
            center_x = box['x'] + box['width'] / 2
            center_y = box['y'] + box['height'] / 2
            await page.mouse.move(center_x, center_y)
            await page.mouse.move(center_x + mv, center_y + mv)
            await page.mouse.move(center_x, center_y)
        return True
    except (Exception,) as error:
        logger.warning(f'Ошибка имитации движения мыши - {error}')
        return False


async def get_price(manager: "BrowserManager") -> tuple[bool, float | str]:
    """
    Получение цены
    :param manager: менеджер браузера
    :return: (success, price или error_message)
    """
    result = await find_price(manager)
    if result[0]:
        strprice = result[1]
        page = manager.pages['price']
        if not await mouse_move(page, move_field, 1):
            return False, 'Ошибка имитации движения мыши'
        try:
            price = clean_price(strprice, lang_from_url(page.url))
        except ValueError:
            return False, f'Не удалось распарсить цену из {strprice!r}'
        return True, price
    else:
        return False, result[1]


async def find_price(manager: "BrowserManager") -> tuple[bool, str]:
    """
    Поиск цены в браузере
    :param manager: менеджер браузера
    :return: (success, price_text или error_message)
    """
    try:
        page = manager.pages['price']
        await page.bring_to_front()

        # Закрытие popup, если есть
        await _close_popup(page)

        price_element = page.locator(f"xpath={price_field}").first
        # timeout — иначе при отсутствии элемента висим на дефолтных 30с
        price_text = await price_element.text_content(timeout=TIMEOUT_MEDIUM)
        return True, price_text or ""
    except (Exception,) as error:
        error_text = f"Не удалось загрузить цену - {error}"
        # при штатной остановке драйвер уже снесён — это не сбой, не шумим в error-канал
        if shutdown_requested():
            logger.warning(error_text)
        else:
            logger.error(error_text)
        return False, error_text


# Потолок на FIN-кадр целиком. У каждого шага свой таймаут, но дерево вложенности глубокое:
# close_dom_popups (несколько evaluate на попытку) сидит внутри get_price, а тот — внутри кадра,
# и в сумме худший случай уходит в минуты. У OTC-ветки такой потолок есть
# (otc_app.SHOT_TOTAL_BUDGET), у FIN не было. Промах стоит дорого: неудачный кадр в FIN уводит
# в рестарт (fall=True), поэтому берём с запасом над суммой внутренних ожиданий, а не впритык.
SCREENSHOT_TOTAL_TIMEOUT = 120   # сек


async def _screenshot_steps(manager: "BrowserManager", take_shot: bool, qr) -> tuple[bool, float | str]:
    """
    Шаги снятия кадра. Наружу — через screenshot() с общим потолком.
    :param manager: менеджер браузера
    :param take_shot: False — только цена без скриншота; True — снимаем скрин и кладём QR.
    :param qr: кортеж (qr110, qr85) — QR-оверлеи
    :return: (success, price или error_message)
    """
    try:
        price_result = await get_price(manager)
        if not price_result[0]:
            return False, price_result[1]

        if not take_shot:  # если требуется только цена без скриншота
            return True, price_result[1]

        # Грузится только окно main — все скрины снимаются с него.
        page = manager.pages['main']
        await page.bring_to_front()

        # Закрытие popup, если есть
        await _close_popup(page)

        if not await mouse_move(page, move_field, 0):
            return False, 'Ошибка имитации движения мыши'

        # Окна, накрывшие ЗОНУ КАДРА, — ПОСЛЕДНИМ шагом перед съёмкой. close_dom_popups выше
        # смотрит точку клика (кнопку поиска символа), а онбординг TV («Теперь можно перемещать
        # таблицы индикаторов…») висит над графиком и клику не мешает; появляется он ПОСЛЕ
        # загрузки индикаторов, поэтому чистка в начале функции успевала отработать раньше, чем
        # окно возникнет. Проверено на живом TV 11-09-2026.
        try:
            await clear_zone_overlays(page, screen_zone)
        except (Exception,):
            pass

        element = page.locator(f"xpath={screen_zone}").first
        # У Playwright screenshot нет встроенного таймаута — верхняя граница в shot (browser_io).
        await shot(element, path=shot_path)

        with Image.open(shot_path) as img:
            if qr:
                qr110, qr85 = qr
                paste_overlay(img, qr110, qr110_x, qr110_y)
                paste_overlay(img, qr85, qr85_x, qr85_y)
            img.save(screenshot_path)

        return True, price_result[1]
    except (Exception,) as error:
        error_text = f'Ошибка записи скриншота - {str(error)}'
        # при штатной остановке драйвер уже снесён — это не сбой (как в find_price)
        if shutdown_requested():
            logger.warning(error_text)
        else:
            logger.error(error_text)
        return False, error_text


async def screenshot(manager: "BrowserManager", take_shot: bool, qr) -> tuple[bool, float | str]:
    """Снятие скриншота с окна main под общим потолком SCREENSHOT_TOTAL_TIMEOUT.

    Пропустить кадр дешевле, чем держать цикл минутами на залипшем рендерере: прогноз с
    опозданием всё равно уже не прогноз, а вызывающий (main_app) сам решит судьбу итерации."""
    try:
        return await asyncio.wait_for(_screenshot_steps(manager, take_shot, qr),
                                      timeout=SCREENSHOT_TOTAL_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        error_text = (f'Кадр не уложился в {SCREENSHOT_TOTAL_TIMEOUT}с — '
                      f'пропускаю (TV не отвечает?)')
        logger.error(error_text)
        return False, error_text


async def find_point(manager: "BrowserManager", buy: bool) -> tuple[bool, str]:
    """
    Поиск точки входа
    :param manager: менеджер браузера
    :param buy: направление сигнала (True — покупка). ФЛАГОМ, а не текстом resume: источник
        истины один и тот же по всему коду, и правка текста (редактура, перевод) не развернёт
        ожидание цвета молча. Реестр BinoCore: direction-from-flag.
    :return: (success, error_message)
    """
    color = bull_color if buy else bear_color

    while_time = (datetime.now() + timedelta(minutes=find_time))
    page = manager.pages['price']
    await page.bring_to_front()
    price_element = page.locator(f"xpath={price_field}").first  # локатор постоянен — вне цикла

    # Выход только изнутри: нашли цвет (True), превысили лимит времени или ошибка (False).
    while True:
        try:
            # Проверка тайм-аута в начале итерации
            if datetime.now() > while_time:
                error_text = 'Время поиска точки входа превысило лимит'
                return False, error_text

            await mouse_move(page, price_field, 1)

            # Получаем цвет элемента через evaluate (с верхней границей — у evaluate нет встроенного таймаута)
            tp = await eval_js(price_element, "el => getComputedStyle(el).color")
            tp = str(tp)

            if color in tp:
                return True, ''

            # Пауза между проверками, чтобы не грузить CPU
            await asyncio.sleep(0.1)

        except (Exception,) as error:
            error_text = f'Ошибка определения входа в опцион - {str(error)}'
            return False, error_text


# Сколько пар пробуем за один опцион, прежде чем признать заход неудачным, и тормоз между
# ними. Три, а не одна: промах по одной паре — рутина (актив уехал из выдачи поиска TV,
# чужая помеха над строкой), и пропустить его дешевле, чем рестартить процесс. Пауза — та же
# защита от холостого цикла на мёртвом браузере, что INIT_FAIL_PAUSE у квизов.
FIN_PAIR_ATTEMPTS = 3
FIN_PAIR_FAIL_PAUSE = 5   # сек


async def find_option_data(manager: "BrowserManager", log_data: Option, used_val: list,
                           stop_event=None) -> bool:
    """
    Поиск данных для опциона
    :param manager: менеджер браузера
    :param used_val: список последних использованных валютных пар
    :param log_data: класс с данными
    :param stop_event: событие остановки — чтобы пауза между парами не держала SIGTERM
    :return: True — данные записаны в log_data (Option) и валюта выставлена в браузере;
             False — ни одна из пар не завелась. Вызывающий (main_app) пропускает опцион, а
             после FIN_INIT_MAX_FAILS подряд просит рестарт: столько промахов кряду по РАЗНЫМ
             активам означает, что не работает браузер, а не выдача TV.
    """
    active_binary_list = await database.option_data_tv(tf=log_data.find_timeframe, exclude_ids=used_val)
    if active_binary_list is False:  # сбой пула (контракт execute_query) — это отвал БД, НЕ «нет пар»
        await close_program(manager=manager, status=1,
                            text='Сбой БД при чтении пар (option_data_tv) — перезапуск')
        return False  # close_program вызывает sys.exit, но на всякий случай
    if not active_binary_list and used_val:
        # Второй проход, как у OTC: свежих пар не осталось (пул активных сузился до дедуп-окна) —
        # игнорируем окно и повторяем по ПОЛНОМУ списку. Без него выборка остаётся пустой НАВСЕГДА
        # до рестарта, как только активных пар станет не больше окна: БД исправна, браузер исправен,
        # диспетчер видит живой юнит, а постов нет. `used_val` в условии — чтобы на первом заходе
        # (окно пустое) не гонять тот же запрос дважды.
        logger.warning('FIN: свежих пар не осталось — игнорирую дедуп-окно, повторяю по полному списку')
        active_binary_list = await database.option_data_tv(
            tf=log_data.find_timeframe, exclude_ids=[]) or []
    if not active_binary_list:  # пустой список — реально нет валютных пар для опциона
        await close_program(manager=manager, status=1, text='Не найдено валютных пар для опциона')
        return False  # close_program вызывает sys.exit, но на всякий случай

    # Кандидаты: те же топ-3 по рангу, что и раньше, но теперь их МОЖНО перебрать. Пара,
    # которой нет в выдаче поиска TV, больше не убивает процесс — берём следующую (так же
    # ведёт себя OTC в parce_otc). Порядок случайный, то есть первая попытка равна прежнему
    # `random.randint(0, 2)`; список короче трёх — работаем с тем, что есть.
    candidates = active_binary_list[:FIN_PAIR_ATTEMPTS]
    random.shuffle(candidates)
    for attempt, pair_data in enumerate(candidates, 1):
        log_data.add_option_data(pair_data)
        if await init_valute_browser(manager, log_data.name.replace('/', ''), log_data.exchange):
            return True
        if attempt < len(candidates):
            # Тормоз перед следующей парой — на случай, когда браузер МЁРТВ: Playwright тогда
            # отбивает клики мгновенно, и перебор превратился бы в холостой цикл на сотни
            # оборотов в секунду (замер на квизах 16-09-2026: 506 об/с, error.log перетирался
            # за минуты, CPU в полке). Пока браузер жив, пауза теряется на фоне таймаутов.
            if await sleep_or_stop(stop_event, FIN_PAIR_FAIL_PAUSE):
                return False    # остановка сигналом во время паузы
    logger.error(f'FIN: ни одна из {len(candidates)} пар не завелась в браузере')
    return False


async def dop_plus_message():
    """Дополнительное сообщение для плюсов"""
    message_text = dop_plus10_message()
    ok, err = await send_photo_safe(PLUS_SERIES_IMAGE, message_text,
                                    mes_type='дополнительное сообщение плюсов')
    if ok:
        return True, ''
    return False, f"Ошибка отправки дополнительного сообщения плюсов - {err}"


async def time_sleep():
    """Случайная пауза между циклами main (§7: границы в .env MAIN_CYCLE_PAUSE_MIN/MAX,
    дефолты = историческому хардкоду 100/120; для OTC +30, как было)."""
    sleep_time = random.randint(main_cycle_pause_min, main_cycle_pause_max)
    if binary:
        return sleep_time
    else:
        return sleep_time + 30
