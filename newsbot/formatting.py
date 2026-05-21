# newsbot/formatting.py
"""Форматирование карточек для Telegram (HTML parse mode)."""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Dict

from dateutil import parser as date_parser

from .config import CAT_EMOJI


def _to_datetime(dt_like) -> datetime:
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
    Большая карточка (HTML):
    🎪 <b>Title</b>
    📅 Today 12:34
    🏷️ event
    💬 краткое описание (если есть)
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

    title = item.get("title") or "No title"
    if len(title) > 120:
        title = title[:117] + "…"

    tag = (item.get("category") or "other").lower()
    tag_emoji = CAT_EMOJI.get(tag, "🏷️")
    link = item.get("link") or ""
    summary = item.get("summary") or ""

    result = (
        f"{tag_emoji} <b>{escape(title)}</b>\n"
        f"📅 {escape(date_str)}\n"
        f"🏷️ {escape(tag)}\n"
        f"🔗 {escape(link)}\n"
    )
    if summary:
        result += f"💬 <i>{escape(summary)}</i>\n"

    return result


def _format_compact_line(item: Dict) -> str:
    """
    Компактная строка для списков (HTML):
    🎪 <b>Title</b>
    📅 Aug 17, 2025 · 🔗 https://...
    """
    dt = _to_datetime(item.get("date"))
    date_str = dt.strftime("%b %d, %Y")
    cat = (item.get("category") or "other").lower()
    emoji = CAT_EMOJI.get(cat, "🏷️")
    title = item.get("title") or "No title"
    if len(title) > 140:
        title = title[:137] + "…"
    link = item.get("link") or ""

    return (
        f"{emoji} <b>{escape(title)}</b>\n"
        f"📅 {escape(date_str)} · <a href=\"{escape(link, quote=True)}\">открыть</a>\n"
    )


__all__ = ["format_news_item", "_format_compact_line"]
