import asyncio
import hashlib
import html as html_mod
import json
import os
import random
import re
import socket
import sys
import warnings
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import quote_plus, unquote

# Убираем предупреждение о переименовании duckduckgo_search -> ddgs
warnings.filterwarnings("ignore", category=RuntimeWarning)

import aiohttp
import dns.resolver
import exifread
import phonenumbers
from bs4 import BeautifulSoup
from phonenumbers import carrier, geocoder, timezone as pn_timezone

from config import settings, detect_local_proxy
from database import is_target_blocked
from validators import TargetType, normalize_car_plate

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

HAS_DDGS = False
try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        HAS_DDGS = True
    except ImportError:
        pass

try:
    from telethon import TelegramClient
    from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.types import InputPhoneContact
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False

TG_API_ID = 2040
TG_API_HASH = "b18441a1ff607e10a989891a5462e627"
TELETHON_SESSION_FILE = "osint_userbot.session"
_telethon_lock = asyncio.Lock()
_pw_lock = asyncio.Lock()

ANSI_ESCAPE_RE = re.compile(r"(?:\x1B[@-_][0-?]*[ -/]*[@-~]|\[\d+(?:;\d+)*m)")
CLI_JUNK_PHRASES = (
    "twitter :", "github :", "for btc donations", "palenath", "megadose",
    "email used,", "phone number used,", "[-]", "[x]", "****", "----",
    "website", "rate limit", "1fhdm49qfzx6pjmhjle5tb2k6catlmzpxz"
)

EXTRACT_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-\(]*9\d{2}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EXTRACT_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,6}")
EXTRACT_IP_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")

# Ищем только конечные ссылки на профиль человека (без вложенных /artist/123 или /wall/123!)
EXTRACT_TG_LINK_RE = re.compile(r"t\.me/([a-zA-Z][a-zA-Z0-9_]{3,30})(?![a-zA-Z0-9_/])")
EXTRACT_PLAIN_AT_NICK_RE = re.compile(r"(?<![a-zA-Z0-9_.+-])@([a-zA-Z][a-zA-Z0-9_]{3,30})\b")
EXTRACT_VK_SLUG_RE = re.compile(r"vk\.(?:com|ru)/([a-zA-Z0-9_\.]{3,30})(?![a-zA-Z0-9_/])")
EXTRACT_OTHER_NICK_RE = re.compile(
    r"(?:instagram\.com/|tiktok\.com/@|github\.com/|pikabu\.ru/@|habr\.com/ru/users/|steamcommunity\.com/id/|drive2\.ru/users/)([a-zA-Z0-9_\.-]{3,30})(?![a-zA-Z0-9_/])"
)

# Все системные разделы ВК, группы и служебные слова, которые НЕ являются пользователями
STOP_WORDS_SLUGS = {
    "artist", "audio", "music", "podcasts", "clips", "video", "videos", "wall", "login",
    "feed", "friends", "albums", "album", "photos", "photo", "public", "club", "event",
    "groups", "group", "page", "pages", "app", "apps", "market", "topic", "board",
    "docs", "doc", "note", "notes", "tag", "tags", "people", "communities", "catalog",
    "share.php", "foaf.php", "badbrowser.php", "images", "joinchat", "share",
    "addstickers", "proxy", "iv", "s", "contact", "support", "about", "privacy", "search",
    "legal", "terms", "help", "settings", "restore", "away.php", "al_profile.php", "palenath",
    "media", "import", "keyframes", "supports", "charset", "font-face", "layer",
    "gmail", "mail", "yandex", "icloud", "rambler", "outlook", "hotmail", "inbox", "list", "bk"
}

# Признаки того, что страница ВК — это группа/паблик/заглушка, а НЕ человек
VK_NON_USER_TITLES = (
    "404", "вход", "быстрый вход", "ошибка", "bad browser", "добро пожаловать",
    "у вас отключен", "частное сообщество", "сообщество", "открытая группа",
    "закрытая группа", "публичная страница", "музыка вконтакте", "видео вконтакте",
    "клипы вконтакте", "запись на стене", "стена вконтакте", "каталог пользователей"
)


def clean_cli_output(raw_text: str) -> str:
    no_ansi = ANSI_ESCAPE_RE.sub("", raw_text)
    clean_lines = []
    for line in no_ansi.splitlines():
        s = line.strip()
        if not s:
            continue
        s_low = s.lower()
        if any(junk in s_low for junk in CLI_JUNK_PHRASES):
            continue
        if s.startswith("[+]") or "http" in s_low or "найдено" in s_low:
            clean_lines.append(s)
    return "\n".join(clean_lines) if clean_lines else "Совпадений не обнаружено."


async def run_cli(args: list[str], timeout: int = 55) -> str:
    for cmd in (args, [sys.executable, "-m", *args]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="ignore").strip()
            if out:
                return clean_cli_output(out)
        except Exception:
            continue
    return "Совпадений не обнаружено."


