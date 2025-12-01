import os
import logging
from typing import Any, Dict, List, cast
import re
import time
import random
import json
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from io import BytesIO
from PyPDF2 import PdfReader

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai_client = None

# Попробуем импортировать requests-html для JS сайтов
try:
    from requests_html import HTMLSession, HTMLResponse
    HAS_REQUESTS_HTML = True
except ImportError:
    HAS_REQUESTS_HTML = False
    logger.warning("requests-html not installed. JavaScript sites may not work properly.")


def get_smart_user_agent(url: str) -> str:
    """Выбираем User-Agent в зависимости от сайта."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    # Для современных сайтов используем последние версии
    modern_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ]
    
    # Для старых сайтов или сомнительных
    conservative_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    ]
    
    # Определяем тип сайта
    modern_domains = ['tochka.com', 'career.habr.com', 'linkedin.com', 'glassdoor.com']
    if any(modern_domain in domain for modern_domain in modern_domains):
        return random.choice(modern_agents)
    
    return random.choice(conservative_agents)


def load_system_prompt() -> str:
    """Load the system prompt from system_prompt.txt if it exists."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "system_prompt.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful assistant."
    except Exception as e:
        logger.warning(f"Failed to load system_prompt.txt: {e}")
        return "You are a helpful assistant."


# =========================
# Парсинг текста / ссылок / PDF - УЛУЧШЕННЫЙ ДЛЯ JS САЙТОВ
# =========================

def clean_text(raw: str) -> str:
    """Приводит текст в аккуратный вид."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_url(text: str) -> bool:
    """Проверяем, похожа ли строка на URL."""
    if not text:
        return False
    text = text.strip()
    
    # Проверяем основные признаки URL
    url_patterns = [
        r'^https?://[^\s/$.?#].[^\s]*$',
        r'^[^\s/$.?#]+\.[^\s/$.?#]+(/[^\s]*)?$',
        r'^www\.[^\s/$.?#]+\.[^\s/$.?#]+(/[^\s]*)?$',
    ]
    
    for pattern in url_patterns:
        if re.match(pattern, text, re.IGNORECASE):
            return True
    
    return False


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Достаём текст из PDF по сырым байтам."""
    try:
        reader = PdfReader(BytesIO(data))
        pages_text: List[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages_text.append(page_text)
        return clean_text("\n\n".join(pages_text))
    except Exception as e:
        logger.error(f"Error while extracting text from PDF bytes: {e}")
        return ""


def _extract_json_ld(soup: BeautifulSoup) -> str:
    """Извлекаем структурированные данные из JSON-LD."""
    json_ld_data = []
    scripts = soup.find_all('script', type='application/ld+json')
    
    for script in scripts:
        try:
            data = json.loads(script.string)
            # Ищем данные о вакансиях
            if isinstance(data, dict):
                if data.get('@type') in ['JobPosting', 'JobPosting', 'JobPosting']:
                    # Извлекаем информацию о вакансии
                    job_info = []
                    for key in ['title', 'description', 'responsibilities', 'requirements', 
                                'qualifications', 'skills', 'educationRequirements', 
                                'experienceRequirements', 'baseSalary', 'employmentType']:
                        if key in data:
                            job_info.append(f"{key}: {data[key]}")
                    if job_info:
                        json_ld_data.append("\n".join(job_info))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get('@type') in ['JobPosting', 'JobPosting']:
                        job_info = []
                        for key in ['title', 'description', 'responsibilities']:
                            if key in item:
                                job_info.append(f"{key}: {item[key]}")
                        if job_info:
                            json_ld_data.append("\n".join(job_info))
        except:
            continue
    
    return "\n\n".join(json_ld_data) if json_ld_data else ""


def _extract_meta_content(soup: BeautifulSoup) -> str:
    """Извлекаем контент из мета-тегов (часто есть на JS сайтах)."""
    meta_content = []
    
    # Meta description
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        meta_content.append(meta_desc['content'])
    
    # Open Graph
    og_desc = soup.find('meta', attrs={'property': 'og:description'})
    if og_desc and og_desc.get('content'):
        meta_content.append(og_desc['content'])
    
    # Twitter
    twitter_desc = soup.find('meta', attrs={'name': 'twitter:description'})
    if twitter_desc and twitter_desc.get('content'):
        meta_content.append(twitter_desc['content'])
    
    # Title
    title = soup.find('title')
    if title and title.string:
        meta_content.append(f"Title: {title.string}")
    
    # H1
    h1 = soup.find('h1')
    if h1 and h1.get_text(strip=True):
        meta_content.append(f"Header: {h1.get_text(strip=True)}")
    
    return "\n".join(meta_content)


def _try_javascript_rendering(url: str) -> str:
    """Пробуем рендерить JavaScript сайты."""
    if not HAS_REQUESTS_HTML:
        raise RuntimeError("requests-html not available")
    
    session = HTMLSession()
    
    try:
        # Для tochka.com и подобных используем специальные параметры
        headers = {
            'User-Agent': get_smart_user
