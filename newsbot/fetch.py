# newsbot/fetch.py
import re
import json
import requests
import feedparser
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from newsbot.config import (
    DEFAULT_HEADERS,
    DOMAIN_RULES,
    DEFAULT_ALLOWED,
    DEFAULT_BANNED,
)
from newsbot.classify import classify_news


# ================ utils: url/domain =================
def get_domain_from_url(u: str) -> str:
    netloc = urlparse(u).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> Tuple[str, str]:
    """
    Нормализуем URL и выделяем домен.
    Если путь указывает на новостной/блоговый/ивентный раздел — оставляем этот раздел.
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
        "blog", "news", "press", "newsroom", "articles", "media",
        "stories", "updates",
        # добавили ивентные корни
        "events", "event", "calendar", "webinars", "talks",
    )
    if path and path.split("/")[0].lower() in news_roots:
        normalized_url = f"{root}/{path.split('/')[0]}"
    else:
        normalized_url = root

    return normalized_url, host


# ================ feed discovery ====================
async def discover_feed(base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Сначала ищем RSS/Atom через <link rel="alternate"> и типовые пути.
    Если нет — пытаемся найти валидный HTML-листинг (включая ивенты).
    """
    try:
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        root = f"{scheme}://{host}"

        # Поддомены/базы
        subdomains = [
            "blog", "news", "press", "newsroom", "media", "stories", "source",
            "about", "company", "corporate", "updates",
            # ивенты
            "events",
        ]
        candidate_bases = [root] + [f"{scheme}://{sd}.{host}" for sd in subdomains]

        # 1) link rel="alternate"
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

        # 2) типовые RSS пути (+events)
        common_paths = [
            "/blog/feed", "/blog/rss", "/blog/feed.xml", "/blog/atom.xml", "/blog/index.xml",
            "/news/feed", "/news/rss", "/news/feed.xml", "/news/atom.xml", "/news/index.xml",
            "/press/feed", "/press/rss",
            "/events/feed", "/events/rss", "/events/index.xml",
            "/rss", "/rss.xml", "/atom.xml", "/feed", "/feed.xml", "/index.xml",
        ]
        for base in candidate_bases:
            for path in common_paths:
                fu = urljoin(base, path)
                if await verify_feed(fu):
                    return fu, "rss"

        # 3) HTML-листинги (включая ивенты)
        news_paths = [
            "/blog", "/news", "/press", "/press-releases", "/newsroom",
            "/articles", "/stories", "/story", "/updates",
            # ивенты
            "/events", "/event", "/calendar", "/webinars", "/talks",
        ]
        banned_paths = list(DEFAULT_BANNED)

        def looks_like_news_url(u: str) -> bool:
            up = urlparse(u)
            path = up.path.lower()
            if any(path == bp or path.startswith(bp + "/") for bp in banned_paths):
                return False
            return any(path == np or path.startswith(np + "/") for np in news_paths)

        # Сначала /blog и /events
        ordered_candidates = []
        for base in candidate_bases:
            for first in ("/blog", "/events"):
                u = urljoin(base, first)
                if u not in ordered_candidates:
                    ordered_candidates.append(u)
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
                    # обычные блоговые карточки
                    "article, .post, .blog-post, .news-item, [class*='post'], [class*='article'], [class*='blog'],"
                    # карточки ивентов
                    ".event, .event-card, .events-list__item, [itemscope][itemtype*='Event'], [class*='event-']"
                )
                has_titles = 0
                for el in candidates[:30]:
                    t = el.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                    a = el.find("a", href=True)
                    if t and a and len(t.get_text(strip=True)) > 10:
                        has_titles += 1
                        if has_titles >= 2:
                            return news_url, "html"
            except Exception:
                continue

        # 4) Фолбек: корень
        try:
            r = requests.get(root, timeout=10, headers=DEFAULT_HEADERS)
            if r.status_code == 200:
                return root, "html"
        except Exception:
            pass

    except Exception:
        pass

    return None, None


async def verify_feed(feed_url: str) -> bool:
    try:
        response = requests.get(feed_url, timeout=10, headers=DEFAULT_HEADERS)
        if response.status_code != 200:
            return False
        feed = feedparser.parse(response.content)
        return len(feed.entries) > 0 and not feed.bozo
    except Exception:
        return False


