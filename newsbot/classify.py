# newsbot/classify.py
"""
Категоризация новостей:
- event   — мероприятия (ивенты, вебинары, саммиты и т.п.)
- product — релизы/апдейты продукта и фичи
- cases   — кейсы и внедрения (customer stories)
- other   — всё остальное
"""

import re
from typing import Dict, List, Optional, Pattern

# Ключевые паттерны для категорий (регистронезависимо)
CATEGORY_PATTERNS: Dict[str, Pattern[str]] = {
    "event": re.compile(
        r"\b("
        r"webinar|conference|summit|expo|keynote|workshop|meetup|session|panel|talk|"
        r"booth|roadshow|roundtable|hackathon|agenda|register|registration|rsvp|"
        r"livestream|live\s*stream|join us|addevent"
        r")\b",
        re.IGNORECASE,
    ),
    "product": re.compile(
        r"\b("
        r"release|launche?s?|update|updated|feature|ga\b|beta\b|preview|sdk|api|integration|"
        r"now available|introduc\w*|announc\w*|version|v\d+(?:\.\d+)*"
        r")\b",
        re.IGNORECASE,
    ),
    "cases": re.compile(
        r"\b("
        r"case\s*study|customer|client|success\s*story|deployment|implementation|rollout|"
        r"adopts?|chooses?|selects?|uses"
        r")\b",
        re.IGNORECASE,
    ),
}


def classify_news(
    title: str,
    link: str = "",
    tags: Optional[List[str]] = None,
    summary: str = "",
) -> str:
    """
    Возвращает одну из категорий: 'event' | 'product' | 'cases' | 'other'.

    Сигнатура соответствует использованию в боте:
    - title:   заголовок новости
    - link:    URL новости (полезно для доменных эвристик, например addevent)
    - tags:    список тегов/категорий из RSS (если есть)
    - summary: краткое описание/анонс

    Эвристика простая: проверяем объединённый текст на совпадение с паттернами,
    приоритет — event → product → cases.
    """
    blob = " ".join(
        part for part in [title or "", link or "", " ".join(tags or []), summary or ""]
        if part
    )

    if CATEGORY_PATTERNS["event"].search(blob):
        return "event"
    if CATEGORY_PATTERNS["product"].search(blob):
        return "product"
    if CATEGORY_PATTERNS["cases"].search(blob):
        return "cases"
    return "other"


__all__ = ["CATEGORY_PATTERNS", "classify_news"]

