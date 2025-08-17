# newsbot/fetch.py
"""
Поиск фидов и сбор новостей (RSS/Atom/HTML) + хелперы дат и пагинации.
Возвращаем элементы в формате:
{
    'id': str,
    'title': str,
    'link': str,
    'date': datetime,
    'category': 'event'|'product'|'cases'|'other'
}
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from .config import (
    DEFAULT_HEADERS,
    DEFAULT_ALLOWED,
    DEFAULT_BANNED,
    DOMAIN_RULES,
)
from .classify import classify_news

logger = logging.getLogger(__name__)


# ------------------ URL helpers ------------------
def get_domain_from_url(u: str) -> str:
    """Вернуть домен без www."""
    netloc = urlparse(u).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> Tuple[str, str]:
    """
    Нормализовать URL и вернуть (normalized_url, host).
    - Если введён просто домен → вернём корень (scheme://host)
    - Если путь похож на раздел новостей (blog/news/press/...) → сохраним только корень раздела
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    root = f"{parsed.scheme}://{host}"

    path = (parsed.path or "").strip("/")
    news_roots = (
        "blog",
        "news",
        "press",
        "newsroom",
        "articles",
        "media",
        "stories",
        "updates",
    )
    if path and path.split("/")[0].lower() in news_roots:
        normalized_url = f"{root}/{path.split('/')[0]}"
    else:
        normalized_url = root

    return normalized_url, host


# ------------------ Feed discovery ------------------
async def discover_feed(base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Ищем сначала RSS/Atom, затем валидный HTML-раздел новостей/блога (без solutions/pricing и т.п.).
    Возвращает (url, 'rss'|'atom'|'html') или (None, None).
    """
    try:
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        root = f"{scheme}://{host}"

        # Популярные поддомены + корень
        subdomains = [
            "blog",
            "news",
            "press",
            "newsroom",
            "media",
            "stories",
            "source",
            "about",
            "company",
            "corporate",
            "updates",
        ]
        candidate_bases = [root] + [f"{scheme}://{sd}.{host}" for sd in subdomains]

        # 1) <link rel="alternate"> RSS/Atom
        for base in candidate_bases:
            try:
                r = requests.get(base, timeout=12, headers=DEFAULT_HEADERS)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.content, "html.parser")
                for link in soup.find_all("link", rel="alternate"):
                    type_attr = (link.get("type") or "").lower()
                    if any(t in type_attr for t in ["rss", "atom", "xml"]):
                        feed_url = urljoin(base, link.get("href"))
                        feed_type = "atom" if "atom" in type_attr else "rss"
                        if await verify_feed(feed_url):
                            return feed_url, feed_type
            except Exception:
                continue

        # 2) Распространённые пути фидов
        common_paths = [
            "/blog/feed",
            "/blog/rss",
            "/blog/feed.xml",
            "/blog/atom.xml",
            "/blog/index.xml",
            "/news/feed",
            "/news/rss",
            "/news/feed.xml",
            "/news/atom.xml",
            "/news/index.xml",
            "/press/feed",
            "/press/rss",
            "/rss",
            "/rss.xml",
            "/atom.xml",
            "/feed",
            "/feed.xml",
            "/index.xml",
        ]
        for base in candidate_bases:
            for path in common_paths:
                fu = urljoin(base, path)
                if await verify_feed(fu):
                    return fu, "rss"

        # 3) HTML-разделы новостей/блога (валидация)
        news_paths = [
            "/blog",
            "/news",
            "/press",
            "/press-releases",
            "/newsroom",
            "/articles",
            "/stories",
            "/story",
            "/updates",
        ]
        banned_paths = list(DEFAULT_BANNED)

        def looks_like_news_url(u: str) -> bool:
            up = urlparse(u)
            path = up.path.lower()
            if any(path == bp or path.startswith(bp + "/") for bp in banned_paths):
                return False
            return any(path == np or path.startswith(np + "/") for np in news_paths)

        # Сначала /blog, затем остальное
        ordered_candidates: List[str] = []
        for base in candidate_bases:
            ordered_candidates.append(urljoin(base, "/blog"))
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
                soup = BeautifulSoup(r.content, "html.parser")
                candidates = soup.select(
                    "article, .post, .blog-post, .news-item, "
                    "[class*='post'], [class*='article'], [class*='blog']"
                )
                has_titles = 0
                for el in candidates[:20]:
                    t = el.select_one(
                        "h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']"
                    )
                    a = el.find("a", href=True)
                    if t and a and len(t.get_text(strip=True)) > 10:
                        has_titles += 1
                        if has_titles >= 2:
                            return news_url, "html"
            except Exception:
                continue

        # 4) Фолбэк: главная как HTML
        try:
            r = requests.get(root, timeout=10, headers=DEFAULT_HEADERS)
            if r.status_code == 200:
                return root, "html"
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error discovering feed for {base_url}: {e}")

    return None, None


async def verify_feed(feed_url: str) -> bool:
    """Проверить, что URL — валидный RSS/Atom (хотя бы одна запись, без bozo)."""
    try:
        response = requests.get(feed_url, timeout=10, headers=DEFAULT_HEADERS)
        if response.status_code != 200:
            return False
        feed = feedparser.parse(response.content)
        return len(feed.entries) > 0 and not feed.bozo
    except Exception:
        return False


# ------------------ Date helpers ------------------
def _try_parse_date_text(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date_parser.parse(text, fuzzy=True, ignoretz=True)
    except Exception:
        return None


def parse_pub_date(item_soup: BeautifulSoup, page_soup: BeautifulSoup, link: str) -> datetime:
    """
    Дата публикации из карточки/листинга HTML:
    1) <time datetime> или текст в <time>
    2) мета на странице (article:published_time и т.п.)
    3) классы .date/.published
    4) дата в URL /YYYY/MM/DD/
    5) now()
    """
    # 1) <time>
    time_tag = item_soup.find("time")
    if time_tag:
        if time_tag.has_attr("datetime"):
            dt = _try_parse_date_text(time_tag["datetime"])
            if dt:
                return dt
        dt = _try_parse_date_text(time_tag.get_text(" ", strip=True))
        if dt:
            return dt

    # 2) meta на листинге
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
        if m and m.has_attr("content"):
            dt = _try_parse_date_text(m["content"])
            if dt:
                return dt

    # 3) Общие классы
    cand = item_soup.select_one(
        ".date, .post-date, .published, .posted-on, .entry-date, [class*='date'], [class*='time']"
    )
    if cand:
        dt = _try_parse_date_text(cand.get_text(" ", strip=True))
        if dt:
            return dt

    # 4) Дата в URL
    if link:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)", link)
        if m:
            try:
                y, mth, d = map(int, m.groups())
                return datetime(y, mth, d)
            except Exception:
                pass

    # 5) Фолбэк
    return datetime.now()


async def extract_date_from_article_page(article_url: str) -> datetime:
    """Уточняем дату публикации, сходив на страницу статьи (meta/time/JSON-LD/паттерны)."""
    try:
        response = requests.get(article_url, timeout=12, headers=DEFAULT_HEADERS)
        if response.status_code != 200:
            return datetime.now()

        soup = BeautifulSoup(response.content, "html.parser")

        # 1) meta
        meta_selectors = [
            'meta[property="article:published_time"]',
            'meta[name="article:published_time"]',
            'meta[property="og:published_time"]',
            'meta[name="publish_date"]',
            'meta[name="publication_date"]',
            'meta[name="date"]',
            'meta[name="created"]',
        ]
        for selector in meta_selectors:
            m = soup.select_one(selector)
            if m and m.get("content"):
                try:
                    return date_parser.parse(m["content"], ignoretz=True)
                except Exception:
                    pass

        # 2) <time>
        t = soup.select_one("time[datetime]")
        if t:
            try:
                return date_parser.parse(t["datetime"], ignoretz=True)
            except Exception:
                pass
        t2 = soup.find("time")
        if t2:
            try:
                return date_parser.parse(t2.get_text(" ", strip=True), ignoretz=True)
            except Exception:
                pass

        # 3) JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = script.string or script.text
                if not data:
                    continue
                obj = json.loads(data)
                candidates = obj if isinstance(obj, list) else [obj]
                for c in list(candidates):
                    if not isinstance(c, dict):
                        continue
                    graph = c.get("@graph")
                    if isinstance(graph, list):
                        candidates.extend([g for g in graph if isinstance(g, dict)])
                    for k in ("datePublished", "dateCreated", "uploadDate"):
                        if k in c and isinstance(c[k], str):
                            try:
                                return date_parser.parse(c[k], ignoretz=True)
                            except Exception:
                                continue
            except Exception:
                continue

        # 4) Паттерны
        text = soup.get_text(" ", strip=True)
        patterns = [
            r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}\b",
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{1,2}/\d{1,2}/\d{4}\b",
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


def parse_publication_date(entry) -> datetime:
    """Дата для RSS/Atom записи."""
    date_fields = ["published_parsed", "updated_parsed", "created_parsed"]
    for field in date_fields:
        if hasattr(entry, field) and getattr(entry, field):
            try:
                parsed_time = getattr(entry, field)
                return datetime(*parsed_time[:6])
            except Exception:
                continue

    string_fields = ["published", "updated", "created", "pubDate"]
    for field in string_fields:
        if hasattr(entry, field) and getattr(entry, field):
            try:
                date_str = getattr(entry, field)
                return date_parser.parse(date_str, ignoretz=True)
            except Exception:
                continue

    return datetime.now()


# ------------------ Pagination helper ------------------
async def _fetch_list_pages_with_pagination(start_url: str, max_pages: int = 3) -> List[BeautifulSoup]:
    """
    Загружаем страницу списка + до max_pages-1 следующих:
    - <link rel="next">
    - <a rel="next">
    - эвристики /page/2 или ?page=2
    """
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
            soup = BeautifulSoup(r.content, "html.parser")
            soups.append(soup)

            # пытаемся найти next
            next_link: Optional[str] = None
            link_tag = soup.find("link", rel=lambda v: v and "next" in v.lower())
            if link_tag and link_tag.get("href"):
                next_link = urljoin(current, link_tag["href"])

            if not next_link:
                a_next = soup.find("a", rel=lambda v: v and "next" in v.lower())
                if a_next and a_next.get("href"):
                    next_link = urljoin(current, a_next["href"])

            if not next_link:
                # эвристики: /page/2, /page/3
                m = re.search(r"(.*?/page/)(\d+)/?$", current)
                if m:
                    base, num = m.group(1), int(m.group(2))
                    next_link = base + str(num + 1)
                else:
                    # ?page=2
                    if "?" in current:
                        if re.search(r"([?&])page=(\d+)", current):
                            next_link = re.sub(
                                r"([?&])page=(\d+)",
                                lambda m: f"{m.group(1)}page={int(m.group(2)) + 1}",
                                current,
                            )
                        else:
                            next_link = current + "&page=2"
                    else:
                        next_link = current.rstrip("/") + "/page/2"

            # защита: не уходим на другой домен
            if urlparse(next_link).netloc and (
                get_domain_from_url(next_link) != get_domain_from_url(start_url)
            ):
                break

            current = next_link
        except Exception as e:
            logger.debug(f"Pagination fetch error: {e}")
            break

    return soups


# ------------------ Domain fragments ------------------
def _fragments_for_domain(domain: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    rules: Dict[str, Dict[str, Tuple[str, ...]]] = DOMAIN_RULES.get(domain, {})
    allow = rules.get("allow", DEFAULT_ALLOWED)
    ban = rules.get("ban", DEFAULT_BANNED)
    return allow, ban


# ------------------ Fetch items ------------------
async def fetch_items(feed_url: str, feed_type: str) -> List[Dict]:
    """
    Сбор новостей:
    - RSS/Atom: парсинг через feedparser + категоризация
    - HTML: ищем карточки и/или все ссылки разделов блога; дату уточняем со страницы
    """
    items: List[Dict] = []

    try:
        if feed_type in ["rss", "atom"]:
            response = requests.get(feed_url, timeout=15, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                pub_date = parse_publication_date(entry)

                # Теги/summary для лучшей категоризации
                rss_tags: List[str] = []
                if hasattr(entry, "tags") and entry.tags:
                    # feedparser хранит либо списком объектов, либо dict-like
                    for t in entry.tags:
                        term = (t.get("term") if isinstance(t, dict) else getattr(t, "term", None))
                        if term:
                            rss_tags.append(str(term))
                summary = entry.get("summary", "") or entry.get("description", "")

                category = classify_news(
                    entry.get("title", ""),
                    entry.get("link", ""),
                    rss_tags,
                    summary,
                )

                item_id = entry.get("id", entry.get("link", "")) or f"{entry.get('title', '')}_{pub_date.isoformat()}"
                items.append(
                    {
                        "id": item_id,
                        "title": entry.get("title", "No title"),
                        "link": entry.get("link", ""),
                        "date": pub_date,
                        "category": category,
                    }
                )

        elif feed_type == "html":
            soups = await _fetch_list_pages_with_pagination(feed_url, max_pages=3)
            allowed_fragments, banned_fragments = _fragments_for_domain(get_domain_from_url(feed_url))

            found_items: List[Tuple[BeautifulSoup, BeautifulSoup]] = []

            # 1) Ищем «карточки»
            for soup in soups:
                selectors = [
                    "article",
                    ".blog-post",
                    ".post",
                    ".news-item",
                    ".article-item",
                    ".entry",
                    ".post-item",
                    ".blog-item",
                    ".news-post",
                    ".article-card",
                    "[class*='post-']",
                    "[class*='blog-']",
                    "[class*='article-']",
                    "[class*='news-']",
                    "[class*='entry-']",
                ]
                for selector in selectors:
                    try:
                        elements = soup.select(selector)
                        valid_articles: List[Tuple[BeautifulSoup, BeautifulSoup]] = []
                        for elem in elements:
                            title_elem = elem.select_one(
                                "h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']"
                            )
                            link_elem = elem.find("a", href=True)
                            if not (title_elem and link_elem):
                                continue
                            title_text = title_elem.get_text().strip()
                            link_href = link_elem.get("href", "")
                            full = urljoin(feed_url, link_href)
                            path_lower = urlparse(full).path.lower()
                            if (
                                len(title_text) > 8
                                and any(path_lower == af or path_lower.startswith(af + "/") for af in allowed_fragments)
                                and not any(path_lower == bf or path_lower.startswith(bf + "/") for bf in banned_fragments)
                                and not any(sk in link_href.lower() for sk in ["mailto:", "tel:", "#", "javascript:"])
                            ):
                                valid_articles.append((elem, soup))
                        if len(valid_articles) >= 2:
                            found_items.extend(valid_articles)
                            break
                    except Exception as e:
                        logger.debug(f"Selector error {selector}: {e}")
                        continue

            # 2) Если карточек мало — «план C»: собрать все ссылки /blog/... и т.п.
            if len(found_items) < 3:
                seen_links = set()
                for soup in soups:
                    for a in soup.select("a[href]"):
                        href = a.get("href", "")
                        full = urljoin(feed_url, href)
                        if full in seen_links:
                            continue
                        seen_links.add(full)
                        path_lower = urlparse(full).path.lower()
                        if (
                            any(path_lower == af or path_lower.startswith(af + "/") for af in allowed_fragments)
                            and not any(path_lower == bf or path_lower.startswith(bf + "/") for bf in banned_fragments)
                            and not any(sk in href.lower() for sk in ["mailto:", "tel:", "#", "javascript:"])
                        ):
                            # псевдокарточка: ближайший контейнер
                            container: BeautifulSoup = a
                            for _ in range(3):
                                if container.parent:
                                    container = container.parent
                            found_items.append((container, soup))

            logger.info(f"Processing {len(found_items)} potential articles from HTML pages")

            # 3) Формируем items (дату уточняем со страницы)
            added = set()
            for item_node, page_soup in found_items:
                try:
                    a = item_node.find("a", href=True)
                    if not a:
                        continue
                    link = urljoin(feed_url, a["href"])
                    if link in added:
                        continue
                    if "mailto:" in link or "tel:" in link or link.endswith("#"):
                        continue

                    title_elem = item_node.select_one(
                        "h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']"
                    )
                    title = ""
                    if title_elem:
                        title = title_elem.get_text(" ", strip=True)
                    if not title:
                        title = a.get_text(" ", strip=True)
                    title = re.sub(r"\s+", " ", title).strip()
                    if len(title) < 5:
                        continue

                    # Предварительная дата + точное уточнение
                    pub_date = parse_pub_date(item_node, page_soup, link)
                    precise = await extract_date_from_article_page(link)
                    if precise:
                        pub_date = precise

                    category = classify_news(title, link)

                    items.append(
                        {
                            "id": link,
                            "title": title[:200],
                            "link": link,
                            "date": pub_date,
                            "category": category,
                        }
                    )
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


__all__ = [
    "get_domain_from_url",
    "normalize_url",
    "discover_feed",
    "verify_feed",
    "_try_parse_date_text",
    "parse_pub_date",
    "extract_date_from_article_page",
    "parse_publication_date",
    "_fetch_list_pages_with_pagination",
    "fetch_items",
    "_fragments_for_domain",
]

