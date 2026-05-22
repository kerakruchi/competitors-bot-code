# bot.py
import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
import sqlite3
import logging
from html import escape
from datetime import datetime
from typing import List, Dict, Tuple
from zoneinfo import ZoneInfo

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from newsbot.config import (
    DB_PATH,
    SCHEDULE_TZ,
    SCHEDULE_HOUR,
    SCHEDULE_MINUTE,
    CAT_RU,
    CAT_EMOJI,
    AI_ENABLED,
)
from newsbot.fetch import normalize_url, discover_feed, fetch_items
from newsbot.classify import classify_news
from newsbot.formatting import format_news_item, _format_compact_line
from newsbot.digest import format_digest

try:
    from playwright.async_api import async_playwright as _pw  # noqa: F401
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

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

    # ---------------------- DB ----------------------

    def init_database(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute("""
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
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                item_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                pub_date TIMESTAMP,
                category TEXT DEFAULT 'other',
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_id) REFERENCES sources (id),
                UNIQUE(source_id, item_id)
            )
        """)

        # Миграция: добавляем новые колонки если их нет
        for col, col_def in [("category", "TEXT DEFAULT 'other'"), ("summary", "TEXT")]:
            try:
                cursor.execute(f"ALTER TABLE item_cache ADD COLUMN {col} {col_def}")
            except Exception:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favourites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT,
                link TEXT,
                pub_date TIMESTAMP,
                category TEXT,
                source_domain TEXT,
                summary TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, link)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                digest_mode BOOLEAN DEFAULT FALSE,
                schedule_hour INTEGER DEFAULT 12,
                schedule_minute INTEGER DEFAULT 0,
                schedule_tz TEXT DEFAULT 'Europe/Moscow',
                last_digest_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        conn.close()

    def _get_user_settings(self, user_id: int) -> Dict:
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT digest_mode, schedule_hour, schedule_minute, schedule_tz, last_digest_date "
            "FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "digest_mode": bool(row[0]),
                "schedule_hour": row[1],
                "schedule_minute": row[2],
                "schedule_tz": row[3],
                "last_digest_date": row[4],
            }
        return {
            "digest_mode": False,
            "schedule_hour": SCHEDULE_HOUR,
            "schedule_minute": SCHEDULE_MINUTE,
            "schedule_tz": SCHEDULE_TZ,
            "last_digest_date": None,
        }

    def _ensure_user_settings(self, user_id: int):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO user_settings (user_id, schedule_hour, schedule_minute, schedule_tz) "
            "VALUES (?, ?, ?, ?)",
            (user_id, SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TZ),
        )
        conn.commit()
        conn.close()

    # ---------------------- AI ----------------------

    async def _classify_item(self, item: Dict) -> Tuple[str, str]:
        """Возвращает (category, summary). AI если доступен, иначе regex."""
        if AI_ENABLED:
            try:
                from newsbot.ai_classify import ai_classify_and_summarize
                cat, summary = await ai_classify_and_summarize(
                    title=item.get("title", ""),
                    link=item.get("link", ""),
                )
                return cat, summary
            except Exception:
                pass
        return classify_news(item.get("title", ""), item.get("link", "")), ""

    # ---------------------- Commands ----------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self._ensure_user_settings(user_id)
        settings = self._get_user_settings(user_id)
        hh = settings["schedule_hour"]
        mm = settings["schedule_minute"]
        tz = settings["schedule_tz"]
        digest_status = "вкл" if settings["digest_mode"] else "выкл"
        ai_status = "вкл ✅" if AI_ENABLED else "выкл (нет OPENAI_API_KEY)"

        msg = (
            "🤖 <b>News Monitor Bot</b>\n\n"
            "Мониторю новости конкурентов и присылаю новые публикации.\n\n"
            "<b>Источники:</b>\n"
            "• /add &lt;url&gt; — сайт или LinkedIn-страница\n"
            "• /remove &lt;domain&gt; — удалить\n"
            "• /pause &lt;domain&gt; — приостановить\n"
            "• /resume &lt;domain&gt; — возобновить\n"
            "• /list — все источники\n\n"
            "<b>Новости:</b>\n"
            "• /check — проверить прямо сейчас\n"
            "• /search &lt;запрос&gt; — поиск по кешу\n"
            "• /event · /product · /cases · /other — по категориям\n"
            "• /favourites — избранное\n\n"
            "<b>Настройки:</b>\n"
            "• /schedule HH:MM — время уведомлений\n"
            f"• /digest — дайджест (сейчас: <b>{digest_status}</b>)\n\n"
            f"⏰ Проверка в <b>{hh:02d}:{mm:02d} ({escape(tz)})</b>\n"
            f"🤖 AI-классификация: <b>{ai_status}</b>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    async def add_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /add &lt;url&gt;\n\nПримеры:\n"
                "/add https://microsoft.com\n"
                "/add https://linkedin.com/company/microsoft",
                parse_mode=ParseMode.HTML,
            )
            return

        url = context.args[0]
        user_id = update.effective_user.id
        self._ensure_user_settings(user_id)

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
                    f"❌ {escape(domain)} уже отслеживается.", parse_mode=ParseMode.HTML
                )
                conn.close()
                return

            await update.message.reply_text(
                f"🔍 Анализирую {escape(domain)}...", parse_mode=ParseMode.HTML
            )

            feed_url, feed_type = await discover_feed(normalized_url)
            if not feed_url:
                await update.message.reply_text(
                    f"❌ Не удалось найти фид для {escape(domain)}", parse_mode=ParseMode.HTML
                )
                conn.close()
                return

            cursor.execute(
                "INSERT INTO sources (user_id, domain, url, feed_url, feed_type) VALUES (?, ?, ?, ?, ?)",
                (user_id, domain, normalized_url, feed_url, feed_type),
            )
            source_id = cursor.lastrowid
            conn.commit()

            items = await fetch_items(feed_url, feed_type)
            if items:
                for it in items:
                    cat, summary = await self._classify_item(it)
                    try:
                        cursor.execute(
                            "INSERT OR IGNORE INTO item_cache "
                            "(source_id, item_id, title, link, pub_date, category, summary) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source_id, it.get("id"), it.get("title"),
                             it.get("link"), it.get("date"), cat, summary),
                        )
                    except Exception:
                        continue
                conn.commit()

            conn.close()

            extra = ""
            if feed_type == "linkedin" and not _PLAYWRIGHT_AVAILABLE:
                extra = (
                    "\n⚠️ LinkedIn требует Playwright:\n"
                    "<code>pip install playwright && playwright install chromium</code>"
                )

            await update.message.reply_text(
                f"✅ Добавлен <b>{escape(domain)}</b>\n"
                f"📡 Тип: <b>{escape(feed_type.upper())}</b>\n"
                f"🤖 AI: <b>{'вкл' if AI_ENABLED else 'выкл'}</b>"
                + extra,
                parse_mode=ParseMode.HTML,
            )

            await self.send_initial_preview(update, domain, items or [], limit=3)

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
                f"❌ Ошибка: {escape(str(e))}", parse_mode=ParseMode.HTML
            )

    async def list_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT domain, status, feed_type, last_check, created_at "
            "FROM sources WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(
                "📭 Источников нет.\n\nДобавьте: /add &lt;url&gt;",
                parse_mode=ParseMode.HTML,
            )
            return

        message = "📊 <b>Отслеживаемые источники:</b>\n\n"
        for i, (domain, status, feed_type, last_check, _) in enumerate(rows, 1):
            status_emoji = {"active": "🟢", "paused": "⏸"}.get(status, "🔴")
            try:
                lc = datetime.fromisoformat(last_check) if isinstance(last_check, str) else last_check
                lc_str = lc.strftime("%m-%d %H:%M") if lc else "Никогда"
            except Exception:
                lc_str = str(last_check) if last_check else "Никогда"

            message += (
                f"{i}. {status_emoji} <b>{escape(domain)}</b>\n"
                f"   📡 {escape((feed_type or '').upper())} · 🕒 {escape(lc_str)}\n\n"
            )

        await update.message.reply_text(message, parse_mode=ParseMode.HTML)

    async def remove_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /remove &lt;domain&gt;\nПример: /remove microsoft.com",
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
            "SELECT id FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain)
        )
        result = cursor.fetchone()
        if not result:
            await update.message.reply_text(
                f"❌ Домен <code>{escape(domain)}</code> не найден. "
                "Используйте /list для просмотра источников.",
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
            f"✅ Удалён <code>{escape(domain)}</code>.", parse_mode=ParseMode.HTML
        )

    async def pause_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /pause &lt;domain&gt;", parse_mode=ParseMode.HTML
            )
            return
        domain = context.args[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]
        user_id = update.effective_user.id

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sources SET status = 'paused' WHERE user_id = ? AND domain = ?",
            (user_id, domain),
        )
        if cursor.rowcount == 0:
            await update.message.reply_text(
                f"❌ Домен <code>{escape(domain)}</code> не найден.", parse_mode=ParseMode.HTML
            )
        else:
            conn.commit()
            await update.message.reply_text(
                f"⏸ Мониторинг <b>{escape(domain)}</b> приостановлен.\n"
                "Возобновить: /resume " + escape(domain),
                parse_mode=ParseMode.HTML,
            )
        conn.close()

    async def resume_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /resume &lt;domain&gt;", parse_mode=ParseMode.HTML
            )
            return
        domain = context.args[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]
        user_id = update.effective_user.id

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sources SET status = 'active' WHERE user_id = ? AND domain = ?",
            (user_id, domain),
        )
        if cursor.rowcount == 0:
            await update.message.reply_text(
                f"❌ Домен <code>{escape(domain)}</code> не найден.", parse_mode=ParseMode.HTML
            )
        else:
            conn.commit()
            await update.message.reply_text(
                f"▶️ Мониторинг <b>{escape(domain)}</b> возобновлён.", parse_mode=ParseMode.HTML
            )
        conn.close()

    async def check_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ручная проверка всех источников пользователя."""
        user_id = update.effective_user.id
        await update.message.reply_text("🔄 Проверяю источники...", parse_mode=ParseMode.HTML)
        count = await self._check_user_sources(context, user_id, force=True)
        if count == 0:
            await self._send_recent_digest(context, user_id, update)
        else:
            await update.message.reply_text(
                f"✅ Готово. Найдено новых: <b>{count}</b>", parse_mode=ParseMode.HTML
            )

    async def _send_recent_digest(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        update=None,
    ):
        """Отправляет дайджест из кеша за последние 7 дней если новых статей нет."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT s.domain, ic.title, ic.link, ic.pub_date, ic.category "
            "FROM item_cache ic "
            "JOIN sources s ON ic.source_id = s.id "
            "WHERE s.user_id = ? AND ic.pub_date >= date('now', '-7 days') "
            "ORDER BY ic.pub_date DESC LIMIT 30",
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            msg = "✅ Новых материалов нет. Кеш за последние 7 дней пуст."
            if update:
                await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            else:
                await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
            return

        from collections import defaultdict
        by_domain: dict = defaultdict(list)
        for domain, title, link, pub_date, category in rows:
            by_domain[domain].append((title, link, pub_date, category))

        CAT_EMOJI = {"event": "🎪", "product": "🧩", "cases": "📘", "other": "🏷️"}
        lines = ["📰 <b>Последние статьи за 7 дней:</b>\n"]
        for domain, articles in by_domain.items():
            lines.append(f"<b>{escape(domain)}</b>")
            for title, link, pub_date, category in articles[:5]:
                emoji = CAT_EMOJI.get(category or "other", "🏷️")
                date_str = pub_date[:10] if pub_date else ""
                lines.append(f"  {emoji} <a href=\"{link}\">{escape(title)}</a> <i>{date_str}</i>")
            if len(articles) > 5:
                lines.append(f"  <i>...и ещё {len(articles) - 5}</i>")
            lines.append("")

        text = "\n".join(lines).strip()
        try:
            if update:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            else:
                await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Failed to send recent digest to {user_id}: {e}")

    async def search_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /search &lt;запрос&gt;\nПример: /search webinar",
                parse_mode=ParseMode.HTML,
            )
            return

        query = " ".join(context.args).lower()
        user_id = update.effective_user.id

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ic.title, ic.link, ic.pub_date, COALESCE(ic.category,'other'), s.domain
            FROM item_cache ic
            JOIN sources s ON s.id = ic.source_id
            WHERE s.user_id = ? AND LOWER(ic.title) LIKE ?
            ORDER BY ic.pub_date DESC
            LIMIT 10
            """,
            (user_id, f"%{query}%"),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(
                f"🔍 Ничего не найдено по запросу «{escape(query)}».",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [f"🔍 <b>Результаты поиска «{escape(query)}»:</b>\n"]
        for title, link, pub_date, category, domain in rows:
            try:
                dt = datetime.fromisoformat(pub_date) if isinstance(pub_date, str) else pub_date
                date_str = dt.strftime("%d.%m.%Y") if dt else ""
            except Exception:
                date_str = ""
            emoji = CAT_EMOJI.get(category, "🏷️")
            lines.append(
                f"{emoji} <b>{escape(title or 'No title')}</b>\n"
                f"   📅 {date_str} · {escape(domain)}\n"
                f"   <a href=\"{escape(link or '', quote=True)}\">{escape(link or '')}</a>\n"
            )

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def set_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /schedule HH:MM\nПример: /schedule 09:30",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            parts = context.args[0].split(":")
            hh, mm = int(parts[0]), int(parts[1])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except Exception:
            await update.message.reply_text(
                "❌ Неверный формат. Используйте HH:MM, например: /schedule 09:30",
                parse_mode=ParseMode.HTML,
            )
            return

        user_id = update.effective_user.id
        self._ensure_user_settings(user_id)

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET schedule_hour = ?, schedule_minute = ?, last_digest_date = NULL WHERE user_id = ?",
            (hh, mm, user_id),
        )
        conn.commit()
        conn.close()

        await update.message.reply_text(
            f"⏰ Время уведомлений установлено: <b>{hh:02d}:{mm:02d}</b>",
            parse_mode=ParseMode.HTML,
        )

    async def toggle_digest(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        self._ensure_user_settings(user_id)
        settings = self._get_user_settings(user_id)
        new_mode = not settings["digest_mode"]

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE user_settings SET digest_mode = ? WHERE user_id = ?",
            (new_mode, user_id),
        )
        conn.commit()
        conn.close()

        if new_mode:
            await update.message.reply_text(
                "📋 <b>Режим дайджеста включён.</b>\n\n"
                "Все новые статьи будут собраны в один ежедневный дайджест.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                "🔔 <b>Режим дайджеста выключен.</b>\n\n"
                "Каждая новая статья будет приходить отдельным сообщением.",
                parse_mode=ParseMode.HTML,
            )

    # ---------------------- Favourites ----------------------

    async def favourites(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT title, link, pub_date, COALESCE(category,'other'), source_domain, summary
            FROM favourites WHERE user_id = ?
            ORDER BY created_at DESC LIMIT 10
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await update.message.reply_text(
                "⭐️ Избранных пока нет.\n\nНажмите ⭐️ под любой статьёй, чтобы добавить."
            )
            return

        lines = ["⭐️ <b>Избранные материалы:</b>\n"]
        for title, link, pub_date, category, domain, summary in rows:
            try:
                dt = datetime.fromisoformat(pub_date) if isinstance(pub_date, str) else pub_date
                date_str = dt.strftime("%d.%m.%Y") if dt else ""
            except Exception:
                date_str = ""
            emoji = CAT_EMOJI.get(category, "🏷️")
            lines.append(
                f"{emoji} <b>{escape(title or 'No title')}</b>\n"
                f"📅 {date_str} · {escape(domain or '')}\n"
                f"<a href=\"{escape(link or '', quote=True)}\">{escape(link or '')}</a>"
            )
            if summary:
                lines.append(f"💬 <i>{escape(summary)}</i>")
            lines.append("")

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    async def save_favourite_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработчик кнопки ⭐️ В избранное."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if not data.startswith("fav:"):
            return
        try:
            item_cache_id = int(data.split(":", 1)[1])
        except Exception:
            return

        user_id = query.from_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT ic.title, ic.link, ic.pub_date, ic.category, ic.summary, s.domain
            FROM item_cache ic
            JOIN sources s ON s.id = ic.source_id
            WHERE ic.id = ? AND s.user_id = ?
            """,
            (item_cache_id, user_id),
        )
        row = cursor.fetchone()

        if not row:
            conn.close()
            return

        title, link, pub_date, category, summary, domain = row
        try:
            cursor.execute(
                "INSERT OR IGNORE INTO favourites "
                "(user_id, title, link, pub_date, category, source_domain, summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, title, link, pub_date, category, domain, summary),
            )
            conn.commit()
            saved = cursor.rowcount > 0
        except Exception:
            saved = False
        conn.close()

        if saved:
            keyboard = [[InlineKeyboardButton("✅ Сохранено в избранном", callback_data="noop")]]
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception:
                pass
        else:
            await query.answer("Уже есть в избранном!", show_alert=False)

    # ---------------------- Categories ----------------------

    async def event_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "event")

    async def product_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "product")

    async def cases_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "cases")

    async def other_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._send_category_list(update, "other")

    async def _send_category_list(self, update: Update, category: str, limit: int = 10):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        # Сначала ищем по сохранённой категории
        cursor.execute(
            """
            SELECT ic.title, ic.link, ic.pub_date, ic.category
            FROM item_cache ic
            JOIN sources s ON s.id = ic.source_id
            WHERE s.user_id = ? AND ic.category = ?
            ORDER BY ic.pub_date DESC LIMIT ?
            """,
            (user_id, category, limit),
        )
        rows = cursor.fetchall()

        if not rows:
            # Фоллбэк: классифицируем на лету для старых записей без категории
            cursor.execute(
                """
                SELECT ic.title, ic.link, ic.pub_date
                FROM item_cache ic
                JOIN sources s ON s.id = ic.source_id
                WHERE s.user_id = ?
                ORDER BY ic.pub_date DESC LIMIT 300
                """,
                (user_id,),
            )
            all_rows = cursor.fetchall()
            conn.close()

            items: List[Dict] = []
            for title, link, pub_date in all_rows:
                cat = classify_news(title or "", link or "")
                if cat == category:
                    try:
                        dt = datetime.fromisoformat(pub_date) if isinstance(pub_date, str) else pub_date
                    except Exception:
                        dt = datetime.now()
                    items.append({"title": title, "link": link, "date": dt, "category": cat})
        else:
            conn.close()
            items = []
            for title, link, pub_date, cat in rows:
                try:
                    dt = datetime.fromisoformat(pub_date) if isinstance(pub_date, str) else pub_date
                except Exception:
                    dt = datetime.now()
                items.append({"title": title, "link": link, "date": dt, "category": cat or category})

        if not items:
            await update.message.reply_text("Ничего не найдено в этой категории.")
            return

        items.sort(key=lambda x: x["date"], reverse=True)
        items = items[:limit]

        title_map = {
            "event": "Ивенты",
            "product": "Продукты",
            "cases": "Кейсы",
            "other": "Другое",
        }
        lines = [f"<b>{escape(title_map.get(category, 'Новости'))}</b>\n"]
        for it in items:
            lines.append(_format_compact_line(it))

        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    # ---------------------- Initial preview ----------------------

    async def send_initial_preview(
        self, update: Update, domain: str, items: List[Dict], limit: int = 3
    ):
        if not items:
            await update.message.reply_text(
                f"📭 Свежих материалов для {escape(domain)} не найдено.",
                parse_mode=ParseMode.HTML,
            )
            return

        items_sorted = sorted(items, key=lambda x: x["date"], reverse=True)
        preview = items_sorted[:limit]

        await update.message.reply_text(
            f"📰 <b>Последние материалы {escape(domain)}</b>: {len(preview)} из {len(items_sorted)}",
            parse_mode=ParseMode.HTML,
        )

        for i, item in enumerate(preview, 1):
            msg = f"<b>{i}/{len(preview)}</b>\n{format_news_item(item)}"
            await update.message.reply_text(
                msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            await asyncio.sleep(0.25)

    # ---------------------- Scheduler ----------------------

    async def periodic_check(self, context: ContextTypes.DEFAULT_TYPE):
        """Запускается каждые 5 минут. Отправляет уведомления пользователям по их расписанию."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT user_id FROM sources WHERE status = 'active' AND first_sent = TRUE"
        )
        user_ids = [r[0] for r in cursor.fetchall()]
        conn.close()

        for user_id in user_ids:
            settings = self._get_user_settings(user_id)
            try:
                tz = ZoneInfo(settings["schedule_tz"])
                now_local = datetime.now(tz)
            except Exception:
                now_local = datetime.now()

            sched_h = settings["schedule_hour"]
            sched_m = settings["schedule_minute"]
            today_str = now_local.strftime("%Y-%m-%d")

            now_min = now_local.hour * 60 + now_local.minute
            sched_min = sched_h * 60 + sched_m
            diff = (now_min - sched_min) % (24 * 60)

            if diff < 5 and settings["last_digest_date"] != today_str:
                logger.info(f"Scheduled check for user {user_id}")
                count = await self._check_user_sources(context, user_id)

                if count == 0:
                    await self._send_recent_digest(context, user_id)

                conn = sqlite3.connect(self.db_path, timeout=30)
                c = conn.cursor()
                c.execute(
                    "UPDATE user_settings SET last_digest_date = ? WHERE user_id = ?",
                    (today_str, user_id),
                )
                conn.commit()
                conn.close()

    async def _check_user_sources(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, force: bool = False
    ) -> int:
        """
        Проверяет источники пользователя и отправляет новые материалы.
        Возвращает количество найденных новых статей.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, domain, feed_url, feed_type FROM sources "
            "WHERE user_id = ? AND status = 'active' AND first_sent = TRUE",
            (user_id,),
        )
        sources = cursor.fetchall()

        settings = self._get_user_settings(user_id)
        digest_mode = settings["digest_mode"]

        all_new_by_source: Dict[str, List[Dict]] = {}
        total_new = 0

        for source_id, domain, feed_url, feed_type in sources:
            try:
                items = await fetch_items(feed_url, feed_type)
                if not items:
                    continue

                cursor.execute(
                    "SELECT item_id FROM item_cache WHERE source_id = ?", (source_id,)
                )
                cached_ids = {row[0] for row in cursor.fetchall()}
                new_items = [it for it in items if it["id"] not in cached_ids]

                if not new_items:
                    cursor.execute(
                        "UPDATE sources SET last_check = ? WHERE id = ?",
                        (datetime.now(), source_id),
                    )
                    conn.commit()
                    continue

                logger.info(f"{len(new_items)} new items for {domain} (user {user_id})")
                new_items.sort(key=lambda x: x["date"])
                total_new += len(new_items)

                domain_new: List[Dict] = []
                for item in new_items:
                    cat, summary = await self._classify_item(item)
                    item["category"] = cat
                    item["summary"] = summary
                    item["_domain"] = domain

                    try:
                        cursor.execute(
                            "INSERT OR IGNORE INTO item_cache "
                            "(source_id, item_id, title, link, pub_date, category, summary) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (source_id, item["id"], item.get("title"),
                             item.get("link"), item.get("date"), cat, summary),
                        )
                        item["_cache_id"] = cursor.lastrowid
                    except Exception:
                        item["_cache_id"] = None

                    domain_new.append(item)
                    await asyncio.sleep(0.1)

                conn.commit()

                if not digest_mode:
                    for item in domain_new:
                        await self._notify_new_item(context, user_id, item)
                        await asyncio.sleep(0.4)
                else:
                    all_new_by_source[domain] = domain_new

                cursor.execute(
                    "UPDATE sources SET last_check = ? WHERE id = ?",
                    (datetime.now(), source_id),
                )
                conn.commit()

            except Exception as e:
                logger.error(f"Error checking source {domain}: {e}")

        conn.close()

        if digest_mode and all_new_by_source:
            digest_text = format_digest(all_new_by_source)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=digest_text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Failed to send digest to {user_id}: {e}")

        return total_new

    async def _notify_new_item(
        self, context: ContextTypes.DEFAULT_TYPE, user_id: int, item: Dict
    ):
        """Уведомление о новой статье с кнопкой ⭐️."""
        cat = (item.get("category") or "other").lower()
        dt = item.get("date") or datetime.now()
        try:
            date_str = dt.strftime("%d %b %Y %H:%M") if isinstance(dt, datetime) else str(dt)
        except Exception:
            date_str = str(dt)

        title_html = escape(item.get("title") or "No title")
        link = item.get("link") or ""
        summary = item.get("summary") or ""
        domain = item.get("_domain") or ""

        msg = (
            f"📰 <b>{title_html}</b>\n"
            f"📅 {escape(date_str)}\n"
            f"🏷️ {escape(CAT_RU.get(cat, 'другое'))}\n"
            f"🌐 {escape(domain)}\n"
            f"🔗 <a href=\"{escape(link, quote=True)}\">{escape(link)}</a>"
        )
        if summary:
            msg += f"\n\n💬 <i>{escape(summary)}</i>"

        keyboard = []
        cache_id = item.get("_cache_id")
        if cache_id:
            keyboard.append([
                InlineKeyboardButton("⭐️ В избранное", callback_data=f"fav:{cache_id}")
            ])
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        await context.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    # ---------------------- Menu ----------------------

    async def _post_init(self, app: Application):
        commands = [
            BotCommand("start",      "Запустить / справка"),
            BotCommand("add",        "Добавить источник: /add url"),
            BotCommand("remove",     "Удалить: /remove domain"),
            BotCommand("pause",      "Приостановить: /pause domain"),
            BotCommand("resume",     "Возобновить: /resume domain"),
            BotCommand("list",       "Список источников"),
            BotCommand("check",      "Проверить прямо сейчас"),
            BotCommand("search",     "Поиск: /search запрос"),
            BotCommand("favourites", "Избранное"),
            BotCommand("event",      "Ивенты"),
            BotCommand("product",    "Продукты"),
            BotCommand("cases",      "Кейсы"),
            BotCommand("other",      "Другое"),
            BotCommand("schedule",   "Расписание: /schedule HH:MM"),
            BotCommand("digest",     "Режим дайджеста вкл/выкл"),
        ]
        await app.bot.set_my_commands(commands)
        logger.info("Commands set")

    # ---------------------- Runner ----------------------

    def run(self):
        app = (
            Application.builder()
            .token(self.token)
            .post_init(self._post_init)
            .build()
        )

        app.add_handler(CommandHandler("start",      self.start))
        app.add_handler(CommandHandler("add",        self.add_source))
        app.add_handler(CommandHandler("list",       self.list_sources))
        app.add_handler(CommandHandler("remove",     self.remove_source))
        app.add_handler(CommandHandler("pause",      self.pause_source))
        app.add_handler(CommandHandler("resume",     self.resume_source))
        app.add_handler(CommandHandler("check",      self.check_now))
        app.add_handler(CommandHandler("search",     self.search_items))
        app.add_handler(CommandHandler("schedule",   self.set_schedule))
        app.add_handler(CommandHandler("digest",     self.toggle_digest))
        app.add_handler(CommandHandler("favourites", self.favourites))
        app.add_handler(CommandHandler("event",      self.event_cmd))
        app.add_handler(CommandHandler("product",    self.product_cmd))
        app.add_handler(CommandHandler("cases",      self.cases_cmd))
        app.add_handler(CommandHandler("other",      self.other_cmd))

        app.add_handler(CallbackQueryHandler(self.save_favourite_callback, pattern=r"^fav:"))
        app.add_handler(CallbackQueryHandler(
            lambda u, c: u.callback_query.answer(), pattern=r"^noop$"
        ))

        # Проверка каждые 5 минут (вместо одного раза в день)
        try:
            app.job_queue.run_repeating(
                self.periodic_check,
                interval=300,
                first=10,
                name="periodic-check",
            )
            logger.info("Periodic check scheduled every 5 minutes")
        except Exception as e:
            logger.warning(f"Scheduler init skipped: {e}")

        logger.info("Bot starting: run_polling()")
        app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    NewsMonitorBot(token).run()
