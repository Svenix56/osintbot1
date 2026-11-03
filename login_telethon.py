import asyncio
import socket
import urllib.request
from telethon import TelegramClient

# Стандартные открытые ключи Telegram Desktop (ничего регистрировать не нужно)
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"


async def main():
    print("🔐 Авторизация поискового модуля Telethon (делается 1 раз)...")
    client = TelegramClient("osint_userbot", API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"✅ Успешно! Подключён аккаунт: {me.first_name} (@{me.username}) [ID: {me.id}]")
    print("Теперь файл osint_userbot.session создан. Можете запускать main.py!")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())