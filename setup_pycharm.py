import os
import zipfile

FILES = {}

# ==================== 1. REQUIREMENTS.TXT ====================
FILES["requirements.txt"] = """aiogram>=3.13.0
aiohttp>=3.10.0
aiosqlite>=0.20.0
pydantic-settings>=2.4.0
exifread>=3.0.0
phonenumbers>=8.13.45
beautifulsoup4>=4.12.3
dnspython>=2.6.1
socid-extractor>=0.0.27
maigret>=0.4.4
holehe>=1.61
ignorant>=1.2
"""

# ==================== 2. .ENV (НАСТРОЙКИ) ====================
FILES[".env"] = """BOT_TOKEN=ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER
ADMIN_PASSWORD=admin2026
ADMIN_IDS=[]
VK_SERVICE_TOKEN=
FREE_WEEKLY_LIMIT=5
PRICE_SINGLE_DEEP_XTR=150
PRICE_WEEK_XTR=450
PRICE_MONTH_XTR=1000
PRICE_QUARTER_XTR=2500
"""

# ==================== 3. CONFIG.PY ====================
FILES["config.py"] = """from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_PASSWORD: str = "admin2026"
    ADMIN_IDS: list[int] = []
    DB_PATH: str = "osint_bot.db"

    FREE_WEEKLY_LIMIT: int = 5
    HISTORY_RETENTION_DAYS: int = 7
    TG_MSG_CHAR_LIMIT: int = 4000

    PRICE_SINGLE_DEEP_XTR: int = 150
    PRICE_WEEK_XTR: int = 450
    PRICE_MONTH_XTR: int = 1000
    PRICE_QUARTER_XTR: int = 2500

    VK_SERVICE_TOKEN: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()

TARIFFS = {
    "single_deep": {
        "title": "🚀 Разовый DEEP-поиск (1 шт.)",
        "days": 0,
        "deep_credits": 1,
        "price": settings.PRICE_SINGLE_DEEP_XTR,
    },
    "prem_7": {
        "title": "💎 Premium на 7 дней (Безлимит)",
        "days": 7,
        "deep_credits": 0,
        "price": settings.PRICE_WEEK_XTR,
    },
    "prem_30": {
        "title": "👑 Premium на 30 дней (Безлимит)",
        "days": 30,
        "deep_credits": 0,
        "price": settings.PRICE_MONTH_XTR,
    },
    "prem_90": {
        "title": "🔥 VIP Premium на 90 дней (Безлимит)",
        "days": 90,
        "deep_credits": 0,
        "price": settings.PRICE_QUARTER_XTR,
    },
}
"""

# ==================== 4. VALIDATORS.PY ====================
FILES["validators.py"] = """import re
import ipaddress
from enum import Enum


class TargetType(str, Enum):
    IP = "ip"
    DOMAIN = "domain"
    EMAIL = "email"
    COORDS = "coords"
    PHONE = "phone"
    TG_ID = "tg_id"
    FULLNAME = "fullname"
    USERNAME = "username"
    UNKNOWN = "unknown"


EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$")
DOMAIN_RE = re.compile(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\\.)+[a-zA-Z]{2,}$")
COORDS_RE = re.compile(r"^(-?\\d+(\\.\\d+)?),\\s*(-?\\d+(\\.\\d+)?)$")
PHONE_RE = re.compile(r"^\\+?[7891-9]\\d{8,14}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
FULLNAME_RE = re.compile(r"^[а-яА-ЯёЁa-zA-Z]{2,25}\\s+[а-яА-ЯёЁa-zA-Z]{2,25}(?:\\s+[а-яА-ЯёЁa-zA-Z]{2,25})?$")
SOCIAL_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\\.)?(?:t\\.me|vk\\.com|github\\.com|instagram\\.com|tiktok\\.com|youtube\\.com)/@?([a-zA-Z0-9_.-]+)/?$",
    re.IGNORECASE
)


def normalize_target_key(raw: str) -> str:
    _, clean = detect_target_type(raw)
    return clean.strip().lower().lstrip("@")


def detect_target_type(raw_target: str) -> tuple[TargetType, str]:
    clean = raw_target.strip()

    url_match = SOCIAL_URL_RE.match(clean)
    if url_match:
        extracted = url_match.group(1)
        if extracted.startswith("id") and extracted[2:].isdigit():
            return TargetType.TG_ID, extracted[2:]
        return TargetType.USERNAME, extracted

    try:
        ipaddress.ip_address(clean)
        return TargetType.IP, clean
    except ValueError:
        pass

    if COORDS_RE.match(clean):
        return TargetType.COORDS, clean

    if EMAIL_RE.match(clean):
        return TargetType.EMAIL, clean.lower()

    if clean.isdigit() and 5 <= len(clean) <= 11:
        return TargetType.TG_ID, clean

    phone_candidate = re.sub(r"[\\s\\-\\(\\)]", "", clean)
    if PHONE_RE.match(phone_candidate):
        if phone_candidate.startswith("8") and len(phone_candidate) == 11:
            phone_candidate = "+7" + phone_candidate[1:]
        elif not phone_candidate.startswith("+"):
            phone_candidate = "+" + phone_candidate
        return TargetType.PHONE, phone_candidate

    if FULLNAME_RE.match(clean):
        return TargetType.FULLNAME, clean

    if DOMAIN_RE.match(clean) and not clean.startswith("@"):
        return TargetType.DOMAIN, clean.lower()

    if USERNAME_RE.match(clean.lstrip("@")):
        return TargetType.USERNAME, clean.lstrip("@")

    return TargetType.UNKNOWN, clean
"""

