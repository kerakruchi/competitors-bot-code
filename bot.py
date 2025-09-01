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
                (user_id, domain, normalized_url, feed_url, feed_type),
            )
            source_id = cursor.lastrowid
            conn.commit()

            # Кешируем текущие материалы, чтобы не слать задним числом
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
                f"✅ Added {escape(domain)}. Monitoring started.<br>"
                f"📡 Feed type: <b>{escape(feed_type.upper())}</b><br>"
                f"⏰ Daily checks at <b>{hh:02d}:{mm:02d} ({escape(tz)})</b>",
                parse_mode=ParseMode.HTML,
            )

            # Превью из 3 карточек
            await self.send_initial_preview(update, domain, items or [], limit=3)

            # Отметим первичную отправку
            conn2 = sqlite3.connect(self.db_path, timeout=30)
            c2 = conn2.cursor()
            c2.execute(
                "UPDATE sources SET first_sent = TRUE, last_check = ? WHERE id = ?",
                (datetime.now(), source_id),
            )
            conn2.commit()
            conn2.close()

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
                "📭 No sources being monitored.\n\nUse <code>/add &lt;url&gt;</code> to start monitoring a website!",
                parse_mode=ParseMode.HTML,
            )
            return

        message = "📊 <b>Your Monitored Sources:</b>\n\n"
        for i, (domain, status, feed_type, last_check, created_at) in enumerate(
            sources, 1
        ):
            status_emoji = "🟢" if status == "active" else "🔴"
            if not last_check:
                last_check_str = "Never"
            else:
                try:
                    last_check_dt = (
                        datetime.fromisoformat(last_check)
                        if isinstance(last_check, str)
                        else last_check
                    )
                    last_check_str = last_check_dt.strftime("%m-%d %H:%M")
                except Exception:
                    last_check_str = str(last_check)
            message += f"{i}. {status_emoji} <b>{escape(domain)}</b>\n"
            message += f"   📡 Type: {escape(feed_type.upper())}\n"
            message += f"   🕒 Last check: {escape(last_check_str)}\n\n"

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)

    async def remove_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: <code>/remove &lt;domain&gt;</code>\n\nExample:\n<code>/remove microsoft.com</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        domain = context.args[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]

        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id FROM sources WHERE user_id = ? AND domain = ?",
            (user_id, domain),
        )
        result = cursor.fetchone()

        if not result:
            await update.message.reply_text(
                f"❌ Domain <code>{escape(domain)}</code> not found in your monitored sources.\n\nUse <code>/list</code> to see your sources.",
                parse_mode=ParseMode.HTML,
            )
            conn.close()
            return

        source_id = result[0]
        cursor.execute("DELETE FROM item_cache WHERE source_id = ?", (source_id,))
        cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"✅ Removed <code>{escape(domain)}</code> from monitoring.",
            parse_mode=ParseMode.HTML,
        )

    async def favourites(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT title, link, pub_date, COALESCE(category, 'other'), source_domain
            FROM favourites
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text("⭐️ Избранных пока нет.")
            return

        lines = ["⭐️ <b>Избранные материалы:</b>"]
        for title, link, pub_date, category, domain in rows:
            try:
                dt = (
                    datetime.fromisoformat(pub_date)
                    if isinstance(pub_date, str)
                    else pub_date
                )
            except Exception:
                dt = datetime.now()
            date_str = dt.strftime("%b %d, %Y")
            lines.append(
                f"<b>{escape(title)}</b>\n"
                f"📅 {escape(date_str)}\n"
                f"🔗 <a href=\"{escape(link, quote=True)}\">{escape(link)}</a>\n"
            )

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    # --- Категории ---
    async def event_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "event")

    async def product_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "product")

    async def cases_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "cases")

    async def other_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "other")

    async def _send_category_list(
        self, update: Update, category: str, limit: int = 10
    ):
        from newsbot.classify import classify_news  # локальный импорт

        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ic.title, ic.link, ic.pub_date
            FROM item_cache ic
            JOIN sources s ON s.id = ic.source_id
            WHERE s.user_id = ?
            ORDER BY ic.pub_date DESC
            LIMIT 300
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        items: List[Dict] = []
        for title, link, pub_date in rows:
            try:
                cat = classify_news(title or "", link or "")
                if cat == category:
                    dt = (
                        datetime.fromisoformat(pub_date)
                        if isinstance(pub_date, str)
                        else pub_date
                    )
                    items.append({
                        "title": title,
                        "link": link,
                        "date": dt,
                        "category": cat,
                    })
            except Exception:
                continue

        if not items:
            await update.message.reply_text("Ничего не найдено в этой категории пока.")
            return

        items.sort(key=lambda x: x["date"], reverse=True)
        items = items[:limit]

        title_map = {
            "event": "Новости про ивенты",
            "product": "Новости про продукты",
            "cases": "Новости про кейсы",
            "other": "Другие новости",
        }
        lines = [f"<b>{escape(title_map.get(category, 'Новости'))}</b>"]
        for it in items:
            lines.append(_format_compact_line(it))

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    # ------------------ Scheduler job ------------------
    async def periodic_check(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Starting scheduled check for all sources...")

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, user_id, domain, feed_url, feed_type
            FROM sources
            WHERE status = 'active' AND first_sent = TRUE
            """
        )
        sources = cursor.fetchall()
        logger.info(f"Checking {len(sources)} active sources...")

        for source_id, user_id, domain, feed_url, feed_type in sources:
            try:
                items = await fetch_items(feed_url, feed_type)
                if not items:
                    continue

                cursor.execute(
                    "SELECT item_id FROM item_cache WHERE source_id = ?", (source_id,)
                )
                cached_ids = {row[0] for row in cursor.fetchall()}
                new_items = [item for item in items if item["id"] not in cached_ids]

                if new_items:
                    logger.info(f"Found {len(new_items)} new items for {domain}")
                    new_items.sort(key=lambda x: x["date"])
                    for item in new_items:
                        await self._notify_new_item(context, user_id, item)
                        try:
                            cursor.execute(
                                """
                                INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                                VALUES (?, ?, ?, ?, ?)
                                """,
                                (
                                    source_id,
                                    item["id"],
                                    item.get("title"),
                                    item.get("link"),
                                    item.get("date"),
                                ),
                            )
                        except Exception:
                            continue
                        await asyncio.sleep(0.4)

                cursor.execute(
                    "UPDATE sources SET last_check = ? WHERE id = ?",
                    (datetime.now(), source_id),
                )

            except Exception as e:
                logger.error(f"Error checking source {domain}: {e}")

        conn.commit()
        conn.close()
        logger.info("Scheduled check completed.")

    async def _notify_new_item(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, item: Dict
    ):
        cat = (item.get("category") or "other").lower()
        dt = item.get("date") or datetime.now()
        try:
            date_str = (
                dt.strftime("%b %d, %Y %H:%M") if isinstance(dt, datetime) else str(dt)
            )
        except Exception:
            date_str = str(dt)

        title_html = escape(item.get("title", "No title"))
        link = item.get("link", "")
        link_html = escape(link, quote=True)

        msg = (
            "Я нашёл новую статью для тебя:\n\n"
            f"📰 <b>{title_html}</b>\n"
            f"📅 {escape(date_str)}\n"
            f"🏷️ {escape(CAT_RU.get(cat, 'другое'))} ({escape(cat)})\n"
            f"🔗 <a href=\"{link_html}\">{escape(link)}</a>"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    # ------------------ Menu (set /commands) ------------------
    async def _post_init(self, app: Application):
        commands = [
            BotCommand("start", "Запустить бота"),
            BotCommand("list", "Список доменов"),
            BotCommand("add", "Добавить: /add url"),
            BotCommand("remove", "Удалить: /remove domain"),
            BotCommand("favourites", "Показать избранные"),
            BotCommand("event", "Новости про ивенты"),
            BotCommand("product", "Новости про продукты"),
            BotCommand("cases", "Новости про кейсы"),
            BotCommand("other", "Другие новости"),
        ]
        await app.bot.set_my_commands(commands)
        logger.info("Commands set")

    # ------------------ Runner ------------------
    def run(self):
        app = (
            Application.builder()
            .token(self.token)
            .post_init(self._post_init)
            .build()
        )

        # Команды
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        app.add_handler(CommandHandler("remove", self.remove_source))
        app.add_handler(CommandHandler("favourites", self.favourites))
        app.add_handler(CommandHandler("event", self.event_cmd))
        app.add_handler(CommandHandler("product", self.product_cmd))
        app.add_handler(CommandHandler("cases", self.cases_cmd))
        app.add_handler(CommandHandler("other", self.other_cmd))

        # Ежедневное расписание
        try:
            tz = ZoneInfo(SCHEDULE_TZ)
            t = dtime(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, tzinfo=tz)
            app.job_queue.run_daily(self.periodic_check, time=t, name="daily-check")
            logger.info(
                f"Scheduler set for {SCHEDULE_HOUR:02d}:{SCHEDULE_MINUTE:02d} {SCHEDULE_TZ}"
            )
        except Exception as e:
            logger.warning(f"Scheduler init skipped: {e}")

        logger.info("Bot starting: run_polling()")
        # Блокирующий вызов — процесс остаётся жить
        app.run_polling(allowed_updates=None, close_loop=False)


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    NewsMonitorBot(token).run()
