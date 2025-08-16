import os
import asyncio
import sqlite3
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import re
from dateutil import parser as date_parser

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode


# ------------------------- Logging -------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("news-monitor-bot")

# -------------------- Per-domain rules --------------------
DOMAIN_RULES: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "spot.ai":     {"allow": ("/blog",), "ban": ("/pricing", "/careers", "/docs", "/documentation", "/solutions", "/product", "/products")},
    "lumana.ai":   {"allow": ("/blog", "/news"), "ban": ("/solutions", "/solution", "/product", "/products", "/pricing", "/careers")},
    "irisity.com": {"allow": ("/news", "/blog"), "ban": ()},
}

# Общие разрешённые/запрещённые фрагменты путей
DEFAULT_ALLOWED = ("/blog", "/news", "/p_
