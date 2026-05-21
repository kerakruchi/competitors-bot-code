# newsbot/fetch.py
from __future__ import annotations

import asyncio
import json
import logging
import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from .config import DOMAIN_RULES, DEFAULT_ALLOWED, DEFAULT_BANNED, BYPASS_FILTER_DOMAINS

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (news-monitor-bot)"}
TIMEOUT = 15

try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


# ------------------- URL Normalization -------------------

def normalize_url(url: str):
    """
    Нормализует URL и возвращает (normalized_base_url, domain).
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


# ------------------- Path filtering -------------------

def _is_allowed_path(link: str, domain: str) -> bool:
    """Проверяет, разрешён ли путь URL по правилам домена."""
    if domain in BYPASS_FILTER_DOMAINS:
        return True

    path = urlparse(link).path.lower()
    rules = DOMAIN_RULES.get(domain)

    if rules:
        allowed = rules.get("allow", ())
        banned = rules.get("ban", ())
    else:
        allowed = DEFAULT_ALLOWED
        banned = DEFAULT_BANNED

    for ban in banned:
        if path.startswith(ban):
            return False

    if allowed:
        return any(path.startswith(a) for a in allowed)

    return True


# ------------------- HTTP with retry -------------------

def _get_with_retry(url: str, retries: int = 3) -> requests.Response:
    """GET-запрос с экспоненциальным retry (без tenacity для простоты)."""
    import time
    last_exc = None
    for attempt in range(retries):
        try:
            return requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise last_exc


# ------------------- Playwright fetch -------------------

async def _fetch_html_playwright(url: str) -> str | None:
    """Загружает страницу через headless Chromium (для JS-сайтов)."""
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(user_agent=HEADERS["User-Agent"])
            await page.goto(url, timeout=30_000, wait_until="networkidle")
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        logger.debug(f"Playwright fetch failed for {url}: {e}")
        return None


# ------------------- Feed discovery -------------------

async def discover_feed(url: str):
    """
    Определяет тип источника. Приоритет:
    1) LinkedIn → ('linkedin')
    2) Прямой RSS/Atom по переданному URL
    3) RSS/Atom на типовых путях (/blog, /news, /press ...)
    4) HTML на типовых путях (предпочтительнее корня сайта)
    5) Фоллбэк → корень сайта как HTML
    """
    parsed_url = urlparse(url)

    # LinkedIn
    if "linkedin.com" in parsed_url.netloc:
        return url, "linkedin"

    COMMON_PATHS = [
        "news", "blog", "press", "press-center", "presscentre",
        "media", "novosti", "newsroom", "articles", "updates",
    ]

    base = url if url.endswith("/") else url + "/"

    # Шаг 1: прямой RSS/Atom по переданному URL
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo == 0 and parsed.entries:
            return url, "rss"
    except Exception:
        pass

    # Шаг 2: RSS/Atom на типовых путях
    for path in COMMON_PATHS:
        candidate = urljoin(base, path)
        try:
            p = feedparser.parse(candidate)
            if p.bozo == 0 and p.entries:
                return candidate, "rss"
        except Exception:
            pass

    # Шаг 3: HTML на типовых путях (блог/пресс лучше главной страницы)
    for path in COMMON_PATHS:
        candidate = urljoin(base, path)
        try:
            r = _get_with_retry(candidate)
            if r.ok:
                return candidate, "html"
        except Exception:
            pass

    # Шаг 4: фоллбэк — корень как HTML
    return url, "html"


# ------------------- Title cleanup -------------------

_TITLE_STRIP_PREFIXES = ("blog", "news", "press", "article", "post", "read more", "learn more")
_TITLE_STRIP_SUFFIXES = ("learn more", "read more", "read article", "view more", "→", "»")

def _clean_title(text: str) -> str:
    """Убирает навигационный мусор из заголовков (Blog, Learn More и т.п.)"""
    t = text.strip()
    low = t.lower()
    for prefix in _TITLE_STRIP_PREFIXES:
        if low.startswith(prefix) and len(t) > len(prefix) + 2:
            t = t[len(prefix):].lstrip(" :-–—|/")
            low = t.lower()
    for suffix in _TITLE_STRIP_SUFFIXES:
        if low.endswith(suffix):
            t = t[: -len(suffix)].rstrip(" :-–—|/")
            low = t.lower()
    return t.strip()


# ------------------- HTML parser -------------------

def _extract_articles_generic(html: str, base_url: str, domain: str = ""):
    """
    Универсальный HTML-парсер для корпоративных сайтов.
    Применяет фильтрацию путей по DOMAIN_RULES.
    """
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    candidates += soup.select("article")
    candidates += soup.select("[class*=news], [class*=post], [class*=card], [class*=item]")
    candidates += soup.select("ul li a, .cards a, .list a, .news-list a")

    seen = set()
    items = []
    for node in candidates:
        a = node if node.name == "a" else node.find("a", href=True)
        if not a or not a.get("href"):
            continue

        link = urljoin(base_url, a.get("href"))
        if link in seen:
            continue

        # Фильтрация по разрешённым/запрещённым путям
        if domain and not _is_allowed_path(link, domain):
            continue

        seen.add(link)

        raw_title = (a.get_text(strip=True) or node.get_text(strip=True))[:300]
        title = _clean_title(raw_title)
        if not title:
            continue

        dt = None
        time_tag = node.find("time")
        if time_tag:
            date_text = time_tag.get("datetime") or time_tag.get_text(strip=True)
            if date_text:
                try:
                    dt = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
                except Exception:
                    dt = None

        if dt is None:
            dt = datetime(2000, 1, 1)  # фоллбэк — уйдёт вниз при сортировке

        items.append({
            "id": link,
            "title": title,
            "link": link,
            "date": dt,
            "category": "other",
        })

        if len(items) >= 30:
            break

    # Помечаем элементы без реальной даты
    for it in items:
        if it.get("_date_fallback"):
            pass
        it["_date_fallback"] = True  # все HTML-даты — фоллбэк, уточним ниже

    return items


def _fetch_jsonld_date(url: str) -> datetime | None:
    """Синхронно получает дату публикации из JSON-LD на странице статьи."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                for key in ("datePublished", "dateCreated", "uploadDate"):
                    val = data.get(key)
                    if val:
                        return datetime.fromisoformat(
                            val.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
            except Exception:
                pass
    except Exception:
        pass
    return None


async def _enrich_dates(items: list[dict], limit: int = 30):
    """Параллельно подтягивает реальные даты для HTML-статей (до limit штук)."""
    to_enrich = [it for it in items if it.get("_date_fallback")][:limit]
    if not to_enrich:
        return

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=limit) as pool:
        futures = [
            loop.run_in_executor(pool, _fetch_jsonld_date, it["link"])
            for it in to_enrich
        ]
        results = await asyncio.gather(*futures, return_exceptions=True)

    for it, dt in zip(to_enrich, results):
        if isinstance(dt, datetime):
            it["date"] = dt
        it.pop("_date_fallback", None)


# ------------------- Fetch items -------------------

async def fetch_items(url: str, feed_type: str):
    """
    Загружает список новостей.
    Поддерживает: rss/atom, html (с Playwright-фоллбэком), linkedin.
    Возвращает список {id, title, link, date, category}.
    """
    if feed_type == "linkedin":
        from .linkedin import fetch_linkedin_posts
        return await fetch_linkedin_posts(url)

    if feed_type in ("rss", "atom"):
        parsed = feedparser.parse(url)
        items = []
        for entry in parsed.entries:
            try:
                dt = None
                if getattr(entry, "published_parsed", None):
                    try:
                        dt = datetime(*entry.published_parsed[:6])
                    except Exception:
                        dt = None
                if dt is None and getattr(entry, "updated_parsed", None):
                    try:
                        dt = datetime(*entry.updated_parsed[:6])
                    except Exception:
                        dt = None
                if dt is None:
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
    parsed_url = urlparse(url)
    domain = parsed_url.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    html = None
    try:
        resp = _get_with_retry(url)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"HTTP fetch failed for {url}: {e} — trying Playwright...")
        html = await _fetch_html_playwright(url)
        if not html:
            raise

    items = _extract_articles_generic(html, url, domain)
    await _enrich_dates(items)
    return items
