# newsbot/config.py
from typing import Dict, Tuple

# ===================== База и расписание =====================
DB_PATH: str = "news_monitor.db"

# Ежедневная проверка (время локали Telegram JobQueue берём из zoneinfo)
SCHEDULE_TZ: str = "Europe/Moscow"
SCHEDULE_HOUR: int = 12
SCHEDULE_MINUTE: int = 0

# ===================== HTTP заголовки =====================
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsMonitorBot/1.5)"
}

# ===================== Фильтры путей ======================
# Разрешённые/запрещённые фрагменты путей для HTML-поиска статей
DEFAULT_ALLOWED: Tuple[str, ...] = (
    "/blog", "/news", "/press", "/press-releases", "/newsroom",
    "/articles", "/story", "/stories", "/updates",
    # добавили «ивентные» разделы
    "/events", "/event", "/calendar", "/webinars", "/talks",
)

DEFAULT_BANNED: Tuple[str, ...] = (
    "/solutions", "/solution", "/product", "/products",
    "/pricing", "/careers", "/docs", "/documentation",
)

# Возможность переопределить правила для доменов
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
    # События Яндекса — это календарь мероприятий, не блог
    "events.yandex.ru": {
        "allow": ("/", "/events", "/event", "/calendar", "/webinars", "/talks"),
        "ban": (),
    },
}

# ===================== Карты категорий =====================
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
