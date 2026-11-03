import asyncio
import os
import re
from playwright.async_api import async_playwright


async def main():
    print("🌐 Открываю официальный сайт ВКонтакте...")
    print("👉 Пожалуйста, войдите в свой аккаунт ВК в открывшемся окне (например, по QR-коду с телефона).")

    captured_token = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1150, "height": 800}, locale="ru-RU")
        page = await context.new_page()

        # Автоматический перехватчик токена vk1.a... из запросов самого сайта ВК
        def on_request(request):
            nonlocal captured_token
            if captured_token:
                return
            for source in (request.url, request.post_data or ""):
                m = re.search(r"access_token=(vk1\.a\.[a-zA-Z0-9_\-\.]+)", source)
                if m:
                    captured_token = m.group(1)
                    print(f"\n🔑 Перехвачен токен ВК из трафика: {captured_token[:25]}...")

        async def on_response(response):
            nonlocal captured_token
            if captured_token:
                return
            if any(k in response.url for k in ("web_token", "api.vk.", "login.vk.")):
                try:
                    txt = await response.text()
                    m = re.search(r'"access_token"\s*:\s*"(vk1\.a\.[a-zA-Z0-9_\-\.]+)"', txt)
                    if m:
                        captured_token = m.group(1)
                        print(f"\n🔑 Перехвачен токен ВК из ответа сервера: {captured_token[:25]}...")
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto("https://vk.com/")

        # Ждём до 3 минут, пока вы войдёте в аккаунт ВК
        for _ in range(180):
            await page.wait_for_timeout(1000)

            # Пробуем также достать токен из LocalStorage браузера после входа
            if not captured_token:
                try:
                    ls_dump = await page.evaluate("() => JSON.stringify(window.localStorage)")
                    m = re.search(r"(vk1\.a\.[a-zA-Z0-9_\-\.]{40,})", ls_dump)
                    if m:
                        captured_token = m.group(1)
                except Exception:
                    pass

            # Проверяем, вошёл ли пользователь (появилось ли меню профиля или токен)
            cur_url = page.url
            if captured_token or any(x in cur_url for x in ("/feed", "/im", "/friends", "/al_feed.php")):
                if not captured_token:
                    # Переходим на страницу поиска людей, чтобы ВК гарантированно сгенерировал web_token
                    await page.goto("https://vk.com/search?c[section]=people")
                    await page.wait_for_timeout(3000)
                    try:
                        ls_dump = await page.evaluate("() => JSON.stringify(window.localStorage)")
                        m = re.search(r"(vk1\.a\.[a-zA-Z0-9_\-\.]{40,})", ls_dump)
                        if m:
                            captured_token = m.group(1)
                    except Exception:
                        pass
                break

        # 1. Сохраняем полную сессию браузера (куки + LocalStorage) в файл vk_auth_state.json
        await context.storage_state(path="vk_auth_state.json")
        print("✅ Сессия браузера ВК сохранена в vk_auth_state.json!")

        # 2. Если перехватили токен vk1.a... — автоматически прописываем его в ваш файл .env!
        if captured_token and os.path.exists(".env"):
            with open(".env", "r", encoding="utf-8") as f:
                env_text = f.read()
            if "VK_SERVICE_TOKEN=" in env_text:
                env_text = re.sub(r"VK_SERVICE_TOKEN=.*", f"VK_SERVICE_TOKEN={captured_token}", env_text)
            else:
                env_text += f"\nVK_SERVICE_TOKEN={captured_token}\n"
            with open(".env", "w", encoding="utf-8") as f:
                f.write(env_text)
            print("✅ Токен VK_SERVICE_TOKEN автоматически прописан в файл .env!")

        await browser.close()
        print("\n🎉 ГОТОВО! Теперь перезапустите main.py — поиск по ВК полностью разблокирован!")


if __name__ == "__main__":
    asyncio.run(main())