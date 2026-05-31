import asyncio
import signal
import sys
from datetime import datetime, timedelta

from pyrogram.errors import Unauthorized

from apps.app import get_water, time_sleep, request_shutdown
from apps.browser_app import init_load
from apps.exit_app import close_program, session_dead_shutdown
from apps.main_app import main
from logs import init_logger
from messages import weekend_message, start_message
from settings.config import get_app, channel_id, binary, database, program_id
from settings.timing import USERBOT_RETRY_DELAY, TG_SEND_TIMEOUT

logger = init_logger(__name__)


async def bot():
    """Запуск бота"""
    logger.report('🚀 Стартую')

    # Поднимаем пулы БД (program + binodex) до первого запроса. Раньше get_app/
    # session_dead_shutdown — последний пишет close_program в БД при отвале юзербота.
    await database.connect()

    # Создаём Pyrogram Client внутри event loop
    app = get_app()

    # Запуск юзербота. Недействительная session → штатный стоп с записью в БД и алертом,
    # без перезапуска (пока не обновят данные). Прочие сбои — до 5 попыток переподключения.
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            await app.start()
            break
        except Unauthorized as error:
            await session_dead_shutdown(error)  # делает sys.exit(0); return — страховка
            return
        except (Exception,) as error:
            logger.warning(f"Попытка {attempt}/{attempts} запуска юзербота не удалась: {error}")
            try:
                if getattr(app, "is_connected", False):
                    await app.stop()
            except (Exception,):
                pass
            if attempt < attempts:
                await asyncio.sleep(USERBOT_RETRY_DELAY)
            else:
                logger.error(f"Юзербот не подключился после {attempts} попыток: {error}")
                sys.exit(1)

    if binary:
        now = datetime.now()  # один снимок времени — иначе возможен переход минуты/часа между вызовами
        if now.isoweekday() == 1 and now.hour == 3 and now.minute < 25:
            try:
                await asyncio.wait_for(
                    app.send_photo(chat_id=channel_id, photo='pictures/start_week.png', caption=start_message()),
                    timeout=TG_SEND_TIMEOUT)
            except (Exception,) as error:
                logger.error(f'Ошибка отправки стартового сообщения - {error}')
        if (now + timedelta(hours=2)).weekday() >= 5:
            try:
                await asyncio.wait_for(
                    app.send_photo(chat_id=channel_id, photo='pictures/end_week.png', caption=weekend_message()),
                    timeout=TG_SEND_TIMEOUT)
            except (Exception,) as error:
                logger.error(f'Ошибка отправки сообщения о выходных - {error}')
            await database.close_program(program_id=program_id)
            await close_program(manager=None, status=0, text='Закрываюсь 🔱 (выходные)')
            return

    water_naked = get_water()
    qr = water_naked[1] if water_naked[0] else None

    manager = await init_load()
    if not manager:
        logger.error("Перезагрузка бота - не загрузился драйвер")
        sys.exit(1)

    logger.info("✅ Браузер инициализирован, страницы: %s", list(manager.pages.keys()))
    logger.info("🔄 Переход в main loop...")

    # Graceful shutdown по SIGTERM/SIGINT (systemctl stop / диспетчер) — как в
    # примере, но async-вариант: signal.signal+KeyboardInterrupt в asyncio не
    # ловится внутри корутины, поэтому через loop.add_signal_handler + Event.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_stop_signal():
        stop_event.set()
        request_shutdown()  # подавить main_bug_message — это штатная остановка, не сбой

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            # *args у add_signal_handler опционален — ложное срабатывание инспекции
            # noinspection PyArgumentList
            loop.add_signal_handler(_sig, _on_stop_signal)
        except NotImplementedError:
            pass  # Windows — graceful по сигналам недоступен

    while not stop_event.is_set():
        res_option = await main(manager=manager, qr=qr, stop_event=stop_event)
        # OTC: подозрение на отвал cookies (одинаковая цена N раз подряд) → перезагрузка
        if not binary and res_option[4] > 2:
            await close_program(manager=manager, status=1, text='Подозрение на отвал cookies')
            return  # close_program делает sys.exit; явный выход (правило 9)

        # Остановка по сигналу (SIGTERM/SIGINT): ошибка из-за гибели Playwright-драйвера —
        # это штатный стоп, не сбой; уходим в graceful-ветку ниже (status=false).
        if stop_event.is_set():
            break

        if not res_option[0] and res_option[2]:
            await app.stop()
            await close_program(manager=manager, status=1, text=f'Перезагрузка бота ☄️. Ошибка - {res_option[3]}')
            return  # close_program делает sys.exit; явный выход (правило 9)

        # Прерываемый сон: проснёмся сразу при сигнале остановки
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=await time_sleep())
        except asyncio.TimeoutError:
            pass

        if binary and not stop_event.is_set():
            if (datetime.now() + timedelta(hours=2)).weekday() >= 5:
                if not res_option[1]:
                    # Прерываемый сон (как выше) — иначе SIGTERM завис бы тут на 100–150с
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=await time_sleep())
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    await asyncio.wait_for(
                        app.send_photo(chat_id=channel_id, photo='pictures/end_week.png', caption=weekend_message()),
                        timeout=TG_SEND_TIMEOUT)
                except (Exception,) as error:
                    logger.error(f'Ошибка отправки сообщения о выходных - {error}')
                await app.stop()
                await database.close_program(program_id=program_id)
                await close_program(manager=manager, status=0, text='Закрываюсь 🔱')
                return

    # Сюда — только по SIGTERM/SIGINT: помечаем программу остановленной (status=false)
    # и чисто закрываемся. Единственное сообщение о закрытии шлёт close_program(text=...) ниже.
    try:
        await app.stop()
    except (Exception,):
        pass
    await database.close_program(program_id=program_id)
    await close_program(manager=manager, status=0, text='Остановлен сигналом 🛑')


if __name__ == "__main__":
    asyncio.run(bot())