class OSINTEngine:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=18)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        }
        self._pw = None
        self._browser = None

    async def _get_playwright_browser(self):
        if not HAS_PLAYWRIGHT:
            return None
        async with _pw_lock:
            if self._browser is None:
                try:
                    self._pw = await async_playwright().start()
                    self._browser = await self._pw.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                    )
                    print("🌐 [Playwright] Фоновый браузер Chromium успешно запущен!")
                except Exception as e:
                    print(f"⚠️ [Playwright Launch Error]: {e}")
                    return None
            return self._browser

    async def fetch_with_playwright(self, url: str, wait_ms: int = 2000) -> tuple[str, str]:
        """Открывает страницу в Chromium (подхватывает vk_auth_state.json, если вы вошли через login_vk.py)."""
        browser = await self._get_playwright_browser()
        if not browser:
            return "", ""
        context = None
        try:
            ctx_kwargs = {
                "user_agent": self.headers["User-Agent"],
                "locale": "ru-RU",
                "viewport": {"width": 1280, "height": 800}
            }
            if os.path.exists("vk_auth_state.json"):
                ctx_kwargs["storage_state"] = "vk_auth_state.json"

            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(wait_ms)
            title = await page.title()
            content = await page.content()
            return title, content
        except Exception as e:
            print(f"[Playwright Page Error] {url}: {e}")
            return "", ""
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    @staticmethod
    def _decode_bytes(raw_b: bytes) -> str:
        try:
            txt = raw_b.decode("utf-8")
            if "windows-1251" in txt.lower():
                return raw_b.decode("cp1251", errors="ignore")
            return txt
        except UnicodeDecodeError:
            return raw_b.decode("cp1251", errors="ignore")

    async def fetch_url_text(self, url: str, session: aiohttp.ClientSession, custom_ua: str | None = None) -> str:
        headers = {"User-Agent": custom_ua or self.headers["User-Agent"], "Accept-Language": "ru-RU,ru;q=0.9"}
        try:
            async with session.get(url, headers=headers, allow_redirects=True, ssl=False) as r:
                if r.status == 200:
                    return self._decode_bytes(await r.read())
        except Exception:
            pass
        return ""

    # ==================== ПОИСКОВИК (DDGS + PLAYWRIGHT BING) ====================

    async def web_search_clean(self, query: str, session: aiohttp.ClientSession, max_results: int = 8) -> list[dict]:
        results = []

        if HAS_DDGS:
            for p_val in [None]:
                try:
                    def _do_ddg(p):
                        with DDGS(proxy=p, timeout=10) as ddgs:
                            return list(ddgs.text(query, region="ru-ru", max_results=max_results))

                    raw_res = await asyncio.to_thread(_do_ddg, p_val)
                    for r in raw_res:
                        results.append({
                            "title": r.get("title", ""),
                            "href": unquote(r.get("href", "") or r.get("link", "")),
                            "body": r.get("body", "") or r.get("snippet", "")
                        })
                    if results:
                        return results
                except Exception:
                    pass

        if HAS_PLAYWRIGHT:
            _, html_b = await self.fetch_with_playwright(f"https://www.bing.com/search?q={quote_plus(query)}", wait_ms=1500)
            if html_b:
                soup = BeautifulSoup(html_b, "html.parser")
                for li in soup.select("li.b_algo")[:max_results]:
                    a = li.select_one("h2 a")
                    p = li.select_one(".b_caption p")
                    if a and a.get("href", "").startswith("http"):
                        results.append({
                            "title": a.get_text(" ", strip=True),
                            "href": unquote(a.get("href", "")),
                            "body": p.get_text(" ", strip=True) if p else ""
                        })
                if results:
                    return results
        return results

    # ==================== ПОИСК ПОЛЬЗОВАТЕЛЕЙ ВКОНТАКТЕ (БЕЗ ГРУПП И СИСТЕМНЫХ СТРАНИЦ!) ====================

    async def vk_lookup(self, query: str, session: aiohttp.ClientSession, by_name: bool = False) -> dict | list:
        clean_q = query.strip().lstrip("@")
        if not by_name and clean_q.lower() in STOP_WORDS_SLUGS:
            return {"found": False}

        print(f"🔎 [VK] Поиск пользователя ВК: {clean_q} (by_name={by_name})")

        # 1. Если есть токен в .env (полученный через login_vk.py) — используем быстрый официальный API
        if settings.VK_SERVICE_TOKEN and len(settings.VK_SERVICE_TOKEN) > 20 and "ВСТАВЬТЕ" not in settings.VK_SERVICE_TOKEN:
            try:
                if by_name:
                    params = {"q": clean_q, "count": 5, "fields": "bdate,city,domain,contacts,site", "access_token": settings.VK_SERVICE_TOKEN.strip(), "v": "5.199", "lang": "ru"}
                    async with session.get("https://api.vk.com/method/users.search", params=params, ssl=False) as r:
                        items = (await r.json()).get("response", {}).get("items", [])
                        if items:
                            return [{"id": u.get("id"), "full_name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(), "city": u.get("city", {}).get("title", "Не указан"), "bdate": u.get("bdate", "Скрыта"), "url": f"https://vk.com/{u.get('domain') or 'id' + str(u.get('id'))}"} for u in items]
                else:
                    params = {"user_ids": clean_q, "fields": "first_name,last_name,bdate,city,domain,status,contacts,site", "access_token": settings.VK_SERVICE_TOKEN.strip(), "v": "5.199", "lang": "ru"}
                    async with session.get("https://api.vk.com/method/users.get", params=params, ssl=False) as r:
                        users = (await r.json()).get("response", [])
                        if users:
                            u = users[0]
                            return {"found": True, "vk_id": u.get("id"), "full_name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(), "url": f"https://vk.com/{u.get('domain') or 'id' + str(u.get('id'))}", "bdate": u.get("bdate", "Скрыта"), "city": u.get("city", {}).get("title", "Не указан"), "status": u.get("status", ""), "mobile_phone": u.get("mobile_phone", ""), "site": u.get("site", "")}
            except Exception:
                pass

        # 2. Открытие профиля через Playwright (проверяем, что это ЧЕЛОВЕК, а не группа/сообщество!)
        async def resolve_user_via_playwright(slug: str) -> dict:
            s_clean = slug.strip().lstrip("@")
            if s_clean.lower() in STOP_WORDS_SLUGS or s_clean.lower().startswith(("club", "public", "event", "wall", "album", "artist", "app")):
                return {"found": False}
            target_slug = f"id{s_clean}" if s_clean.isdigit() else s_clean
            vk_url = f"https://vk.com/{target_slug}"

            if HAS_PLAYWRIGHT:
                for test_url in (f"https://m.vk.com/{target_slug}", vk_url):
                    title, html_text = await self.fetch_with_playwright(test_url, wait_ms=2200)
                    if not html_text:
                        continue

                    # Если в коде страницы отрицательный owner_id или ссылка vk://vk.com/club... — это ГРУППА, пропускаем!
                    if re.search(r"vk://vk\.com/(?:club|public|event)\d+", html_text) or re.search(r'"group_id":\s*\d+', html_text):
                        print(f"ℹ️ [VK Skip] {vk_url} — это группа/сообщество, а не пользователь.")
                        return {"found": False}

                    soup = BeautifulSoup(html_text, "html.parser")
                    og_type = soup.find("meta", property="og:type")
                    if og_type and og_type.has_attr("content") and og_type["content"] not in ("profile", "website"):
                        continue

                    name_el = (
                        soup.select_one("#owner_page_name")
                        or soup.select_one(".OwnerPageName")
                        or soup.select_one("h2.op_header")
                        or soup.select_one(".pp_cont h2")
                        or soup.find("meta", property="og:title")
                    )
                    raw_name = ""
                    if name_el:
                        raw_name = name_el["content"] if name_el.has_attr("content") else name_el.get_text(" ", strip=True)
                    if not raw_name and title:
                        raw_name = title.split("|")[0].replace("ВКонтакте", "").strip()

                    if raw_name:
                        clean_name = raw_name.split("|")[0].replace("ВКонтакте", "").strip()
                        c_low = clean_name.lower()
                        # Отсеиваем все группы, паблики и системные заглушки
                        if any(bad in c_low for bad in VK_NON_USER_TITLES) or c_low in ("вконтакте", "vk", ""):
                            continue

                        info_text = ""
                        for sel in (".OwnerInfo__row", ".pp_info", ".profile_info", "meta[property='og:description']"):
                            els = soup.select(sel)
                            if els:
                                info_text = " | ".join(
                                    (el["content"] if el.has_attr("content") else el.get_text(" ", strip=True))
                                    for el in els[:3]
                                )
                                if info_text:
                                    break

                        id_m = re.search(r"vk\.com/id(\d+)", html_text) or re.search(r'"owner_id":\s*(\d+)', html_text) or re.search(r"wall(\d+)_", html_text)
                        print(f"✅ [Playwright VK User OK] Найден пользователь: {clean_name} ({vk_url})")
                        return {
                            "found": True,
                            "vk_id": id_m.group(1) if id_m else target_slug,
                            "full_name": clean_name,
                            "url": vk_url,
                            "city": info_text[:90] if info_text else "Профиль пользователя ВК",
                            "bdate": "См. в профиле"
                        }

            # 3. Поиск через кэш поисковика
            for sq in [f"site:vk.com/{target_slug}", f'"{target_slug}" site:vk.com']:
                for item in await self.web_search_clean(sq, session, max_results=5):
                    if f"/{target_slug.lower()}" in item["href"].lower():
                        clean_name = item["title"].split("|")[0].replace("ВКонтакте", "").strip()
                        if clean_name and not any(bad in clean_name.lower() for bad in VK_NON_USER_TITLES):
                            return {
                                "found": True,
                                "vk_id": target_slug,
                                "full_name": clean_name,
                                "url": vk_url,
                                "city": item["body"][:80] if item.get("body") else "Найдено в индексе ВК",
                                "bdate": "См. страницу"
                            }
            return {"found": False}

        # Поиск по Имени и Фамилии
        if by_name:
            results = []
            seen_urls = set()

            if HAS_PLAYWRIGHT:
                people_url = (
                    f"https://vk.com/search?c[q]={quote_plus(clean_q)}&c[section]=people"
                    if os.path.exists("vk_auth_state.json")
                    else f"https://vk.com/people/{quote_plus('_'.join(clean_q.split()))}"
                )
                _, html_people = await self.fetch_with_playwright(people_url, wait_ms=2200)
                if html_people:
                    soup = BeautifulSoup(html_people, "html.parser")
                    for a in soup.select("div.people_row div.labeled.name a, .search_item a, div.people_row a")[:15]:
                        href = a.get("href", "")
                        txt = a.get_text(" ", strip=True)
                        if href.startswith("/") and len(href) > 2:
                            slug_p = href.lstrip("/").split("/")[0].split("?")[0]
                            if slug_p.lower() not in STOP_WORDS_SLUGS and not slug_p.lower().startswith(("club", "public", "event")):
                                full_u = f"https://vk.com/{slug_p}"
                                if full_u not in seen_urls and len(txt) > 3 and not any(b in txt.lower() for b in VK_NON_USER_TITLES):
                                    seen_urls.add(full_u)
                                    results.append({"id": slug_p, "full_name": txt[:60], "city": "Поиск людей ВК", "bdate": "", "url": full_u})
                                    if len(results) >= 5:
                                        return results

            for item in await self.web_search_clean(f'site:vk.com "{clean_q}"', session, max_results=10):
                m = EXTRACT_VK_SLUG_RE.search(item["href"])
                if m:
                    slug = m.group(1)
                    if slug.lower() in STOP_WORDS_SLUGS or slug.lower().startswith(("wall", "club", "public", "event", "albums", "people", "artist")):
                        continue
                    full_u = f"https://vk.com/{slug}"
                    if full_u not in seen_urls:
                        seen_urls.add(full_u)
                        clean_t = item["title"].split("|")[0].replace("ВКонтакте", "").strip()
                        if clean_t and not any(b in clean_t.lower() for b in VK_NON_USER_TITLES):
                            results.append({"id": slug, "full_name": clean_t, "city": item["body"][:70] if item.get("body") else "Профиль ВК", "bdate": "по ссылке", "url": full_u})
                        if len(results) >= 5:
                            return results
            return results

        return await resolve_user_via_playwright(clean_q)

    # ==================== TELETHON + WHATSAPP ====================

    async def telethon_resolve_phone_or_nick(self, phone: str | None = None, username: str | None = None) -> dict:
        if not HAS_TELETHON or not os.path.exists(TELETHON_SESSION_FILE):
            return {"found": False, "auth": False}
        async with _telethon_lock:
            client = TelegramClient("osint_userbot", TG_API_ID, TG_API_HASH)
            try:
                await asyncio.wait_for(client.connect(), timeout=10)
                if not await client.is_user_authorized():
                    await client.disconnect()
                    return {"found": False, "auth": False}
                if phone:
                    clean_ph = "+" + re.sub(r"\D", "", phone)
                    contact = InputPhoneContact(client_id=random.randint(1, 999999), phone=clean_ph, first_name="OSINT", last_name="User")
                    imp = await client(ImportContactsRequest([contact]))
                    if imp.users:
                        u = imp.users[0]
                        bio = ""
                        try:
                            full_u = await client(GetFullUserRequest(u.id))
                            bio = getattr(full_u.full_user, "about", "") or ""
                        except Exception:
                            pass
                        try:
                            await client(DeleteContactsRequest(id=[u.id]))
                        except Exception:
                            pass
                        reg_info = self.estimate_tg_reg_date(u.id)
                        full_name = f"{u.first_name or ''} {u.last_name or ''}".strip()
                        if full_name == "OSINT User":
                            full_name = u.username or f"ID {u.id}"
                        await client.disconnect()
                        return {
                            "found": True, "auth": True, "tg_id": u.id, "username": u.username,
                            "full_name": full_name, "bio": bio, "reg_date": reg_info["estimated_registration"]
                        }
                    await client.disconnect()
                    return {"found": False, "auth": True}
                elif username:
                    clean_u = username.strip().lstrip("@")
                    ent = await client.get_entity(clean_u)
                    if ent:
                        reg_info = self.estimate_tg_reg_date(ent.id)
                        full_name = f"{getattr(ent, 'first_name', '') or ''} {getattr(ent, 'last_name', '') or ''}".strip() or getattr(ent, "title", "")
                        await client.disconnect()
                        return {
                            "found": True, "auth": True, "tg_id": ent.id, "username": getattr(ent, "username", clean_u),
                            "full_name": full_name, "reg_date": reg_info["estimated_registration"]
                        }
            except Exception:
                pass
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        return {"found": False, "auth": False}

    async def check_whatsapp_profile(self, digits: str, session: aiohttp.ClientSession) -> dict:
        wa_url = f"https://wa.me/{digits}"
        api_url = f"https://api.whatsapp.com/send/?phone={digits}"
        wa_name = None
        html_txt = await self.fetch_url_text(api_url, session)
        if html_txt:
            soup = BeautifulSoup(html_txt, "html.parser")
            h3 = soup.find("h3", class_="_9vd5")
            if h3:
                txt = h3.get_text(strip=True)
                if txt and not re.sub(r"[\s\+\-\(\)]", "", txt).isdigit() and "WhatsApp" not in txt:
                    wa_name = txt
        return {"url": wa_url, "api_url": api_url, "wa_name": wa_name}

    # ==================== CLI И ОСТАЛЬНЫЕ МОДУЛИ ====================

    async def run_maigret(self, username: str, full: bool = False) -> str:
        sites = "250" if full else "50"
        return await run_cli(["maigret", username, "--top-sites", sites, "--no-color", "--timeout", "8"], timeout=120 if full else 40)

    async def run_holehe(self, email: str) -> str:
        return await run_cli(["holehe", email, "--only-used", "--no-color"], timeout=40)

    async def run_ignorant(self, phone: str) -> str:
        clean_digits = re.sub(r"\D", "", phone)
        if clean_digits.startswith("7") and len(clean_digits) == 11:
            return await run_cli(["ignorant", "7", clean_digits[1:], "--only-used", "--no-color"], timeout=35)
        return "Совпадений не обнаружено."

    async def extended_social_apis(self, username: str, session: aiohttp.ClientSession) -> dict:
        results = {}
        found_names, found_emails, found_locations, found_profiles = [], [], [], []
        try:
            async with session.get(f"https://steamcommunity.com/id/{username}?xml=1", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    xml_txt = await r.text()
                    if "<steamID64>" in xml_txt:
                        soup = BeautifulSoup(xml_txt, "html.parser")
                        rname = soup.find("realname").get_text(strip=True) if soup.find("realname") else ""
                        loc = soup.find("location").get_text(strip=True) if soup.find("location") else ""
                        results["steam"] = {"realname": rname, "location": loc}
                        found_profiles.append(f"Steam ({rname or username})")
                        if rname:
                            found_names.append(f"{rname} (Steam)")
                        if loc:
                            found_locations.append(f"{loc} (Steam)")
        except Exception:
            pass
        try:
            async with session.get(f"https://api.github.com/users/{username}", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    gh = await r.json()
                    results["github_profile"] = {"name": gh.get("name"), "blog": gh.get("blog"), "location": gh.get("location"), "email": gh.get("email"), "bio": gh.get("bio")}
                    found_profiles.append("GitHub")
                    if gh.get("name"):
                        found_names.append(f"{gh['name']} (GitHub)")
                    if gh.get("location"):
                        found_locations.append(f"{gh['location']} (GitHub)")
                    if gh.get("email"):
                        found_emails.append(gh["email"])
            async with session.get(f"https://api.github.com/users/{username}/events/public", headers=self.headers, ssl=False) as r2:
                if r2.status == 200:
                    for ev in (await r2.json())[:15]:
                        for c in ev.get("payload", {}).get("commits", []):
                            em = c.get("author", {}).get("email", "")
                            nm = c.get("author", {}).get("name", "")
                            if em and "noreply" not in em and em not in found_emails:
                                found_emails.append(em)
                            if nm and nm not in found_names and len(nm) > 2:
                                found_names.append(f"{nm} (Git Commit)")
        except Exception:
            pass
        results["_summary"] = {"names": found_names, "emails": found_emails, "locations": found_locations, "profiles": found_profiles}
        return results

    async def tiktok_youtube_lookup(self, username: str, session: aiohttp.ClientSession) -> dict:
        res = {"tiktok": "Не найден", "youtube": "Не найден"}
        tt_url = f"https://www.tiktok.com/@{username}"
        html_tt = await self.fetch_url_text(tt_url, session)
        if html_tt:
            soup = BeautifulSoup(html_tt, "html.parser")
            title_el = soup.find("title")
            title_txt = title_el.get_text(strip=True) if title_el else ""
            if title_txt and "TikTok" in title_txt and f"@{username.lower()}" in html_tt.lower():
                res["tiktok"] = f"{title_txt.replace('| TikTok', '').strip()} ({tt_url})"
        yt_url = f"https://www.youtube.com/@{username}"
        html_yt = await self.fetch_url_text(yt_url, session)
        if html_yt:
            soup = BeautifulSoup(html_yt, "html.parser")
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.has_attr("content"):
                res["youtube"] = f"{og_title['content']} ({yt_url})"
        return res

    async def email_extra_osint(self, email: str, session: aiohttp.ClientSession) -> dict:
        extra = {"duolingo": None, "pgp_uid": None, "mail_server": None}
        try:
            async with session.get(f"https://www.duolingo.com/2017-06-30/users?email={quote_plus(email)}", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    users = (await r.json()).get("users", [])
                    if users:
                        extra["duolingo"] = {"username": users[0].get("username"), "name": users[0].get("name")}
        except Exception:
            pass
        dns_res = self.dns_and_ip_resolve(email.split("@")[-1])
        if dns_res.get("MX"):
            extra["mail_server"] = dns_res["MX"][:2]
        return extra

    async def phone_extra_osint(self, phone: str, session: aiohttp.ClientSession) -> dict:
        digits = re.sub(r"\D", "", phone)
        eight_digits = ("8" + digits[1:]) if (digits.startswith("7") and len(digits) == 11) else digits
        short_10 = digits[-10:] if len(digits) >= 10 else digits
        dash_fmt = f"{short_10[:3]}-{short_10[3:6]}-{short_10[6:8]}-{short_10[8:10]}" if len(short_10) == 10 else digits

        tg_direct, wa_direct = await asyncio.gather(
            self.telethon_resolve_phone_or_nick(phone=phone),
            self.check_whatsapp_profile(digits, session)
        )

        info = {
            "telegram_direct": tg_direct, "whatsapp_direct": wa_direct,
            "messengers": {"whatsapp": f"https://wa.me/{digits}", "telegram": f"https://t.me/+{digits}", "viber": f"viber://chat?number={digits}"},
            "htmlweb_mnp": {}, "leakcheck_phone": "Утечек по номеру не найдено",
            "discovered_nicknames": [], "discovered_vk_slugs": [], "discovered_emails": [], "discovered_names": [], "web_mentions": []
        }

        found_nicks, found_vk, found_names = [], [], []
        found_emails = set()

        if tg_direct.get("found"):
            if tg_direct.get("username"):
                found_nicks.append(tg_direct["username"])
            if tg_direct.get("full_name"):
                found_names.append(f"{tg_direct['full_name']} (Telegram)")

        if wa_direct.get("wa_name"):
            found_names.append(f"{wa_direct['wa_name']} (WhatsApp)")
            wa_clean = wa_direct["wa_name"].strip().lstrip("@")
            if re.match(r"^[a-zA-Z0-9_\.]{4,25}$", wa_clean) and wa_clean.lower() not in STOP_WORDS_SLUGS and wa_clean not in found_nicks:
                found_nicks.append(wa_clean)

        try:
            async with session.get(f"https://htmlweb.ru/geo/api.php?json&telcod={digits}", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    data = await r.json()
                    info["htmlweb_mnp"] = {"region": data.get("region", {}).get("name"), "city": data.get("0", {}).get("name"), "operator": data.get("0", {}).get("oper"), "operator_brand": data.get("0", {}).get("oper_brand")}
        except Exception:
            pass

        try:
            async with session.get(f"https://leakcheck.io/api/public?check={digits}", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    lc = await r.json()
                    if lc.get("success") and lc.get("found", 0) > 0:
                        dbs = [f"{s.get('name')} ({s.get('date') or 'н/д'})" for s in lc.get("sources", [])]
                        info["leakcheck_phone"] = f"Найдено в базах ({lc['found']}): {', '.join(dbs[:6])}"
        except Exception:
            pass

        for q in [f'"{digits}" OR "{eight_digits}" OR "{dash_fmt}"', f'"{short_10}" (vk.com OR t.me OR whatsapp OR avito OR тел)']:
            for item in await self.web_search_clean(q, session, max_results=8):
                combined_urls = f"{item['title']} {item['href']} {item['body']}"
                plain_text_only = f"{item['title']} {item['body']}"
                if item not in info["web_mentions"] and len(info["web_mentions"]) < 8:
                    info["web_mentions"].append({"title": item["title"][:90], "url": item["href"][:120], "snippet": item["body"][:140]})
                for tg_m in EXTRACT_TG_LINK_RE.findall(combined_urls):
                    if tg_m.lower() not in STOP_WORDS_SLUGS and not tg_m.isdigit() and tg_m not in found_nicks:
                        found_nicks.append(tg_m)
                for at_m in EXTRACT_PLAIN_AT_NICK_RE.findall(plain_text_only):
                    if at_m.lower() not in STOP_WORDS_SLUGS and not at_m.isdigit() and at_m not in found_nicks:
                        found_nicks.append(at_m)
                for vk_m in EXTRACT_VK_SLUG_RE.findall(combined_urls):
                    cl = vk_m.lower()
                    if cl not in STOP_WORDS_SLUGS and not cl.startswith(("wall", "club", "public", "event", "albums", "artist", "audio", "video", "clip", "app", "topic", "page")):
                        if vk_m not in found_vk:
                            found_vk.append(vk_m)
                        if not cl.startswith("id") and not cl.isdigit() and vk_m not in found_nicks:
                            found_nicks.append(vk_m)
                for em_m in EXTRACT_EMAIL_RE.findall(combined_urls):
                    if not em_m.endswith((".png", ".jpg", ".gif", ".js", ".css")):
                        found_emails.add(em_m.lower())
                        prefix = em_m.split("@")[0].lower()
                        if len(prefix) >= 4 and prefix not in STOP_WORDS_SLUGS and not prefix.isdigit() and prefix not in found_nicks:
                            found_nicks.append(prefix)

        info["discovered_nicknames"] = found_nicks[:8]
        info["discovered_vk_slugs"] = found_vk[:5]
        info["discovered_emails"] = sorted(list(found_emails))[:5]
        info["discovered_names"] = found_names[:5]
        return info

    async def car_plate_osint(self, plate_cyr: str, session: aiohttp.ClientSession) -> dict:
        cyr_upper, lat_lower = normalize_car_plate(plate_cyr)
        nomerogram_url = f"https://www.nomerogram.ru/n/{lat_lower}/"
        reg_match = re.search(r"(\d{2,3})$", cyr_upper)
        reg_code = reg_match.group(1) if reg_match else ""
        regions_map = {"77": "Москва", "97": "Москва", "99": "Москва", "177": "Москва", "197": "Москва", "199": "Москва", "777": "Москва", "797": "Москва", "799": "Москва", "977": "Москва", "50": "Московская обл.", "90": "Московская обл.", "150": "Московская обл.", "190": "Московская обл.", "750": "Московская обл.", "790": "Московская обл.", "78": "Санкт-Петербург", "98": "Санкт-Петербург", "178": "Санкт-Петербург", "198": "Санкт-Петербург", "778": "Санкт-Петербург"}
        region_name = regions_map.get(reg_code, f"Код региона {reg_code}")

        info = {
            "plate_cyr": cyr_upper, "plate_lat": lat_lower.upper(), "region": f"{region_name} ({reg_code})",
            "nomerogram_url": nomerogram_url, "car_model": "Не определена (откройте ссылку Номерограм)",
            "photos": [], "vins_found": [], "phones_found": [], "owner_links": [], "web_mentions": [], "local_db_hits": []
        }
        vin_re = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

        title_n, html_txt = await self.fetch_with_playwright(nomerogram_url, wait_ms=2000) if HAS_PLAYWRIGHT else ("", await self.fetch_url_text(nomerogram_url, session))
        if html_txt:
            soup = BeautifulSoup(html_txt, "html.parser")
            h1_el = soup.find("h1")
            raw_t = (h1_el.get_text(" ", strip=True) if h1_el else "") or title_n
            if raw_t and "404" not in raw_t and "Just a moment" not in raw_t:
                clean_model = raw_t.replace("Номерограм", "").replace("|", "").strip()
                if clean_model:
                    info["car_model"] = clean_model[:120]
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "nomerogram" in src and src not in info["photos"]:
                    info["photos"].append(src)

        for q in [f'"{cyr_upper}" OR "{lat_lower.upper()}"', f'"{cyr_upper}" (drive2 OR nomerogram OR VIN)']:
            for item in await self.web_search_clean(q, session, max_results=8):
                combined = f"{item['title']} {item['href']} {item['body']}"
                if item not in info["web_mentions"] and len(info["web_mentions"]) < 8:
                    info["web_mentions"].append({"title": item["title"][:90], "url": item["href"][:120], "snippet": item["body"][:140]})
                if "nomerogram.ru" in item["href"] and info["car_model"].startswith("Не определена"):
                    info["car_model"] = item["title"].split("|")[0].replace("Номерограм", "").strip()[:100]
                for v in vin_re.findall(combined):
                    if not v.isdigit() and any(ch.isdigit() for ch in v) and v not in info["vins_found"]:
                        info["vins_found"].append(v)
                for ph in EXTRACT_PHONE_RE.findall(combined):
                    if ph.strip() not in info["phones_found"]:
                        info["phones_found"].append(ph.strip())
        return info

    async def ip_deep_intelligence(self, ip: str, session: aiohttp.ClientSession) -> dict:
        res = {"geo": {}, "shodan_internetdb": {}, "flags": []}
        try:
            async with session.get(f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,isp,org,as,reverse,mobile,proxy,hosting&lang=ru", ssl=False) as r:
                if r.status == 200:
                    res["geo"] = await r.json()
        except Exception:
            pass
        try:
            async with session.get(f"https://internetdb.shodan.io/{ip}", headers=self.headers, ssl=False) as r:
                if r.status == 200:
                    res["shodan_internetdb"] = await r.json()
        except Exception:
            pass
        return res

    async def extract_contacts_and_ip_by_nick(self, username: str, vk_prof: dict, tg_prof: dict, breaches: dict, ext_soc: dict, session: aiohttp.ClientSession) -> dict:
        found_phones, found_ips = set(), set()
        found_emails = set(ext_soc.get("_summary", {}).get("emails", []))
        if isinstance(vk_prof, dict) and vk_prof.get("mobile_phone"):
            found_phones.add(vk_prof["mobile_phone"])
        for s in breaches.get("stealers_raw", []):
            if s.get("ip") and s.get("ip").lower() != "not found":
                found_ips.add(f"{s['ip']} (ПК: {s.get('computer_name') or 'н/д'})")
        for c in breaches.get("commits", []):
            for em in EXTRACT_EMAIL_RE.findall(c):
                if "noreply" not in em:
                    found_emails.add(em)
        return {"phones": sorted(list(found_phones))[:5], "emails": sorted(list(found_emails))[:5], "ips": sorted(list(found_ips))[:5]}

    async def deep_breach_intelligence(self, target: str, target_type: TargetType, session: aiohttp.ClientSession) -> dict:
        intel = {"databases": [], "fields": [], "stealers": "Чисто", "stealers_raw": [], "commits": [], "xposed_details": []}
        if target_type in (TargetType.EMAIL, TargetType.USERNAME):
            try:
                async with session.get(f"https://leakcheck.io/api/public?check={target}", headers=self.headers, ssl=False) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("success") and data.get("found", 0) > 0:
                            intel["fields"] = data.get("fields", [])
                            intel["databases"] = [f"{s.get('name')} ({s.get('date') or 'н/д'})" for s in data.get("sources", [])]
            except Exception:
                pass
        if target_type in (TargetType.EMAIL, TargetType.USERNAME, TargetType.DOMAIN):
            ep_map = {TargetType.EMAIL: f"search-by-email?email={target}", TargetType.USERNAME: f"search-by-username?username={target}", TargetType.DOMAIN: f"search-by-domain?domain={target}"}
            try:
                async with session.get(f"https://cavalier.hudsonrock.com/api/json/v2/osint-tools/{ep_map[target_type]}", headers=self.headers, ssl=False) as r:
                    if r.status == 200:
                        stealers = (await r.json()).get("stealers", [])
                        if stealers:
                            intel["stealers_raw"] = stealers[:5]
                            ips_s = [s.get("ip") for s in stealers if s.get("ip")]
                            comps = [s.get("computer_name") for s in stealers if s.get("computer_name")]
                            extra_s = f" | IP: {', '.join(ips_s[:2])} | ПК: {', '.join(comps[:2])}" if (ips_s or comps) else ""
                            intel["stealers"] = f"Записей в логах стилеров: {len(stealers)}{extra_s}"
            except Exception:
                pass
        return intel

    @staticmethod
    def estimate_tg_reg_date(tg_id: int) -> dict:
        checkpoints = [(1000000, 1380585600), (10000000, 1414800000), (50000000, 1451606400), (100000000, 1475280000), (300000000, 1498867200), (500000000, 1525132800), (800000000, 1559347200), (1100000000, 1588291200), (1500000000, 1609459200), (2000000000, 1633046400), (5000000000, 1654041600), (6000000000, 1685577600), (7000000000, 1711929600), (7500000000, 1735689600), (8000000000, 1767225600)]
        if tg_id <= checkpoints[0][0]:
            est_ts = checkpoints[0][1]
        elif tg_id >= checkpoints[-1][0]:
            est_ts = checkpoints[-1][1]
        else:
            est_ts = checkpoints[0][1]
            for i in range(len(checkpoints) - 1):
                id1, ts1 = checkpoints[i]
                id2, ts2 = checkpoints[i + 1]
                if id1 <= tg_id <= id2:
                    est_ts = int(ts1 + ((tg_id - id1) / (id2 - id1)) * (ts2 - ts1))
                    break
        return {"telegram_id": tg_id, "estimated_registration": datetime.fromtimestamp(est_ts, tz=timezone.utc).strftime("%Y-%m (%B %Y)")}

    async def telegram_web_profile(self, username: str, session: aiohttp.ClientSession) -> dict:
        if not username or username.lower() in STOP_WORDS_SLUGS:
            return {"exists": False}
        url = f"https://t.me/{username}"
        html_txt = await self.fetch_url_text(url, session)
        if html_txt:
            soup = BeautifulSoup(html_txt, "html.parser")
            title = soup.find("div", class_="tgme_page_title")
            desc = soup.find("div", class_="tgme_page_description")
            if title:
                mtproto_info = await self.telethon_resolve_phone_or_nick(username=username)
                return {"exists": True, "url": url, "display_name": title.get_text(strip=True), "bio": desc.get_text(strip=True)[:150] if desc else "", "tg_id": mtproto_info.get("tg_id"), "reg_date": mtproto_info.get("reg_date")}
        return {"exists": False}

    async def check_second_nicknames(self, username: str, session: aiohttp.ClientSession) -> dict:
        base = username.lower()
        clean = re.sub(r"[_\.\-]", "", base)
        variants = sorted(list({f"{clean}_", f"_{clean}", f"{clean}1", f"real_{clean}", f"{clean}.official", base.replace("_", "."), base.replace(".", "_")} - {base}))[:6]
        found_tg = []
        for v in variants[:4]:
            prof = await self.telegram_web_profile(v, session)
            if prof.get("exists"):
                found_tg.append(f"@{v} ({prof.get('display_name')})")
        return {"generated_aliases": variants, "active_in_telegram": found_tg}

    async def gravatar_lookup(self, email: str, session: aiohttp.ClientSession) -> dict:
        h = hashlib.md5(email.strip().lower().encode()).hexdigest()
        try:
            async with session.get(f"https://en.gravatar.com/{h}.json", ssl=False) as r:
                if r.status == 200:
                    entry = (await r.json()).get("entry", [{}])[0]
                    return {"username": entry.get("preferredUsername"), "display_name": entry.get("displayName"), "full_name": entry.get("name", {}).get("formatted"), "location": entry.get("currentLocation")}
        except Exception:
            pass
        return {}

    @staticmethod
    def dns_and_ip_resolve(domain: str) -> dict:
        res = {"A": [], "MX": [], "TXT": [], "NS": []}
        for rtype in res.keys():
            try:
                res[rtype] = [str(r) for r in dns.resolver.resolve(domain, rtype, lifetime=5)]
            except Exception:
                pass
        return res

    async def crtsh_subdomains(self, domain: str, session: aiohttp.ClientSession) -> list[str]:
        try:
            async with session.get(f"https://crt.sh/?q=%.{domain}&output=json", ssl=False) as r:
                if r.status == 200:
                    data = await r.json()
                    return sorted(list({s.strip() for item in data for s in item.get("name_value", "").split("\n")}))[:60]
        except Exception:
            pass
        return []

    @staticmethod
    def phone_lookup(phone: str) -> dict:
        try:
            p = phonenumbers.parse(phone, None)
            return {"formatted": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL), "valid": phonenumbers.is_valid_number(p), "region": geocoder.description_for_number(p, "ru"), "carrier": carrier.name_for_number(p, "ru") or "Не определён (MNP)", "timezone": list(pn_timezone.time_zones_for_number(p))}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def extract_exif(file_bytes: bytes) -> dict:
        tags = exifread.process_file(BytesIO(file_bytes), details=False)
        return {k: str(v) for k, v in tags.items() if k not in ("JPEGThumbnail", "TIFFThumbnail")}

    async def search_local_dumps(self, query: str) -> list[str]:
        dumps_dir = "./local_dumps"
        if not os.path.exists(dumps_dir):
            return []
        results = []
        try:
            for fname in os.listdir(dumps_dir):
                fpath = os.path.join(dumps_dir, fname)
                if os.path.isfile(fpath) and fname.endswith((".txt", ".csv", ".tsv", ".log")):
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if query.lower() in line.lower():
                                results.append(f"[{fname}] {line.strip()[:250]}")
                                if len(results) >= 15:
                                    return results
        except Exception:
            pass
        return results

    # ==================== ГЛАВНЫЙ ОРКЕСТРАТОР ====================

    async def execute_scan(self, target: str, target_type: TargetType, deep: bool = False) -> tuple[str, str]:
        if await is_target_blocked(target):
            msg = "🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>"
            return msg, msg

        p1_target = f"{target} ({target_type.value.upper()})"
        p2_name, p3_tg, p4_vk = "Не обнаружено", "Не найден", "Не найден"
        p5_tt_yt = "TikTok: нет | YouTube: нет"
        p6_platforms, p7_leaks = "Нет совпадений", "Утечек не обнаружено"
        p8_stealers = "Чисто (в логах инфостилеров не найден)"
        p9_geo_net = "Скрыт (в открытых профилях и дампах не засвечен)"
        p10_aliases = "Не обнаружено"
        raw_sections: list[tuple[str, any]] = []

        connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=self.timeout, trust_env=True) as session:
            if target_type == TargetType.USERNAME:
                tg_prof, vk_prof, tt_yt, ext_soc, breaches, aliases, maigret_out = await asyncio.gather(
                    self.telegram_web_profile(target, session),
                    self.vk_lookup(target, session),
                    self.tiktok_youtube_lookup(target, session),
                    self.extended_social_apis(target, session),
                    self.deep_breach_intelligence(target, target_type, session),
                    self.check_second_nicknames(target, session),
                    self.run_maigret(target, full=deep)
                )
                extracted_contacts = await self.extract_contacts_and_ip_by_nick(target, vk_prof if isinstance(vk_prof, dict) else {}, tg_prof, breaches, ext_soc, session)

                names_found = list(ext_soc.get("_summary", {}).get("names", []))
                if isinstance(vk_prof, dict) and vk_prof.get("found"):
                    names_found.insert(0, f"{vk_prof.get('full_name')} (ВК)")
                    p4_vk = f"{vk_prof.get('url')} — {vk_prof.get('full_name')} ({vk_prof.get('city')})"
                elif "vk.com/" in maigret_out.lower():
                    p4_vk = f"https://vk.com/{target} (Найден через Maigret)"

                if tg_prof.get("exists"):
                    names_found.insert(0, f"{tg_prof.get('display_name')} (TG)")
                    id_extra = f" [ID: {tg_prof['tg_id']}, рег: {tg_prof['reg_date']}]" if tg_prof.get("tg_id") else ""
                    p3_tg = f"{tg_prof.get('url')} — {tg_prof.get('display_name')}{id_extra}"
                if names_found:
                    p2_name = " / ".join(dict.fromkeys(names_found))

                p5_tt_yt = f"TikTok: {tt_yt['tiktok']} | YouTube: {tt_yt['youtube']}"
                found_sites = list(ext_soc.get("_summary", {}).get("profiles", []))
                for line in maigret_out.splitlines():
                    if "[+]" in line:
                        s_name = line.strip().split()[1].rstrip(":")
                        if s_name not in found_sites:
                            found_sites.append(s_name)
                p6_platforms = f"Найдено ({len(found_sites)}): {', '.join(found_sites[:10])}" if found_sites else "Совпадений в топ-сайтах нет"
                if breaches["databases"]:
                    p7_leaks = f"Найдено баз ({len(breaches['databases'])}): {', '.join(breaches['databases'][:6])} | Поля: {', '.join(breaches['fields'][:5])}"
                p8_stealers = breaches["stealers"]

                contact_parts = []
                if extracted_contacts["phones"]:
                    contact_parts.append(f"📞 Тел: {', '.join(extracted_contacts['phones'])}")
                if extracted_contacts["ips"]:
                    contact_parts.append(f"🌐 IP: {', '.join(extracted_contacts['ips'])}")
                if extracted_contacts["emails"]:
                    contact_parts.append(f"✉️ Email: {', '.join(extracted_contacts['emails'])}")
                if contact_parts:
                    p9_geo_net = " | ".join(contact_parts)

                alias_parts = []
                if aliases.get("active_in_telegram"):
                    alias_parts.append("TG-алиасы: " + ", ".join(aliases["active_in_telegram"]))
                else:
                    alias_parts.append("Вероятные ники: " + ", ".join(aliases.get("generated_aliases", [])[:4]))
                p10_aliases = " | ".join(alias_parts)
                raw_sections.extend([("ТЕЛЕФОНЫ / IP / EMAIL", extracted_contacts), ("СОЦСЕТИ", ext_soc), ("TELEGRAM", tg_prof), ("ВК", vk_prof), ("УТЕЧКИ", breaches), ("MAIGRET", maigret_out)])

            elif target_type == TargetType.EMAIL:
                grav, extra_em, breaches, holehe_out = await asyncio.gather(
                    self.gravatar_lookup(target, session), self.email_extra_osint(target, session),
                    self.deep_breach_intelligence(target, target_type, session), self.run_holehe(target)
                )
                duo = extra_em.get("duolingo") or {}
                pivot_nick = grav.get("username") or duo.get("username") or target.split("@")[0]
                if await is_target_blocked(pivot_nick):
                    msg = "🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>"
                    return msg, msg

                names_list = []
                if grav.get("full_name") or grav.get("display_name"):
                    names_list.append(f"{grav.get('full_name') or grav.get('display_name')} (Gravatar)")
                if duo.get("name"):
                    names_list.append(f"{duo['name']} (Duolingo)")
                if names_list:
                    p2_name = " / ".join(names_list)

                used_services = [l.strip().replace("[+]", "").strip() for l in holehe_out.splitlines() if "[+]" in l]
                p6_platforms = f"Регистраций почты ({len(used_services)}): {', '.join(used_services[:9])}" if used_services else "Активных регистраций не найдено"
                if breaches["databases"]:
                    p7_leaks = f"Найдено баз ({len(breaches['databases'])}): {', '.join(breaches['databases'][:6])}"
                p8_stealers = breaches["stealers"]
                p9_geo_net = f"Гео: {grav.get('location') or 'н/д'} | MX: {', '.join(extra_em.get('mail_server') or [target.split('@')[-1]])}"
                p10_aliases = f"Связанный ник: @{pivot_nick}"
                raw_sections.extend([("GRAVATAR", {"gravatar": grav, "extra": extra_em}), ("УТЕЧКИ", breaches), ("HOLEHE", holehe_out)])

                if len(pivot_nick) >= 4:
                    tg_p, vk_p, tt_yt_p = await asyncio.gather(self.telegram_web_profile(pivot_nick, session), self.vk_lookup(pivot_nick, session), self.tiktok_youtube_lookup(pivot_nick, session))
                    if tg_p.get("exists"):
                        p3_tg = f"По нику @{pivot_nick}: {tg_p.get('url')} ({tg_p.get('display_name')})"
                    if isinstance(vk_p, dict) and vk_p.get("found"):
                        p4_vk = f"По нику @{pivot_nick}: {vk_p.get('url')} ({vk_p.get('full_name')})"
                        if p2_name == "Не обнаружено":
                            p2_name = f"{vk_p.get('full_name')} (ВК)"
                    p5_tt_yt = f"TikTok: {tt_yt_p['tiktok']} | YouTube: {tt_yt_p['youtube']}"

            elif target_type == TargetType.PHONE:
                p_meta = self.phone_lookup(target)
                p_extra, ig_out = await asyncio.gather(self.phone_extra_osint(target, session), self.run_ignorant(target))
                hw = p_extra.get("htmlweb_mnp", {})
                oper = hw.get("operator_brand") or hw.get("operator") or p_meta.get("carrier")
                reg_city = f"{hw.get('region') or p_meta.get('region')}, {hw.get('city') or ''}".strip(", ")

                tg_dir, wa_dir = p_extra.get("telegram_direct", {}), p_extra.get("whatsapp_direct", {})
                disc_nicks, disc_vk, disc_emails = p_extra.get("discovered_nicknames", []), p_extra.get("discovered_vk_slugs", []), p_extra.get("discovered_emails", [])
                disc_names = list(p_extra.get("discovered_names", []))

                if tg_dir.get("found"):
                    tg_uname_str = f"@{tg_dir['username']}" if tg_dir.get("username") else "без @username"
                    tg_part = f"TG: {tg_uname_str} (Имя: {tg_dir.get('full_name')}, ID: {tg_dir.get('tg_id')}, рег: {tg_dir.get('reg_date')}) — {p_extra['messengers']['telegram']}"
                else:
                    tg_part = f"TG: {p_extra['messengers']['telegram']}"
                wa_name_str = f" ({wa_dir['wa_name']})" if wa_dir.get("wa_name") else ""
                wa_part = f"WhatsApp: {p_extra['messengers']['whatsapp']}{wa_name_str}"
                p3_tg = f"{tg_part} | {wa_part}"

                email_part = f" | ✉️ Email: {', '.join(disc_emails[:3])}" if disc_emails else ""
                p9_geo_net = f"Регион: {reg_city} | Оператор: {oper}{email_part}"

                if disc_nicks:
                    p10_aliases = f"Вытащенные ники (из TG / WA / Сети): {', '.join('@' + n for n in disc_nicks[:6])}"
                elif not tg_dir.get("auth"):
                    p10_aliases = "В вебе не засвечен (💡 Авторизуйте Telethon в /admin -> 📱 Пробив ТГ по номеру, чтобы доставать @ник из самого Telegram!)"
                else:
                    p10_aliases = "Никнейм скрыт настройками приватности Telegram ('Кто может найти меня по номеру')"

                p7_leaks = p_extra.get("leakcheck_phone", "Не найдено")
                used_ig = [l.strip().replace("[+]", "").strip() for l in ig_out.splitlines() if "[+]" in l]
                mentions = [m["title"] for m in p_extra.get("web_mentions", [])[:3]]
                p6_platforms = f"Привязки (Ignorant): {', '.join(used_ig) or 'нет'} | Упоминаний в сети: {len(p_extra.get('web_mentions', []))} ({'; '.join(mentions)})"

                primary_nick = tg_dir.get("username") or (disc_nicks[0] if disc_nicks else None)
                primary_vk_slug = disc_vk[0] if disc_vk else primary_nick
                if primary_nick or primary_vk_slug:
                    tg_piv, vk_piv, tt_yt_piv = await asyncio.gather(
                        self.telegram_web_profile(primary_nick, session) if primary_nick else asyncio.sleep(0, result={"exists": False}),
                        self.vk_lookup(primary_vk_slug, session) if primary_vk_slug else asyncio.sleep(0, result={"found": False}),
                        self.tiktok_youtube_lookup(primary_nick, session) if primary_nick else asyncio.sleep(0, result={"tiktok": "Не найден", "youtube": "Не найден"})
                    )
                    if isinstance(vk_piv, dict) and vk_piv.get("found"):
                        disc_names.append(f"{vk_piv.get('full_name')} (ВК)")
                        p4_vk = f"По нику @{primary_vk_slug}: {vk_piv.get('url')} ({vk_piv.get('full_name')})"
                    if tg_piv.get("exists") and not tg_dir.get("found"):
                        disc_names.append(f"{tg_piv.get('display_name')} (TG @{primary_nick})")
                        p3_tg = f"TG (@{primary_nick} — {tg_piv.get('display_name')}): {tg_piv.get('url')} | {wa_part}"
                    if primary_nick:
                        p5_tt_yt = f"По нику @{primary_nick}: TikTok: {tt_yt_piv['tiktok']} | YouTube: {tt_yt_piv['youtube']}"

                if p4_vk == "Не найден" and disc_names:
                    clean_person_name = re.sub(r"\(.*?\)", "", disc_names[0]).strip()
                    if " " in clean_person_name:
                        vk_by_n = await self.vk_lookup(clean_person_name, session, by_name=True)
                        if isinstance(vk_by_n, list) and vk_by_n:
                            p4_vk = "По имени: " + " ; ".join(f"{u['full_name']} ({u['url']})" for u in vk_by_n[:2])
                if disc_names:
                    p2_name = " / ".join(dict.fromkeys(disc_names))
                raw_sections.extend([("ТЕЛЕФОН ДОСЬЕ", p_extra), ("MNP", {"phonenumbers": p_meta, "htmlweb": hw}), ("IGNORANT", ig_out)])

            elif target_type == TargetType.TG_ID:
                reg = self.estimate_tg_reg_date(int(target))
                vk_id_info = await self.vk_lookup(target, session)
                p3_tg = f"ID: {target} | Оценка даты регистрации в TG: {reg['estimated_registration']}"
                if isinstance(vk_id_info, dict) and vk_id_info.get("found"):
                    p2_name = f"{vk_id_info.get('full_name')} (ВК id{target})"
                    p4_vk = f"{vk_id_info.get('url')} ({vk_id_info.get('full_name')})"
                raw_sections.extend([("TG ID", reg), ("ВК ПО ID", vk_id_info)])

            elif target_type == TargetType.FULLNAME:
                p2_name = target
                vk_list = await self.vk_lookup(target, session, by_name=True)
                if isinstance(vk_list, list) and vk_list:
                    p4_vk = " ; ".join([f"{u['full_name']} ({u.get('city') or 'н/д'}) — {u['url']}" for u in vk_list[:4]])
                else:
                    p4_vk = "Анкет в открытом доступе не найдено"
                raw_sections.append(("ПОИСК ПО ФИО В ВК", vk_list))

            elif target_type == TargetType.CAR_PLATE:
                car_info = await self.car_plate_osint(target, session)
                p2_name = f"Автомобиль: {car_info['car_model']}"
                p4_vk = " ; ".join(car_info["owner_links"][:3]) if car_info["owner_links"] else "Профили Drive2 / ВК не засвечены"
                p6_platforms = f"Номерограм: {car_info['nomerogram_url']} | Фото: {len(car_info['photos'])} шт."
                vin_str = f" | 🔢 VIN: {', '.join(car_info['vins_found'][:2])}" if car_info["vins_found"] else ""
                ph_str = f" | 📞 Тел: {', '.join(car_info['phones_found'][:3])}" if car_info["phones_found"] else ""
                p9_geo_net = f"Регион учёта: {car_info['region']}{vin_str}{ph_str}"
                mentions_preview = [m["title"] for m in car_info.get("web_mentions", [])[:3]]
                p10_aliases = f"Латиница: {car_info['plate_lat']} | Упоминания: {'; '.join(mentions_preview) or 'нет'}"
                raw_sections.append(("АВТОМОБИЛЬ", car_info))

            elif target_type in (TargetType.DOMAIN, TargetType.IP):
                if target_type == TargetType.DOMAIN:
                    dns_info = self.dns_and_ip_resolve(target)
                    subs, breaches = await asyncio.gather(self.crtsh_subdomains(target, session), self.deep_breach_intelligence(target, target_type, session))
                    first_ip = dns_info["A"][0] if dns_info.get("A") else None
                    ip_intel = await self.ip_deep_intelligence(first_ip, session) if first_ip else {}
                    shodan, geo = ip_intel.get("shodan_internetdb", {}), ip_intel.get("geo", {})
                    p6_platforms = f"Поддоменов: {len(subs)} ({', '.join(subs[:5])}) | Порты: {shodan.get('ports', [])}"
                    p7_leaks = f"CVE: {', '.join(shodan.get('vulns', [])[:5]) or 'нет'}"
                    p8_stealers = breaches["stealers"]
                    p9_geo_net = f"IP: {first_ip} ({geo.get('country')}, {geo.get('isp')})"
                    raw_sections.extend([("DNS", dns_info), ("SHODAN", ip_intel), ("ПОДДОМЕНЫ", subs)])
                else:
                    ip_intel = await self.ip_deep_intelligence(target, session)
                    shodan, geo = ip_intel.get("shodan_internetdb", {}), ip_intel.get("geo", {})
                    p6_platforms = f"Порты (Shodan): {shodan.get('ports', [])} | Хосты: {shodan.get('hostnames', [])}"
                    p7_leaks = f"CVE: {', '.join(shodan.get('vulns', [])[:6]) or 'нет'}"
                    p9_geo_net = f"{geo.get('country')}, {geo.get('city')} | ISP: {geo.get('isp')} ({geo.get('as')})"
                    raw_sections.append(("SHODAN & IP", ip_intel))

        esc = html_mod.escape
        short_10_html = "\n".join([
            "🕵️‍♂️ <b>ДОСЬЕ OSINT-РАЗВЕДКИ</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            f"1️⃣ <b>Объект:</b> <code>{esc(p1_target)}</code>",
            f"2️⃣ <b>Имя / ФИО / Авто:</b> {esc(p2_name)}",
            f"3️⃣ <b>Telegram и WhatsApp:</b> {esc(p3_tg)}",
            f"4️⃣ <b>ВКонтакте / Реестры:</b> {esc(p4_vk)}",
            f"5️⃣ <b>TikTok и YouTube:</b> {esc(p5_tt_yt)}",
            f"6️⃣ <b>Сервисы и площадки:</b> {esc(p6_platforms)}",
            f"7️⃣ <b>Слитые базы и уязвимости:</b> {esc(p7_leaks)}",
            f"8️⃣ <b>Логи инфостилеров:</b> {esc(p8_stealers)}",
            f"9️⃣ <b>Телефон / IP / VIN / Гео:</b> {esc(p9_geo_net)}",
            f"🔟 <b>Найденные ники и связи:</b> {esc(p10_aliases)}",
            "━━━━━━━━━━━━━━━━━━━━",
        ])
        short_10_plain = re.sub(r"<[^>]+>", "", short_10_html)
        if not deep:
            return short_10_html, short_10_plain

        full_blocks = [short_10_plain, "", "==================================================", " ПОЛНЫЙ РАЗВЁРНУТЫЙ ОТЧЁТ (DEEP MODE + PIVOTING)", "=================================================="]
        for title, data in raw_sections:
            formatted = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            full_blocks.append(f"\n[ {title} ]\n{'-' * len(title)}\n{formatted}")
        return short_10_html, "\n".join(full_blocks)