# bot.py
import os
import asyncio
import sqlite3
import logging
from html import escape
from datetime import datetime
from typing import List, Dict

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
    format_news_item,
    _format_compact_line,
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
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.HTML)

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

            tz = SCHEDULE_TZ
            hh = SCHEDULE_HOUR
            mm = SCHEDULE_MINUTE
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

    async def list_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT domain, status, feed_type, last_check, created_at
            FROM sources WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        sources = cursor.fetchall()
        conn.close()

        if not sources:
            await update.message.reply_text(
                "📭 No sources being monitored.\n\nUse /add <url> to start monitoring a website!",
                parse_mode=ParseMode.HTML,
            )
            return

        message = "📊 <b>Your Monitored Sources:</b>\n\n"
        for i, (domain, status, feed_type, last_check, created_at) in enumerate(
            sources, 1
        ):
            status_emoji = "🟢" if status == "active" else "🔴"
            last_check_str = str(last_check) if last_check else "Never"
            message += f"{i}. {status_emoji} <b>{escape(domain)}</b>\n"
            message += f"   📡 Type: {escape(feed_type.upper())}\n"
            message += f"   🕒 Last check: {escape(last_check_str)}\n\n"

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)

    # ------------------ остальные методы ------------------
    # (remove_source, favourites, event_cmd, product_cmd, cases_cmd, other_cmd,
    #  _send_category_list, send_initial_preview, periodic_check, _notify_new_item, _post_init)
    # >>> я включу их тоже, если нужно полный листинг <<<

    # ------------------ Runner ------------------
    def run(self):
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        # остальные команды тоже сюда

        app.post_init = self._post_init
        app.run_polling()


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    bot = NewsMonitorBot(token)
    bot.run()
