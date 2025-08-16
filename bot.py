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
            
            # Try common RSS/Atom paths
            common_paths = [
                '/feed', '/feeds', '/rss', '/rss.xml', '/atom.xml',
                '/blog/feed', '/blog/rss', '/news/feed', '/news/rss',
                '/press/feed', '/press/rss', '/feed.xml', '/index.xml',
                '/blog/index.xml', '/news/index.xml'
            ]
            
            for path in common_paths:
                feed_url = urljoin(base_url, path)
                if await self.verify_feed(feed_url):
                    return feed_url, 'rss'
            
            # Try to find news page and parse HTML
            news_paths = ['/news', '/press', '/blog', '/press-releases', '/newsroom']
            for path in news_paths:
                news_url = urljoin(base_url, path)
                try:
                    news_response = requests.get(news_url, timeout=10, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    if news_response.status_code == 200:
                        return news_url, 'html'
                except:
                    continue
                    
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
                # Enhanced HTML parsing
                response = requests.get(feed_url, timeout=15, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                response.raise_for_status()
                
                soup = BeautifulSoup(response.content, 'html.parser')
                
                # Enhanced selectors for finding articles
                article_selectors = [
                    'article',
                    '.post', '.blog-post', '.news-item', '.article-item',
                    '.entry', '.post-item', '.blog-item', '.news-post',
                    '[class*="post"]', '[class*="article"]', '[class*="blog"]',
                    '[class*="news"]', '[class*="entry"]', '[class*="item"]'
                ]
                
                found_items = []
                for selector in article_selectors:
                    try:
                        elements = soup.select(selector)
                        if elements and len(elements) >= 3:  # Good selector if it finds multiple items
                            found_items = elements[:20]  # Take up to 20 items
                            break
                    except:
                        continue
                
                # If no good selector found, try a more general approach
                if not found_items:
                    # Look for common container patterns
                    containers = soup.find_all(['div', 'section'], class_=re.compile(r'blog|post|article|news|content|main', re.I))
                    for container in containers:
                        articles = container.find_all(['article', 'div', 'li'], limit=20)
                        if len(articles) > 5:  # If we found a container with many items
                            found_items = articles
                            break
                
                logger.info(f"Found {len(found_items)} potential articles on HTML page")
                
                for item in found_items:
                    try:
                        # Enhanced title extraction
                        title_elem = None
                        title_selectors = [
                            'h1', 'h2', 'h3', 'h4', 'h5', 
                            '.title', '.headline', '.post-title', '.article-title',
                            '[class*="title"]', '[class*="headline"]'
                        ]
                        
                        for selector in title_selectors:
                            title_elem = item.select_one(selector)
                            if title_elem and title_elem.get_text(strip=True):
                                break
                        
                        # Enhanced link extraction
                        link_elem = item.find('a', href=True)
                        if not link_elem:
                            # Look for link in title
                            if title_elem:
                                link_elem = title_elem.find('a', href=True) or title_elem.find_parent('a', href=True)
                        
                        # Enhanced date extraction
                        pub_date = self.extract_date_from_html(item)
                        
                        if title_elem and link_elem:
                            title = title_elem.get_text().strip()
                            # Clean up title
                            title = re.sub(r'\s+', ' ', title)  # Remove extra whitespace
                            title = title[:200] if len(title) > 200 else title  # Limit length
                            
                            if len(title) < 10:  # Skip very short titles
                                continue
                                
                            link = urljoin(feed_url, link_elem['href'])
                            
                            # Skip duplicate links
                            if any(existing['link'] == link for existing in items):
                                continue
                            
                            items.append({
                                'id': link,
                                'title': title,
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
            domain = domain[4:]
            
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get source ID
        cursor.execute("SELECT id FROM sources WHERE user_id = ? AND domain = ?", (user_id, domain))
        result = cursor.fetchone()
        
        if not result:
            await update.message.reply_text(f"❌ Domain `{domain}` not found in your monitored sources.\n\nUse `/list` to see your sources.", parse_mode=ParseMode.MARKDOWN)
            conn.close()
            return
        
        source_id = result[0]
        
        # Delete source and its cached items
        cursor.execute("DELETE FROM item_cache WHERE source_id = ?", (source_id,))
        cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        
        conn.commit()
        conn.close()
        
        await update.message.reply_text(f"✅ Removed `{domain}` from monitoring.", parse_mode=ParseMode.MARKDOWN)

    async def test_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Test command: /test <domain>"""
        if not context.args:
            await update.message.reply_text("Usage: /test <domain>\n\nExample:\n`/test microsoft.com`", parse_mode=ParseMode.MARKDOWN)
            return
            
        domain = context.args[0].lower()
        if domain.startswith('www.'):
            domain = domain[4:]
            
        user_id = update.effective_user.id
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT feed_url, feed_type FROM sources WHERE user_id = ? AND domain = ?", 
                      (user_id, domain))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            await update.message.reply_text(f"❌ Domain `{domain}` not found in your monitored sources.\n\nUse `/list` to see your sources.", parse_mode=ParseMode.MARKDOWN)
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
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get all active sources that have been initially sent
        cursor.execute('''
            SELECT id, user_id, domain, feed_url, feed_type
            FROM sources 
            WHERE status = 'active' AND first_sent = TRUE
        ''')
        
        sources = cursor.fetchall()
        logger.info(f"Checking {len(sources)} active sources...")
        
        for source_id, user_id, domain, feed_url, feed_type in sources:
            try:
                # Fetch latest items
                items = await self.fetch_items(feed_url, feed_type)
                
                if not items:
                    continue
                
                # Get cached item IDs
                cursor.execute("SELECT item_id FROM item_cache WHERE source_id = ?", (source_id,))
                cached_ids = {row[0] for row in cursor.fetchall()}
                
                # Find new items
                new_items = [item for item in items if item['id'] not in cached_ids]
                
                if new_items:
                    logger.info(f"Found {len(new_items)} new items for {domain}")
                    
                    # Sort by date (oldest first for sending)
                    new_items.sort(key=lambda x: x['date'])
                    
                    # Send new items to user (limit to 3 per check)
                    for item in new_items[:3]:
                        message = f"🆕 *New from {domain}*\n\n{self.format_news_item(item)}"
                        
                        try:
                            await context.bot.send_message(
                                chat_id=user_id, 
                                text=message, 
                                parse_mode=ParseMode.MARKDOWN,
                                disable_web_page_preview=True
                            )
                            
                            # Cache the item
                            cursor.execute('''
                                INSERT OR IGNORE INTO item_cache (source_id, item_id, title, link, pub_date)
                                VALUES (?, ?, ?, ?, ?)
                            ''', (source_id, item['id'], item['title'], item['link'], item['date']))
                            
                            # Small delay between messages
                            await asyncio.sleep(1)
                            
                        except Exception as e:
                            logger.error(f"Error sending message to user {user_id}: {e}")
                
                # Update last check time
                cursor.execute("UPDATE sources SET last_check = ? WHERE id = ?", 
                             (datetime.now(), source_id))
                
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

*💡 Examples:*
`/add https://microsoft.com`
`/add techcrunch.com`
`/add https://blog.openai.com`

*🔔 Monitoring:*
I check for updates every 20 minutes and will send you new items as soon as they appear!

Ready to start? Try `/add <website_url>` now!
        """
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)

    def run(self):
        """Run the bot"""
        app = Application.builder().token(self.token).build()
        
        # Add command handlers
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("add", self.add_source))
        app.add_handler(CommandHandler("list", self.list_sources))
        app.add_handler(CommandHandler("remove", self.remove_source))
        app.add_handler(CommandHandler("test", self.test_source))
        
        # Add periodic job for monitoring (every 20 minutes)
        job_queue = app.job_queue
        job_queue.run_repeating(self.periodic_check, interval=1200, first=60)  # 20 minutes, start after 1 minute
        
        print("🤖 News Monitor Bot is starting...")
        print("🔑 Token configured successfully")
        print("📊 Monitoring checks every 20 minutes")
        print("💾 Database: news_monitor.db")
        
        # Run the bot
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    # Your bot token
    BOT_TOKEN = "8247686235:AAENkGQjszdXz1Q9DX2HjdAhb8lAtDTEYkM"
    
    bot = NewsMonitorBot(BOT_TOKEN)
    bot.run()