# ==================== 5. DATABASE.PY ====================
FILES["database.py"] = """import secrets
import aiosqlite
from datetime import datetime, timedelta, timezone
from config import settings
from validators import normalize_target_key


async def init_db():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                premium_until TIMESTAMP,
                deep_credits INTEGER DEFAULT 0,
                weekly_used INTEGER DEFAULT 0,
                week_reset_at TIMESTAMP,
                created_at TIMESTAMP
            )
        \"\"\")
        await db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tariff_key TEXT,
                amount_xtr INTEGER,
                charge_id TEXT,
                telegram_payment_charge_id TEXT UNIQUE,
                created_at TIMESTAMP
            )
        \"\"\")
        await db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target TEXT,
                mode TEXT,
                content TEXT,
                created_at TIMESTAMP
            )
        \"\"\")
        await db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS blacklist (
                target_norm TEXT PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMP
            )
        \"\"\")
        await db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS gift_codes (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses_left INTEGER DEFAULT 1,
                created_at TIMESTAMP
            )
        \"\"\")
        await db.commit()


async def cleanup_old_reports():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.HISTORY_RETENTION_DAYS)).isoformat()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM reports WHERE created_at < ?", (cutoff,))
        await db.commit()


async def get_or_create_user(user_id: int, username: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    clean_uname = username.lower().lstrip("@") if username else None
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()

        if not row:
            week_reset = (now + timedelta(days=7)).isoformat()
            await db.execute(
                \"\"\"INSERT INTO users 
                   (user_id, username, premium_until, deep_credits, weekly_used, week_reset_at, created_at)
                   VALUES (?, ?, NULL, 0, 0, ?, ?)\"\"\",
                (user_id, clean_uname, week_reset, now.isoformat())
            )
            await db.commit()
            return {
                "user_id": user_id,
                "username": clean_uname,
                "is_premium": False,
                "premium_until": None,
                "deep_credits": 0,
                "weekly_used": 0,
            }

        if clean_uname and row["username"] != clean_uname:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (clean_uname, user_id))
            await db.commit()

        week_reset_at = datetime.fromisoformat(row["week_reset_at"])
        weekly_used = row["weekly_used"]
        if now >= week_reset_at:
            weekly_used = 0
            week_reset_at = now + timedelta(days=7)
            await db.execute(
                "UPDATE users SET weekly_used = 0, week_reset_at = ? WHERE user_id = ?",
                (week_reset_at.isoformat(), user_id)
            )
            await db.commit()

        is_premium = False
        premium_until = row["premium_until"]
        if premium_until:
            is_premium = datetime.fromisoformat(premium_until) > now

        return {
            "user_id": user_id,
            "username": clean_uname or row["username"],
            "is_premium": is_premium,
            "premium_until": premium_until,
            "deep_credits": row["deep_credits"],
            "weekly_used": weekly_used,
        }


async def increment_free_usage(user_id: int):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("UPDATE users SET weekly_used = weekly_used + 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def consume_deep_credit(user_id: int) -> bool:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.execute("SELECT deep_credits FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if row and row[0] > 0:
                await db.execute("UPDATE users SET deep_credits = deep_credits - 1 WHERE user_id = ?", (user_id,))
                await db.commit()
                return True
    return False


async def apply_tariff(user_id: int, tariff_key: str, days: int, credits: int, amount_xtr: int, charge_id: str, tg_charge_id: str):
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(user_id)

    async with aiosqlite.connect(settings.DB_PATH) as db:
        if days > 0:
            base = datetime.fromisoformat(user["premium_until"]) if (user["is_premium"] and user["premium_until"]) else now
            new_until = (base + timedelta(days=days)).isoformat()
            await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_until, user_id))
        if credits > 0:
            await db.execute("UPDATE users SET deep_credits = deep_credits + ? WHERE user_id = ?", (credits, user_id))

        await db.execute(
            \"\"\"INSERT INTO payments (user_id, tariff_key, amount_xtr, charge_id, telegram_payment_charge_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)\"\"\",
            (user_id, tariff_key, amount_xtr, charge_id, tg_charge_id, now.isoformat())
        )
        await db.commit()


async def save_report(user_id: int, target: str, mode: str, content: str) -> int:
    await cleanup_old_reports()
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reports (user_id, target, mode, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, target, mode, content, now)
        )
        await db.commit()
        return cur.lastrowid


async def get_user_reports(user_id: int) -> list[dict]:
    await cleanup_old_reports()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, target, mode, created_at FROM reports WHERE user_id = ? ORDER BY id DESC LIMIT 15",
            (user_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_report_by_id(report_id: int, user_id: int) -> dict | None:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reports WHERE id = ? AND user_id = ?", (report_id, user_id)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def add_to_blacklist(raw_target: str, reason: str = "Защищено администратором") -> str:
    norm = normalize_target_key(raw_target)
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO blacklist (target_norm, reason, created_at) VALUES (?, ?, ?)",
            (norm, reason, now)
        )
        await db.commit()
    return norm


async def remove_from_blacklist(raw_target: str) -> str:
    norm = normalize_target_key(raw_target)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM blacklist WHERE target_norm = ?", (norm,))
        await db.commit()
    return norm


async def is_target_blocked(raw_target: str) -> bool:
    norm = normalize_target_key(raw_target)
    if not norm:
        return False
    async with aiosqlite.connect(settings.DB_PATH) as db:
        async with db.execute("SELECT 1 FROM blacklist WHERE target_norm = ?", (norm,)) as cur:
            return (await cur.fetchone()) is not None


async def get_blacklist() -> list[dict]:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM blacklist ORDER BY created_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_admin_stats() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        u_total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        u_prem = (await (await db.execute("SELECT COUNT(*) FROM users WHERE premium_until > ?", (now,))).fetchone())[0]
        r_total = (await (await db.execute("SELECT COUNT(*) FROM reports")).fetchone())[0]
        b_total = (await (await db.execute("SELECT COUNT(*) FROM blacklist")).fetchone())[0]
        xtr_sum = (await (await db.execute("SELECT COALESCE(SUM(amount_xtr), 0) FROM payments")).fetchone())[0]
        return {"users": u_total, "premium": u_prem, "reports": r_total, "blocked": b_total, "xtr_total": xtr_sum}


async def get_all_user_ids() -> list[int]:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        rows = await (await db.execute("SELECT user_id FROM users")).fetchall()
        return [r[0] for r in rows]


async def admin_grant_premium_by_identifier(identifier: str, days: int) -> tuple[bool, int | None, str]:
    clean = identifier.strip().lstrip("@").lower()
    now = datetime.now(timezone.utc)
    new_until = (now + timedelta(days=days)).isoformat() if days > 0 else None

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        target_uid = None
        if clean.isdigit():
            target_uid = int(clean)
            await get_or_create_user(target_uid)
        else:
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (clean,)) as cur:
                row = await cur.fetchone()
                if row:
                    target_uid = row["user_id"]

        if not target_uid:
            return False, None, f"Пользователь @{clean} ещё не запускал бота. Создайте для него подарочный промокод в меню админки!"

        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_until, target_uid))
        await db.commit()
        status_str = f"до {new_until[:10]}" if new_until else "СНЯТ"
        return True, target_uid, status_str


async def create_gift_code(days: int, uses: int = 1) -> str:
    code = f"GIFT-{days}D-{secrets.token_hex(3).upper()}"
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute(
            "INSERT INTO gift_codes (code, days, uses_left, created_at) VALUES (?, ?, ?, ?)",
            (code, days, uses, now)
        )
        await db.commit()
    return code


async def redeem_gift_code(user_id: int, code: str) -> tuple[bool, int]:
    clean_code = code.strip().upper()
    now = datetime.now(timezone.utc)
    user = await get_or_create_user(user_id)

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM gift_codes WHERE code = ? AND uses_left > 0", (clean_code,)) as cur:
            row = await cur.fetchone()
            if not row:
                return False, 0

        days = row["days"]
        base = datetime.fromisoformat(user["premium_until"]) if (user["is_premium"] and user["premium_until"]) else now
        new_until = (base + timedelta(days=days)).isoformat()

        await db.execute("UPDATE gift_codes SET uses_left = uses_left - 1 WHERE code = ?", (clean_code,))
        await db.execute("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_until, user_id))
        await db.commit()
        return True, days
"""

