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
    "spot.ai":     {"allow": ("/blog",), "ban": ("/pricing", "/careers", "/docs", "/documentation", "/solutions", "/product", "/products")},
    "lumana.ai":   {"allow": ("/blog", "/news"), "ban": ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers")},
    "irisity.com": {"allow": ("/news", "/blog"), "ban": ()},
}

# Общие разрешённые/запрещённые фрагменты путей
DEFAULT_ALLOWED = ("/blog", "/news", "/press", "/press-releases", "/newsroom", "/articles", "/story", "/stories", "/updates")
DEFAULT_BANNED  = ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers", "/docs", "/documentation")

# ----------------------- HTTP headers ----------------------
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; NewsMonitorBot/1.2)'
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
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, domain, normalized_url, feed_url, feed_type))

            source_id = cursor.lastrowid
            conn.commit()
            conn.close()

            await update.message.reply_text(f"✅ Added {domain}. Monitoring started.\n📡 Feed type: {feed_type.upper()}")

            await self.send_initial_items(update, source_id)

        except Exception as e:
            logger.exception("Error adding source")
            await update.message.reply_text(f"❌ Error adding source: {str(e)}")

    async def list_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT domain, status, feed_type, last_check, created_at
            FROM sources WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        sources = cursor.fetchall()
        conn.close()

        if not sources:
            await update.message.reply_text(
                "📭 No sources being monitored.\n\nUse `/add <url>` to start monitoring a website!",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        message = "📊 *Your Monitored Sources:*\n\n"
        for i, (domain, status, feed_type, last_check, created_at) in enumerate(sources, 1):
            status_emoji = "🟢" if status == "active" else "🔴"
            last_check_str = "Never" if not last_check else datetime.fromisoformat(last_check).strftime("%m-%d %H:%M")
            message += f"{i}. {status_emoji} *{domain}*\n"
            message += f"   📡 Type: {feed_type.upper()}\n"
            message += f"   🕒 Last check: {last_check_str}\n\n"

        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    async def remove_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /remove <domain>\n\nExample:\n`/remove microsoft.com`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        domain = context.args[0].lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain))
        result = cursor.fetchone()
        if not result:
            await update.message.reply_text(
                f"❌ Domain `{domain}` not found in your monitored sources.\n\nUse `/list` to see your sources.",
                parse_mode=ParseMode.MARKDOWN
            )
            conn.close()
            return

        source_id = result[0]
        cursor.execute("DELETE FROM item_cache WHERE source_id = ?", (source_id,))
        cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"✅ Removed `{domain}` from monitoring.", parse_mode=ParseMode.MARKDOWN)

    async def test_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /test <domain>\n\nExample:\n`/test microsoft.com`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        domain = context.args[0].lower()
        if domain.startswith('www.'):
            domain = domain[4:]

        user_id = update.effective_user.id
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute("SELECT feed_url, feed_type FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain))
        result = cursor.fetchone()
        conn.close()

        if not result:
            await update.message.reply_text(
                f"❌ Domain `{domain}` not found in your monitored sources.\n\nUse `/list` to see your sources.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        feed_url, feed_type = result
        await update.message.reply_text(f"🔍 Testing {domain}...")

        try:
            items = await self.fetch_items(feed_url, feed_type)
            if items:
                items.sort(key=lambda x: x['date'], reverse=True)
                latest_item = items[0]
                message = f"✅ *Test successful for {domain}*\n\n"
                message += f"Latest item:\n{self.format_news_item(latest_item)}"
                message += f"📊 Total items found: {len(items)}"
                await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
            else:
                await update.message.reply_text(f"⚠️ No items found for {domain}")
        except Exception as e:
            await update.message.reply_text(f"❌ Test failed for {domain}: {str(e)}")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome_message = """
🤖 *Welcome to News Monitor Bot!*

I help you monitor company news feeds and get instant notifications when new content is published.

*📋 Commands:*
- `/add <url>` - Add a company website to monitor
- `/list` - Show all your monitored sources  
- `/remove <domain>` - Remove a source from monitoring
- `/test <domain>` - Test a source manually

*🚀 How it works:*
1. Add a company website with `/add <url>`
2. I'll automatically find their RSS feed or news page
3. You'll immediately get the 3 most recent news items
4. I'll continuously monitor and notify you of new posts

*🔔 Monitoring:*
I check for updates every 20 minutes and will send you new items as soon as they appear!
        """
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

    # ------------------ Scheduler ------------------
    async def periodic_check(self, context: ContextTypes.DEFAULT_TYPE):
        logger.info("Starting periodic check for all sources...")

        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, user_id, domain, feed_url, feed_type
            FROM sources
            WHERE status = 'active' AND first_sent = TRUE
        ''')
        sources = cursor.fetchall()
        logger.info(f"Checking {len(sources)} active sources...")

        for source_id, user_id, domain, feed_url, feed_type in sources:
            try:
                items = await self.fetch_items(feed_url, feed_type)
                if not items:
                    continue

                cursor.execute("SELECT item_id FROM item_cache WHERE source_id = ?", (source_id,))
                cached_ids = {row[0] for row in cursor.fetchall()}

                new_items = [item for item in items if item['id'] not in cached_ids]

                if new_items:
                    logger.info(f"Found {len(new_items)} new items for {domain}")
                    new_items.sort(key=lambda x: x['date'])
                    for item in new_items[:3]:
                        message = f"🆕 *New from {domain}*\n\n{self.format_news_item(item)}"
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=message,
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True
                            )
                            cursor.execute('''
                                INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (source_id, item['id'], item['title'], item['link'], item['date']))
                            await asyncio.sleep(1)
                        except Exception as e:
                            logger.error(f"Error sending message to user {user_id}: {e}")

                cursor.execute("UPDATE sources SET last_check = ? WHERE id = ?", (datetime.now(), source_id))

            except Exception as e:
                logger.error(f"Error checking source {domain}: {e}")

        conn.commit()
        conn.close()
        logger.info("Periodic check completed.")

    # ------------------ Feed discovery ------------------
    async def discover_feed(self, base_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Prefer official feeds; otherwise pick a real blog/news section (not solutions/pricing/etc)."""
        try:
            parsed = urlparse(base_url)
            scheme = parsed.scheme or 'https'
            host = parsed.netloc
            root = f"{scheme}://{host}"

            # Popular subdomains + root
            subdomains = [
                'blog', 'news', 'press', 'newsroom', 'media', 'stories', 'source',
                'about', 'company', 'corporate', 'updates'
            ]
            candidate_bases = [root] + [f"{scheme}://{sd}.{host}" for sd in subdomains]

            # 1) <link rel="alternate"> RSS/Atom
            for base in candidate_bases:
                try:
                    r = requests.get(base, timeout=12, headers=DEFAULT_HEADERS)
                    if r.status_code != 200:
                        continue
                    soup = BeautifulSoup(r.content, 'html.parser')
                    for link in soup.find_all('link', rel='alternate'):
                        type_attr = (link.get('type') or '').lower()
                        if any(t in type_attr for t in ['rss', 'atom', 'xml']):
                            feed_url = urljoin(base, link.get('href'))
                            feed_type = 'atom' if 'atom' in type_attr else 'rss'
                            if await self.verify_feed(feed_url):
                                return feed_url, feed_type
                except Exception:
                    continue

            # 2) Common RSS paths
            common_paths = [
                '/blog/feed', '/blog/rss', '/blog/feed.xml', '/blog/atom.xml', '/blog/index.xml',
                '/news/feed', '/news/rss', '/news/feed.xml', '/news/atom.xml', '/news/index.xml',
                '/press/feed', '/press/rss',
                '/rss', '/rss.xml', '/atom.xml', '/feed', '/feed.xml', '/index.xml',
            ]
            for base in candidate_bases:
                for path in common_paths:
                    fu = urljoin(base, path)
                    if await self.verify_feed(fu):
                        return fu, 'rss'

            # 3) HTML news sections (validate)
            news_paths = ['/blog', '/news', '/press', '/press-releases', '/newsroom', '/articles', '/stories', '/story', '/updates']
            banned_paths = list(DEFAULT_BANNED)

            def looks_like_news_url(u: str) -> bool:
                up = urlparse(u)
                path = up.path.lower()
                if any(path == bp or path.startswith(bp + '/') for bp in banned_paths):
                    return False
                return any(path == np or path.startswith(np + '/') for np in news_paths)

            # Try /blog first, then others
            ordered_candidates = []
            for base in candidate_bases:
                ordered_candidates.append(urljoin(base, '/blog'))
            for base in candidate_bases:
                for np in news_paths:
                    u = urljoin(base, np)
                    if u not in ordered_candidates:
                        ordered_candidates.append(u)

            for news_url in ordered_candidates:
                try:
                    r = requests.get(news_url, timeout=12, headers=DEFAULT_HEADERS)
                    if r.status_code != 200:
                        continue
                    if not looks_like_news_url(news_url):
                        continue
                    soup = BeautifulSoup(r.content, 'html.parser')
                    candidates = soup.select('article, .post, .blog-post, .news-item, [class*="post"], [class*="article"], [class*="blog"]')
                    has_titles = 0
                    for el in candidates[:20]:
                        t = el.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                        a = el.find('a', href=True)
                        if t and a and len(t.get_text(strip=True)) > 10:
                            has_titles += 1
                            if has_titles >= 2:
                                return news_url, 'html'
                except Exception:
                    continue

            # 4) Fallback: root as HTML (will be filtered later)
            try:
                r = requests.get(root, timeout=10, headers=DEFAULT_HEADERS)
                if r.status_code == 200:
                    return root, 'html'
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error discovering feed for {base_url}: {e}")

        return None, None

    async def verify_feed(self, feed_url: str) -> bool:
        try:
            response = requests.get(feed_url, timeout=10, headers=DEFAULT_HEADERS)
            if response.status_code != 200:
                return False
            feed = feedparser.parse(response.content)
            return len(feed.entries) > 0 and not feed.bozo
        except Exception:
            return False

    async def verify_html_has_articles(self, url: str) -> bool:
        try:
            response = requests.get(url, timeout=10, headers=DEFAULT_HEADERS)
            if response.status_code != 200:
                return False
            soup = BeautifulSoup(response.content, 'html.parser')
            blog_indicators = [
                'article', '.post', '.blog-post', '.news-item',
                '[class*="post"]', '[class*="article"]', '[class*="blog"]'
            ]
            article_count = 0
            for selector in blog_indicators:
                elements = soup.select(selector)
                for elem in elements:
                    title_elem = elem.find(['h1', 'h2', 'h3', 'h4'])
                    link_elem = elem.find('a', href=True)
                    if title_elem and link_elem and len(title_elem.get_text().strip()) > 10:
                        article_count += 1
                        if article_count >= 2:
                            return True
            return False
        except Exception:
            return False

    # ------------------ Date helpers ------------------
    def _try_parse_date_text(self, text: str) -> Optional[datetime]:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return date_parser.parse(text, fuzzy=True, ignoretz=True)
        except Exception:
            return None

    def parse_pub_date(self, item_soup: BeautifulSoup, page_soup: BeautifulSoup, link: str) -> datetime:
        # 1) <time> tag
        time_tag = item_soup.find('time')
        if time_tag:
            if time_tag.has_attr('datetime'):
                dt = self._try_parse_date_text(time_tag['datetime'])
                if dt: return dt
            dt = self._try_parse_date_text(time_tag.get_text(" ", strip=True))
            if dt: return dt

        # 2) meta tags on listing page
        meta_names = [
            ("meta", {"property": "article:published_time"}),
            ("meta", {"name": "article:published_time"}),
            ("meta", {"itemprop": "datePublished"}),
            ("meta", {"property": "og:updated_time"}),
            ("meta", {"name": "pubdate"}),
            ("meta", {"name": "publishdate"}),
            ("meta", {"name": "date"}),
        ]
        for tag, attrs in meta_names:
            m = page_soup.find(tag, attrs=attrs)
            if m and m.has_attr('content'):
                dt = self._try_parse_date_text(m['content'])
                if dt: return dt

        # 3) common classes
        cand = item_soup.select_one('.date, .post-date, .published, .posted-on, .entry-date, [class*="date"], [class*="time"]')
        if cand:
            dt = self._try_parse_date_text(cand.get_text(" ", strip=True))
            if dt: return dt

        # 4) date in URL
        if link:
            m = re.search(r'/(\d{4})/(\d{2})/(\d{2})(?:/|$)', link)
            if m:
                try:
                    y, mth, d = map(int, m.groups())
                    return datetime(y, mth, d)
                except Exception:
                    pass

        return datetime.now()

    async def extract_date_from_article_page(self, article_url: str) -> datetime:
        """Extract publication date by visiting the actual article page (meta, time, JSON-LD)."""
        try:
            response = requests.get(article_url, timeout=12, headers=DEFAULT_HEADERS)
            if response.status_code != 200:
                return datetime.now()

            soup = BeautifulSoup(response.content, 'html.parser')

            # 1) meta tags
            meta_selectors = [
                'meta[property="article:published_time"]',
                'meta[name="article:published_time"]',
                'meta[property="og:published_time"]',
                'meta[name="publish_date"]',
                'meta[name="publication_date"]',
                'meta[name="date"]',
                'meta[name="created"]'
            ]
            for selector in meta_selectors:
                m = soup.select_one(selector)
                if m and m.get('content'):
                    try:
                        return date_parser.parse(m['content'], ignoretz=True)
                    except Exception:
                        pass

            # 2) <time>
            t = soup.select_one('time[datetime]')
            if t:
                try:
                    return date_parser.parse(t['datetime'], ignoretz=True)
                except Exception:
                    pass
            t2 = soup.find('time')
            if t2:
                try:
                    return date_parser.parse(t2.get_text(' ', strip=True), ignoretz=True)
                except Exception:
                    pass

            # 3) JSON-LD
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = script.string or script.text
                    if not data:
                        continue
                    import json
                    obj = json.loads(data)
                    candidates = obj if isinstance(obj, list) else [obj]
                    for c in candidates:
                        if not isinstance(c, dict):
                            continue
                        graph = c.get('@graph')
                        if isinstance(graph, list):
                            candidates.extend([g for g in graph if isinstance(g, dict)])
                        for k in ('datePublished', 'dateCreated', 'uploadDate'):
                            if k in c and isinstance(c[k], str):
                                try:
                                    return date_parser.parse(c[k], ignoretz=True)
                                except Exception:
                                    continue
                except Exception:
                    continue

            # 4) Patterns
            text = soup.get_text(" ", strip=True)
            patterns = [
                r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b',
                r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\b',
                r'\b\d{4}-\d{2}-\d{2}\b',
                r'\b\d{1,2}/\d{1,2}/\d{4}\b'
            ]
            for p in patterns:
                m = re.search(p, text)
                if m:
                    try:
                        return date_parser.parse(m.group(0), ignoretz=True)
                    except Exception:
                        pass

        except Exception as e:
            logger.debug(f"Error extracting date from {article_url}: {e}")

        return datetime.now()

    # ------------------ Categorization ------------------
    CATEGORY_PATTERNS = {
        "event": re.compile(
            r"\b(webinar|conference|summit|expo|keynote|workshop|meetup|session|panel|talk|booth|roadshow|roundtable|hackathon|agenda|register|join us)\b",
            re.I,
        ),
        "product": re.compile(
            r"\b(release|launche?s?|update|updated|feature|ga\b|beta\b|preview|sdk|api|integration|now available|introduc|announc|version|v\d+(\.\d+)*)\b",
            re.I,
        ),
        "cases": re.compile(
            r"\b(case study|customer|client|success story|deployment|implementation|rollout|adopts|chooses|selects|uses)\b",
            re.I,
        ),
    }

    def classify_news(self, title: str, link: str = "", tags: Optional[List[str]] = None, summary: str = "") -> str:
        """
        Возвращает один из: 'event' | 'product' | 'cases' | 'other'
        Смотрим на заголовок + URL + теги/категории из RSS + краткое описание.
        """
        blob = " ".join([
            title or "",
            link or "",
            " ".join(tags or []),
            (summary or "")
        ])
        if self.CATEGORY_PATTERNS["event"].search(blob):
            return "event"
        if self.CATEGORY_PATTERNS["product"].search(blob):
            return "product"
        if self.CATEGORY_PATTERNS["cases"].search(blob):
            return "cases"
        return "other"

    # ------------------ Fetch items ------------------
    async def fetch_items(self, feed_url: str, feed_type: str) -> List[Dict]:
        items: List[Dict] = []

        try:
            if feed_type in ['rss', 'atom']:
                response = requests.get(feed_url, timeout=15, headers=DEFAULT_HEADERS)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                for entry in feed.entries:
                    pub_date = self.parse_publication_date(entry)

                    # Теги/summary для лучшей категоризации
                    rss_tags: List[str] = []
                    if hasattr(entry, "tags") and entry.tags:
                        for t in entry.tags:
                            term = (t.get("term") if isinstance(t, dict) else getattr(t, "term", None))
                            if term:
                                rss_tags.append(str(term))
                    summary = entry.get("summary", "") or entry.get("description", "")

                    category = self.classify_news(
                        entry.get("title", ""),
                        entry.get("link", ""),
                        rss_tags,
                        summary,
                    )

                    item_id = entry.get('id', entry.get('link', '')) or f"{entry.get('title', '')}_{pub_date.isoformat()}"
                    items.append({
                        'id': item_id,
                        'title': entry.get('title', 'No title'),
                        'link': entry.get('link', ''),
                        'date': pub_date,
                        'category': category,
                    })

            elif feed_type == 'html':
                soups = await self._fetch_list_pages_with_pagination(feed_url, max_pages=3)
                allowed_fragments, banned_fragments = self._fragments_for_domain(self.get_domain_from_url(feed_url))

                found_items = []
                # 1) Сначала пробуем «карточки»
                for soup in soups:
                    selectors = [
                        'article',
                        '.blog-post', '.post', '.news-item', '.article-item', '.entry',
                        '.post-item', '.blog-item', '.news-post', '.article-card',
                        '[class*="post-"]', '[class*="blog-"]', '[class*="article-"]',
                        '[class*="news-"]', '[class*="entry-"]'
                    ]
                    for selector in selectors:
                        try:
                            elements = soup.select(selector)
                            valid_articles = []
                            for elem in elements:
                                title_elem = elem.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                                link_elem = elem.find('a', href=True)
                                if not (title_elem and link_elem):
                                    continue
                                title_text = title_elem.get_text().strip()
                                link_href = link_elem.get('href', '')
                                full = urljoin(feed_url, link_href)
                                path_lower = urlparse(full).path.lower()
                                if (
                                    len(title_text) > 8 and
                                    any(path_lower == af or path_lower.startswith(af + '/') for af in allowed_fragments) and
                                    not any(path_lower == bf or path_lower.startswith(bf + '/') for bf in banned_fragments) and
                                    not any(sk in link_href.lower() for sk in ['mailto:', 'tel:', '#', 'javascript:'])
                                ):
                                    valid_articles.append((elem, soup))
                            if len(valid_articles) >= 2:
                                found_items.extend(valid_articles)
                                break
                        except Exception as e:
                            logger.debug(f"Selector error {selector}: {e}")
                            continue

                # 2) Если карточек мало — «план С»: собрать все ссылки /blog/… и т.п.
                if len(found_items) < 3:
                    seen_links = set()
                    for soup in soups:
                        for a in soup.select('a[href]'):
                            href = a.get('href', '')
                            full = urljoin(feed_url, href)
                            if full in seen_links:
                                continue
                            seen_links.add(full)
                            path_lower = urlparse(full).path.lower()
                            if (
                                any(path_lower == af or path_lower.startswith(af + '/') for af in allowed_fragments) and
                                not any(path_lower == bf or path_lower.startswith(bf + '/') for bf in banned_fragments) and
                                not any(sk in href.lower() for sk in ['mailto:', 'tel:', '#', 'javascript:'])
                            ):
                                container = a
                                for _ in range(3):
                                    if container.parent:
                                        container = container.parent
                                found_items.append((container, soup))

                logger.info(f"Processing {len(found_items)} potential articles from HTML pages")

                # 3) Формирование items (всегда уточняем дату со страницы)
                added = set()
                for item_node, page_soup in found_items:
                    try:
                        a = item_node.find('a', href=True)
                        if not a:
                            continue
                        link = urljoin(feed_url, a['href'])
                        if link in added:
                            continue
                        if 'mailto:' in link or 'tel:' in link or link.endswith('#'):
                            continue

                        title_elem = item_node.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                        title = ''
                        if title_elem:
                            title = title_elem.get_text(' ', strip=True)
                        if not title:
                            title = a.get_text(' ', strip=True)
                        title = re.sub(r'\s+', ' ', title).strip()
                        if len(title) < 5:
                            continue

                        # Предварительная дата + точное уточнение
                        pub_date = self.parse_pub_date(item_node, page_soup, link)
                        precise = await self.extract_date_from_article_page(link)
                        if precise:
                            pub_date = precise

                        category = self.classify_news(title, link)

                        items.append({
                            'id': link,
                            'title': title[:200],
                            'link': link,
                            'date': pub_date,
                            'category': category,
                        })
                        added.add(link)

                        if len(items) >= 40:
                            break
                    except Exception as e:
                        logger.debug(f"Error parsing HTML item: {e}")
                        continue

        except Exception as e:
            logger.error(f"Error fetching items from {feed_url}: {e}")

        logger.info(f"Successfully parsed {len(items)} items")
        return items

    def _fragments_for_domain(self, domain: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        rules = DOMAIN_RULES.get(domain, {})
        allow = rules.get("allow", DEFAULT_ALLOWED)
        ban = rules.get("ban", DEFAULT_BANNED)
        return allow, ban

    async def _fetch_list_pages_with_pagination(self, start_url: str, max_pages: int = 3) -> List[BeautifulSoup]:
        """Загружаем страницу списка + до 2 следующих (rel=next, /page/2, ?page=2 ...)"""
        soups: List[BeautifulSoup] = []
        visited = set()
        current = start_url

        for _ in range(max_pages):
            if not current or current in visited:
                break
            visited.add(current)
            try:
                r = requests.get(current, timeout=12, headers=DEFAULT_HEADERS)
                if r.status_code != 200:
                    break
                soup = BeautifulSoup(r.content, 'html.parser')
                soups.append(soup)

                # пытаемся найти next
                next_link = None
                link_tag = soup.find('link', rel=lambda v: v and 'next' in v.lower())
                if link_tag and link_tag.get('href'):
                    next_link = urljoin(current, link_tag['href'])

                if not next_link:
                    a_next = soup.find('a', rel=lambda v: v and 'next' in v.lower())
                    if a_next and a_next.get('href'):
                        next_link = urljoin(current, a_next['href'])

                if not next_link:
                    # эвристики: /page/2, /page/3
                    m = re.search(r'(.*?/page/)(\d+)/?$', current)
                    if m:
                        base, num = m.group(1), int(m.group(2))
                        next_link = base + str(num + 1)
                    else:
                        # ?page=2
                        if '?' in current:
                            if re.search(r'([?&])page=(\d+)', current):
                                next_link = re.sub(r'([?&])page=(\d+)', lambda m: f"{m.group(1)}page={int(m.group(2)) + 1}", current)
                            else:
                                next_link = current + "&page=2"
                        else:
                            next_link = current.rstrip('/') + '/page/2'

                if urlparse(next_link).netloc and (self.get_domain_from_url(next_link) != self.get_domain_from_url(start_url)):
                    break

                current = next_link
            except Exception as e:
                logger.debug(f"Pagination fetch error: {e}")
                break

        return soups

    def parse_publication_date(self, entry) -> datetime:
        date_fields = ['published_parsed', 'updated_parsed', 'created_parsed']
        for field in date_fields:
            if hasattr(entry, field) and getattr(entry, field):
                try:
                    parsed_time = getattr(entry, field)
                    return datetime(*parsed_time[:6])
                except Exception:
                    continue

        string_fields = ['published', 'updated', 'created', 'pubDate']
        for field in string_fields:
            if hasattr(entry, field) and getattr(entry, field):
                try:
                    date_str = getattr(entry, field)
                    return date_parser.parse(date_str, ignoretz=True)
                except Exception:
                    continue

        return datetime.now()

    def format_news_item(self, item: Dict) -> str:
        pub_date = item['date']
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

        title = item['title']
        if len(title) > 120:
            title = title[:117] + "..."

        tag = (item.get('category') or 'other').lower()
        tag_emoji = {'event': '🎪', 'product': '🧩', 'cases': '📘', 'other': '🏷️'}.get(tag, '🏷️')

        return f"{tag_emoji} *{title}*\n📅 {date_str}\n🏷️ {tag}\n🔗 {item['link']}\n"

    # ------------------ Initial send ------------------
    async def send_initial_items(self, update: Update, source_id: int):
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()

            cursor.execute("SELECT feed_url, feed_type, domain FROM sources WHERE id = ?", (source_id,))
            result = cursor.fetchone()
            if not result:
                return

            feed_url, feed_type, domain = result
            items = await self.fetch_items(feed_url, feed_type)
            if not items:
                await update.message.reply_text(f"📭 No recent items found for {domain}")
                conn.close()
                return

            items.sort(key=lambda x: x['date'], reverse=True)
            recent_items = items[:3]

            await update.message.reply_text(
                f"📰 *Latest {len(recent_items)} items from {domain}:*\n_Found {len(items)} total articles_",
                parse_mode=ParseMode.MARKDOWN
            )

            for i, item in enumerate(recent_items, 1):
                message = f"*{i}/{len(recent_items)}*\n{self.format_news_item(item)}"
                await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

                cursor.execute('''
                    INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                    VALUES (?, ?, ?, ?, ?)
                ''', (source_id, item['id'], item['title'], item['link'], item['date']))

                await asyncio.sleep(0.4)

            cursor.execute("UPDATE sources SET first_sent = TRUE, last_check = ? WHERE id = ?",
                           (datetime.now(), source_id))
            conn.commit()
            conn.close()

        except Exception:
            logger.exception("Error sending initial items")

    # ------------------ Runner ------------------
    def run(self):
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        app.add_handler(CommandHandler("remove", self.remove_source))
        app.add_handler(CommandHandler("test", self.test_source))

        job_queue = app.job_queue
        if job_queue:
            job_queue.run_repeating(self.periodic_check, interval=1200, first=60)
        else:
            logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")

        print("🤖 News Monitor Bot is starting...")
        print("📊 Monitoring checks every 20 minutes")
        print("💾 Database: news_monitor.db")

        app.run_polling(allowed_updates=Update.ALL_TYPES)


# ======================== MAIN ===========================
if __name__ == '__main__':
    # Читаем из BOT_TOKEN или TELEGRAM_BOT_TOKEN (оба варианта поддержаны)
    BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN_HERE"
    bot = NewsMonitorBot(BOT_TOKEN)
    bot.run()
