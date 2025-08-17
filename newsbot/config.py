# newsbot/config.py
"""
Глобальные константы и настройки бота.
"""

from typing import Dict, Tuple
from zoneinfo import ZoneInfo
from datetime import time

# ===================== БАЗА ДАННЫХ =====================
DB_PATH: str = "news_monitor.db"


# ===================== РАСПИСАНИЕ ======================
# Таймзона для расписания (Москва)
SCHEDULE_TZ: ZoneInfo = ZoneInfo("Europe/Moscow")

# Ежедневный запуск проверки в 12:00 по Москве
SCHEDULE_DAILY_TIME: time = time(hour=12, minute=0, tzinfo=SCHEDULE_TZ)

# Имя задачи (для JobQueue)
SCHEDULE_JOB_NAME: str = "daily_news_check_moscow_noon"


# ================== ПРАВИЛА ДЛЯ ДОМЕНОВ =================
# Приоритетные/запрещённые части путей для конкретных доменов
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

# Общие разрешённые/запрещённые префиксы путей (если домен не в DOMAIN_RULES)
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


# ================== HTTP ЗАГОЛОВКИ =====================
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsMonitorBot/1.4)"
}


# ================= КАТЕГОРИИ НОВОСТЕЙ ==================
# Локализация названий категорий
CAT_RU: Dict[str, str] = {
    "event": "ивент",
    "product": "продукт",
    "cases": "кейс",
    "other": "другое",
}

# Эмодзи для категорий
CAT_EMOJI: Dict[str, str] = {
    "event": "🎪",
    "product": "🧩",
    "cases": "📘",
    "other": "🏷️",
}


__all__ = [
    "DB_PATH",
    "SCHEDULE_TZ",
    "SCHEDULE_DAILY_TIME",
    "SCHEDULE_JOB_NAME",
    "DOMAIN_RULES",
    "DEFAULT_ALLOWED",
    "DEFAULT_BANNED",
    "DEFAULT_HEADERS",
    "CAT_RU",
    "CAT_EMOJI",
]

