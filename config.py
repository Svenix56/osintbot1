import os
import socket
import urllib.request
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo


def get_persistent_db_path() -> str:
    """Выбирает постоянную папку для БД, которая не стирается при git pull."""
    if os.path.exists("/data") and os.access("/data", os.W_OK):
        return "/data/osint_bot.db"
    storage_dir = Path.home() / "osint_bot_data"
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        return str(storage_dir / "osint_bot.db")
    except Exception:
        return "osint_bot.db"


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_PASSWORD: str = "admin2026"
    ADMIN_IDS: list[int] = []
    DB_PATH: str = get_persistent_db_path()

    # Настройки авто-сохранения базы в GitHub (чтобы подписки не слетали при редеплое)
    GITHUB_TOKEN: str = ""
    GITHUB_REPO: str = ""      # Формат: username/repo_name
    GITHUB_BRANCH: str = "main"

    FREE_WEEKLY_LIMIT: int = 5
    HISTORY_RETENTION_DAYS: int = 7
    TG_MSG_CHAR_LIMIT: int = 4000
    SEARCH_COOLDOWN_SECONDS: int = 10

    PRICE_MONTH_XTR: int = 70
    PRICE_LIFETIME_XTR: int = 700
    VK_SERVICE_TOKEN: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()

TARIFFS = {
    "prem_30": {
        "title": "💎 Premium на 1 месяц (30 дней)",
        "days": 30,
        "deep_credits": 0,
        "price": 70,
    },
    "prem_lifetime": {
        "title": "👑 Premium НАВСЕГДА (Бессрочный)",
        "days": 36500,
        "deep_credits": 0,
        "price": 700,
    },
}


def detect_local_proxy() -> str | None:
    sys_proxies = urllib.request.getproxies()
    for key in ("https", "http", "all"):
        if key in sys_proxies and sys_proxies[key]:
            return sys_proxies[key]
    for port, proto in [(2080, "socks5"), (2081, "http"), (12334, "http"), (10808, "socks5"), (10809, "http"), (7890, "http"), (1080, "socks5")]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return f"{proto}://127.0.0.1:{port}"
    return None