# ================ date helpers ======================
def _try_parse_date_text(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    # иногда встречаются диапазоны дат "2025-09-01 – 2025-09-02"
    text = re.split(r"\s*[–—-]\s*", text)[0]
    try:
        return date_parser.parse(text, fuzzy=True, ignoretz=True)
    except Exception:
        return None


def parse_pub_date(item_soup: BeautifulSoup, page_soup: BeautifulSoup, link: str) -> datetime:
    """
    Дата из карточки листинга:
    1) <time datetime> / <time>text</time>
    2) itemprop="startDate" (ивенты), data-* с датой
    3) мета на странице листинга
    4) классы .date / .time
    5) дата в URL
    """
    # 1) time
    time_tag = item_soup.find("time")
    if time_tag:
        # itemprop="startDate" для Event
        if time_tag.has_attr("itemprop") and time_tag.get("itemprop") == "startDate":
            dt = _try_parse_date_text(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
            if dt:
                return dt
        # обычный datetime
        if time_tag.has_attr("datetime"):
            dt = _try_parse_date_text(time_tag["datetime"])
            if dt:
                return dt
        dt = _try_parse_date_text(time_tag.get_text(" ", strip=True))
        if dt:
            return dt

    # 2) data-* на самих карточках (часто у календарей)
    date_like = item_soup.get("data-date") or item_soup.get("data-datetime") or item_soup.get("content")
    if date_like:
        dt = _try_parse_date_text(date_like)
        if dt:
            return dt
    # также попробуем itemprop=startDate где-то внутри
    start_prop = item_soup.find(attrs={"itemprop": "startDate"})
    if start_prop:
        dt = _try_parse_date_text(start_prop.get("content") or start_prop.get("datetime") or start_prop.get_text(" ", strip=True))
        if dt:
            return dt

    # 3) мета на странице листинга
    meta_names = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"itemprop": "datePublished"}),
        ("meta", {"property": "og:updated_time"}),
        ("meta", {"name": "pubdate"}),
        ("meta", {"name": "publishdate"}),
        ("meta", {"name": "date"}),
        # ивенты иногда используют startDate в JSON-LD/мета
        ("meta", {"itemprop": "startDate"}),
    ]
    for tag, attrs in meta_names:
        m = page_soup.find(tag, attrs=attrs)
        if m and m.has_attr("content"):
            dt = _try_parse_date_text(m["content"])
            if dt:
                return dt

    # 4) общие классы на карточке
    cand = item_soup.select_one(".date, .post-date, .published, .posted-on, .entry-date, [class*='date'], [class*='time']")
    if cand:
        dt = _try_parse_date_text(cand.get_text(" ", strip=True))
        if dt:
            return dt

    # 5) дата в URL
    if link:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)", link)
        if m:
            try:
                y, mth, d = map(int, m.groups())
                return datetime(y, mth, d)
            except Exception:
                pass

    return datetime.now()


async def extract_date_from_article_page(article_url: str) -> datetime:
    """
    Заходим на страницу статьи/события и ищем:
    - meta (article:published_time и т.п.)
    - <time datetime>
    - JSON-LD (включая Event.startDate)
    - текстовые паттерны
    """
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
            # иногда встречается itemprop
            'meta[itemprop="datePublished"]',
            'meta[itemprop="startDate"]',
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

        # 3) JSON-LD (в т.ч. Event.startDate)
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = script.string or script.text
                if not data:
                    continue
                obj = json.loads(data)
                candidates = obj if isinstance(obj, list) else [obj]
                i = 0
                while i < len(candidates):
                    c = candidates[i]
                    i += 1
                    if not isinstance(c, dict):
                        continue
                    graph = c.get("@graph")
                    if isinstance(graph, list):
                        candidates.extend([g for g in graph if isinstance(g, dict)])

                    for k in ("datePublished", "dateCreated", "uploadDate", "startDate"):
                        if k in c and isinstance(c[k], str):
                            try:
                                return date_parser.parse(c[k], ignoretz=True)
                            except Exception:
                                continue
            except Exception:
                continue

        # 4) текстовые паттерны
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

    except Exception:
        pass

    return datetime.now()


def parse_publication_date(entry) -> datetime:
    """
    Для RSS / Atom: published_parsed / updated_parsed / created_parsed
    или строковые published/updated/created/pubDate.
    """
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


