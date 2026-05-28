import asyncio
import sys
from datetime import datetime, timedelta

from apps.app import get_water, time_sleep
from apps.browser_app import init_load
from apps.exit_app import close_program
from apps.main_app import main
from logs import init_logger
from messages import weekend_message, start_message
from settings.config import get_app, channel_id, binary, database, program_id

logger = init_logger(__name__)


async def bot():
    """Запуск бота"""
    logger.report('🚀 Стартую')

    # Создаём Pyrogram Client внутри event loop
    app = get_app()

    try:
        await app.start()
    except (Exception,) as error:
        logger.error(f"Не прогрузился юзер-бот. Останавливаюсь и жду решения проблемы. Ошибка {error}")
        sys.exit(0)

    if binary:
        if datetime.now().isoweekday() == 1 and datetime.now().hour == 3 and datetime.now().minute < 25:
            try:
                await app.send_message(chat_id=channel_id, text=start_message())
            except (Exception,) as error:
                logger.error(f'Ошибка отправки стартового сообщения - {error}')
        if datetime.weekday(datetime.now() + timedelta(hours=2)) >= 5:
            await app.send_message(chat_id=channel_id, text=weekend_message())
            logger.report('Закрываюсь 🔱')
            database.close_program(program_id=program_id)
            await app.stop()
            return

    water_naked = get_water()
    if not water_naked[0]:
        water = None
        qr = None
        water_otc = None
    else:
        water = water_naked[1]
        qr = water_naked[3]
        water_otc = water_naked[4]

    manager = await init_load()
    if not manager:
        logger.error("Перезагрузка бота - не загрузился драйвер")
        sys.exit(1)

    logger.info("✅ Браузер инициализирован, страницы: %s", list(manager.pages.keys()))
    logger.info("🔄 Переход в main loop...")

    while True:
        if binary:
            res_option = await main(manager=manager, water=water, qr=qr)
        else:
            res_option = await main(manager=manager, water=water_otc, qr=qr)
            if res_option[4] > 2:
                await close_program(manager=manager, status=1, text='Подозрение на отвал cookies')

        if not res_option[0] and res_option[2]:
            await app.stop()
            await close_program(manager=manager, status=1, text=f'Перезагрузка бота ☄️. Ошибка - {res_option[3]}')

        await asyncio.sleep(await time_sleep())

        if binary:
            if datetime.weekday(datetime.now() + timedelta(hours=2)) >= 5:
                if not res_option[1]:
                    await asyncio.sleep(await time_sleep())
                    continue
                await app.send_message(chat_id=channel_id, text=weekend_message())
                await app.stop()
                database.close_program(program_id=program_id)
                await close_program(manager=manager, status=0, text='Закрываюсь 🔱')
                return


if __name__ == "__main__":
    asyncio.run(bot())