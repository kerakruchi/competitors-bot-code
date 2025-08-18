# newsbot/fetch.py
import re
import json
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

from newsbot.config import (
    DEFAULT_HEADERS,
    DOMAIN_RULES,
    DEFAULT_ALLOWED,
    DEFAULT_BANNED,
    BYPASS_FILTER_DOMAINS,
)
from newsbot.classify import classify_news


# ================== URL helpers ==================
def get_domain_from_url(u: str) -> str:
    netloc = urlparse(u).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_url(url: str) -> Tuple[str, str]:
    """Вернёт (normalized_url, domain). Если дан путь вроде /blog — оставим только корневой раздел."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    root = f"{parsed.scheme}://{host}"
    path = (parsed.path or "").strip("/")
    news_roots = ("blog", "news", "press", "newsroom", "articles", "media", "stories", "updates", "events")
    if path and path.split("/")[0].lower() in news_roots:
        normalized_url = f"{root}/{path.split('/')[0]}"
    else:
        normalized_url = root

    return normalized_url, host


# ================== Feed discovery ==================
async def discover_feed(base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Сначала ищем RSS/Atom (<link rel="alternate">, типичные пути). Если не нашли — возвращаем
    валидную HTML-страницу раздела новостей/блога; в крайнем случае — корень сайта.
    """
    try:
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.netloc
        root = f"{scheme}://{host}"

        subdomains = [
            "blog", "news", "press", "newsroom", "media", "stories",
            "about", "company", "corporate", "updates",
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
                    if any(t in type_attr for t in ("rss", "atom", "xml")):
                        feed_url = urljoin(base, link.get("href"))
                        feed_type = "atom" if "atom" in type_attr else "rss"
                        if await verify_feed(feed_url):
                            return feed_url, feed_type
            except Exception:
                continue

        # 2) Типовые RSS пути
        common_paths = [
            "/blog/feed", "/blog/rss", "/blog/feed.xml", "/blog/atom.xml", "/blog/index.xml",
            "/news/feed", "/news/rss", "/news/feed.xml", "/news/atom.xml", "/news/index.xml",
            "/press/feed", "/press/rss",
            "/rss", "/rss.xml", "/atom.xml", "/feed", "/feed.xml", "/index.xml",
        ]
        for base in candidate_bases:
            for path in common_paths:
                fu = urljoin(base, path)
                if await verify_feed(fu):
                    return fu, "rss"

        # 3) HTML разделы новостей/блога
        news_paths = [
            "/blog", "/news", "/press", "/press-releases", "/newsroom",
            "/articles", "/stories", "/story", "/updates", "/events"
        ]
        banned_paths = list(DEFAULT_BANNED)

        def looks_like_news_url(u: str) -> bool:
            up = urlparse(u)
            path = up.path.lower()
            if any(path == bp or path.startswith(bp + "/") for bp in banned_paths):
                return False
            return any(path == np or path.startswith(np + "/") for np in news_paths)

        ordered_candidates = []
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
                    t = el.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                    a = el.find("a", href=True)
                    if t and a and len(t.get_text(strip=True)) > 10:
                        has_titles += 1
                        if has_titles >= 2:
                            return news_url, "html"
            except Exception:
                continue

        # 4) Fallback: корень
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


# ================== Date helpers ==================
def _try_parse_date_text(text: str) -> Optional[datetime]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date_parser.parse(text, fuzzy=True, ignoretz=True)
    except Exception:
        return None


def parse_pub_date(item_soup: BeautifulSoup, page_soup: BeautifulSoup, link: str) -> datetime:
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

    # 3) классы
    cand = item_soup.select_one(".date, .post-date, .published, .posted-on, .entry-date, [class*='date'], [class*='time']")
    if cand:
        dt = _try_parse_date_text(cand.get_text(" ", strip=True))
        if dt:
            return dt

    # 4) дата в URL
    if link:
        m = re.search(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)", link)
        if m:
            try:
                y, mth, d = map(int, m.groups())
                return datetime(y, mth, d)
            except Exception:
                pass

    # 5) now()
    return datetime.now()


async def extract_date_from_article_page(article_url: str) -> datetime:
    """Точное определение даты со страницы статьи: meta/time/JSON-LD/паттерны."""
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
        for sel in meta_selectors:
            m = soup.select_one(sel)
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

        # 3) JSON-LD (часто для Event)
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
                    # Event-ключи
                    for k in ("datePublished", "dateCreated", "uploadDate", "startDate"):
                        if k in c and isinstance(c[k], str):
                            try:
                                return date_parser.parse(c[k], ignoretz=True)
                            except Exception:
                                continue
            except Exception:
                continue

        # 4) Паттерны в тексте
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
    """Парсинг даты из RSS/Atom entry."""
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


# ================== Pagination ==================
async def _fetch_list_pages_with_pagination(start_url: str, max_pages: int = 3) -> List[BeautifulSoup]:
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

            # rel=next
            next_link = None
            link_tag = soup.find("link", rel=lambda v: v and "next" in v.lower())
            if link_tag and link_tag.get("href"):
                next_link = urljoin(current, link_tag["href"])

            if not next_link:
                a_next = soup.find("a", rel=lambda v: v and "next" in v.lower())
                if a_next and a_next.get("href"):
                    next_link = urljoin(current, a_next["href"])

            if not next_link:
                # /page/2
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

            # не уходим на другой домен
            if urlparse(next_link).netloc and (
                get_domain_from_url(next_link) != get_domain_from_url(start_url)
            ):
                break

            current = next_link
        except Exception:
            break

    return soups


# ================== Domain fragments ==================
def _fragments_for_domain(domain: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    rules = DOMAIN_RULES.get(domain, {})
    allow = rules.get("allow", DEFAULT_ALLOWED)
    ban = rules.get("ban", DEFAULT_BANNED)
    return allow, ban


# ====== JSON scanners (для SPA/Next.js и JSON-LD на листинге) ======
def _iter_dicts(obj: Any):
    """Глубокий итератор по всем словарям в JSON-структуре."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_dicts(it)


def _extract_items_from_inline_json(soup: BeautifulSoup, base_url: str) -> List[Dict]:
    """
    Пытаемся достать события/посты из JSON внутри страницы:
    - JSON-LD Event (на листинге)
    - Next.js __NEXT_DATA__
    - Любые объекты, где есть (title|name) + (url|slug) и startDate|datePublished
    """
    items: List[Dict] = []

    # 1) JSON-LD на листинге (Event/Article)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.text
            if not raw:
                continue
            data = json.loads(raw)
            for node in _iter_dicts(data):
                name = node.get("name") or node.get("headline") or node.get("title")
                url = node.get("url")
                start_date = node.get("startDate") or node.get("datePublished") or node.get("dateCreated")
                if name and (url or start_date):
                    link = urljoin(base_url, url) if url else base_url
                    try:
                        dt = date_parser.parse(start_date, ignoretz=True) if start_date else datetime.now()
                    except Exception:
                        dt = datetime.now()
                    cat = classify_news(name, link)
                    items.append({
                        "id": link,
                        "title": str(name)[:200],
                        "link": link,
                        "date": dt,
                        "category": cat,
                    })
        except Exception:
            continue

    # 2) Next.js/любой inline JSON
    possible_scripts = soup.find_all("script")
    for sc in possible_scripts:
        # Ищем __NEXT_DATA__ или достаточно «толстые» JSON
        attr_texts = [
            sc.get("id") or "",
            sc.get("type") or "",
        ]
        text = sc.string or sc.text or ""
        cond_next = (sc.get("id") == "__NEXT_DATA__") or ("__NEXT_DATA__" in text)
        cond_json_like = ("{" in text and "}" in text and len(text) > 2000)
        if not (cond_next or cond_json_like):
            continue
        try:
            # В некоторых случаях там может быть JS, пробуем выдернуть JSON «по скобкам»
            raw = text.strip()
            # Небезошибочно, но часто срабатывает
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                continue
            data = json.loads(raw[start:end+1])
        except Exception:
            continue

        # Ищем узлы с (title|name) + (url|slug) + (startDate|datePublished)
        for node in _iter_dicts(data):
            name = node.get("name") or node.get("title")
            url = node.get("url") or node.get("slug")
            # Иногда url хранится как {"pathname": "/events/..."}
            if isinstance(url, dict):
                url = url.get("pathname") or url.get("path") or url.get("href")
            # Дата
            start_date = node.get("startDate") or node.get("datePublished") or node.get("dateCreated") or node.get("date")

            if name and (url or start_date):
                link = urljoin(base_url, url) if isinstance(url, str) else base_url
                try:
                    dt = date_parser.parse(start_date, ignoretz=True) if isinstance(start_date, str) else datetime.now()
                except Exception:
                    dt = datetime.now()
                cat = classify_news(name, link)
                items.append({
                    "id": link,
                    "title": str(name)[:200],
                    "link": link,
                    "date": dt,
                    "category": cat,
                })

    return items


# ================== Fetch items ==================
async def fetch_items(feed_url: str, feed_type: str) -> List[Dict]:
    items: List[Dict] = []

    try:
        if feed_type in ("rss", "atom"):
            response = requests.get(feed_url, timeout=15, headers=DEFAULT_HEADERS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)

            for entry in feed.entries:
                pub_date = parse_publication_date(entry)

                # Теги/summary — для лучшей категоризации
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
            domain = get_domain_from_url(feed_url)
            bypass = domain in BYPASS_FILTER_DOMAINS
            allowed_fragments, banned_fragments = _fragments_for_domain(domain)

            # БАЗОВЫЕ СТРАНИЦЫ
            soups = await _fetch_list_pages_with_pagination(feed_url, max_pages=3)

            # ДОПОЛНИТЕЛЬНЫЕ ХАБЫ для обходных доменов (например, events.yandex.ru)
            if bypass:
                extra_hubs = ["/events", "/event", "/conf", "/conference"]
                # не дублируем уже загруженное
                seen_html = {soup.decode()[:200] for soup in soups}
                for hub in extra_hubs:
                    try:
                        hub_url = urljoin(feed_url if feed_url.endswith("/") else feed_url + "/", hub.lstrip("/"))
                        r = requests.get(hub_url, timeout=12, headers=DEFAULT_HEADERS)
                        if r.status_code != 200:
                            continue
                        sp = BeautifulSoup(r.content, "html.parser")
                        sig = sp.decode()[:200]
                        if sig not in seen_html:
                            soups.append(sp)
                            seen_html.add(sig)
                    except Exception:
                        continue

            found_items = []

            # 1) Карточки
            for soup in soups:
                selectors = [
                    "article",
                    ".blog-post", ".post", ".news-item", ".article-item", ".entry",
                    ".post-item", ".blog-item", ".news-post", ".article-card",
                    "[class*='post-']", "[class*='blog-']", "[class*='article-']",
                    "[class*='news-']", "[class*='entry-']",
                ]
                local_found = 0
                for selector in selectors:
                    try:
                        elements = soup.select(selector)
                        for elem in elements:
                            t_elem = elem.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                            a_elem = elem.find("a", href=True)
                            if not a_elem:
                                continue

                            link_href = a_elem.get("href", "")
                            full = urljoin(feed_url, link_href)
                            path_lower = urlparse(full).path.lower()

                            # Фильтрация путей (с учётом bypass)
                            cond_allowed = (
                                True if bypass else any(path_lower == af or path_lower.startswith(af + "/") for af in allowed_fragments)
                            )
                            cond_banned = (
                                True if bypass else not any(path_lower == bf or path_lower.startswith(bf + "/") for bf in banned_fragments)
                            )
                            if not (cond_allowed and cond_banned):
                                continue
                            if any(sk in link_href.lower() for sk in ["mailto:", "tel:", "#", "javascript:"]):
                                continue

                            title_text = ""
                            if t_elem:
                                title_text = t_elem.get_text(" ", strip=True)
                            if not title_text:
                                title_text = a_elem.get_text(" ", strip=True)
                            if len(title_text) < 5:
                                continue

                            found_items.append((elem, soup))
                            local_found += 1
                        # если нашли хоть что-то по этому селектору — хватит
                        if local_found >= 2:
                            break
                    except Exception:
                        continue

            # 2) Если карточек мало — brute-force ссылки по домену
            if len(found_items) < 3:
                seen_links = set()
                for soup in soups:
                    for a in soup.select("a[href]"):
                        href = a.get("href", "")
                        full = urljoin(feed_url, href)
                        if full in seen_links:
                            continue
                        seen_links.add(full)

                        u = urlparse(full)
                        if get_domain_from_url(full) != domain:
                            continue

                        path_lower = u.path.lower()

                        # Для bypass доменов — максимально мягкие условия
                        cond_allowed = (
                            True if bypass else any(path_lower == af or path_lower.startswith(af + "/") for af in allowed_fragments)
                        )
                        cond_banned = (
                            True if bypass else not any(path_lower == bf or path_lower.startswith(bf + "/") for bf in banned_fragments)
                        )

                        # Доп. правило для событий: явно разрешаем /events/... и /event/...
                        looks_like_event = path_lower.startswith("/events") or path_lower.startswith("/event")

                        if (cond_allowed and cond_banned) or looks_like_event:
                            if any(sk in href.lower() for sk in ["mailto:", "tel:", "#", "javascript:"]):
                                continue
                            # поднимаемся к контейнеру
                            container = a
                            for _ in range(3):
                                if hasattr(container, "parent") and container.parent:
                                    container = container.parent
                            found_items.append((container, soup))

            # 2a) Пытаемся выдернуть элементы из inline JSON (JSON-LD/Event, __NEXT_DATA__)
            if len(found_items) == 0:
                for soup in soups:
                    inline_items = _extract_items_from_inline_json(soup, feed_url)
                    # inline_items уже полноценные items — сразу добавим
                    items.extend(inline_items)

            # 3) Сформировать items из найденных контейнеров
            added = set()
            for item_node, page_soup in found_items:
                try:
                    a = item_node.find("a", href=True)
                    if not a:
                        continue
                    link = urljoin(feed_url, a["href"])
                    if link in added:
                        continue
                    if any(link.startswith(s) for s in ("mailto:", "tel:")) or link.endswith("#"):
                        continue

                    # Заголовок
                    t_elem = item_node.select_one("h1, h2, h3, h4, h5, .title, [class*='title'], [class*='headline']")
                    title = ""
                    if t_elem:
                        title = t_elem.get_text(" ", strip=True)
                    if not title:
                        title = a.get_text(" ", strip=True)
                    title = re.sub(r"\s+", " ", title).strip()
                    if len(title) < 5:
                        continue

                    # Дата (черновая) + уточнение со страницы
                    pub_date = parse_pub_date(item_node, page_soup, link)
                    precise = await extract_date_from_article_page(link)
                    if precise:
                        pub_date = precise

                    # Категория
                    category = classify_news(title, link)

                    items.append({
                        "id": link,
                        "title": title[:200],
                        "link": link,
                        "date": pub_date,
                        "category": category,