# ================ pagination =========================
async def _fetch_list_pages_with_pagination(start_url: str, max_pages: int = 3) -> List[BeautifulSoup]:
    """
    Загружаем страницу списка + до max_pages-1 следующих:
    - link rel="next"
    - a[rel*=next]
    - /page/N
    - ?page=N
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

            # найдём next
            next_link = None
            link_tag = soup.find("link", rel=lambda v: v and "next" in v.lower())
            if link_tag and link_tag.get("href"):
                next_link = urljoin(current, link_tag["href"])

            if not next_link:
                a_next = soup.find("a", rel=lambda v: v and "next" in v.lower())
                if a_next and a_next.get("href"):
                    next_link = urljoin(current, a_next["href"])

            if not next_link:
                # /page/N
                m = re.search(r"(.*?/page/)(\d+)/?$", current)
                if m:
                    base, num = m.group(1), int(m.group(2))
                    next_link = base + str(num + 1)
                else:
                    # ?page=N
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

            # не уходим на другой домен
            if urlparse(next_link).netloc and (get_domain_from_url(next_link) != get_domain_from_url(start_url)):
                break

            current = next_link
        except Exception:
            break

    return soups


# ================ fragments per domain ==============
def _fragments_for_domain(domain: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    rules = DOMAIN_RULES.get(domain, {})
    allow = rules.get("allow", DEFAULT_ALLOWED)
    ban = rules.get("ban", DEFAULT_BANNED)
    return allow, ban


# ================ main: fetch items =================
async def fetch_items(feed_url: str, feed_type: str) -> List[Dict]:
    """
    Унифицированный сбор элементов из RSS/Atom или HTML-листингов (в т.ч. ивенты).
    Каждый элемент: {id, title, link, date, category}
    """
    items: List[Dict] = []

    try:
        if feed_type in ["rss", "atom"]:
            response = requests.get(feed_url, timeout=15, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                pub_date = parse_publication_date(entry)

                # категории/summary для лучшей классификации
                rss_tags: List[str] = []
                if hasattr(entry, "tags") and entry.tags:
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
                items.append({
                    "id": item_id,
                    "title": entry.get("title", "No title"),
                    "link": entry.get("link", ""),
                    "date": pub_date,
                    "category": category,
                })

        elif feed_type == "html":
            soups = await _fetch_list_pages_with_pagination(feed_url, max_pages=3)
            allowed_fragments, banned_fragments = _fragments_for_domain(get_domain_from_url(feed_url))

            found_items: List[Tuple[BeautifulSoup, BeautifulSoup]] = []

            # 1) блоговые + ивентные карточки
            for soup in soups:
                selectors = [
                    # блог/новости
                    "article",
                    ".blog-post", ".post", ".news-item", ".article-item", ".entry",
                    ".post-item", ".blog-item", ".news-post", ".article-card",
                    "[class*='post-']", "[class*='blog-']", "[class*='article-']",
                    "[class*='news-']", "[class*='entry-']",
                    # ивенты
                    ".event", ".event-card", ".events-list__item",
                    "[itemscope][itemtype*='Event']",
                    "[class*='event-']",
                ]
                for selector in selectors:
                    try:
                        elements = soup.select(selector)
                        valid_articles = []
                        for elem in elements:
                            title_elem = elem.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                            link_elem = elem.find("a", href=True)
                            if not (title_elem and link_elem):
                                continue
                            title_text = title_elem.get_text().strip()
                            link_href = link_elem.get("href", "")
                            full = urljoin(feed_url, link_href)
                            path_lower = urlparse(full).path.lower()
                            if (
                                len(title_text) > 6
                                and any(path_lower == af or path_lower.startswith(af + "/") for af in allowed_fragments)
                                and not any(path_lower == bf or path_lower.startswith(bf + "/") for bf in banned_fragments)
                                and not any(sk in link_href.lower() for sk in ["mailto:", "tel:", "#", "javascript:"])
                            ):
                                valid_articles.append((elem, soup))
                        if len(valid_articles) >= 2:
                            found_items.extend(valid_articles)
                            break
                    except Exception:
                        continue

            # 2) если карточек мало — соберём по всем ссылкам разрешённых разделов
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
                            container = a
                            for _ in range(3):
                                if container.parent:
                                    container = container.parent
                            found_items.append((container, soup))

            # 3) финальное формирование items (уточняем дату со страницы)
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

                    # заголовок
                    title_elem = item_node.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                    title = title_elem.get_text(" ", strip=True) if title_elem else a.get_text(" ", strip=True)
                    title = re.sub(r"\s+", " ", (title or "")).strip()
                    if len(title) < 3:
                        continue

                    # дата: сначала с карточки, затем — со страницы
                    pub_date = parse_pub_date(item_node, page_soup, link)
                    precise = await extract_date_from_article_page(link)
                    if precise:
                        pub_date = precise

                    category = classify_news(title, link)

                    items.append({
                        "id": link,
                        "title": title[:200],
                        "link": link,
                        "date": pub_date,
                        "category": category,
                    })
                    added.add(link)

                    if len(items) >= 80:
                        break
                except Exception:
                    continue

    except Exception:
        pass

    return items
