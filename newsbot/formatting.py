# newsbot/formatting.py
"""
Форматирование карточек для отправки в Telegram.
Использует CAT_EMOJI из config.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict

from dateutil import parser as date_parser

from .config import CAT_EMOJI


def _to_datetime(dt_like) -> datetime:
    """Аккуратно превратить строку/дату в datetime (без TZ)."""
    if isinstance(dt_like, datetime):
        return dt_like
    if isinstance(dt_like, str):
        try:
            return date_parser.parse(dt_like, ignoretz=True)
        except Exception:
            return datetime.now()
    return datetime.now()


def format_news_item(item: Dict) -> str:
    """
    Большая карточка:
    🎪 *Title*
    📅 Today 12:34
    🏷️ event
    🔗 https://...
    """
    pub_date = _to_datetime(item.get("date"))
    now = datetime.now()
    days_diff = (now.date() - pub_date.date()).days

    if days_diff == 0:
        date_str = f"Today {pub_date.strftime('%H:%M')}"
    elif days_diff == 1:
        date_str = f"Yesterday {pub_date.strftime('%H:%M')}"
    elif days_diff < 7:
        date_str = pub_date.strftime("%A %H:%M")
    else:
        date_str = pub_date.strftime("%b %d, %Y %H:%M")

    title = item.get("title", "No title")
    if len(title) > 120:
        title = title[:117] + "…"

    tag = (item.get("category") or "other").lower()
    tag_emoji = CAT_EMOJI.get(tag, "🏷️")

    return f"{tag_emoji} *{title}*\n📅 {date_str}\n🏷️ {tag}\n🔗 {item.get('link', '')}\n"


def _format_compact_line(item: Dict) -> str:
    """
    Компактная строка для списков:
    🎪 *Title*
    📅 Aug 17, 2025
    🔗 https://...
    """
    dt = _to_datetime(item.get("date"))
    date_str = dt.strftime("%b %d, %Y")
    cat = (item.get("category") or "other").lower()
    emoji = CAT_EMOJI.get(cat, "🏷️")
    title = item.get("title", "No title")
    if len(title) > 140:
        title = title[:137] + "…"
    return f"{emoji} *{title}*\n📅 {date_str}\n🔗 {item.get('link', '')}\n"


__all__ = ["format_news_item", "_format_compact_line"]

