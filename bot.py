# bot.py
import os
import asyncio
import sqlite3
import logging
from html import escape
from datetime import datetime, time as dtime
from typing import List, Dict
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- наши модули ---
from newsbot.config import (
    DB_PATH,
    SCHEDULE_TZ,
    SCHEDULE_HOUR,
    SCHEDULE_MINUTE,
    CAT_RU,
)
from newsbot.fetch import (
    normalize_url,
    discover_feed,
    fetch_items,
)
from newsbot.formatting import (
    format_news_item,       # HTML
    _format_compact_line,   # HTML
)

# ------------------------- Logging -------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("news-monitor-bot")


# ========================= BOT ============================
class NewsMonitorBot:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        self.token = token
        self.db_path = DB_PATH
        self.init_database()

    # ------------------ DB init ------------------
    def init_database(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                feed_url TEXT,
                feed_type TEXT,  -- 'rss', 'atom', 'html'
                status TEXT DEFAULT 'active',
                first_sent BOOLEAN DEFAULT FALSE,
                last_check TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, domain)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS item_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                item_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                pub_date TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_id) REFERENCES sources (id),
                UNIQUE(source_id, item_id)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS favourites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT,
                link TEXT,
                pub_date TIMESTAMP,
                category TEXT,
                source_domain TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, link)
            )
            """
        )

        conn.commit()
        conn.close()

    # ------------------ Commands ------------------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        tz = SCHEDULE_TZ
        hh = SCHEDULE_HOUR
        mm = SCHEDULE_MINUTE
        welcome_message = (
            "🤖 <b>Welcome to News Monitor Bot!</b>\n\n"
            "Я помогу мониторить новости компаний и присылать новые посты.\n\n"
            "<b>Команды:</b>\n"
            "• <code>/start</code> — запустить бота\n"
            "• <code>/list</code> — список доменов\n"
            "• <code>/add &lt;url&gt;</code> — добавить домен\n"
            "• <code>/remove &lt;domain&gt;</code> — удалить домен\n"
            "• <code>/favourites</code> — показать избранные\n"
            "• <code>/event</code> — только ивенты\n"
            "• <code>/product</code> — про продукты\n"
            "• <code>/cases</code> — кейсы\n"
            "• <code>/other</code> — другое\n\n"
            f"⏰ Автопроверка: <b>каждый день в {hh:02d}:{mm:02d} ({escape(tz)})</b>."
        )
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

    async def add_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/add &lt;company_website_url&gt;</code>\n\n"
                "Example:\n<code>/add https://microsoft.com</code>\n<code>/add techcrunch.com</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        url = context.args[0]
        user_id = update.effective_user.id

        try:
            normalized_url, domain = normalize_url(url)

            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM sources WHERE user_id = ? AND domain = ?",
                (user_id, domain),
            )
            if cursor.fetchone():
                await update.message.reply_text(
                    f"❌ {escape(domain)} is already being monitored.",
                    parse_mode=ParseMode.HTML,
                )
                conn.close()
                return

            await update.message.reply_text(
                f"🔍 Analyzing {escape(domain)}... Looking for news feeds...",
                parse_mode=ParseMode.HTML,
            )

            feed_url, feed_type = await discover_feed(normalized_url)
            if not feed_url:
                await update.message.reply_text(
                    f"❌ Could not find a news feed for {escape(domain)}",
                    parse_mode=ParseMode.HTML,
                )
                conn.close()
                return

            # Сохраняем источник
            cursor.execute(
                """
                INSERT INTO sources (user_id, domain, url, feed_url, feed_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_i_
