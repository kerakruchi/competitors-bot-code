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

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class NewsMonitorBot:
    def __init__(self, token: str):
        self.token = token
        self.db_path = "news_monitor.db"
        self.init_database()

    def init_database(self):
        """Initialize SQLite database"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        cursor = conn.cursor()

        # Sources table
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

        # Items cache table
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

    async def add_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add command: /add <url>"""
        if not context.args:
            await update.message.reply_text(
                "Usage: /add <company_website_url>\n\nExample:\n`/add https://microsoft.com`\n`/add techcrunch.com`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        url = context.args[0]
        user_id = update.effective_user.id

        try:
            # Normalize URL and extract domain
            normalized_url, domain = self.normalize_url(url)

            # Check if already exists
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain))
            if cursor.fetchone():
                await update.message.reply_text(f"❌ {domain} is already being monitored.")
                conn.close()
                return

            await update.message.reply_text(f"🔍 Analyzing {domain}... Looking for news feeds...")

            # Try to find RSS/Atom feed or HTML news page
            feed_url, feed_type = await self.discover_feed(normalized_url)

            if not feed_url:
                await update.message.reply_text(f"❌ Could not find a news feed for {domain}")
                conn.close()
                return

            # Save to database
            cursor.execute('''
                INSERT INTO sources (user_id, domain, url, feed_url, feed_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, domain, normalized_url, feed_url, feed_type))

            source_id = cursor.lastrowid
            conn.commit()
            conn.close()

            await update.message.reply_text(f"✅ Added {domain}. Monitoring started.\n📡 Feed type: {feed_type.upper()}")

            # Get and send first 3 items
            await self.send_initial_items(update, source_id)

        except Exception as e:
            logger.exception("Error adding source")
            await update.message.reply_text(f"❌ Error adding source: {str(e)}")

    def normalize_url(self, url: str) -> Tuple[str, str]:
        """Normalize URL and extract domain.
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
        news_roots = ('blog', 'news', 'press', 'newsroom', 'articles', 'media')
        if path and path.split('/')[0].lower() in news_roots:
            normalized_url = f"{root}/{path.split('/')[0]}"
        else:
            normalized_url = root

        return normalized_url, host

    async def discover_feed(self, base_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Discover RSS/Atom feed or HTML news/blog page.
        Ищем по: сам домен → популярные поддомены → стандартные RSS пути → страницы новостей.
        """
        try:
            parsed = urlparse(base_url)
            scheme = parsed.scheme or 'https'
            host = parsed.netloc
            root = f"{scheme}://{host}"

            # 0) Базовая страница доступна?
            try:
                resp0 = requests.get(base_url, timeout=12, headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; NewsMonitorBot/1.0)'
                })
                if resp0.status_code == 200:
                    # Если вдруг это уже RSS — ок
                    if await self.verify_feed(base_url):
                        return base_url, 'rss'
                    # Если это раздел новостей (blog/news/press/...), примем как HTML-источник
                    if any(seg in parsed.path.lower() for seg in ['/blog', '/news', '/press', '/newsroom', '/articles', '/media']):
                        return base_url, 'html'
            except Exception:
                pass

            # 1) Популярные поддомены, где часто лежат новости
            subdomains = [
                'blog', 'news', 'press', 'newsroom', 'media', 'stories', 'source',
                'about', 'company', 'corporate', 'investors', 'ir', 'pr', 'updates'
            ]
            candidate_bases = [root] + [f"{scheme}://{sd}.{host}" for sd in subdomains]

            # 2) На каждой базе: ищем <link rel="alternate"> (RSS/Atom)
            for base in candidate_bases:
                try:
                    r = requests.get(base, timeout=12, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
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

            # 3) Стандартные RSS пути (и рядом с /blog)
            common_paths = [
                '/blog/feed', '/blog/rss', '/blog/feed.xml', '/blog/atom.xml', '/blog/index.xml',
                '/news/feed', '/news/rss', '/news/feed.xml', '/news/atom.xml', '/news/index.xml',
                '/rss', '/rss.xml', '/atom.xml', '/feed', '/feed.xml', '/index.xml',
                '/press/feed', '/press/rss'
            ]
            for base in candidate_bases:
                for path in common_paths:
                    fu = urljoin(base, path)
                    if await self.verify_feed(fu):
                        return fu, 'rss'

            # 4) Разделы новостей/блога как HTML
            news_paths = ['/blog', '/news', '/press', '/press-releases', '/newsroom', '/articles', '/stories', '/media', '/updates']
            for base in candidate_bases:
                for path in news_paths:
                    news_url = urljoin(base, path)
                    try:
                        news_response = requests.get(news_url, timeout=12, headers={
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'Accept-Language': 'en-US,en;q=0.9'
                        })
                        if news_response.status_code == 200:
                            return news_url, 'html'
                    except Exception:
                        continue

            # 5) В крайнем случае — главная как HTML
            try:
                r = requests.get(root, timeout=10, headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; NewsMonitorBot/1.0)'
                })
                if r.status_code == 200:
                    return root, 'html'
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error discovering feed for {base_url}: {e}")

        return None, None

    async def verify_feed(self, feed_url: str) -> bool:
        """Verify that a feed URL is valid and parseable"""
        try:
            response = requests.get(feed_url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; NewsBot/1.0)'
            })
            if response.status_code != 200:
                return False

            feed = feedparser.parse(response.content)
            return len(feed.entries) > 0 and not feed.bozo

        except Exception:
            return False

    async def verify_html_has_articles(self, url: str) -> bool:
        """Verify that an HTML page contains actual articles/news"""
        try:
            response = requests.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
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

    async def send_initial_items(self, update: Update, source_id: int):
        """Send the 3 most recent items for a newly added source"""
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

                await asyncio.sleep(0.5)

            cursor.execute("UPDATE sources SET first_sent = TRUE, last_check = ? WHERE id = ?",
                           (datetime.now(), source_id))
            conn.commit()
            conn.close()

        except Exception as e:
            logger.exception("Error sending initial items")

    # ---------- NEW: helpers for HTML publication dates ----------
    def _try_parse_date_text(self, text: str) -> Optional[datetime]:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return date_parser.parse(text, fuzzy=True, ignoretz=True)
        except Exception:
            return None

    def parse_pub_date(self, item_soup: BeautifulSoup, page_soup: BeautifulSoup, link: str) -> datetime:
        """
        Extract publication date from an article card or page soup.
        Priority:
        1) <time datetime> or <time>text</time>
        2) meta article:published_time etc.
        3) common .date/.published classes
        4) date in URL /YYYY/MM/DD/
        5) fallback now()
        """
        # 1) <time> tag
        time_tag = item_soup.find('time')
        if time_tag:
            if time_tag.has_attr('datetime'):
                dt = self._try_parse_date_text(time_tag['datetime'])
                if dt:
                    return dt
            dt = self._try_parse_date_text(time_tag.get_text(" ", strip=True))
            if dt:
                return dt

        # 2) meta tags (page level)
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
                if dt:
                    return dt

        # 3) common classes
        cand = item_soup.select_one(
            '.date, .post-date, .published, .posted-on, .entry-date, [class*="date"], [class*="time"]'
        )
        if cand:
            dt = self._try_parse_date_text(cand.get_text(" ", strip=True))
            if dt:
                return dt

        # 4) date in URL
        if link:
            m = re.search(r'/(\d{4})/(\d{2})/(\d{2})(?:/|$)', link)
            if m:
                try:
                    y, mth, d = map(int, m.groups())
                    return datetime(y, mth, d)
                except Exception:
                    pass

        # 5) fallback
        return datetime.now()
    # -------------------------------------------------------------

    async def fetch_items(self, feed_url: str, feed_type: str) -> List[Dict]:
        """Fetch items from RSS/Atom feed or HTML page"""
        items: List[Dict] = []

        try:
            if feed_type in ['rss', 'atom']:
                response = requests.get(feed_url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; NewsBot/1.0)'
                })
                response.raise_for_status()

                feed = feedparser.parse(response.content)
                for entry in feed.entries:
                    pub_date = self.parse_publication_date(entry)

                    item_id = entry.get('id', entry.get('link', ''))
                    if not item_id:
                        item_id = f"{entry.get('title', '')}_{pub_date.isoformat()}"

                    items.append({
                        'id': item_id,
                        'title': entry.get('title', 'No title'),
                        'link': entry.get('link', ''),
                        'date': pub_date
                    })

            elif feed_type == 'html':
                # Parse an HTML news/blog page
                response = requests.get(feed_url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'html.parser')

                # candidates
                article_selectors = [
                    'article',
                    '.blog-post', '.post', '.news-item', '.article-item', '.entry',
                    '.post-item', '.blog-item', '.news-post', '.article-card',
                    '[class*="post-"]', '[class*="blog-"]', '[class*="article-"]',
                    '[class*="news-"]', '[class*="entry-"]'
                ]

                found_items = []
                for selector in article_selectors:
                    try:
                        elements = soup.select(selector)
                        valid_articles = []
                        for elem in elements:
                            title_elem = elem.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                            link_elem = elem.find('a', href=True)
                            if title_elem and link_elem:
                                title_text = title_elem.get_text().strip()
                                link_href = link_elem.get('href', '')
                                if (
                                    len(title_text) > 15
                                    and not any(sk in title_text.lower() for sk in ['contact', 'about', 'privacy', 'terms', 'cookie'])
                                    and not any(sk in link_href.lower() for sk in ['mailto:', 'tel:', '#', 'javascript:', '/contact', '/about'])
                                    and ('blog' in link_href.lower() or 'news' in link_href.lower() or 'article' in link_href.lower() or len(link_href.split('/')) > 4)
                                ):
                                    valid_articles.append(elem)
                        if len(valid_articles) >= 3:
                            found_items = valid_articles[:20]
                            logger.info(f"Using selector '{selector}' - found {len(found_items)} articles")
                            break
                    except Exception as e:
                        logger.debug(f"Selector error {selector}: {e}")
                        continue

                # fallback containers
                if not found_items:
                    blog_containers = soup.find_all(['div', 'section', 'main'],
                                                    class_=re.compile(r'blog|post|article|news|content|main|grid|list', re.I))
                    for container in blog_containers:
                        potential = container.find_all(['div', 'article', 'li'], limit=30)
                        valid_articles = []
                        for article in potential:
                            title_elem = article.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                            link_elem = article.find('a', href=True)
                            if title_elem and link_elem:
                                title_text = title_elem.get_text().strip()
                                link_href = link_elem.get('href', '')
                                if (
                                    len(title_text) > 15
                                    and not any(sk in title_text.lower() for sk in ['contact', 'about', 'privacy', 'terms'])
                                    and not any(sk in link_href.lower() for sk in ['mailto:', 'tel:', '#', 'javascript:'])
                                    and (link_href.startswith('http') or (link_href.startswith('/') and len(link_href) > 5))
                                ):
                                    valid_articles.append(article)
                        if len(valid_articles) >= 3:
                            found_items = valid_articles[:20]
                            logger.info(f"Found {len(found_items)} articles in container")
                            break

                logger.info(f"Processing {len(found_items)} potential articles from HTML page")

                for item in found_items:
                    try:
                        title_elem = item.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                        if not title_elem:
                            continue

                        link_elem = item.find('a', href=True) or title_elem.find('a', href=True) or title_elem.find_parent('a', href=True)
                        if not link_elem:
                            continue

                        title = re.sub(r'\s+', ' ', title_elem.get_text().strip())
                        if (
                            len(title) < 15 or
                            any(sk in title.lower() for sk in ['contact us', 'about us', 'privacy policy', 'terms', 'customer stories', 'solutions'])
                        ):
                            continue

                        link = urljoin(feed_url, link_elem['href'])
                        if any(existing['link'] == link for existing in items) or 'mailto:' in link or 'tel:' in link or link.endswith('#'):
                            continue

                        # First try to get date from card/meta/URL
                        pub_date = self.parse_pub_date(item, soup, link)

                        # If looks like fallback "now", try precise date from article page
                        if (datetime.now() - pub_date).total_seconds() < 60:
                            precise = await self.extract_date_from_article_page(link)
                            if precise and abs((precise - pub_date).total_seconds()) > 60:
                                pub_date = precise

                        items.append({
                            'id': link,
                            'title': title[:200],
                            'link': link,
                            'date': pub_date
                        })

                    except Exception as e:
                        logger.debug(f"Error parsing HTML item: {e}")
                        continue

        except Exception as e:
            logger.error(f"Error fetching items from {feed_url}: {e}")

        logger.info(f"Successfully parsed {len(items)} items")
        return items

    async def extract_date_from_article_page(self, article_url: str) -> datetime:
        """Extract publication date by visiting the actual article page"""
        try:
            response = requests.get(article_url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            })
            if response.status_code != 200:
                return datetime.now()

            soup = BeautifulSoup(response.content, 'html.parser')

            meta_selectors = [
                'meta[property="article:published_time"]',
                'meta[property="article:modified_time"]',
                'meta[name="publish_date"]',
                'meta[name="publication_date"]',
                'meta[name="date"]',
                'meta[name="created"]'
            ]
            for selector in meta_selectors:
                meta_elem = soup.select_one(selector)
                if meta_elem and meta_elem.get('content'):
                    try:
                        return date_parser.parse(meta_elem['content'], ignoretz=True)
                    except Exception:
                        continue

            time_elem = soup.select_one('time[datetime]')
            if time_elem:
                try:
                    return date_parser.parse(time_elem['datetime'], ignoretz=True)
                except Exception:
                    pass

            # Last resort: pattern search in text
            text_content = soup.get_text()
            date_patterns = [
                r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',
                r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})\b',
                r'\b(\d{4}-\d{2}-\d{2})\b',
                r'\b(\d{1,2}/\d{1,2}/\d{4})\b'
            ]
            for pattern in date_patterns:
                m = re.search(pattern, text_content, re.IGNORECASE)
                if m:
                    try:
                        if len(m.groups()) == 3:
                            month_name, day, year = m.groups()
                            date_str = f"{month_name} {day}, {year}"
                        else:
                            date_str = m.group(1)
                        return date_parser.parse(date_str, ignoretz=True)
                    except Exception:
                        continue

        except Exception as e:
            logger.debug(f"Error extracting date from {article_url}: {e}")

        return datetime.now()

    def parse_publication_date(self, entry) -> datetime:
        """Enhanced date parsing for RSS/Atom entries"""
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
        """Format news item for Telegram message"""
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

        return f"📰 *{title}*\n📅 {date_str}\n🔗 {item['link']}\n"

    async def list_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List command: /list"""
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
        """Remove command: /remove <domain>"""
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
        """Test command: /test <domain>"""
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

    async def periodic_check(self, context: ContextTypes.DEFAULT_TYPE):
        """Periodic check for new items"""
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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command handler"""
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

    def run(self):
        """Run the bot"""
        app = Application.builder().token(self.token).build()

        # Handlers
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        app.add_handler(CommandHandler("remove", self.remove_source))
        app.add_handler(CommandHandler("test", self.test_source))

        # Periodic job for monitoring (every 20 minutes)
        job_queue = app.job_queue
        if job_queue:
            job_queue.run_repeating(self.periodic_check, interval=1200, first=60)
        else:
            logger.warning("JobQueue is not available. Install python-telegram-bot[job-queue].")

        print("🤖 News Monitor Bot is starting...")
        print("📊 Monitoring checks every 20 minutes")
        print("💾 Database: news_monitor.db")

        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    # Временно оставлено захардкоженным — позже лучше вынести в переменную окружения.
    BOT_TOKEN = "8247686235:AAENkGQjszdXz1Q9DX2HjdAhb8lAtDTEYkM"
    bot = NewsMonitorBot(BOT_TOKEN)
    bot.run()
