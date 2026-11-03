import socket
import urllib.request
from pydantic_settings import BaseSettings, SettingsConfigDict

# Глобально принуждаем весь Python использовать ТОЛЬКО IPv4 (убирает WinError 121 на Windows!)
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo


class Settings(BaseSettings):
    BOT_TOKEN: str
    ADMIN_PASSWORD: str = "admin2026"
    ADMIN_IDS: list[int] = []
    DB_PATH: str = "osint_bot.db"

    FREE_WEEKLY_LIMIT: int = 5
    HISTORY_RETENTION_DAYS: int = 7
    TG_MSG_CHAR_LIMIT: int = 4000
    SEARCH_COOLDOWN_SECONDS: int = 10  # Лимит: 1 поиск каждые 10 секунд

    # Цены: 70 звёзд в месяц, 700 звёзд навсегда
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
        "price": 70,  # 70 ⭐ в месяц
    },
    "prem_lifetime": {
        "title": "👑 Premium НАВСЕГДА (Бессрочный)",
        "days": 36500,
        "deep_credits": 0,
        "price": 700,  # 700 ⭐ навсегда
    },
}


def detect_local_proxy() -> str | None:
    """Автоматически определяет активный прокси/VPN порт на ПК."""
    sys_proxies = urllib.request.getproxies()
    for key in ("https", "http", "all"):
        if key in sys_proxies and sys_proxies[key]:
            return sys_proxies[key]

    common_ports = [
        (2080, "socks5"), (2081, "http"), (12334, "http"), (2334, "socks5"),
        (10808, "socks5"), (10809, "http"), (7890, "http"), (7897, "http"),
        (1080, "socks5"), (9050, "socks5"), (9150, "socks5"),
    ]
    for port, proto in common_ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return f"{proto}://127.0.0.1:{port}"
    return None