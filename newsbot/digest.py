# newsbot/digest.py
"""Форматирование ежедневного дайджеста."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Dict, List

from .config import CAT_EMOJI


def format_digest(items_by_source: Dict[str, List[Dict]], date: datetime = None) -> str:
    """
    Форматирует дайджест, сгруппированный по источникам.
    items_by_source: {"domain": [item, ...], ...}
    """
    if date is None:
        date = datetime.now()

    date_str = date.strftime("%d %B %Y")
    total = sum(len(v) for v in items_by_source.values())

    lines = [
        f"📋 <b>Дайджест за {escape(date_str)}</b>",
        f"Найдено новых материалов: <b>{total}</b>",
    ]

    for domain, items in items_by_source.items():
        if not items:
            continue
        lines.append(f"\n<b>🔹 {escape(domain)}</b>  ({len(items)} шт.)")

        for item in items[:5]:
            cat = (item.get("category") or "other").lower()
            emoji = CAT_EMOJI.get(cat, "🏷️")
            title = (item.get("title") or "No title")[:100]
            link = item.get("link") or ""
            dt = item.get("date")

            date_part = ""
            if isinstance(dt, datetime):
                date_part = f" <i>({dt.strftime('%d.%m')})</i>"

            lines.append(
                f"  {emoji} <a href=\"{escape(link, quote=True)}\">{escape(title)}</a>{date_part}"
            )

        if len(items) > 5:
            lines.append(f"  <i>…и ещё {len(items) - 5}</i>")

    return "\n".join(lines)