# ==================== 6. OSINT_ENGINE.PY ====================
FILES["osint_engine.py"] = """import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from io import BytesIO

import aiohttp
import dns.resolver
import exifread
import phonenumbers
from bs4 import BeautifulSoup
from phonenumbers import carrier, geocoder, timezone as pn_timezone

from config import settings
from database import is_target_blocked
from validators import TargetType


async def run_cli(args: list[str], timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = stdout.decode("utf-8", errors="ignore").strip()
        return out if out else "Совпадений не найдено."
    except FileNotFoundError:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="ignore").strip()
            return out if out else "Совпадений не найдено."
        except Exception:
            return f"Утилита {args[0]} не установлена."
    except asyncio.TimeoutError:
        return f"Таймаут ({timeout} с)."
    except Exception as e:
        return f"Ошибка {args[0]}: {e}"


class OSINTEngine:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=25)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
        }

    async def run_maigret(self, username: str, full: bool = False) -> str:
        sites = "300" if full else "50"
        t_out = 150 if full else 45
        return await run_cli(["maigret", username, "--top-sites", sites, "--no-color", "--timeout", "8"], timeout=t_out)

    async def run_holehe(self, email: str) -> str:
        return await run_cli(["holehe", email, "--only-used", "--no-color"], timeout=45)

    async def run_ignorant(self, phone: str) -> str:
        clean_digits = re.sub(r"\\D", "", phone)
        if clean_digits.startswith("7") and len(clean_digits) == 11:
            return await run_cli(["ignorant", "7", clean_digits[1:], "--only-used", "--no-color"], timeout=40)
        return "Не найдено"

    async def tiktok_youtube_lookup(self, username: str, session: aiohttp.ClientSession) -> dict:
        res = {"tiktok": "Не найден", "youtube": "Не найден"}

        tt_url = f"https://www.tiktok.com/@{username}"
        try:
            async with session.get(tt_url, headers=self.headers) as r:
                if r.status == 200:
                    html = await r.text()
                    soup = BeautifulSoup(html, "html.parser")
                    title_el = soup.find("title")
                    title_txt = title_el.get_text(strip=True) if title_el else ""
                    if title_txt and "TikTok" in title_txt and f"@{username.lower()}" in html.lower():
                        clean_t = title_txt.replace("| TikTok", "").strip()
                        res["tiktok"] = f"{clean_t} ({tt_url})"
        except Exception:
            pass

        yt_url = f"https://www.youtube.com/@{username}"
        try:
            async with session.get(yt_url, headers=self.headers) as r:
                if r.status == 200:
                    html = await r.text()
                    soup = BeautifulSoup(html, "html.parser")
                    og_title = soup.find("meta", property="og:title")
                    if og_title and og_title.has_attr("content"):
                        res["youtube"] = f"{og_title['content']} ({yt_url})"
        except Exception:
            pass

        return res

    async def deep_breach_intelligence(self, target: str, target_type: TargetType, session: aiohttp.ClientSession) -> dict:
        intel = {"databases": [], "fields": [], "stealers": "Чисто", "commits": [], "xposed_details": []}

        if target_type in (TargetType.EMAIL, TargetType.USERNAME):
            try:
                async with session.get(f"https://leakcheck.io/api/public?check={target}", headers=self.headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data.get("success") and data.get("found", 0) > 0:
                            intel["fields"] = data.get("fields", [])
                            intel["databases"] = [
                                f"{s.get('name')} ({s.get('date') or 'н/д'})"
                                for s in data.get("sources", [])
                            ]
            except Exception:
                pass

        if target_type == TargetType.EMAIL:
            try:
                async with session.get(f"https://api.xposedornot.com/v1/breach-analytics?email={target}", headers=self.headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        details = data.get("ExposedBreaches", {}).get("breaches_details", [])
                        for b in details[:12]:
                            b_name = f"{b.get('breach')} ({b.get('xposed_date')})"
                            if b_name not in intel["databases"]:
                                intel["databases"].append(b_name)
                            intel["xposed_details"].append({
                                "db": b.get("breach"),
                                "year": b.get("xposed_date"),
                                "data": b.get("xposed_data")
                            })
            except Exception:
                pass

        if target_type in (TargetType.EMAIL, TargetType.USERNAME, TargetType.DOMAIN):
            ep_map = {
                TargetType.EMAIL: f"search-by-email?email={target}",
                TargetType.USERNAME: f"search-by-username?username={target}",
                TargetType.DOMAIN: f"search-by-domain?domain={target}"
            }
            try:
                url = f"https://cavalier.hudsonrock.com/api/json/v2/osint-tools/{ep_map[target_type]}"
                async with session.get(url, headers=self.headers) as r:
                    if r.status == 200:
                        data = await r.json()
                        stealers = data.get("stealers", [])
                        if stealers:
                            intel["stealers"] = f"Обнаружено записей в логах инфостилеров: {len(stealers)}"
                            intel["stealers_raw"] = stealers[:5]
            except Exception:
                pass

        if target_type in (TargetType.EMAIL, TargetType.USERNAME):
            try:
                gh_headers = {**self.headers, "Accept": "application/vnd.github.cloak-preview+json"}
                q = f"author-email:{target}" if target_type == TargetType.EMAIL else f"author:{target}"
                async with session.get(f"https://api.github.com/search/commits?q={q}&per_page=5", headers=gh_headers) as r:
                    if r.status == 200:
                        for item in (await r.json()).get("items", []):
                            a = item.get("commit", {}).get("author", {})
                            repo = item.get("repository", {}).get("full_name")
                            entry = f"{a.get('name')} <{a.get('email')}> ({repo})"
                            if entry not in intel["commits"]:
                                intel["commits"].append(entry)
            except Exception:
                pass

        return intel

    async def vk_lookup(self, query: str, session: aiohttp.ClientSession, by_name: bool = False) -> dict | list:
        if not settings.VK_SERVICE_TOKEN and not by_name:
            url = f"https://vk.com/{query}"
            try:
                async with session.get(url, headers=self.headers) as r:
                    if r.status == 200:
                        soup = BeautifulSoup(await r.text(), "html.parser")
                        title = soup.find("title")
                        og_title = soup.find("meta", property="og:title")
                        if title and ("ВКонтакте" in title.text or "VK" in title.text):
                            name = og_title["content"] if (og_title and og_title.has_attr("content")) else title.text.split("|")[0].strip()
                            return {"found": True, "full_name": name, "url": url}
            except Exception:
                pass
            return {"found": False}

        if by_name and settings.VK_SERVICE_TOKEN:
            url = "https://api.vk.com/method/users.search"
            params = {"q": query, "count": 5, "fields": "bdate,city,domain", "access_token": settings.VK_SERVICE_TOKEN, "v": "5.199", "lang": "ru"}
            async with session.get(url, params=params) as r:
                items = (await r.json()).get("response", {}).get("items", [])
                return [
                    {
                        "id": u.get("id"),
                        "full_name": f"{u.get('first_name')} {u.get('last_name')}",
                        "city": u.get("city", {}).get("title"),
                        "bdate": u.get("bdate"),
                        "url": f"https://vk.com/{u.get('domain')}"
                    }
                    for u in items
                ]

        if settings.VK_SERVICE_TOKEN:
            url = "https://api.vk.com/method/users.get"
            params = {"user_ids": query, "fields": "first_name,last_name,bdate,city,domain,status", "access_token": settings.VK_SERVICE_TOKEN, "v": "5.199", "lang": "ru"}
            async with session.get(url, params=params) as r:
                users = (await r.json()).get("response", [])
                if users:
                    u = users[0]
                    return {
                        "found": True,
                        "vk_id": u.get("id"),
                        "full_name": f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(),
                        "url": f"https://vk.com/{u.get('domain')}",
                        "bdate": u.get("bdate", "Скрыта"),
                        "city": u.get("city", {}).get("title", "Скрыт"),
                        "status": u.get("status", "")
                    }
        return {"found": False}

    @staticmethod
    def estimate_tg_reg_date(tg_id: int) -> dict:
        checkpoints = [
            (1000000, 1380585600), (10000000, 1414800000), (50000000, 1451606400),
            (100000000, 1475280000), (300000000, 1498867200), (500000000, 1525132800),
            (800000000, 1559347200), (1100000000, 1588291200), (1500000000, 1609459200),
            (2000000000, 1633046400), (5000000000, 1654041600), (6000000000, 1685577600),
            (7000000000, 1711929600), (7500000000, 1735689600), (8000000000, 1767225600)
        ]
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
        return {
            "telegram_id": tg_id,
            "estimated_registration": datetime.fromtimestamp(est_ts, tz=timezone.utc).strftime("%Y-%m (%B %Y)")
        }

    async def telegram_web_profile(self, username: str, session: aiohttp.ClientSession) -> dict:
        url = f"https://t.me/{username}"
        try:
            async with session.get(url, headers=self.headers) as r:
                if r.status == 200:
                    soup = BeautifulSoup(await r.text(), "html.parser")
                    title = soup.find("div", class_="tgme_page_title")
                    desc = soup.find("div", class_="tgme_page_description")
                    if title:
                        return {
                            "exists": True,
                            "url": url,
                            "display_name": title.get_text(strip=True),
                            "bio": desc.get_text(strip=True)[:100] if desc else ""
                        }
        except Exception:
            pass
        return {"exists": False}

    async def check_second_nicknames(self, username: str, session: aiohttp.ClientSession) -> dict:
        base = username.lower()
        clean = re.sub(r"[_\\.\\-]", "", base)
        variants = sorted(list({
            f"{clean}_", f"_{clean}", f"{clean}1", f"real_{clean}",
            f"{clean}.official", base.replace("_", "."), base.replace(".", "_")
        } - {base}))[:6]

        found_tg = []
        for v in variants[:4]:
            prof = await self.telegram_web_profile(v, session)
            if prof.get("exists"):
                found_tg.append(f"@{v} ({prof.get('display_name')})")
        return {"generated_aliases": variants, "active_in_telegram": found_tg}

    async def gravatar_lookup(self, email: str, session: aiohttp.ClientSession) -> dict:
        h = hashlib.md5(email.strip().lower().encode()).hexdigest()
        try:
            async with session.get(f"https://en.gravatar.com/{h}.json") as r:
                if r.status == 200:
                    entry = (await r.json()).get("entry", [{}])[0]
                    return {
                        "username": entry.get("preferredUsername"),
                        "display_name": entry.get("displayName"),
                        "full_name": entry.get("name", {}).get("formatted"),
                        "location": entry.get("currentLocation")
                    }
        except Exception:
            pass
        return {}

    async def ip_free_lookup(self, ip: str, session: aiohttp.ClientSession) -> dict:
        try:
            async with session.get(f"https://ipwho.is/{ip}") as r:
                if r.status == 200:
                    d = await r.json()
                    return {
                        "ip": d.get("ip"),
                        "country": d.get("country"),
                        "city": d.get("city"),
                        "isp": d.get("connection", {}).get("isp"),
                        "asn": d.get("connection", {}).get("asn")
                    }
        except Exception:
            pass
        return {}

    @staticmethod
    def dns_and_ip_resolve(domain: str) -> dict:
        res = {"A": [], "MX": [], "TXT": [], "NS": []}
        for rtype in res.keys():
            try:
                answers = dns.resolver.resolve(domain, rtype, lifetime=5)
                res[rtype] = [str(r) for r in answers]
            except Exception:
                pass
        return res

    async def crtsh_subdomains(self, domain: str, session: aiohttp.ClientSession) -> list[str]:
        try:
            async with session.get(f"https://crt.sh/?q=%.{domain}&output=json") as r:
                if r.status == 200:
                    data = await r.json()
                    subs = {s.strip() for item in data for s in item.get("name_value", "").split("\\n")}
                    return sorted(list(subs))[:60]
        except Exception:
            pass
        return []

    @staticmethod
    def phone_lookup(phone: str) -> dict:
        try:
            p = phonenumbers.parse(phone, None)
            return {
                "formatted": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL),
                "valid": phonenumbers.is_valid_number(p),
                "region": geocoder.description_for_number(p, "ru"),
                "carrier": carrier.name_for_number(p, "ru") or "Не определён (MNP)",
                "timezone": list(pn_timezone.time_zones_for_number(p))
            }
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def extract_exif(file_bytes: bytes) -> dict:
        tags = exifread.process_file(BytesIO(file_bytes), details=False)
        return {k: str(v) for k, v in tags.items() if k not in ("JPEGThumbnail", "TIFFThumbnail")}

    async def execute_scan(self, target: str, target_type: TargetType, deep: bool = False) -> tuple[str, str]:
        if await is_target_blocked(target):
            msg = "🔒 Информация по данному запросу защищена и скрыта администратором."
            return msg, msg

        p1_target = f"{target} ({target_type.value.upper()})"
        p2_name = "Не обнаружено"
        p3_tg = "Не найден"
        p4_vk = "Не найден"
        p5_tt_yt = "TikTok: нет | YouTube: нет"
        p6_platforms = "Нет совпадений"
        p7_leaks = "Утечек не обнаружено"
        p8_stealers = "Чисто (в логах инфостилеров не найден)"
        p9_geo_net = "Нет данных"
        p10_aliases = "Не обнаружено"

        raw_sections: list[tuple[str, any]] = []

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            if target_type == TargetType.USERNAME:
                tg_prof, vk_prof, tt_yt, breaches, aliases, maigret_out = await asyncio.gather(
                    self.telegram_web_profile(target, session),
                    self.vk_lookup(target, session),
                    self.tiktok_youtube_lookup(target, session),
                    self.deep_breach_intelligence(target, target_type, session),
                    self.check_second_nicknames(target, session),
                    self.run_maigret(target, full=deep)
                )

                names_found = []
                if isinstance(vk_prof, dict) and vk_prof.get("found"):
                    names_found.append(f"{vk_prof.get('full_name')} (ВК)")
                    p4_vk = f"{vk_prof.get('url')} ({vk_prof.get('full_name')})"
                if tg_prof.get("exists"):
                    names_found.append(f"{tg_prof.get('display_name')} (TG)")
                    p3_tg = f"{tg_prof.get('url')} — Имя: {tg_prof.get('display_name')}"
                if names_found:
                    p2_name = " / ".join(names_found)

                p5_tt_yt = f"TikTok: {tt_yt['tiktok']} | YouTube: {tt_yt['youtube']}"

                found_sites = [line.strip() for line in maigret_out.splitlines() if "[+]" in line]
                p6_platforms = (
                    f"Найдено профилей: {len(found_sites)} ({', '.join(s.split()[1].rstrip(':') for s in found_sites[:6])})"
                    if found_sites else "Совпадений в топ-сайтах нет"
                )

                if breaches["databases"]:
                    p7_leaks = f"Найдено баз ({len(breaches['databases'])}): {', '.join(breaches['databases'][:5])}"
                p8_stealers = breaches["stealers"]

                if isinstance(vk_prof, dict) and vk_prof.get("city"):
                    p9_geo_net = f"Город (из ВК): {vk_prof.get('city')}"

                alias_parts = []
                if aliases.get("active_in_telegram"):
                    alias_parts.append("TG-алиасы: " + ", ".join(aliases["active_in_telegram"]))
                else:
                    alias_parts.append("Вероятные ники: " + ", ".join(aliases.get("generated_aliases", [])[:4]))
                if breaches.get("commits"):
                    alias_parts.append("GitHub: " + breaches["commits"][0])
                p10_aliases = " | ".join(alias_parts)

                raw_sections.extend([
                    ("TELEGRAM ПРОФИЛЬ", tg_prof),
                    ("ВКОНТАКТЕ", vk_prof),
                    ("TIKTOK И YOUTUBE", tt_yt),
                    ("СЛИТЫЕ БАЗЫ И УТЕЧКИ", breaches),
                    ("ВОЗМОЖНЫЕ ВТОРЫЕ НИКНЕЙМЫ", aliases),
                    ("MAIGRET (ПОЛНЫЙ СПИСОК АККАУНТОВ)", maigret_out)
                ])

            elif target_type == TargetType.EMAIL:
                grav, breaches, holehe_out = await asyncio.gather(
                    self.gravatar_lookup(target, session),
                    self.deep_breach_intelligence(target, target_type, session),
                    self.run_holehe(target)
                )
                pivot_nick = grav.get("username") or target.split("@")[0]

                if await is_target_blocked(pivot_nick):
                    msg = "🔒 Информация по данному запросу защищена и скрыта администратором."
                    return msg, msg

                if grav.get("full_name") or grav.get("display_name"):
                    p2_name = f"{grav.get('full_name') or grav.get('display_name')} (Gravatar)"
                elif breaches.get("commits"):
                    p2_name = breaches["commits"][0].split("<")[0].strip() + " (GitHub)"

                used_services = [l.strip() for l in holehe_out.splitlines() if "[+]" in l]
                p6_platforms = (
                    f"Регистраций почты ({len(used_services)}): {', '.join(s.replace('[+]', '').strip() for s in used_services[:7])}"
                    if used_services else "Активных регистраций не найдено"
                )

                if breaches["databases"]:
                    p7_leaks = f"Найдено баз ({len(breaches['databases'])}): {', '.join(breaches['databases'][:6])} | Поля: {', '.join(breaches['fields'][:5])}"
                p8_stealers = breaches["stealers"]
                p9_geo_net = grav.get("location") or f"Почтовый домен: {target.split('@')[-1]}"
                p10_aliases = f"Возможный ник: @{pivot_nick}" + (f" | GitHub: {breaches['commits'][0]}" if breaches.get("commits") else "")

                raw_sections.extend([
                    ("GRAVATAR", grav),
                    ("СЛИТЫЕ БАЗЫ И УТЕЧКИ", breaches),
                    ("HOLEHE (СЕРВИСЫ)", holehe_out)
                ])

                if len(pivot_nick) >= 4:
                    tg_p, vk_p, tt_yt_p = await asyncio.gather(
                        self.telegram_web_profile(pivot_nick, session),
                        self.vk_lookup(pivot_nick, session),
                        self.tiktok_youtube_lookup(pivot_nick, session)
                    )
                    if tg_p.get("exists"):
                        p3_tg = f"По нику @{pivot_nick}: {tg_p.get('url')} ({tg_p.get('display_name')})"
                    if isinstance(vk_p, dict) and vk_p.get("found"):
                        p4_vk = f"По нику @{pivot_nick}: {vk_p.get('url')} ({vk_p.get('full_name')})"
                        if p2_name == "Не обнаружено":
                            p2_name = f"{vk_p.get('full_name')} (ВК по нику почты)"
                    p5_tt_yt = f"TikTok: {tt_yt_p['tiktok']} | YouTube: {tt_yt_p['youtube']}"

                    if deep:
                        maigret_p = await self.run_maigret(pivot_nick, full=False)
                        raw_sections.extend([
                            (f"🔗 PIVOTING (@{pivot_nick}): TELEGRAM / ВК / TT / YT", {"tg": tg_p, "vk": vk_p, "tt_yt": tt_yt_p}),
                            (f"🔗 PIVOTING (@{pivot_nick}): MAIGRET", maigret_p)
                        ])

            elif target_type == TargetType.PHONE:
                p_meta = self.phone_lookup(target)
                ig_out = await self.run_ignorant(target)
                p9_geo_net = f"Регион: {p_meta.get('region')} | Оператор: {p_meta.get('carrier')} | Пояс: {', '.join(p_meta.get('timezone', []))}"
                used_ig = [l.strip() for l in ig_out.splitlines() if "[+]" in l]
                p6_platforms = f"Привязка номера: {', '.join(used_ig)}" if used_ig else "Привязок в Ignorant не обнаружено"
                raw_sections.extend([("МЕТАДАННЫЕ НОМЕРА", p_meta), ("IGNORANT", ig_out)])

            elif target_type == TargetType.TG_ID:
                reg = self.estimate_tg_reg_date(int(target))
                vk_id_info = await self.vk_lookup(target, session)
                p3_tg = f"ID: {target} | Оценка даты регистрации в TG: {reg['estimated_registration']}"
                if isinstance(vk_id_info, dict) and vk_id_info.get("found"):
                    p2_name = f"{vk_id_info.get('full_name')} (ВК id{target})"
                    p4_vk = f"{vk_id_info.get('url')} ({vk_id_info.get('full_name')})"
                raw_sections.extend([("TELEGRAM ID АНАЛИЗ", reg), ("ВКОНТАКТЕ ПО ID", vk_id_info)])

            elif target_type == TargetType.FULLNAME:
                p2_name = target
                vk_list = await self.vk_lookup(target, session, by_name=True)
                if isinstance(vk_list, list) and vk_list:
                    top_matches = [f"{u['full_name']} ({u.get('city') or 'н/д'}, ДР: {u.get('bdate') or 'н/д'}) - {u['url']}" for u in vk_list[:3]]
                    p4_vk = " ; ".join(top_matches)
                else:
                    p4_vk = "Для поиска по ФИО в ВК укажите VK_SERVICE_TOKEN в .env"
                raw_sections.append(("ПОИСК ПО ФИО В ВК", vk_list))

            elif target_type in (TargetType.DOMAIN, TargetType.IP):
                if target_type == TargetType.DOMAIN:
                    dns_info = self.dns_and_ip_resolve(target)
                    subs, breaches = await asyncio.gather(
                        self.crtsh_subdomains(target, session),
                        self.deep_breach_intelligence(target, target_type, session)
                    )
                    first_ip = dns_info["A"][0] if dns_info.get("A") else None
                    ip_data = await self.ip_free_lookup(first_ip, session) if first_ip else {}
                    p6_platforms = f"Найдено поддоменов (crt.sh): {len(subs)} ({', '.join(subs[:5])})"
                    p8_stealers = breaches["stealers"]
                    p9_geo_net = f"IP: {first_ip} ({ip_data.get('country')}, {ip_data.get('isp')}) | MX: {', '.join(dns_info.get('MX', [])[:2])}"
                    raw_sections.extend([("DNS", dns_info), ("CRT.SH ПОДДОМЕНЫ", subs), ("IP ДАННЫЕ", ip_data), ("УТЕЧКИ ДОМЕНА", breaches)])
                else:
                    ip_data = await self.ip_free_lookup(target, session)
                    p9_geo_net = f"Страна: {ip_data.get('country')}, Город: {ip_data.get('city')}, Провайдер: {ip_data.get('isp')} (ASN: {ip_data.get('asn')})"
                    raw_sections.append(("IP ДАННЫЕ", ip_data))

        short_10 = "\\n".join([
            "📋 ВЫЖИМКА РАЗВЕДКИ (10 ПУНКТОВ)",
            "━━━━━━━━━━━━━━━━━━",
            f"1. 🎯 Объект: {p1_target}",
            f"2. 👤 Имя / ФИО: {p2_name}",
            f"3. 💬 Telegram: {p3_tg}",
            f"4. 🌐 ВКонтакте: {p4_vk}",
            f"5. 🎬 TikTok и YouTube: {p5_tt_yt}",
            f"6. 📱 Сервисы и соцсети: {p6_platforms}",
            f"7. 🚨 Слитые базы (LeakCheck/Xposed): {p7_leaks}",
            f"8. 🦠 Логи инфостилеров: {p8_stealers}",
            f"9. 📡 Гео / Сеть / Оператор: {p9_geo_net}",
            f"10. 🔄 Вторые ники и связи: {p10_aliases}",
        ])

        if not deep:
            return short_10, short_10

        full_blocks = [
            short_10,
            "",
            "==================================================",
            " ПОЛНЫЙ РАЗВЁРНУТЫЙ ОТЧЁТ (DEEP MODE + PIVOTING)",
            "=================================================="
        ]
        for title, data in raw_sections:
            formatted = json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            full_blocks.append(f"\\n[ {title} ]\\n{'-' * len(title)}\\n{formatted}")

        full_text = "\\n".join(full_blocks)
        return short_10, full_text
"""

