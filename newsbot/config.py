# newsbot/config.py
import os
from typing import Dict, Tuple

# ---------- База данных ----------
DB_PATH = os.getenv("DB_PATH", "news_monitor.db")

# ---------- Расписание (по умолчанию: каждый день в 12:00 Europe/Moscow) ----------
SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "Europe/Moscow")
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "12"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))

# ---------- Пер-доменные правила (что считать новостями) ----------
DOMAIN_RULES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "spot.ai": {
        "allow": ("/blog",),
        "ban": ("/pricing", "/careers", "/docs", "/documentation", "/solutions", "/product", "/products"),
    },
    "lumana.ai": {
        "allow": ("/blog", "/news"),
        "ban": ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers"),
    },
    "irisity.com": {
        "allow": ("/news", "/blog"),
        "ban": (),
    },
}

# Общие разрешённые/запрещённые фрагменты путей
DEFAULT_ALLOWED: Tuple[str, ...] = (
    "/blog",
    "/news",
    "/press",
    "/press-releases",
    "/newsroom",
    "/articles",
    "/story",
    "/stories",
    "/updates",
)
DEFAULT_BANNED: Tuple[str, ...] = (
    "/solutions",
    "/solution",
    "/product",
    "/products",
    "/pricing",
    "/careers",
    "/docs",
    "/documentation",
)

# ---------- HTTP заголовки для запросов ----------
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsMonitorBot/1.4; +https://example.com/bot)"
}

# ---------- Карты категорий и эмодзи ----------
CAT_RU = {
    "event": "ивент",
    "product": "продукт",
    "cases": "кейс",
    "other": "другое",
}

CAT_EMOJI = {
    "event": "🎪",
    "product": "🧩",
    "cases": "📘",
    "other": "🏷️",
}
