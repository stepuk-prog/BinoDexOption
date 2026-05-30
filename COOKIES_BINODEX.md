# Куки/сессия для BinoDex (Privy) — что нужно знать

> Заметка для того, кто будет подключать вход на **binodex.app** в `BinoOptions`.
> Коротко: текущий механизм загрузки кук для binodex **не подходит**, нужны правки. Ниже — почему и что делать.

## TL;DR

- binodex.app логинится через **Privy** (web3-провайдер, домен `privy.io`). Сессия хранится
  **в основном в localStorage**, а не только в cookies.
- Поэтому одних cookies (`context.add_cookies(...)`) для восстановления входа **недостаточно** —
  проверено: заход на `/trade` с одними куками редиректит на лендинг `https://binodex.app/`
  (страница для незалогиненных).
- Куки для binodex собираются отдельным хелпером `new_cookies.py` (проект `Helpers/CookiesProgram`)
  и сохраняются как **`storage_state`** (cookies + localStorage) в **`binodex.cookies.binodex_cookies`**.
- Текущий код `BinoOptions` умеет грузить только плоский `list[dict]` cookies и только через
  `add_cookies()`, и читает их из БД **`Program`**, а не из `binodex`. Так что нужны правки
  на обеих сторонах.

## Как сейчас устроена загрузка кук в BinoOptions

1. Читаются из БД `Program` (пул `'program'`), формат jsonb → `list[dict]` (codec `json.loads`):
   - `settings/config.py:74-80` — `SELECT cookies FROM cookies.tv_cookies / pocket_cookies`
   - codec: `settings/_bootstrap.py:26-28`, `database/postgres.py:42-44`
2. Применяются **только** через `add_cookies`:
   - `apps/cookie_utils.py:8-59` — `prepare_cookies_for_playwright()` + `add_cookies_to_context()`
   - вызовы: `apps/browser_app.py:296`, `apps/otc_app.py:380`
3. **localStorage нигде не восстанавливается** — нет `storage_state`, нет инъекции localStorage
   через `add_init_script` / `page.evaluate`.

## Почему это ломается для binodex

**Нюанс 1 — формат.** `binodex.cookies.binodex_cookies` хранит `storage_state` —
это словарь `{"cookies": [...], "origins": [...]}`, а не плоский список cookie-объектов.
Если скормить его текущему `add_cookies_to_context()`, то `for cookie in cookies` начнёт
итерировать по **ключам словаря** (строки `"cookies"`, `"origins"`), `"cookies".copy()` упадёт,
исключение проглотится (`apps/cookie_utils.py:57`) → `accepted=0`, вход не загрузится.

**Нюанс 2 — главное.** Даже правильный `list[dict]` cookies не залогинит binodex: Privy держит
токены в localStorage, а `add_cookies()` его не трогает. Cookie-only механизм для binodex
**принципиально недостаточен**.

**Нюанс 3 — источник.** Куки binodex лежат в БД **`binodex`** (`cookies.binodex_cookies`),
а `config.py` читает из БД **`Program`** (`cookies.tv_cookies` / `pocket_cookies`).
Ветки для binodex там сейчас нет.

## Что нужно сделать (рекомендуемый путь — через storage_state)

1. **Чтение состояния.** Добавить в `settings/config.py` ветку для binodex:
   читать из пула `'binodex'`: `SELECT cookies FROM cookies.binodex_cookies WHERE user_id = $1`.
   Значение — `dict` (`storage_state`), а не список. (Пул `'binodex'` уже используется,
   напр. `database.pages(... )` ходит в `binodex.cookies.pages`.)

2. **Восстановление состояния.** Контекст в `init_browser()` создаётся до получения кук
   (`apps/browser_app.py:234`), поэтому есть два варианта:
   - **Проще:** создавать контекст сразу с состоянием — `new_context(storage_state=state, ...)`.
     Playwright принимает обычный `dict` (проверено). Тогда `add_cookies` для binodex не нужен.
   - **Если контекст должен создаваться заранее:** до навигации инжектить localStorage из
     `state["origins"]` через `context.add_init_script(...)`, либо после `goto` сделать
     `page.evaluate` для записи в `localStorage` и `reload`. Cookies из `state["cookies"]`
     при этом — через обычный `add_cookies`.

3. **Не гонять storage_state через `prepare_cookies_for_playwright()`** — эта функция рассчитана
   на плоский список cookie-словарей.

## Запасной вариант, если storage_state не хватит

`storage_state` тащит cookies + localStorage, но **не IndexedDB**. Privy частично использует
IndexedDB. Если после перехода на `storage_state` вход всё равно слетает — следующий шаг:
сохранять/восстанавливать ещё и IndexedDB (через `page.evaluate` дамп/налив, или persistent
context с профилем). Но сначала проверьте `storage_state` — обычно Privy-токена в localStorage
достаточно.

## Где собираются куки

Хелпер: `Helpers/CookiesProgram/new_cookies.py`
- ручной вход (~100 сек) → `context.storage_state()` → запись в `binodex.cookies.binodex_cookies`
  (колонка `cookies` jsonb) + дамп `binodex_state.pkl`;
- встроенный чек: перечитывает состояние из БД, поднимает контекст через
  `new_context(storage_state=...)`, грузит `/trade` и считает вход активным, только если
  остались на `/trade` (а не редирект на `/`).

Структура таблицы `binodex.cookies.binodex_cookies` — аналог `Program.cookies.pocket_cookies`
(`user_id` PK, `cookies` jsonb, `updated_at`, `screen`, `option`, `bias`, `meta`), но **без**
FK на `telegram.telegram` (в БД `binodex` схемы `telegram` нет).
