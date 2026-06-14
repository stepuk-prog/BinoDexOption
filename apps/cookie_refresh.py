
"""Авто-рефреш OTC-кук binodex: оркестрация (async, в боте).

Бот собирает креды+селекторы через asyncpg, запускает DB-free воркер (apps/binodex_session.py)
ПОДПРОЦЕССОМ (sync-Playwright нельзя в asyncio-loop), получает storage_state из stdout и пишет
его в БД через asyncpg. Так в проекте один драйвер БД (asyncpg), без sync-зависимости.
"""
import asyncio
import json
import sys

from logs import init_logger
from settings.config import database, refresher_worker, cookies_pocket_id

logger = init_logger(__name__)

WORKER_TIMEOUT = 200   # сек на воркер (Playwright-логин + ожидание кода с почты)


async def refresh_otc_cookies(user_id: int | None = None, do_setup: bool = False) -> bool:
    """Перевыпустить storage_state binodex и записать в БД. user_id — владелец кук
    (по умолчанию cookies_pocket_id экземпляра). do_setup=True — заодно прокликать настройку сайта.
    :return: True — куки обновлены в БД; False — любой сбой (залогирован)."""
    user_id = user_id if user_id is not None else cookies_pocket_id

    creds = await database.get_mail_creds(user_id)
    if not creds or not creds['mail'] or not creds['mail_app_pass']:
        logger.error(f'Нет mail/app-password для id={user_id} — авто-рефреш невозможен')
        return False
    rows = await database.binodex_selectors()
    if not rows:
        logger.error('Нет селекторов в binodex_settings — авто-рефреш невозможен')
        return False

    payload = json.dumps({
        'mail': creds['mail'],
        'app_pass': creds['mail_app_pass'],
        'selectors': {r['par_name']: r['par_value'] for r in rows},
        'do_setup': do_setup,
    }).encode()

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, refresher_worker,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except (Exception,) as error:
        logger.error(f'Не удалось запустить воркер рефреша кук: {error}')
        return False

    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout=WORKER_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error('Воркер рефреша кук не уложился в таймаут')
            return False

        if proc.returncode != 0:
            logger.error(f'Воркер рефреша кук вернул код {proc.returncode}: '
                         f'{err.decode(errors="ignore").strip()[:300]}')
            return False

        try:
            storage_state = json.loads(out)['storage_state']
        except (Exception,) as error:
            logger.error(f'Воркер рефреша кук вернул некорректный результат: {error}')
            return False

        if await database.save_otc_cookies(user_id, storage_state) is False:
            logger.error('Не удалось записать свежий storage_state в БД')
            return False
        return True
    finally:
        # На ЛЮБОМ незавершённом исходе (таймаут / отмена по SIGTERM / исключение) — не оставить
        # воркер сиротой/зомби. wait() с потолком: D-state лучше дожать в зомби, чем висеть вечно.
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (Exception,):
                pass