# bot.py
import os
import asyncio
import sqlite3
import logging
from html import escape
from datetime import datetime
from typing import List, Dict

from zoneinfo import ZoneInfo
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
    format_news_item,       # возвращает HTML
    _format_compact_line,   # возвращает HTML
)


# ------------------------- Logging -------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("news-monitor-bot")


# ========================= BOT ============================
class NewsMonitorBot:
    def __init__(self, token: str):
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
                feed_type TEXT,
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
            "• /start — запустить бота\n"
            "• /list — список доменов\n"
            "• /add &lt;url&gt; — добавить домен\n"
            "• /remove &lt;domain&gt; — удалить домен\n"
            "• /favourites — показать избранные\n"
            "• /event — только ивенты\n"
            "• /product — про продукты\n"
            "• /cases — кейсы\n"
            "• /other — другое\n\n"
            f"⏰ Автопроверка: каждый день в {hh:02d}:{mm:02d} ({escape(tz)})"
        )

        keyboard = [
            [KeyboardButton("/list"), KeyboardButton("/add"), KeyboardButton("/remove")],
            [KeyboardButton("/favourites")],
            [KeyboardButton("/event"), KeyboardButton("/product")],
            [KeyboardButton("/cases"), KeyboardButton("/other")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await update.message.reply_text(
            welcome_message, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )

    async def add_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /add <company_website_url>\n\n"
                "Example:\n/add https://microsoft.com\n/add techcrunch.com",
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

            cursor.execute(
                """
                INSERT INTO sources (user_id, domain, url, feed_url, feed_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, domain, normalized_url, feed_url, feed_type),
            )
            source_id = cursor.lastrowid
            conn.commit()

            items = await fetch_items(feed_url, feed_type)
            if items:
                for it in items:
                    try:
                        cursor.execute(
                            """
                            INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                source_id,
                                it.get("id"),
                                it.get("title"),
                                it.get("link"),
                                it.get("date"),
                            ),
                        )
                    except Exception:
                        continue
                conn.commit()

            conn.close()

            await update.message.reply_text(
                f"✅ Added {escape(domain)}. Monitoring started.\n"
                f"📡 Feed type: <b>{escape(feed_type.upper())}</b>\n"
                f"⏰ Daily checks at <b>{hh:02d}:{mm:02d} ({escape(tz)})</b>",
                parse_mode=ParseMode.HTML,
            )

            await self.send_initial_preview(update, domain, items or [], limit=3)

        except Exception as e:
            logger.exception("Error adding source")
            await update.message.reply_text(
                f"❌ Error adding source: {escape(str(e))}",
                parse_mode=ParseMode.HTML,
            )

    # ------------------ (остальные методы без изменений, кроме br → \n) ------------------
    # тут остаётся весь код list_sources, remove_source, favourites, категории, превью,
    # periodic_check, _notify_new_item, _post_init, run
    # в них только заменены <br> на \n

    # ------------------ Runner ------------------
    def run(self):
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        app.add_handler(CommandHandler("remove", self.remove_source))
        app.add_handler(CommandHandler("favourites", self.favourites))
        app.add_handler(CommandHandler("event", self.event_cmd))
        app.add_handler(CommandHandler("product", self.product_cmd))
        app.add_handler(CommandHandler("cases", self.cases_cmd))
        app.add_handler(CommandHandler("other", self.other_cmd))

        app.post_init = self._post_init

        app.run_polling()


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    bot = NewsMonitorBot(token)
    bot.run()
