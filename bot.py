import asyncio
import sqlite3
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timedelta
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
        conn = sqlite3.connect(self.db_path)
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
            await update.message.reply_text("Usage: /add <company_website_url>\n\nExample:\n`/add https://microsoft.com`\n`/add techcrunch.com`", parse_mode=ParseMode.MARKDOWN)
            return
            
        url = context.args[0]
        user_id = update.effective_user.id
        
        try:
            # Normalize URL and extract domain
            normalized_url, domain = self.normalize_url(url)
            
            # Check if already exists
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM sources WHERE user_id = ? AND domain = ?", 
                         (user_id, domain))
            if cursor.fetchone():
                await update.message.reply_text(f"❌ {domain} is already being monitored.")
                conn.close()
                return
            
            await update.message.reply_text(f"🔍 Analyzing {domain}... Looking for news feeds...")
            
            # Try to find RSS/Atom feed
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
            logger.error(f"Error adding source: {e}")
            await update.message.reply_text(f"❌ Error adding source: {str(e)}")

    def normalize_url(self, url: str) -> Tuple[str, str]:
        """Normalize URL and extract domain"""
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
            
        normalized_url = f"{parsed.scheme}://{parsed.netloc}"
        return normalized_url, domain

    async def discover_feed(self, base_url: str) -> Tuple[Optional[str], Optional[str]]:
        """Discover RSS/Atom feed from website"""
        try:
            # First, try to get the main page and look for feed links
            response = requests.get(base_url, timeout=15, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            })
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Look for RSS/Atom feed links in HTML head
            for link in soup.find_all('link', rel='alternate'):
                type_attr = link.get('type', '').lower()
                if any(feed_type in type_attr for feed_type in ['rss', 'atom', 'xml']):
                    feed_url = urljoin(base_url, link.get('href'))
                    feed_type = 'atom' if 'atom' in type_attr else 'rss'
                    
                    # Verify the feed works
                    if await self.verify_feed(feed_url):
                        return feed_url, feed_type
            
            # Try common RSS/Atom paths (prioritize blog/news paths)
            common_paths = [
                '/blog/feed', '/blog/rss', '/blog/feed.xml', '/blog/atom.xml',
                '/news/feed', '/news/rss', '/news/feed.xml', '/news/atom.xml',
                '/feed', '/feeds', '/rss', '/rss.xml', '/atom.xml',
                '/press/feed', '/press/rss', '/feed.xml', '/index.xml',
                '/blog/index.xml', '/news/index.xml'
            ]
            
            for path in common_paths:
                feed_url = urljoin(base_url, path)
                if await self.verify_feed(feed_url):
                    return feed_url, 'rss'
            
            # Try to find news/blog page and parse HTML (prioritize blog over main site)
            news_paths = ['/blog', '/news', '/press', '/press-releases', '/newsroom', '/articles']
            for path in news_paths:
                news_url = urljoin(base_url, path)
                try:
                    news_response = requests.get(news_url, timeout=10, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    if news_response.status_code == 200:
                        # Verify this page has actual articles
                        if await self.verify_html_has_articles(news_url):
                            return news_url, 'html'
                except:
                    continue
            
            # Last resort: try main page
            if await self.verify_html_has_articles(base_url):
                return base_url, 'html'
                    
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
            
            # Look for indicators that this is a blog/news page
            blog_indicators = [
                'article', '.post', '.blog-post', '.news-item', 
                '[class*="post"]', '[class*="article"]', '[class*="blog"]'
            ]
            
            article_count = 0
            for selector in blog_indicators:
                try:
                    elements = soup.select(selector)
                    for elem in elements:
                        # Check if element has title and looks like an article
                        title_elem = elem.find(['h1', 'h2', 'h3', 'h4'])
                        link_elem = elem.find('a', href=True)
                        if title_elem and link_elem and len(title_elem.get_text().strip()) > 10:
                            article_count += 1
                            if article_count >= 2:  # Found at least 2 articles
                                return True
                except:
                    continue
                    
            return False
            
        except:
            return False

    async def send_initial_items(self, update: Update, source_id: int):
        """Send the 3 most recent items for a newly added source"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Get source info
            cursor.execute("SELECT feed_url, feed_type, domain FROM sources WHERE id = ?", (source_id,))
            result = cursor.fetchone()
            if not result:
                return
                
            feed_url, feed_type, domain = result
            
            # Get latest items
            items = await self.fetch_items(feed_url, feed_type)
            
            if not items:
                await update.message.reply_text(f"📭 No recent items found for {domain}")
                conn.close()
                return
            
            # Sort by date and take top 3
            items.sort(key=lambda x: x['date'], reverse=True)
            recent_items = items[:3]
            
            await update.message.reply_text(
                f"📰 *Latest {len(recent_items)} items from {domain}:*\n_Found {len(items)} total articles_", 
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Send items and cache them
            for i, item in enumerate(recent_items, 1):
                message = f"*{i}/{len(recent_items)}*\n{self.format_news_item(item)}"
                await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                
                # Cache item
                cursor.execute('''
                    INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                    VALUES (?, ?, ?, ?, ?)
                ''', (source_id, item['id'], item['title'], item['link'], item['date']))
                
                await asyncio.sleep(0.5)  # Small delay between messages
            
            # Mark as first sent
            cursor.execute("UPDATE sources SET first_sent = TRUE, last_check = ? WHERE id = ?", 
                         (datetime.now(), source_id))
            
            conn.commit()
            conn.close()
            
        except Exception as e:
            logger.error(f"Error sending initial items: {e}")

    async def fetch_items(self, feed_url: str, feed_type: str) -> List[Dict]:
        """Fetch items from RSS/Atom feed or HTML page"""
        items = []
        
        try:
            if feed_type in ['rss', 'atom']:
                # Parse RSS/Atom feed
                response = requests.get(feed_url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                response.raise_for_status()
                
                feed = feedparser.parse(response.content)
                
                for entry in feed.entries:
                    # Parse date with better handling
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
                # Enhanced HTML parsing for news/blog pages
                response = requests.get(feed_url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                response.raise_for_status()
                
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Enhanced selectors specifically for blog/news articles
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
                        # Filter elements that actually look like articles
                        valid_articles = []
                        for elem in elements:
                            # Must have a meaningful title
                            title_elem = elem.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                            # Must have a link
                            link_elem = elem.find('a', href=True)
                            
                            if title_elem and link_elem:
                                title_text = title_elem.get_text().strip()
                                link_href = link_elem.get('href', '')
                                
                                # Filter out non-article links
                                if (len(title_text) > 15 and  # Meaningful title length
                                    not any(skip in title_text.lower() for skip in ['contact', 'about', 'privacy', 'terms', 'cookie']) and
                                    not any(skip in link_href.lower() for skip in ['mailto:', 'tel:', '#', 'javascript:', '/contact', '/about']) and
                                    ('blog' in link_href.lower() or 'news' in link_href.lower() or 'article' in link_href.lower() or len(link_href.split('/')) > 4)):
                                    valid_articles.append(elem)
                        
                        if len(valid_articles) >= 3:  # Good selector if it finds multiple valid articles
                            found_items = valid_articles[:20]
                            logger.info(f"Using selector '{selector}' - found {len(found_items)} articles")
                            break
                    except Exception as e:
                        logger.debug(f"Error with selector {selector}: {e}")
                        continue
                
                # If no good selector found, try a more targeted approach for blog pages
                if not found_items:
                    # Look specifically for blog/news containers
                    blog_containers = soup.find_all(['div', 'section', 'main'], 
                        class_=re.compile(r'blog|post|article|news|content|main|grid|list', re.I))
                    
                    for container in blog_containers:
                        # Look for repeating patterns that could be articles
                        potential_articles = container.find_all(['div', 'article', 'li'], limit=30)
                        valid_articles = []
                        
                        for article in potential_articles:
                            title_elem = article.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                            link_elem = article.find('a', href=True)
                            
                            if title_elem and link_elem:
                                title_text = title_elem.get_text().strip()
                                link_href = link_elem.get('href', '')
                                
                                if (len(title_text) > 15 and
                                    not any(skip in title_text.lower() for skip in ['contact', 'about', 'privacy', 'terms']) and
                                    not any(skip in link_href.lower() for skip in ['mailto:', 'tel:', '#', 'javascript:']) and
                                    (link_href.startswith('http') or link_href.startswith('/') and len(link_href) > 5)):
                                    valid_articles.append(article)
                        
                        if len(valid_articles) >= 3:
                            found_items = valid_articles[:20]
                            logger.info(f"Found {len(found_items)} articles in container")
                            break
                
                logger.info(f"Processing {len(found_items)} potential articles from HTML page")
                
                for item in found_items:
                    try:
                        # Enhanced title extraction
                        title_elem = item.select_one('h1, h2, h3, h4, h5, .title, [class*="title"], [class*="headline"]')
                        
                        if not title_elem:
                            continue
                        
                        # Enhanced link extraction
                        link_elem = item.find('a', href=True)
                        if not link_elem:
                            # Check if title itself is a link
                            link_elem = title_elem.find('a', href=True)
                            if not link_elem:
                                link_elem = title_elem.find_parent('a', href=True)
                        
                        if not link_elem:
                            continue
                        
                        title = title_elem.get_text().strip()
                        title = re.sub(r'\s+', ' ', title)  # Remove extra whitespace
                        
                        # Skip titles that are too short or look like navigation
                        if (len(title) < 15 or 
                            any(skip in title.lower() for skip in ['contact us', 'about us', 'privacy policy', 'terms', 'customer stories', 'solutions'])):
                            continue
                        
                        link = urljoin(feed_url, link_elem['href'])
                        
                        # Skip duplicate links or invalid links
                        if (any(existing['link'] == link for existing in items) or 
                            'mailto:' in link or 'tel:' in link or link.endswith('#')):
                            continue
                        
                        # Enhanced date extraction
                        pub_date = self.extract_date_from_html(item)
                        
                        # If we couldn't find a date in the item, try to extract from the link
                        if pub_date == datetime.now() or (datetime.now() - pub_date).total_seconds() < 60:
                            pub_date = await self.extract_date_from_article_page(link)
                        
                        items.append({
                            'id': link,
                            'title': title[:200],  # Limit title length
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
            
            # Look for meta tags with publication date
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
                    except:
                        continue
            
            # Look for time elements
            time_elem = soup.select_one('time[datetime]')
            if time_elem:
                try:
                    return date_parser.parse(time_elem['datetime'], ignoretz=True)
                except:
                    pass
            
            # Look for date patterns in the text
            text_content = soup.get_text()
            
            # Look for patterns like "July 30, 2024" or "Jul 30"
            date_patterns = [
                r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b',
                r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})\b',
                r'\b(\d{4}-\d{2}-\d{2})\b',
                r'\b(\d{1,2}/\d{1,2}/\d{4})\b'
            ]
            
            for pattern in date_patterns:
                matches = re.search(pattern, text_content, re.IGNORECASE)
                if matches:
                    try:
                        if len(matches.groups()) == 3:  # Month name pattern
                            month_name, day, year = matches.groups()
                            date_str = f"{month_name} {day}, {year}"
                        else:
                            date_str = matches.group(1)
                        return date_parser.parse(date_str, ignoretz=True)
                    except:
                        continue
            
        except Exception as e:
            logger.debug(f"Error extracting date from {article_url}: {e}")
        
        return datetime.now()

    def parse_publication_date(self, entry) -> datetime:
        """Enhanced date parsing for RSS/Atom entries"""
        # Try different date fields in order of preference
        date_fields = ['published_parsed', 'updated_parsed', 'created_parsed']
        
        for field in date_fields:
            if hasattr(entry, field) and getattr(entry, field):
                try:
                    parsed_time = getattr(entry, field)
                    return datetime(*parsed_time[:6])
                except:
                    continue
        
        # Try string date fields
        string_fields = ['published', 'updated', 'created', 'pubDate']
        
        for field in string_fields:
            if hasattr(entry, field) and getattr(entry, field):
                try:
                    date_str = getattr(entry, field)
                    return date_parser.parse(date_str, ignoretz=True)
                except:
                    continue
        
        # Fallback to current time
        return datetime.now()

    def extract_date_from_html(self, item) -> datetime:
        """Extract publication date from HTML article"""
        # Look for date in various formats and locations
        date_selectors = [
            'time', '.date', '.published', '.post-date', '.article-date',
            '[class*="date"]', '[class*="time"]', '[class*="published"]',
            'meta[property="article:published_time"]',
            'meta[name="published"]', 'meta[name="date"]'
        ]
        
        for selector in date_selectors:
            try:
                date_elem = item.select_one(selector)
                if date_elem:
                    # Check for datetime attribute first
                    if date_elem.get('datetime'):
                        try:
                            return date_parser.parse(date_elem['datetime'], ignoretz=True)
                        except:
                            pass
                    
                    # Check for content attribute (meta tags)
                    if date_elem.get('content'):
                        try:
                            return date_parser.parse(date_elem['content'], ignoretz=True)
                        except:
                            pass
                    
                    # Parse text content
                    date_text = date_elem.get_text(strip=True)
                    if date_text:
                        try:
                            return date_parser.parse(date_text, ignoretz=True)
                        except:
                            pass
            except:
                continue
        
        # Look for date patterns in text
        text_content = item.get_text()
        date_patterns = [
            r'\b(\w+\s+\d{1,2},\s+\d{4})\b',  # "January 15, 2024"
            r'\b(\d{1,2}/\d{1,2}/\d{4})\b',    # "01/15/2024"
            r'\b(\d{4}-\d{2}-\d{2})\b',        # "2024-01-15"
            r'\b(\d{1,2}-\d{1,2}-\d{4})\b',    # "15-01-2024"
        ]
        
        for pattern in date_patterns:
            matches = re.search(pattern, text_content)
            if matches:
                try:
                    return date_parser.parse(matches.group(1), ignoretz=True)
                except:
                    continue
        
        # Fallback to current time
        return datetime.now()

    def format_news_item(self, item: Dict) -> str:
        """Enhanced formatting for news items with better date display"""
        # Format date more nicely
        pub_date = item['date']
        
        # Check if it's today, yesterday, or older
        now = datetime.now()
        days_diff = (now.date() - pub_date.date()).days
        
        if days_diff == 0:
            date_str = f"Today {pub_date.strftime('%H:%M')}"
        elif days_diff == 1:
            date_str = f"Yesterday {pub_date.strftime('%H:%M')}"
        elif days_diff < 7:
            date_str = pub_date.strftime("%A %H:%M")  # "Monday 14:30"
        else:
            date_str = pub_date.strftime("%b %d, %Y %H:%M")  # "Jan 15, 2024 14:30"
        
        # Clean and limit title length
        title = item['title']
        if len(title) > 120:
            title = title[:117] + "..."
        
        return f"📰 *{title}*\n📅 {date_str}\n🔗 {item['link']}\n"

    async def list_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List command: /list"""
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT domain, status, feed_type, last_check, created_at
            FROM sources WHERE user_id = ?
            ORDER BY created_at DESC
        ''', (user_id,))
        
        sources = cursor.fetchall()
        conn.close()
        
        if not sources:
            await update.message.reply_text("📭 No sources being monitored.\n\nUse `/add <url>` to start monitoring a website!", parse_mode=ParseMode.MARKDOWN)
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
            await update.message.reply_text("Usage: /remove <domain>\n\nExample:\n`/remove microsoft.com`", parse_mode=ParseMode.MARKDOWN)
            return
            
        domain = context.args[0].lower()
        if domain.startswith('www.'):
            domain = domain[
