import asyncio
import base64
import os
import secrets
import shutil
import aiohttp
import aiosqlite
from datetime import datetime, timedelta, timezone
from config import settings
from validators import normalize_target_key

_github_sync_lock = asyncio.Lock()


async def pull_db_from_github():
    """При запуске бота скачивает актуальную базу подписок osint_bot.db из GitHub."""
    if not settings.GITHUB_TOKEN or not settings.GITHUB_REPO:
        # Если в папке проекта уже лежит osint_bot.db, копируем её в постоянное хранилище
        if os.path.exists("osint_bot.db") and settings.DB_PATH != "osint_bot.db" and not os.path.exists(settings.DB_PATH):
            shutil.copy("osint_bot.db", settings.DB_PATH)
        return

    url = f"https://api.github.com/repos/{settings.GITHUB_REPO.strip()}/contents/osint_bot.db"
    headers = {
        "Authorization": f"Bearer {settings.GITHUB_TOKEN.strip()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "OSINT-Bot-DB-Sync"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, ssl=False) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content_b64 = data.get("content", "")
                    if content_b64:
                        raw_bytes = base64.b64decode(content_b64)
                        if len(raw_bytes) > 100:
                            with open(settings.DB_PATH, "wb") as f:
                                f.write(raw_bytes)
                            print(f"☁️ [GitHub Sync] База подписок успешно загружена из GitHub ({len(raw_bytes)} байт)!")
                else:
                    print(f"ℹ️ [GitHub Sync] Файл osint_bot.db в репозитории ещё не создан (статус {resp.status}), будет создан автоматически.")
    except Exception as e:
        print(f"⚠️ [GitHub Pull Warning]: {e}")


async def push_db_to_github():
    """Автоматически сохраняет базу подписок osint_bot.db в ваш GitHub-репозиторий."""
    if not settings.GITHUB_TOKEN or not settings.GITHUB_REPO or not os.path.exists(settings.DB_PATH):
        return

    async with _github_sync_lock:
        url = f"https://api.github.com/repos/{settings.GITHUB_REPO.strip()}/contents/osint_bot.db"
        headers = {
            "Authorization": f"Bearer {settings.GITHUB_TOKEN.strip()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "OSINT-Bot-DB-Sync"
        }
        try:
            with open(settings.DB_PATH, "rb") as f:
                raw_bytes = f.read()
            b64_data = base64.b64encode(raw_bytes).decode("utf-8")

            async with aiohttp.ClientSession() as session:
                sha = None
                async with session.get(url, headers=headers, ssl=False) as r_get:
                    if r_get.status == 200:
                        sha = (await r_get.json()).get("sha")

                payload = {
                    "message": f"Auto-backup subscriptions DB ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})",
                    "content": b64_data,
                }
                if sha:
                    payload["sha"] = sha

                async with session.put(url, headers=headers, json=payload, ssl=False) as r_put:
                    if r_put.status in (200, 201):
                        print("☁️ [GitHub Sync] База подписок osint_bot.db успешно сохранена в GitHub!")
                    else:
                        err_txt = await r_put.text()
                        print(f"⚠️ [GitHub Push Error {r_put.status}]: {err_txt[:150]}")
        except Exception as e:
            print(f"⚠️ [GitHub Push Exception]: {e}")


async def init_db():
    await pull_db_from_github()
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                premium_until TIMESTAMP,
                deep_credits INTEGER DEFAULT 0,
                weekly_used INTEGER DEFAULT 0,
                week_reset_at TIMESTAMP,
                created_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tariff_key TEXT,
                amount_xtr INTEGER,
                charge_id TEXT,
                telegram_payment_charge_id TEXT UNIQUE,
                created_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target TEXT,
                mode TEXT,
                content TEXT,
                created_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS blacklist (
                target_norm TEXT PRIMARY KEY,
                reason TEXT,
                created_at TIMESTAMP
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS gift_codes (
                code TEXT PRIMARY KEY,
                days INTEGER,
                uses_left INTEGER DEFAULT 1,
                created_at TIMESTAMP
            )
        ''')
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
                '''INSERT INTO users 
                   (user_id, username, premium_until, deep_credits, weekly_used, week_reset_at, created_at)
                   VALUES (?, ?, NULL, 0, 0, ?, ?)''',
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
                asyncio.create_task(push_db_to_github())
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
            '''INSERT INTO payments (user_id, tariff_key, amount_xtr, charge_id, telegram_payment_charge_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (user_id, tariff_key, amount_xtr, charge_id, tg_charge_id, now.isoformat())
        )
        await db.commit()
    await push_db_to_github()


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
    await push_db_to_github()
    return norm


async def remove_from_blacklist(raw_target: str) -> str:
    norm = normalize_target_key(raw_target)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM blacklist WHERE target_norm = ?", (norm,))
        await db.commit()
    await push_db_to_github()
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
    await push_db_to_github()
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
    await push_db_to_github()
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
    await push_db_to_github()
    return True, days
