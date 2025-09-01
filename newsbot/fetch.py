# newsbot/fetch.py

import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime

HEADERS = {"User-Agent": "Mozilla/5.0 (news-monitor-bot)"}


# ------------------- URL Normalization -------------------
def normalize_url(url: str):
    """
    Нормализует URL и возвращает (url, domain).
    Пример: "microsoft.com" -> ("https://microsoft.com", "microsoft.com")
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    if domain.startswith("www."):
        domain = domain[4:]

    normalized = f"{parsed.scheme}://{parsed.netloc}"
    return normalized, domain


# ------------------- Feed discovery -------------------
async def discover_feed(url: str):
    """
    Пробуем определить тип источника:
    1) RSS/Atom (feedparser поймает <rss> / <feed>)
    2) HTML (если обычная страница)
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        text = resp.text

        # feedparser попробует RSS/Atom
        parsed = feedparser.parse(text)
        if parsed.bozo == 0 and parsed.entries:
            return url, "rss" if "rss" in text.lower() else "atom"

        # иначе — HTML
        return url, "html"

    except Exception:
        return None, None


# ------------------- HTML parser -------------------
def _extract_articles_generic(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    # кандидаты для поиска новостей
    candidates = []
    candidates += soup.select("article")
    candidates += soup.select("[class*=news], [class*=post], [class*=card]")
    candidates += soup.select("ul li a, .cards a, .list a")

    seen = set()
    items = []
    for a in candidates:
        link_tag = a if a.name == "a" else a.find("a", href=True)
        if not link_tag or not link_tag.get("href"):
            continue
        link = urljoin(base_url, link_tag.get("href"))
        if link in seen:
            continue
        seen.add(link)

        title = (link_tag.get_text(strip=True)
                 or (a.get_text(strip=True) if a else ""))[:300]
        if not title:
            continue

        # дата (если есть <time>)
        date_text = ""
        time_tag = a.find("time")
        if time_tag and (time_tag.get("datetime") or time_tag.get_text(strip=True)):
            date_text = time_tag.get("datetime") or time_tag.get_text(strip=True)

        try:
            dt = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
        except Exception:
            dt = datetime.now()

        items.append({
            "id": link,
            "title": title,
            "link": link,
            "date": dt,
            "category": "other",
        })

        if len(items) >= 30:
            break

    return items


# ------------------- Fetch items -------------------
async def fetch_items(url, feed_type: str):
    """
    Загружаем новости:
    - RSS/Atom через feedparser
    - HTML fallback через BeautifulSoup
    """
    if feed_type in ("rss", "atom"):
        parsed = feedparser.parse(url)
        items = []
        for entry in parsed.entries:
            try:
                dt = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        dt = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass
                if not dt:
                    dt = datetime.now()

                items.append({
                    "id": entry.get("id") or entry.get("link"),
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "date": dt,
                    "category": "other",
                })
            except Exception:
                continue
        return items

    # HTML fallback
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return _extract_articles_generic(resp.text, url)