# ==================== 7. HANDLERS_ADMIN_PAYMENTS.PY ====================
FILES["handlers_admin_payments.py"] = """from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import settings, TARIFFS
from database import (
    get_or_create_user, apply_tariff, get_admin_stats,
    admin_grant_premium_by_identifier, get_all_user_ids,
    add_to_blacklist, remove_from_blacklist, get_blacklist,
    create_gift_code, redeem_gift_code
)

admin_payments_router = Router()

# Сессии авторизованных по паролю админов
AUTHORIZED_ADMINS: set[int] = set(settings.ADMIN_IDS)


class AdminStates(StatesGroup):
    waiting_password = State()
    waiting_grant = State()
    waiting_gift_days = State()
    waiting_block = State()
    waiting_unblock = State()
    waiting_broadcast = State()


def get_tariffs_kb() -> InlineKeyboardMarkup:
    buttons = []
    for key, t in TARIFFS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{t['title']} — {t['price']} ⭐",
            callback_data=f"buy:{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Обновить статистику", callback_data="adm:stats")],
        [InlineKeyboardButton(text="🎁 Выдать/Снять Premium (по ID или @нику)", callback_data="adm:grant")],
        [InlineKeyboardButton(text="🎟 Создать подарочный промокод (для акций)", callback_data="adm:gift")],
        [
            InlineKeyboardButton(text="🛡 Заблокировать поиск цели", callback_data="adm:block"),
            InlineKeyboardButton(text="🔓 Разблокировать цель", callback_data="adm:unblock")
        ],
        [InlineKeyboardButton(text="📋 Список защищённых целей", callback_data="adm:bl_list")],
        [InlineKeyboardButton(text="📢 Сделать рассылку всем", callback_data="adm:broadcast")]
    ])


# ==================== ТАРИФЫ, ПРОМОКОДЫ И ОПЛАТА XTR ====================

@admin_payments_router.message(Command("premium"))
async def cmd_premium(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    status = f"✅ Активен до {user['premium_until'][:10]}" if user["is_premium"] else "❌ Тариф FREE"

    text = (
        "💎 <b>Управление подпиской и тарифами</b>\\n\\n"
        f"▪️ <b>Ваш статус:</b> {status}\\n"
        f"▪️ <b>Разовых DEEP-поисков:</b> {user['deep_credits']} шт.\\n"
        f"▪️ <b>Бесплатных запросов за неделю:</b> {user['weekly_used']} / {settings.FREE_WEEKLY_LIMIT}\\n\\n"
        "🎟 <i>Есть подарочный промокод? Введите:</i> <code>/promo КОД</code>\\n\\n"
        "👇 <i>Выберите тариф (оплата через Telegram Stars):</i>"
    )
    await message.answer(text, reply_markup=get_tariffs_kb(), parse_mode="HTML")


@admin_payments_router.message(Command("promo"))
async def cmd_promo(message: Message, command: CommandObject):
    if not command.args:
        await message.answer("Введите промокод: <code>/promo GIFT-...</code>", parse_mode="HTML")
        return
    ok, days = await redeem_gift_code(message.from_user.id, command.args)
    if ok:
        await message.answer(f"🎉 Промокод активирован! Вам начислено <b>+{days} дн. Premium</b>!", parse_mode="HTML")
    else:
        await message.answer("❌ Промокод недействителен или уже был использован.")


@admin_payments_router.callback_query(F.data.startswith("buy:"))
async def cb_buy_tariff(callback: CallbackQuery, bot: Bot):
    tariff_key = callback.data.split(":", 1)[1]
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await callback.answer("Тариф не найден", show_alert=True)
        return

    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=tariff["title"],
        description="Доступ к глубокому OSINT-поиску (DEEP + Pivoting).",
        payload=f"tariff:{tariff_key}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=tariff["title"], amount=tariff["price"])]
    )


@admin_payments_router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(query.id, ok=True)


@admin_payments_router.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    tariff_key = payment.invoice_payload.split(":", 1)[1]
    tariff = TARIFFS.get(tariff_key, TARIFFS["prem_30"])

    await apply_tariff(
        user_id=message.from_user.id,
        tariff_key=tariff_key,
        days=tariff["days"],
        credits=tariff["deep_credits"],
        amount_xtr=payment.total_amount,
        charge_id=payment.provider_payment_charge_id or "xtr",
        tg_charge_id=payment.telegram_payment_charge_id
    )
    await message.answer(
        f"✅ <b>Оплата успешно проведена!</b>\\nТариф: <b>{tariff['title']}</b> активирован.",
        parse_mode="HTML"
    )


# ==================== АДМИН-ПАНЕЛЬ С ПАРОЛЕМ ====================

async def show_admin_dashboard(message_or_cb: Message | CallbackQuery):
    stats = await get_admin_stats()
    text = (
        "🛠 <b>Закрытая Админ-панель OSINT Бота</b>\\n\\n"
        f"👥 Всего пользователей: <b>{stats['users']}</b>\\n"
        f"💎 Активных Premium: <b>{stats['premium']}</b>\\n"
        f"📁 Отчётов за 7 дней: <b>{stats['reports']}</b>\\n"
        f"🛡 Защищённых целей (в блоке): <b>{stats['blocked']}</b>\\n"
        f"⭐ Заработано всего: <b>{stats['xtr_total']} XTR</b>\\n\\n"
        "Выберите действие на клавиатуре ниже:"
    )
    if isinstance(message_or_cb, Message):
        await message_or_cb.answer(text, reply_markup=get_admin_menu_kb(), parse_mode="HTML")
    else:
        await message_or_cb.message.edit_text(text, reply_markup=get_admin_menu_kb(), parse_mode="HTML")


@admin_payments_router.message(Command("admin"))
async def cmd_admin(message: Message, command: CommandObject, state: FSMContext):
    uid = message.from_user.id
    if command.args and command.args.strip() == settings.ADMIN_PASSWORD:
        AUTHORIZED_ADMINS.add(uid)
        try:
            await message.delete()
        except Exception:
            pass

    if uid in AUTHORIZED_ADMINS:
        await state.clear()
        await show_admin_dashboard(message)
    else:
        await state.set_state(AdminStates.waiting_password)
        await message.answer("🔐 <b>Вход в админ-панель</b>\\nВведите пароль администратора:", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_password)
async def process_admin_password(message: Message, state: FSMContext):
    if message.text and message.text.strip() == settings.ADMIN_PASSWORD:
        AUTHORIZED_ADMINS.add(message.from_user.id)
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("🔓 Пароль принят!")
        await show_admin_dashboard(message)
    else:
        await state.clear()
        await message.answer("❌ Неверный пароль.")


@admin_payments_router.callback_query(F.data.startswith("adm:"))
async def cb_admin_actions(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in AUTHORIZED_ADMINS:
        await callback.answer("Требуется авторизация /admin", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    await callback.answer()

    if action == "stats":
        await show_admin_dashboard(callback)
    elif action == "grant":
        await state.set_state(AdminStates.waiting_grant)
        await callback.message.answer(
            "🎁 <b>Выдача / Снятие Premium</b>\\n"
            "Отправьте через пробел: <code>ID_или_@ник количество_дней</code>\\n"
            "<i>Пример:</i> <code>@ivan_test 30</code> или <code>12345678 0</code> (0 — снять Premium)",
            parse_mode="HTML"
        )
    elif action == "gift":
        await state.set_state(AdminStates.waiting_gift_days)
        await callback.message.answer(
            "🎟 <b>Создание подарочного промокода</b>\\n"
            "Отправьте через пробел: <code>количество_дней количество_активаций</code>\\n"
            "<i>Пример:</i> <code>30 5</code> (код на 30 дней для 5 человек)",
            parse_mode="HTML"
        )
    elif action == "block":
        await state.set_state(AdminStates.waiting_block)
        await callback.message.answer(
            "🛡 <b>Блокировка поиска (Защита данных)</b>\\n"
            "Отправьте любой ник, email, телефон, ID или ссылку, которую бот <b>никому не должен искать</b>:",
            parse_mode="HTML"
        )
    elif action == "unblock":
        await state.set_state(AdminStates.waiting_unblock)
        await callback.message.answer("🔓 Отправьте цель, которую нужно удалить из чёрного списка:")
    elif action == "bl_list":
        bl = await get_blacklist()
        if not bl:
            await callback.message.answer("📋 Чёрный список пуст.")
        else:
            lines = [f"• <code>{item['target_norm']}</code> ({item['created_at'][:10]})" for item in bl[:50]]
            await callback.message.answer("🛡 <b>Защищённые от поиска цели:</b>\\n" + "\\n".join(lines), parse_mode="HTML")
    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.answer("📢 Отправьте текст сообщения для рассылки всем пользователям бота:")


@admin_payments_router.message(AdminStates.waiting_grant)
async def process_grant(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        ident, days_str = message.text.strip().split()
        days = int(days_str)
        ok, target_uid, status_info = await admin_grant_premium_by_identifier(ident, days)
        if ok:
            await message.answer(f"✅ Успешно! Пользователь <code>{ident}</code> (ID: {target_uid}) — Premium {status_info}.", parse_mode="HTML")
            if days > 0 and target_uid:
                try:
                    await bot.send_message(target_uid, f"🎁 Администратор подарил вам подписку <b>PREMIUM на {days} дн.</b>!", parse_mode="HTML")
                except Exception:
                    pass
        else:
            await message.answer(f"⚠️ {status_info}")
    except Exception:
        await message.answer("❌ Неверный формат. Пример: @username 30")


@admin_payments_router.message(AdminStates.waiting_gift_days)
async def process_gift_create(message: Message, state: FSMContext):
    await state.clear()
    try:
        parts = message.text.strip().split()
        days = int(parts[0])
        uses = int(parts[1]) if len(parts) > 1 else 1
        code = await create_gift_code(days, uses)
        await message.answer(
            f"🎟 <b>Подарочный промокод создан!</b>\\n\\n"
            f"Код: <code>{code}</code>\\n"
            f"Срок Premium: <b>{days} дн.</b> | Активаций: <b>{uses}</b>\\n\\n"
            f"Пользователь может активировать его командой:\\n<code>/promo {code}</code>",
            parse_mode="HTML"
        )
    except Exception:
        await message.answer("❌ Ошибка. Отправьте два числа через пробел, например: 30 1")


@admin_payments_router.message(AdminStates.waiting_block)
async def process_block_target(message: Message, state: FSMContext):
    await state.clear()
    norm = await add_to_blacklist(message.text)
    await message.answer(f"🛡 Цель <code>{norm}</code> добавлена в защиту! Теперь бот никому не выдаст по ней данные.", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_unblock)
async def process_unblock_target(message: Message, state: FSMContext):
    await state.clear()
    norm = await remove_from_blacklist(message.text)
    await message.answer(f"🔓 Цель <code>{norm}</code> разблокирована.", parse_mode="HTML")


@admin_payments_router.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    uids = await get_all_user_ids()
    sent = 0
    for uid in uids:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            pass
    await message.answer(f"📢 Рассылка завершена! Доставлено: {sent} из {len(uids)}.")
"""

