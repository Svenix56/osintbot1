import asyncio
import logging
import socket
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database import init_db, cleanup_old_reports
from handlers_admin_payments import admin_payments_router
from handlers_osint import osint_router, queue_worker

logging.basicConfig(level=logging.INFO)


async def main():
    await init_db()
    await cleanup_old_reports()

    # Принудительно используем только IPv4 (убирает ошибку WinError 121 из-за IPv6 на Windows)
    # Укажите порт вашего локального VPN/прокси (например, 10808 для v2rayN или 2080 для NekoRay)
    session = AiohttpSession(timeout=60)
    session._connector_init["family"] = socket.AF_INET

    bot = Bot(token=settings.BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_payments_router)
    dp.include_router(osint_router)

    workers = [asyncio.create_task(queue_worker(bot)) for _ in range(3)]
    print("🚀 OSINT Бот успешно запущен! Откройте его в Telegram и нажмите /start")
    try:
        await dp.start_polling(bot, polling_timeout=30)
    finally:
        for w in workers:
            w.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())