"""Фатальный отказ НА СТАРТЕ: осмысленный код выхода + сообщение оператору.

Зачем отдельный модуль. Конфиг и селекторы читаются на import-time — до того, как поднят
event loop. Обычный путь алертов (`logs.log_init.TelegramBotHandler`) в этот момент не
работает по построению: `emit` кладёт отправку в `asyncio.create_task`, а лупа ещё нет, и
запись молча остаётся только в файле. То есть ЛЮБОЙ отказ конфигурации (не задан ключ .env,
пропала строка селектора, разъехался id бота между таблицами) был для оператора невидим:
процесс падал с кодом 1, диспетчер поднимал его заново, тот снова падал — и так по кругу,
без единого сообщения.

Здесь — синхронная отправка через `urllib` (stdlib, лупа не нужна) плюс дозапись в
`logs/error.log`, и осмысленный код выхода: `EXIT_SETUP` (12) = «нужен человек», а не `1`
(«краш, рестартани меня»), потому что сам по себе такой отказ не рассосётся.

Жить это обязано в `settings/`, а не в `logs/`: импорт `logs.fatal` выполнил бы
`logs/__init__.py` → `log_init` → `settings.config`, то есть круг ровно с тем модулем,
который отсюда и падает. Зависимости — stdlib, `settings.constant` и `dotenv`: последний
нужен ради `load_dotenv`, потому что модуль обязан работать и когда его позвали РАНЬШЕ
`settings.config` (например, из `settings.env` на первом же чтении .env-ключа).
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import NoReturn

from dotenv import load_dotenv

from settings.constant import EXIT_SETUP

# Корень проекта (settings/fatal.py → ..). И .env, и каталог логов якорим ОТСЮДА, а не от
# cwd: под systemd рабочая директория своя, а у тула/диагностики — любая, и cwd-относительные
# пути тогда читают чужой .env и пишут лог мимо. Тот же приём в logs/log_init.py (_LOG_DIR),
# settings/constant.py и settings/env.py.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_LOGS = _ROOT / 'logs'

load_dotenv(_ROOT / '.env', override=False)   # идемпотентно: .env мог быть загружен вызывающим

_TG_API = 'https://api.telegram.org/bot{token}/sendMessage'
_SEND_TIMEOUT = 10        # сек: мёртвая сеть не должна ещё и подвешивать падающий старт
_TG_TEXT_LIMIT = 3900     # с запасом под лимит sendMessage (4096 единиц UTF-16)


def _write_log(text: str) -> None:
    """Дозаписать в error.log и stderr. Без logging: его хендлеры могут быть ещё не подняты
    (падение случается посреди импорта settings), а запись нужна гарантированно."""
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} ФАТАЛЬНО (старт): {text}'
    print(line, file=sys.stderr)
    try:
        with open(_LOGS / 'error.log', 'a', encoding='utf8') as handle:
            handle.write(line + '\n')
    except (Exception,):
        pass   # нет каталога/прав — stderr уже написан, в journald запись попадёт


def _as_int(name: str) -> int:
    """Числовой ключ адреса из ENV; пусто/нет/не число → 0 («не задан»).

    Своя функция, а НЕ settings.env.env_int: тот на нечисловом значении зовёт fatal_exit, то
    есть нас же — рекурсия внутри обработчика падения. Здесь любой мусор молча становится
    нулём: задача модуля — доставить сообщение по адресу, который читается, а не отвалидировать
    .env (его валидирует config, и отказ оттуда мы как раз и несём)."""
    try:
        return int(os.getenv(name) or 0)
    except ValueError:
        return 0


def _error_topic_key() -> str:
    """Имя ключа темы ошибок: у этой программы ПЯТЬ инстансов из одной папки, и FIN с OTC
    пишут в РАЗНЫЕ темы одного форума (ERROR_TOPIC_FIN / ERROR_TOPIC_OTC) — ровно как выбирает
    settings/logger_config.py. Режим берём из ENV тем же parse_bool-правилом (1/true/yes/on),
    а не из конфига: конфиг — тот самый модуль, который в этот момент падает."""
    binary = (os.getenv('BINARY') or '').strip().lower() in ('1', 'true', 'yes', 'on')
    return 'ERROR_TOPIC_FIN' if binary else 'ERROR_TOPIC_OTC'


def _send_telegram(text: str) -> None:
    """Синхронно отправить оператору. Адрес берём ИЗ ENV напрямую, а не из settings.config:
    он и есть тот модуль, который в этот момент падает.

    Ветвление форум/канал повторяет logger_config.error_dest: тема форума, если заданы ОБА
    ключа, иначе плоский канал. Сравнение обязано быть ЧИСЛОВЫМ (_as_int), как там env_int:
    os.getenv отдаёт СТРОКИ, а строка '0' истинна. На ERROR_TOPIC=0 — ровно это предлагает
    .env.example, и ровно так выглядит свежий деплой семьи — строковая проверка увела бы алерт
    в форум с thread_id=0, Telegram ответил бы ошибкой, та легла бы в лог, и отказ старта снова
    остался бы невидимым: то есть отказал бы ровно тот сценарий, ради которого модуль написан.
    Логгер в этой же конфигурации выбирает плоский канал — расходиться с ним нельзя."""
    # Фолбэк на TOKEN — тот же бот-логгер, что шлёт остальные алерты программы. Отдельный
    # ERROR_TOKEN есть не везде: у этой программы и у английской пары его в .env НЕ БЫЛО вовсе
    # (ревизия 17-09-2026, п.1.1), и модуль молча возвращался — то есть отказ конфигурации
    # уходил кодом EXIT_SETUP=12, который у диспетчера OPERATOR_ONLY: ни релокации, ни
    # перезапуска, ни сообщения оператору, след только в error.log на ноде. Отказывал ровно
    # тот сценарий, ради которого модуль и написан. Фолбэк в КОДЕ, а не строка в .env:
    # иначе те же грабли повторятся на следующем деплое семьи.
    token = os.getenv('ERROR_TOKEN') or os.getenv('TOKEN')
    if not token:
        return
    forum, topic = _as_int('ERROR_FORUM'), _as_int(_error_topic_key())
    chat_id = forum if (forum and topic) else _as_int('ERROR_CHANNEL')
    if not chat_id:
        return
    payload = {'chat_id': chat_id, 'text': text[:_TG_TEXT_LIMIT]}
    if forum and topic:
        payload['message_thread_id'] = topic   # int: в Bot API поле объявлено Integer
    request = urllib.request.Request(
        _TG_API.format(token=token),
        data=json.dumps(payload).encode('utf8'),
        headers={'Content-Type': 'application/json'},
    )
    try:
        urllib.request.urlopen(request, timeout=_SEND_TIMEOUT).close()
    except (Exception,) as error:
        _write_log(f'сообщение о фатальном старте не ушло в Telegram: {error}')


def notify_fatal(text: str) -> None:
    """Записать отказ в error.log/stderr и синхронно отправить оператору. Не завершает
    процесс — это делает вызывающий (обычно через `fatal_exit`)."""
    _write_log(text)
    _send_telegram(f'‼️ {os.getenv("PROG_KEY", "BinoOptions")} не стартовал\n\n{text}')


def fatal_exit(text: str) -> NoReturn:
    """Сообщить и выйти с кодом EXIT_SETUP (12) — «нужен человек».

    Код важен: раньше такие отказы выходили с `1` («непредвиденный краш»), и диспетчер
    честно поднимал процесс заново — снова и снова, хотя ни один из них не мог поправиться
    сам. 12 говорит диспетчеру, что ждать бессмысленно и нужно вмешательство.

    Тип возврата — NoReturn, и это не украшение. Вызывающие стоят на путях, где после
    fatal_exit кода уже нет: `except ...: fatal_exit(...)` перед чтением переменной,
    `find_par`, возвращающий str. С `-> None` проверяющий считает, что функция может
    вернуться, и честно ругается «может быть не определено» / «может вернуть None».
    NoReturn убирает это и заодно фиксирует контракт: продолжения после вызова не бывает.
    """
    notify_fatal(text)
    raise SystemExit(EXIT_SETUP)
