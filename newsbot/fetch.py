# newsbot/fetch.py

from __future__ import annotations

import requests
import feedparser
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime

HEADERS = {"User-Agent": "Mozilla/5.0 (news-monitor-bot)"}
TIMEOUT = 15


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


# ------------------- Feed discovery -------------------
async def discover_feed(url: str):
    """
    Пытаемся определить тип источника.
    1) Если по URL доступен RSS/Atom — вернём (url, 'rss'/'atom').
    2) Иначе попробуем типовые страницы новостей: /news, /press, /press-center, /media, /novosti.
       - если там есть RSS/Atom — вернём их;
       - иначе вернём (страницу, 'html'), чтобы работал HTML-парсер.
    3) Если ничего не найдено — вернём (url, 'html') как фоллбек.
    """
    # Сначала пробуем как RSS/Atom напрямую
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo == 0 and parsed.entries:
            # Простая эвристика: если в корне есть <rss> — 'rss', иначе 'atom'
            # (feedparser сам нормализует, но нам тип не критичен)
            return url, "rss"
    except Exception:
        pass

    # Если не получилось — проверим саму страницу плюс типовые пути
    COMMON_PATHS = ["news", "press", "press-center", "presscentre", "media", "novosti"]

    def _check_candidate(candidate_url: str):
        # Сначала пробуем как RSS/Atom
        try:
            p = feedparser.parse(candidate_url)
            if p.bozo == 0 and p.entries:
                return candidate_url, "rss"
        except Exception:
            pass

        # Иначе просто проверим, что HTML страница открывается
        try:
            r = requests.get(candidate_url, headers=HEADERS, timeout=TIMEOUT)
            if r.ok:
                return candidate_url, "html"
        except Exception:
            pass
        return None

    # Проверим исходный URL как HTML
    base_res = _check_candidate(url)
    if base_res:
        return base_res

    # Перебираем типовые подстраницы
    base = url if url.endswith("/") else url + "/"
    for path in COMMON_PATHS:
        candidate = urljoin(base, path)
        res = _check_candidate(candidate)
        if res:
            return res

    # Фоллбек — хотя бы html по исходному адресу
    return url, "html"


# ------------------- HTML parser -------------------
def _extract_articles_generic(html: str, base_url: str):
    """
    Универсальный HTML-парсер новостей для корпоративных сайтов.
    Ищет статьи по типичным селекторам и пытается извлечь title/link/date.
    """
    soup = BeautifulSoup(html, "html.parser")

    # кандидаты для поиска новостей
    candidates = []
    # 1) семантические теги
    candidates += soup.select("article")
    # 2) часто встречающиеся классы
    candidates += soup.select("[class*=news], [class*=post], [class*=card], [class*=item]")
    # 3) списки с ссылками
    candidates += soup.select("ul li a, .cards a, .list a, .news-list a")

    seen = set()
    items = []
    for node in candidates:
        # нормализуем к ссылке
        a = node if node.name == "a" else node.find("a", href=True)
        if not a or not a.get("href"):
            continue

        link = urljoin(base_url, a.get("href"))
        if link in seen:
            continue
        seen.add(link)

        # заголовок
        title = (a.get_text(strip=True) or node.get_text(strip=True))[:300]
        if not title:
            continue

        # дата (если есть <time> или дата в data-* атрибуте)
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
            # Fallback: без даты считаем сейчас — чтобы не терять элемент
            dt = datetime.now()

        items.append(
            {
                "id": link,
                "title": title,
                "link": link,
                "date": dt,
                "category": "other",
            }
        )

        if len(items) >= 30:
            break

    return items


# ------------------- Fetch items -------------------
async def fetch_items(url: str, feed_type: str):
    """
    Загружаем список новостей:
    - RSS/Atom через feedparser (по URL)
    - HTML fallback через requests + BeautifulSoup
    Возвращаем список словарей: {id,title,link,date,category}
    """
    if feed_type in ("rss", "atom"):
        parsed = feedparser.parse(url)
        items = []
        for entry in parsed.entries:
            try:
                # дата публикации: пытаемся аккуратно извлечь
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

                items.append(
                    {
                        "id": entry.get("id") or entry.get("link"),
                        "title": entry.get("title"),
                        "link": entry.get("link"),
                        "date": dt,
                        "category": "other",
                    }
                )
            except Exception:
                continue
        return items

    # HTML fallback
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return _extract_articles_generic(resp.text, url)
