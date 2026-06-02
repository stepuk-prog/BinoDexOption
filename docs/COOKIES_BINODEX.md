# Куки/сессия binodex.app (Privy) — как устроено

binodex.app логинится через **Privy** (web3-провайдер). Сессия хранится **в основном в
`localStorage`**, поэтому одних cookies недостаточно — нужен **`storage_state`** (cookies +
localStorage). Заход на `/trade` без storage_state редиректит на лендинг (Privy уводит
неавторизованных).

## Где лежат и как грузятся

- **Хранилище:** `binodex.cookies.binodex_cookies` — `user_id` (PK) + `cookies` (jsonb,
  это `storage_state` `{cookies:[…], origins:[…]}`) + `updated_at`. `user_id` берётся из
  `settings.option_setting.cookies_pocket`.
- **Загрузка на старте:** `settings/config.py` читает `storage_state` из БД через
  `bootstrap_fetch('binodex', …)`; контекст создаётся `new_context(storage_state=state, …)`
  в `apps/browser_app.init_browser`. На **каждом** init куки перечитываются из БД
  (`database.get_otc_cookies`) — пересоздание браузера подхватывает свежий refresh.
- Тест-оверрайд: env `COOK_OTC` (user_id в `binodex_cookies`) — подмена без правки БД.

## Детект отвала (см. lifecycle-standard §4.1/§4.1.1)

- **URL:** авторизация жива, если остались на `…/trade` (`on_trade`); редирект прочь = отвал.
- **UI-gate (§4.1.1):** даже на `/trade` SPA может зависнуть на сплеше (Privy-токен протух без
  редиректа) — на init ждём готовность торгового UI (кнопка выбора пары); не дождались →
  `CookiesExpired`.
- **WS-liveness (§4.4):** обрыв WS-фида котировок (`feed_dead`) — дополнительный сигнал.

## Авто-восстановление (Privy email-OTP)

При отвале OTC-кук бот перелогинивается сам (политика **Recover-3→Exit**, §4.3):

- **Воркер** `apps/binodex_session.py` — самодостаточный sync-скрипт без БД: получает на stdin
  `{mail, app_pass, selectors, do_setup}`, открывает binodex, вводит e-mail, читает 6-значный код
  по IMAP, при `do_setup` прокликивает настройку сайта, отдаёт `storage_state` в stdout.
- **Оркестратор** `apps/cookie_refresh.py` (async) — через asyncpg читает креды/селекторы,
  запускает воркер подпроцессом (`sys.executable`; sync-Playwright нельзя в asyncio-loop),
  пишет `storage_state` в БД (`database.save_otc_cookies`). `main._recover_otc_cookies` делает
  до 3 попыток с алертами «отвалились / восстановлены / не восстановить для {name}».
- **Креды почты** — `telegram.telegram`: `mail` + `mail_app_pass` (16-символьный **Gmail
  app-password**, требует 2FA; обычный пароль для IMAP не годится). Имя владельца — `name`.
- **Селекторы** входа/настройки — `binodex.settings.binodex_settings` (`login_*` / `setup_*`),
  читаются по `par_name`.
- **Гочи:** Privy шлёт код с ДВУХ адресов (`no-reply@privy.io` и `no-reply@mail.privy.io`) —
  фильтр по домену `privy.io`; отправка e-mail надёжнее через `Enter`, не по кнопке; после входа
  письма Privy удаляются (Gmail Trash), чтобы ящик не засорялся.

## Ручной сбор (резерв)

`Helpers/CookiesProgram/new_cookies.py` — ручной вход в браузере → `context.storage_state()` →
запись в `binodex.cookies.binodex_cookies`. Используется, когда нужен холодный сбор без авто-флоу.

## Запасной вариант, если storage_state не хватит

`storage_state` тащит cookies + localStorage, но **не IndexedDB**. Privy частично использует
IndexedDB. Если после восстановления вход всё равно слетает — следующий шаг: сохранять/наливать
IndexedDB (`page.evaluate` дамп/restore) либо persistent context с профилем. Обычно
Privy-токена в localStorage достаточно.
