# newsbot/ai_classify.py
"""AI-классификация и краткое описание статей через OpenAI API."""
import json
import logging
from typing import Tuple

logger = logging.getLogger(__name__)

_client = None

SYSTEM_PROMPT = """You are a news classifier for a B2B tech competitor monitoring bot.
Classify articles into exactly one of these categories:
- event: conferences, webinars, summits, workshops, meetups, roundtables, trade shows
- product: product releases, feature launches, API updates, integrations, SDK, beta, GA announcements
- cases: customer stories, case studies, deployments, implementations, client wins, use cases
- other: company news, hiring, financial results, general partnerships

Also write a 1-2 sentence summary in Russian describing what the article is about.

Respond with valid JSON only, no extra text:
{"category": "event|product|cases|other", "summary": "краткое описание на русском"}"""


def _get_client():
    global _client
    if _client is None:
        from .config import OPENAI_API_KEY
        if not OPENAI_API_KEY:
            return None
        try:
            from openai import AsyncOpenAI
            _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        except ImportError:
            logger.warning("openai package not installed. Run: pip install openai")
            return None
    return _client


async def ai_classify_and_summarize(
    title: str, link: str = "", content: str = ""
) -> Tuple[str, str]:
    """
    Классифицирует статью и генерирует краткое описание через GPT-4o-mini.
    Возвращает (category, summary). При ошибке — ('other', '').
    """
    client = _get_client()
    if client is None:
        return "other", ""

    text_input = f"Title: {title}\nURL: {link}"
    if content:
        text_input += f"\nContent preview: {content[:400]}"

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=150,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text_input},
            ],
        )
        raw = response.choices[0].message.content.strip()
        result = json.loads(raw)
        category = result.get("category", "other")
        if category not in ("event", "product", "cases", "other"):
            category = "other"
        return category, result.get("summary", "")
    except Exception as e:
        logger.debug(f"AI classify failed for '{title}': {e}")
        return "other", ""
