# newsbot/config.py
import os

# ====== Storage ======
DB_PATH = os.getenv("DB_PATH", "news_monitor.db")

# ====== Schedule (12:00 Europe/Moscow по умолчанию) ======
SCHEDULE_TZ = os.getenv("SCHEDULE_TZ", "Europe/Moscow")
SCHEDULE_HOUR = int(os.getenv("SCHEDULE_HOUR", "12"))
SCHEDULE_MINUTE = int(os.getenv("SCHEDULE_MINUTE", "0"))

# ====== HTTP headers ======
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsMonitorBot/1.6; +https://example.com/bot)"
}

# ====== Per-domain path rules ======
DOMAIN_RULES = {
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
    # добавляй сюда кастомные правила для доменов по мере необходимости
}

# Общие разрешённые/запрещённые фрагменты путей
DEFAULT_ALLOWED = (
    "/blog",
    "/news",
    "/press",
    "/press-releases",
    "/newsroom",
    "/articles",
    "/story",
    "/stories",
    "/updates",
    "/events",   # добавили events
)
DEFAULT_BANNED = (
    "/solutions",
    "/solution",
    "/product",
    "/products",
    "/pricing",
    "/careers",
    "/docs",
    "/documentation",
)

# ====== Categories (i18n + emojis) ======
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

# ====== Domains with relaxed HTML filtering (bypass allow/ban checks on list pages) ======
# Используется для сайтов со сложной версткой событий/лендингов, где обычные фильтры
# могут отсекать полезные ссылки. Для этих доменов применяются более мягкие условия
# (и дополнительно пробуем /events, /event, /conf, /conference).
BYPASS_FILTER_DOMAINS = {
    "events.yandex.ru",
    # добавляй сюда другие домены по необходимости
}