# ==================== 8. HANDLERS_OSINT.PY ====================
FILES["handlers_osint.py"] = """import asyncio
import html
import time
import uuid
from dataclasses import dataclass, field
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, CommandObject

from config import settings
from database import (
    get_or_create_user, increment_free_usage, consume_deep_credit,
    save_report, get_user_reports, get_report_by_id, is_target_blocked
)
from validators import detect_target_type, TargetType
from osint_engine import OSINTEngine
from handlers_admin_payments import get_tariffs_kb

osint_router = Router()
engine = OSINTEngine()

PENDING_TARGETS: dict[str, str] = {}


@dataclass(order=True)
class ScanJob:
    priority: int
    created_at: float = field(compare=True)
    user_id: int = field(compare=False)
    chat_id: int = field(compare=False)
    status_msg_id: int = field(compare=False)
    target: str = field(compare=False)
    target_type: TargetType = field(compare=False)
    deep: bool = field(compare=False)


task_queue: asyncio.PriorityQueue[ScanJob] = asyncio.PriorityQueue()


async def send_report_result(bot: Bot, chat_id: int, status_msg_id: int | None, target: str, short_10: str, full_text: str, deep: bool):
    if not deep or len(full_text) <= settings.TG_MSG_CHAR_LIMIT:
        body = f"<pre>{html.escape(full_text if deep else short_10)}</pre>"
        if status_msg_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=body, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=body, parse_mode="HTML")
    else:
        preview = f"<pre>{html.escape(short_10)}</pre>\\n\\n📄 <i>Полный развёрнутый отчёт прикреплён в .txt файле ниже:</i>"
        if status_msg_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_msg_id, text=preview, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=preview, parse_mode="HTML")

        txt_bytes = full_text.encode("utf-8")
        safe_name = "".join(c if c.isalnum() else "_" for c in target)[:25]
        doc = BufferedInputFile(txt_bytes, filename=f"osint_{safe_name}.txt")
        await bot.send_document(chat_id=chat_id, document=doc, caption=f"📊 Полный отчёт по цели: {target}")


async def queue_worker(bot: Bot):
    while True:
        job: ScanJob = await task_queue.get()
        try:
            short_10, full_text = await engine.execute_scan(job.target, job.target_type, deep=job.deep)
            await save_report(job.user_id, job.target, "DEEP" if job.deep else "QUICK", full_text)
            await send_report_result(bot, job.chat_id, job.status_msg_id, job.target, short_10, full_text, job.deep)
        except Exception as e:
            await bot.edit_message_text(
                chat_id=job.chat_id, message_id=job.status_msg_id, text=f"⚠️ Ошибка при выполнении поиска: {e}"
            )
        finally:
            task_queue.task_done()


async def enqueue_search(bot: Bot, chat_id: int, user_id: int, username: str | None, raw_target: str, deep: bool):
    if await is_target_blocked(raw_target):
        await bot.send_message(chat_id, "🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>", parse_mode="HTML")
        return

    user = await get_or_create_user(user_id, username)
    target_type, clean_target = detect_target_type(raw_target)

    if target_type == TargetType.UNKNOWN:
        await bot.send_message(chat_id, "❌ Не удалось определить тип цели. Отправьте ник, email, телефон, домен, IP, ID или Имя Фамилию.")
        return

    if deep:
        if not user["is_premium"]:
            used_credit = await consume_deep_credit(user_id)
            if not used_credit:
                await bot.send_message(
                    chat_id,
                    "🔒 Режим <b>DEEP + Pivoting</b> доступен по подписке <b>Premium</b> или за разовый пакет.",
                    reply_markup=get_tariffs_kb(),
                    parse_mode="HTML"
                )
                return
    else:
        if not user["is_premium"] and user["weekly_used"] >= settings.FREE_WEEKLY_LIMIT:
            await bot.send_message(
                chat_id,
                f"⛔ Вы исчерпали бесплатный лимит ({settings.FREE_WEEKLY_LIMIT} запросов в неделю).",
                reply_markup=get_tariffs_kb()
            )
            return
        if not user["is_premium"]:
            await increment_free_usage(user_id)

    priority = 1 if (user["is_premium"] or deep) else 2
    status_msg = await bot.send_message(
        chat_id,
        f"⏳ <b>Поиск запущен...</b>\\n"
        f"Цель: <code>{clean_target}</code> ({target_type.value})\\n"
        f"Режим: <b>{'🚀 DEEP (Полный + Pivoting)' if deep else '⚡ QUICK (10 пунктов без воды)'}</b>",
        parse_mode="HTML"
    )

    await task_queue.put(ScanJob(
        priority=priority,
        created_at=time.time(),
        user_id=user_id,
        chat_id=chat_id,
        status_msg_id=status_msg.message_id,
        target=clean_target,
        target_type=target_type,
        deep=deep
    ))


@osint_router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "🕵️‍♂️ <b>OSINT-бот разведки по открытым источникам</b>\\n\\n"
        "Просто <b>отправьте мне в чат</b> любую цель:\\n"
        "• Никнейм или ссылку (<code>@durov</code>, <code>vk.com/durov</code>, <code>tiktok.com/@nick</code>)\\n"
        "• Email (<code>example@gmail.com</code>)\\n"
        "• Номер телефона (<code>+79991234567</code>)\\n"
        "• Telegram ID / VK ID (<code>123456789</code>) или перешлите сообщение\\n"
        "• Имя и Фамилию (<code>Иван Петров</code>)\\n"
        "• Домен или IP (<code>example.com</code>, <code>1.1.1.1</code>)\\n"
        "• Фотографию файлом (для извлечения EXIF/GPS)\\n\\n"
        "📁 /history — Ваши отчёты за последние 7 дней\\n"
        "💎 /premium — Тарифы и активация промокодов\\n"
        "🔐 /admin — Панель администратора",
        parse_mode="HTML"
    )


@osint_router.message(Command("history"))
async def cmd_history(message: Message):
    reports = await get_user_reports(message.from_user.id)
    if not reports:
        await message.answer("📁 У вас пока нет сохранённых отчётов за последние 7 дней.")
        return

    buttons = []
    for r in reports:
        date_short = r["created_at"][:10]
        buttons.append([InlineKeyboardButton(
            text=f"[{r['mode']}] {r['target']} ({date_short})",
            callback_data=f"hist:{r['id']}"
        )])
    await message.answer(
        "📁 <b>Ваша история поиска (хранится 7 дней):</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@osint_router.callback_query(F.data.startswith("hist:"))
async def cb_history_item(callback: CallbackQuery, bot: Bot):
    rep_id = int(callback.data.split(":", 1)[1])
    rep = await get_report_by_id(rep_id, callback.from_user.id)
    if not rep:
        await callback.answer("Отчёт удалён (прошло более 7 дней).", show_alert=True)
        return
    await callback.answer()
    await send_report_result(bot, callback.message.chat.id, None, rep["target"], rep["content"][:1500], rep["content"], deep=(rep["mode"] == "DEEP"))


@osint_router.message(Command("quick"))
async def cmd_quick(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Укажите цель: <code>/quick цель</code>", parse_mode="HTML")
        return
    await enqueue_search(bot, message.chat.id, message.from_user.id, message.from_user.username, command.args, deep=False)


@osint_router.message(Command("deep"))
async def cmd_deep(message: Message, command: CommandObject, bot: Bot):
    if not command.args:
        await message.answer("Укажите цель: <code>/deep цель</code>", parse_mode="HTML")
        return
    await enqueue_search(bot, message.chat.id, message.from_user.id, message.from_user.username, command.args, deep=True)


@osint_router.message(F.forward_from)
async def handle_forwarded(message: Message):
    u = message.forward_from
    if await is_target_blocked(str(u.id)) or (u.username and await is_target_blocked(u.username)):
        await message.answer("🔒 Информация по данному пользователю защищена администратором.")
        return
    reg = engine.estimate_tg_reg_date(u.id)
    await message.answer(
        f"👤 <b>Данные пересланного профиля:</b>\\n"
        f"Имя: {u.full_name}\\n"
        f"Username: @{u.username or 'нет'}\\n"
        f"Telegram ID: <code>{u.id}</code>\\n"
        f"Регистрация: <b>{reg['estimated_registration']}</b>",
        parse_mode="HTML"
    )


@osint_router.message(F.document | F.photo)
async def handle_exif(message: Message, bot: Bot):
    file_id = message.document.file_id if message.document else message.photo[-1].file_id
    f_info = await bot.get_file(file_id)
    f_bytes = (await bot.download_file(f_info.file_path)).read()
    exif = engine.extract_exif(f_bytes)
    text_rep = json.dumps(exif, indent=2, ensure_ascii=False) if exif else "EXIF метаданные не найдены (отправьте фото файлом/документом)."
    await save_report(message.from_user.id, "EXIF Фото", "QUICK", text_rep)
    await send_report_result(bot, message.chat.id, None, "exif_photo", text_rep, text_rep, deep=False)


@osint_router.message(F.text & ~F.text.startswith("/"))
async def handle_plain_text_target(message: Message):
    if await is_target_blocked(message.text):
        await message.answer("🔒 <b>Информация по данному запросу защищена и скрыта администратором.</b>", parse_mode="HTML")
        return

    t_type, clean_target = detect_target_type(message.text)
    if t_type == TargetType.UNKNOWN:
        await message.answer("❌ Не удалось распознать цель. Отправьте корректный ник, email, телефон, домен, IP или Имя Фамилию.")
        return

    token = uuid.uuid4().hex[:10]
    PENDING_TARGETS[token] = clean_target

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Коротко: 10 пунктов без воды (FREE)", callback_data=f"run:q:{token}")],
        [InlineKeyboardButton(text="🚀 Полный отчёт + Pivoting (DEEP)", callback_data=f"run:d:{token}")]
    ])
    await message.answer(
        f"🎯 Распознана цель: <code>{clean_target}</code>\\n"
        f"Тип данных: <b>{t_type.value.upper()}</b>\\n\\n"
        "Выберите вариант отчёта:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@osint_router.callback_query(F.data.startswith("run:"))
async def cb_run_selected_mode(callback: CallbackQuery, bot: Bot):
    _, mode_flag, token = callback.data.split(":")
    target = PENDING_TARGETS.get(token)
    if not target:
        await callback.answer("Запрос устарел, отправьте цель заново.", show_alert=True)
        return

    await callback.answer()
    await callback.message.delete()
    await enqueue_search(
        bot=bot,
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        username=callback.from_user.username,
        raw_target=target,
        deep=(mode_flag == "d")
    )
"""

# ==================== 9. MAIN.PY ====================
FILES["main.py"] = """import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import settings
from database import init_db, cleanup_old_reports
from handlers_admin_payments import admin_payments_router
from handlers_osint import osint_router, queue_worker

logging.basicConfig(level=logging.INFO)


async def main():
    await init_db()
    await cleanup_old_reports()

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(admin_payments_router)
    dp.include_router(osint_router)

    workers = [asyncio.create_task(queue_worker(bot)) for _ in range(3)]
    print("🚀 OSINT Бот успешно запущен! Откройте его в Telegram и нажмите /start")
    try:
        await dp.start_polling(bot)
    finally:
        for w in workers:
            w.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
"""

def build():
    for filename, content in FILES.items():
        if filename == ".env" and os.path.exists(".env"):
            continue
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Создан файл: {filename}")

    with zipfile.ZipFile("osint_bot_pro.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in FILES.keys():
            zf.write(filename)
    print("\\n📦 Архив osint_bot_pro.zip успешно собран в папке проекта!")

if __name__ == "__main__":
    build()