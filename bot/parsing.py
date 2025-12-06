import logging
import re
import time
import random
import os
from io import BytesIO
from typing import Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader

from config import (
    PROXY_URL, 
    MIN_MEANINGFUL_TEXT_LENGTH,
    CLOUDSCRAPER_ENABLED,
    FORCE_MOBILE_HH,
    RETRY_COUNT,
    IS_RENDER
)

logger = logging.getLogger(__name__)

GENERIC_VACANCY_ERROR_MSG = (
    "Не удалось автоматически получить текст вакансии с сайта.\n"
    "Пожалуйста, скопируйте и отправьте текст вакансии вручную."
)

# =========================
# ПЕРЕМЕННЫЕ ИЗ ENVIRONMENT
# =========================

# Актуальные User-Agent
DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

# Headers для десктоп
BROWSER_HEADERS = {
    "User-Agent": DESKTOP_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Headers для мобильных
MOBILE_HEADERS = {
    "User-Agent": MOBILE_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def clean_text(raw: str) -> str:
    """Очистка текста"""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Извлечение текста из PDF"""
    try:
        reader = PdfReader(BytesIO(data))
        pages_text = []

        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                pages_text.append(page_text)

        text = "\n\n".join(pages_text)
        text = clean_text(text)

        if not text or len(text) < 50:
            raise ValueError("PDF файл пуст или не содержит читаемого текста")

        logger.info(f"✅ Извлечено {len(text)} символов из PDF")
        return text

    except ValueError:
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка чтения PDF: {e}", exc_info=True)
        raise ValueError(
            "Не удалось прочитать PDF файл. "
            "Убедитесь, что файл не повреждён и содержит текст."
        )

def looks_like_url(text: str) -> bool:
    """Проверка URL"""
    if not text:
        return False
    text = text.strip()
    URL_REGEX = re.compile(r"^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$", re.IGNORECASE)
    return bool(URL_REGEX.match(text))

def normalize_url(text: str) -> str:
    """Нормализация URL"""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text

def html_to_text(html: str) -> str:
    """Извлечение текста из HTML"""
    if not html:
        return ""
    
    try:
        soup = BeautifulSoup(html, "lxml")  # Используем lxml для скорости
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "button"]):
            element.decompose()
        
        # Специфично для HH.ru
        if 'hh.ru' in html.lower():
            # Удаляем блоки с похожими вакансиями, рекламой и т.д.
            for element in soup.find_all(class_=re.compile(r'(vacancy-serp-item|sidebar|related|similar|recommended|bloko-column)')):
                element.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        text = clean_text(text)
        
        # Удаляем слишком короткие строки
        lines = [line for line in text.split('\n') if len(line.strip()) > 5]
        text = '\n'.join(lines)
        
        return text
        
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста: {e}")
        # Пробуем простой regex fallback
        try:
            text = re.sub(r'<[^>]+>', '\n', html)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return clean_text(text)
        except:
            return ""

def _format_proxy_for_requests(proxy_url: str) -> Optional[dict]:
    """Форматирует прокси для requests"""
    if not proxy_url:
        return None
    
    proxy = proxy_url.strip()
    
    if proxy.startswith(('http://', 'https://', 'socks5://')):
        return {'http': proxy, 'https': proxy}
    
    # Добавляем схему если нужно
    if '@' in proxy:
        return {'http': f"http://{proxy}", 'https': f"http://{proxy}"}
    else:
        return {'http': f"http://{proxy}", 'https': f"http://{proxy}"}

# =========================
# МЕТОД 1: ПРОСТОЙ ЗАПРОС с улучшениями
# =========================

def _try_simple_request(url: str, use_proxy: bool = True, force_mobile: bool = False) -> Tuple[bool, str, Optional[str]]:
    """Пробуем просто зайти как обычный браузер"""
    
    proxies = None
    if use_proxy and PROXY_URL:
        proxies = _format_proxy_for_requests(PROXY_URL)
    
    try:
        # Выбираем headers
        if force_mobile:
            headers = MOBILE_HEADERS.copy()
            # Для HH.ru преобразуем URL в мобильную версию
            if 'hh.ru' in url and not url.startswith(('https://m.hh.ru', 'http://m.hh.ru')):
                url = url.replace('https://hh.ru', 'https://m.hh.ru')
                url = url.replace('http://hh.ru', 'http://m.hh.ru')
                logger.info(f"Используем мобильную версию HH: {url}")
        else:
            headers = BROWSER_HEADERS.copy()
        
        # Добавляем случайные заголовки для обхода блокировок
        headers.update({
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'DNT': '1',
        })
        
        session = requests.Session()
        session.headers.update(headers)
        
        # Добавляем небольшую задержку
        time.sleep(random.uniform(1, 2))
        
        response = session.get(
            url, 
            proxies=proxies, 
            timeout=15, 
            allow_redirects=True,
            verify=False  # Может помочь с некоторыми SSL
        )
        
        logger.info(f"Статус: {response.status_code}, размер: {len(response.text)} символов")
        
        html = response.text
        
        # Проверяем на капчу и блокировки
        html_lower = html.lower()
        has_captcha = any(x in html_lower for x in [
            'captcha', 'cloudflare', 'access denied', 
            'ddos-guard', 'recaptcha', 'hcaptcha',
            'подтвердите что вы не робот'
        ])
        
        if response.status_code == 200 and not has_captcha and len(html) > 800:
            logger.info(f"✅ Простой запрос УСПЕШЕН!")
            return True, html, None
        else:
            if has_captcha:
                return False, html, "Капча/блокировка"
            elif response.status_code == 403:
                return False, html, "403 Forbidden"
            elif response.status_code == 429:
                return False, html, "429 Too Many Requests"
            elif len(html) < 500:
                return False, html, "Короткий ответ"
            else:
                return False, html, f"HTTP {response.status_code}"
                
    except requests.exceptions.SSLError:
        # Пробуем без SSL проверки
        try:
            response = requests.get(url, headers=headers, timeout=15, verify=False)
            if response.status_code == 200 and len(response.text) > 800:
                return True, response.text, None
            return False, response.text, f"HTTP {response.status_code} (SSL bypass)"
        except Exception as e:
            return False, "", f"SSL Error: {e}"
    except Exception as e:
        logger.error(f"❌ Ошибка простого запроса: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 2: CLOUDSCRAPER (основной метод для HH.ru)
# =========================

def _try_cloudscraper(url: str) -> Tuple[bool, str, Optional[str]]:
    """Пробуем Cloudscraper - основной метод для обхода защиты"""
    if not CLOUDSCRAPER_ENABLED:
        return False, "", "Cloudscraper отключен"
    
    try:
        import cloudscraper
        
        logger.info(f"🔄 Пробуем Cloudscraper для {url}")
        
        # Создаем scraper с настройками для обхода защиты
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False
            },
            delay=10
        )
        
        proxies = None
        if PROXY_URL:
            proxies = _format_proxy_for_requests(PROXY_URL)
        
        # Для HH.ru всегда используем мобильную версию
        if 'hh.ru' in url and FORCE_MOBILE_HH and not url.startswith(('https://m.hh.ru', 'http://m.hh.ru')):
            url = url.replace('https://hh.ru', 'https://m.hh.ru')
            url = url.replace('http://hh.ru', 'http://m.hh.ru')
            logger.info(f"Cloudscraper использует мобильную версию: {url}")
        
        # Используем мобильные headers для HH.ru
        headers = MOBILE_HEADERS.copy() if 'hh.ru' in url else BROWSER_HEADERS.copy()
        
        response = scraper.get(
            url, 
            headers=headers,
            proxies=proxies, 
            timeout=30
        )
        
        logger.info(f"Cloudscraper статус: {response.status_code}")
        
        if response.status_code == 200:
            html = response.text
            
            if len(html) > 800 and 'captcha' not in html.lower():
                logger.info(f"✅ Cloudscraper УСПЕШЕН!")
                return True, html, None
            else:
                return False, html, f"Капча или короткий ответ ({len(html)} chars)"
        else:
            return False, response.text, f"HTTP {response.status_code}"
            
    except ImportError:
        logger.warning("Cloudscraper не установлен")
        return False, "", "Cloudscraper не установлен"
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 3: РЕЗЕРВНЫЙ МЕТОД - API через сторонний сервис
# =========================

def _try_scraping_ant(url: str) -> Tuple[bool, str, Optional[str]]:
    """Резервный метод через ScrapingAnt API (если настроен)"""
    api_key = os.environ.get('SCRAPINGANT_API_KEY')
    if not api_key:
        return False, "", "ScrapingAnt API key not set"
    
    try:
        logger.info(f"🔄 Пробуем ScrapingAnt API для {url}")
        
        # Используем мобильную версию для HH.ru
        if 'hh.ru' in url and not url.startswith(('https://m.hh.ru', 'http://m.hh.ru')):
            url = url.replace('https://hh.ru', 'https://m.hh.ru')
        
        api_url = f"https://api.scrapingant.com/v2/general"
        params = {
            'url': url,
            'x-api-key': api_key,
            'browser': 'false',  # Без браузера для скорости
            'proxy_country': 'RU',
            'return_text': 'true'
        }
        
        response = requests.get(api_url, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            html = data.get('text', '')
            
            if len(html) > 800:
                logger.info(f"✅ ScrapingAnt УСПЕШЕН!")
                return True, html, None
            else:
                return False, html, f"Короткий ответ ({len(html)} chars)"
        else:
            return False, "", f"API error: {response.status_code}"
            
    except Exception as e:
        logger.error(f"❌ ScrapingAnt ошибка: {e}")
        return False, "", str(e)

# =========================
# ГЛАВНАЯ ФУНКЦИЯ ПАРСИНГА
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Главная функция парсинга с приоритетом Cloudscraper
    """
    logger.info(f"🚀 Начинаем парсинг: {url}")
    logger.info(f"Настройки: Cloudscraper={CLOUDSCRAPER_ENABLED}, IS_RENDER={IS_RENDER}")
    
    # Проверяем URL
    if not url or not looks_like_url(url):
        raise ValueError("Некорректная ссылка")
    
    url = normalize_url(url)
    
    # Определяем методы в зависимости от окружения
    if IS_RENDER:
        # На Render используем только легкие методы
        methods = [
            ("Cloudscraper", lambda: _try_cloudscraper(url)),
            ("Простой запрос (мобильный)", lambda: _try_simple_request(url, force_mobile=True)),
        ]
    else:
        # Локально можно использовать все методы
        methods = [
            ("Cloudscraper", lambda: _try_cloudscraper(url)),
            ("Простой запрос (мобильный)", lambda: _try_simple_request(url, force_mobile=True)),
            ("Простой запрос (десктоп)", lambda: _try_simple_request(url, force_mobile=False)),
        ]
    
    # Добавляем API метод если есть ключ
    if os.environ.get('SCRAPINGANT_API_KEY'):
        methods.append(("ScrapingAnt API", lambda: _try_scraping_ant(url)))
    
    logger.info(f"Будем пробовать {len(methods)} методов")
    
    # Пробуем все методы
    last_error = None
    
    for method_name, method_func in methods:
        try:
            logger.info(f"🔄 Пробуем {method_name}...")
            
            success, html, error = method_func()
            
            if success:
                text = html_to_text(html)
                logger.info(f"{method_name}: извлечено {len(text)} символов текста")
                
                if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                    logger.info(f"✅ {method_name} УСПЕШЕН!")
                    return text
                else:
                    logger.warning(f"⚠️ {method_name}: мало текста ({len(text)} символов)")
                    continue
            else:
                logger.warning(f"⚠️ {method_name}: {error}")
                last_error = error
                continue
                
        except Exception as e:
            logger.warning(f"⚠️ {method_name} вызвал исключение: {e}")
            last_error = e
            continue
    
    # Все методы не сработали
    logger.error(f"❌ Все методы не сработали для {url}")
    
    # Информативное сообщение для пользователя
    if 'hh.ru' in url:
        error_msg = (
            "HH.ru активно блокирует автоматические запросы.\n\n"
            "🔧 **Как решить:**\n"
            "1. Откройте ссылку в браузере\n"
            "2. Нажмите Ctrl+A (выделить все)\n"
            "3. Нажмите Ctrl+C (скопировать)\n"
            "4. Отправьте текст сюда\n\n"
            "📝 Или пришлите вакансию текстом вручную"
        )
    elif 'habr.com' in url:
        error_msg = (
            "Для вакансий с Habr лучше скопировать текст вручную.\n"
            "Или используйте direct ссылку на описание вакансии."
        )
    elif last_error and ("403" in str(last_error)):
        error_msg = "Сайт заблокировал доступ (403 Forbidden). Скопируйте текст вручную."
    elif last_error and ("429" in str(last_error)):
        error_msg = "Слишком много запросов. Подождите 5 минут и попробуйте снова."
    else:
        error_msg = GENERIC_VACANCY_ERROR_MSG
    
    raise ValueError(error_msg)

# =========================
# ФУНКЦИЯ ДЛЯ РЕЗЮМЕ (PDF)
# =========================

def parse_resume_from_pdf(pdf_content: bytes) -> str:
    """Парсинг резюме из PDF"""
    try:
        text = extract_text_from_pdf_bytes(pdf_content)
        if len(text) < 100:
            raise ValueError("Резюме слишком короткое или нечитаемое")
        return text
    except Exception as e:
        logger.error(f"❌ Ошибка парсинга резюме: {e}")
        raise ValueError("Не удалось прочитать резюме. Убедитесь, что файл в формате PDF и содержит текст.")

# =========================
# ФУНКЦИЯ ДЛЯ ВАКАНСИИ (URL)
# =========================

def parse_vacancy_from_url(url: str) -> str:
    """Парсинг вакансии по URL"""
    try:
        text = fetch_url_text_via_proxy(url)
        if len(text) < 200:
            raise ValueError("Текст вакансии слишком короткий")
        return text
    except ValueError as e:
        # Пробрасываем пользовательские сообщения как есть
        raise e
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка парсинга: {e}")
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)
