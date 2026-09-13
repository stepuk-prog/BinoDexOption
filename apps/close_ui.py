"""Единый словарь признаков «кнопка закрытия / отказа» для ВСЕХ чисток интерфейса TV.

Было пять независимых реализаций одной задачи, у каждой свой список слов и селекторов:
`_DISMISS_JS` и `_ZONE_CLEAR_JS` (apps/browser_app), `_sweep_close_buttons` (там же),
`closeSelectors` внутри `TV_POPUP_SUPPRESS_JS` и `TV_DISMISS_PROMO_JS` (apps/browser_session).
Правка одного списка три остальных не чинила — а найденный признак новой закрывашки нужен
СРАЗУ всем: они закрывают одни и те же окна, просто с разных сторон (точка клика, зона кадра,
страница без ориентира, MutationObserver, промо-апселл).

⚠️ Слова — И русские, И английские. Списки исторически были только русскими, и в английской
ветке (EnglishBinoOptions) из подписей не срабатывало НИ ОДНО слово — работали только
атрибутные ветки. Здесь UI обычно русский, но язык зависит от АККАУНТА в куках, а не от ветки
кода: сменили куки — и половина чисток замолчала бы. Держим оба набора в обеих ветках.

Подстановка в JS — через `with_close_config()`: плейсхолдеры вида `__CLOSE_WORDS__` меняются
на JSON-литералы. Аргументом evaluate это не передаём намеренно — один и тот же текст нужен и
для `page.evaluate`, и для `add_init_script` (там аргументов нет вовсе).
"""
import json

# Подписи кнопок целиком, в нижнем регистре (сравнение — точным совпадением текста кнопки).
CLOSE_WORDS = [
    'закрыть', 'close',
    'понятно', 'got it', 'ok', 'ок', 'okay',
    'не сейчас', 'not now', 'позже', 'later', 'maybe later',
    'отмена', 'cancel', 'пропустить', 'skip', 'dismiss',
    'нет, спасибо', 'no thanks', 'no, thanks',
]

# Атрибутные признаки: хеши классов TV крутит на каждой выкатке, а data-name/aria-label/title —
# нет. Подстроки задаются ОДНИМ списком и раскладываются в две формы: CSS-селекторы (там, где
# элемент ещё надо выбрать) и регулярку (там, где он уже найден и проверяется его атрибут —
# _ZONE_CLEAR_JS). Раньше вторая форма была захардкожена в JS, и новый маркер, добавленный
# сюда, до чистки зоны кадра не доходил — ровно та болезнь, ради которой заводился этот модуль.
CLOSE_ATTR_MARKERS = ['close', 'закр']

CLOSE_ATTR_SELECTORS = [
    # `i` в конце — CSS-флаг регистронезависимости. data-name без префикса button: его вешают
    # и на div'ы, остальные признаки на практике только у кнопок.
    *[f'[data-name*="{m}" i]' for m in CLOSE_ATTR_MARKERS],
    *[f'button[aria-label*="{m}" i]' for m in CLOSE_ATTR_MARKERS],
    *[f'button[title*="{m}" i]' for m in CLOSE_ATTR_MARKERS],
    *[f'button[class*="{m}" i]' for m in CLOSE_ATTR_MARKERS],
    # Отдельной строкой: это не «признак закрывашки», а конкретное имя класса TV.
    'button[class*="navButton-"]',
]

# Та же пара подстрок для JS-регулярки. Маркеры не экранируем намеренно — список наш, в нём
# только буквы; появится метасимвол — экранировать здесь, а не по местам использования.
CLOSE_ATTR_PATTERN = '|'.join(CLOSE_ATTR_MARKERS)

# Для «слепой» чистки — там, где точки-ориентира нет (страница без известного селектора) и
# внутри MutationObserver'а: ищем крестик по тем же признакам, но без разбора геометрии.
CLOSE_SWEEP_SELECTORS = [
    'button[class*="closeButton" i]', 'button[class*="close-button" i]',
    '[data-name*="close" i]',
    '[aria-label*="close" i]', '[aria-label*="закр" i]',
]

# Промо-апселл TV: у него нет крестика, закрывает ВТОРИЧНАЯ кнопка («Отклонить предложение»).
# Первым делом ищем её по классу secondary, это надёжнее текста; регулярка — фолбэк.
DECLINE_PATTERN = (r'отклон|отказ|не сейчас|нет,? спасибо|закрыть|'
                   r'decline|no,? thanks|not now|maybe later|dismiss|close')


def with_close_config(js: str) -> str:
    """Подставить словарь в текст JS. Плейсхолдеры: __CLOSE_WORDS__,
    __CLOSE_ATTR_SELECTORS__, __CLOSE_ATTR_PATTERN__, __CLOSE_SWEEP_SELECTORS__,
    __DECLINE_PATTERN__."""
    return (js
            .replace('__CLOSE_WORDS__', json.dumps(CLOSE_WORDS, ensure_ascii=False))
            .replace('__CLOSE_ATTR_SELECTORS__', json.dumps(CLOSE_ATTR_SELECTORS, ensure_ascii=False))
            .replace('__CLOSE_ATTR_PATTERN__', CLOSE_ATTR_PATTERN)
            .replace('__CLOSE_SWEEP_SELECTORS__', json.dumps(CLOSE_SWEEP_SELECTORS, ensure_ascii=False))
            .replace('__DECLINE_PATTERN__', DECLINE_PATTERN))
