# newsbot/linkedin.py
"""LinkedIn company page scraper (требует Playwright)."""
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


def is_linkedin_url(url: str) -> bool:
    return "linkedin.com" in urlparse(url).netloc


async def fetch_linkedin_posts(url: str) -> list[dict]:
    """
    Скрапит публичные посты LinkedIn-страницы компании через Playwright.
    Возвращает список {id, title, link, date, category}.

    Ограничения: LinkedIn требует входа для большей части контента,
    публичные посты доступны только частично.
    """
    if not PLAYWRIGHT_AVAILABLE:
        logger.warning(
            "Playwright не установлен — LinkedIn недоступен. "
            "Установите: pip install playwright && playwright install chromium"
        )
        return []

    items = []
    posts_url = url.rstrip("/") + "/posts/"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            await page.goto(posts_url, timeout=30_000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3_000)

            # Пробуем разные селекторы (LinkedIn меняет вёрстку)
            selectors = [
                "div.feed-shared-update-v2",
                "li.profile-creator-shared-feed-update__container",
                "div[data-urn]",
            ]

            posts = []
            for sel in selectors:
                posts = await page.query_selector_all(sel)
                if posts:
                    break

            for post in posts[:15]:
                try:
                    text_el = await post.query_selector(
                        "span.break-words, div.feed-shared-text, span.attributed-text-segment-list__content"
                    )
                    text = (await text_el.inner_text()).strip() if text_el else ""
                    if not text:
                        continue

                    title = text[:200]
                    # Постоянной ссылки на отдельный пост нет — используем страницу компании
                    items.append({
                        "id": f"li_{hash(title)}",
                        "title": title,
                        "link": posts_url,
                        "date": datetime.now(),
                        "category": "other",
                    })
                except Exception:
                    continue

            await browser.close()

    except Exception as e:
        logger.error(f"LinkedIn fetch error for {url}: {e}")

    return items
