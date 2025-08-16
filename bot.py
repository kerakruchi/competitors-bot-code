import os
import asyncio
import sqlite3
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import re
from dateutil import parser as date_parser

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

# ------------------------- Logging -------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("news-monitor-bot")

# -------------------- Per-domain rules --------------------
# Можно дополнять по мере необходимости
DOMAIN_RULES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "spot.ai":   {"allow": ("/blog",), "ban": ("/pricing", "/careers", "/docs", "/documentation", "/solutions", "/product", "/products")},
    "lumana.ai": {"allow": ("/blog", "/news"), "ban": ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers")},
    "irisity.com": {"allow": ("/news", "/blog"), "ban": ()},
}

# Общие разрешённые/запрещённые фрагменты путей
DEFAULT_ALLOWED = ("/blog", "/news", "/press", "/press-releases", "/newsroom", "/articles", "/story", "/stories", "/updates")
DEFAULT_BANNED  = ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers", "/docs", "/documentation")

# ----------------------- HTTP headers ----------------------
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; NewsMonitorBot/1.2; +https://example.com/bot)'
}

# ========================= BOT ============================
class NewsMonitorBot:
    def __init__(self, token: str):
        self.token = token
        self.db_path = "news_monitor.db"
        self.init_database()

    # ------------------ DB init ------------------
    def init_database(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
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
        ''')

        cursor.execute('''
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
        ''')

        conn.commit()
        conn.close()

    # ------------------ Helpers ------------------
    @staticmethod
    def get_domain_from_url(u: str) -> str:
        netloc = urlparse(u).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc

    def normalize_url(self, url: str) -> Tuple[str, str]:
        """
        Normalize URL and extract domain.
        - Если введён просто домен → вернём корень (scheme://host)
        - Если введён путь и он похож на раздел новостей (blog/news/press/...) → сохраним этот раздел
        """
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith('www.'):
            host = host[4:]

        root = f"{parsed.scheme}://{host}"

        path = (parsed.path or '').strip('/')
        news_roots = ('blog', 'news', 'press', 'newsroom', 'articles', 'media', 'stories', 'updates')
        if path and path.split('/')[0].lower() in news_roots:
            normalized_url = f"{root}/{path.split('/')[0]}"
        else:
            normalized_url = root

        return normalized_url, host

    # ------------------ Commands ------------------
    async def add_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /add <company_website_url>\n\nExample:\n`/add https://microsoft.com`\n`/add techcrunch.com`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        url = context.args[0]
        user_id = update.effective_user.id

        try:
            normalized_url, domain = self.normalize_url(url)

            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain))
            if cursor.fetchone():
                await update.message.reply_text(f"❌ {domain} is already being monitored.")
                conn.close()
                return

            await update.message.reply_text(f"🔍 Analyzing {domain}... Looking for news feeds...")

            feed_url, feed_type = await self.discover_feed(normalized_url)
            if not feed_url:
                await update.message.reply_text(f"❌ Could not find a news feed for {domain}")
                conn.close()
                return

            cursor.execute('''
                INSERT INTO sources (user_id, domain, url, feed_url, feed_type)
                VALUES (?, ?, ?, ?,
