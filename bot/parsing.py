import logging
import re
import time
import random
import os
from io import BytesIO
from typing import Optional, Dict, Any  # ← ДОБАВЬ ЭТУ СТРОКУ!

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader

from config import (
    PROXY_URL, 
    MIN_MEANINGFUL_TEXT_LENGTH,
    SELENIUM_ENABLED,
    SELENIUM_TIMEOUT,
    SELENIUM_HEADLESS
)

logger = logging.getLogger(__name__)

# Единое "человеческое" сообщение пользователю, когда не удалось получить вакансию по ссылке
GENERIC_VACANCY_ERROR_MSG = (
    "Не удалось автоматически получить текст вакансии с сайта.\n"
    "Пожалуйста, скопируйте и отправьте текст вакансии вручную."
)

# =========================
# КОНФИГУРАЦИЯ ИЗ СТАТЬИ (ДОБАВИТЬ В Environment на Render)
# =========================
CLOUDSCRAPER_ENABLED = os.getenv("CLOUDSCRAPER_ENABLED", "false").lower() == "true"
PLAYWRIGHT_ENABLED = os.getenv("PLAYWRIGHT_ENABLED", "false").lower() == "true"

# Полные заголовки как в статье
FULL_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "TE": "trailers",
}

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ТЕКСТА
# =========================

def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# =========================
# PDF ПАРСЕР
# =========================

def extract_text_from_pdf_bytes(data: bytes) -> str:
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
        # уже "человеческая" ошибка
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка чтения PDF: {e}", exc_info=True)
        raise ValueError(
            "Не удалось прочитать PDF файл. "
            "Убедитесь, что файл не повреждён и содержит текст."
        )

# =========================
# ЛОГИКА РАБОТЫ СО ССЫЛКАМИ
# =========================

URL_REGEX = re.compile(
    r"^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$",
    re.IGNORECASE,
)

def looks_like_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    return bool(URL_REGEX.match(text))

def normalize_url(text: str) -> str:
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text

def html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()
        text = soup.get_text(separator="\n")
        return clean_text(text)
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {e}", exc_info=True)
        return ""

# =========================
# МЕТОДЫ ИЗ СТАТЬИ ДЛЯ ОБХОДА КАПЧИ И 403
# =========================

def _is_blocked_page(html: str) -> bool:
    """Улучшенная проверка блокировок"""
    if not html or len(html) < 100:
        return True
    
    html_lower = html.lower()
    
    block_indicators = [
        "captcha",
        "cloudflare",
        "access denied",
        "доступ запрещен",
        "403 forbidden",
        "are you human",
        "подтвердите что вы не робот",
        "security check",
        "ddos-guard",
    ]
    
    return any(indicator in html_lower for indicator in block_indicators)

def parse_with_cloudscraper(url: str, proxy_url: Optional[str] = None) -> str:
    """Метод из статьи: cloudscraper для обхода Cloudflare"""
    try:
        import cloudscraper
        
        logger.info(f"☁️ Cloudscraper: парсим {url}")
        
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False
            }
        )
        
        proxies = None
        if proxy_url:
            normalized_proxy = _normalize_proxy_url(proxy_url)
            proxies = {'http': normalized_proxy, 'https': normalized_proxy}
        
        response = scraper.get(
            url, 
            headers=FULL_BROWSER_HEADERS,
            proxies=proxies,
            timeout=30
        )
        
        if response.status_code == 403:
            raise ValueError("Сайт заблокировал доступ")
        
        response.raise_for_status()
        
        html = response.text
        
        if _is_blocked_page(html):
            raise ValueError("Обнаружена блокировка")
        
        logger.info(f"✅ Cloudscraper успешен: {len(html)} символов")
        return html
        
    except ImportError:
        raise ImportError("Cloudscraper не установлен")
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка: {e}")
        raise

def parse_with_undetected_chromedriver(url: str, proxy_url: Optional[str] = None) -> str:
    """Главный метод из статьи: undetected-chromedriver"""
    try:
        import undetected_chromedriver as uc
        
        logger.info(f"🛡️ Undetected ChromeDriver: парсим {url}")
        
        options = uc.ChromeOptions()
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        if proxy_url:
            proxy_for_uc = _format_proxy_for_browser(proxy_url)
            options.add_argument(f'--proxy-server={proxy_for_uc}')
        
        driver = uc.Chrome(options=options, version_main=120, suppress_welcome=True)
        
        try:
            driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                "userAgent": FULL_BROWSER_HEADERS["User-Agent"]
            })
            
            driver.execute_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            """)
            
            driver.get(url)
            time.sleep(random.uniform(3, 6))
            
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
            time.sleep(1)
            
            html = driver.page_source
            
            if _is_blocked_page(html):
                raise ValueError("Обнаружена блокировка")
            
            logger.info(f"✅ Undetected ChromeDriver успешен: {len(html)} символов")
            return html
            
        finally:
            try:
                driver.quit()
            except:
                pass
                
    except ImportError:
        raise ImportError("Undetected ChromeDriver не установлен")
    except Exception as e:
        logger.error(f"❌ Undetected ChromeDriver ошибка: {e}")
        raise

def parse_with_playwright(url: str, proxy_url: Optional[str] = None) -> str:
    """Альтернативный метод из статьи: Playwright"""
    try:
        from playwright.sync_api import sync_playwright
        
        logger.info(f"🎭 Playwright: парсим {url}")
        
        proxy_config = None
        if proxy_url:
            proxy_parts = _parse_proxy_url(proxy_url)
            proxy_config = {
                "server": f"http://{proxy_parts['host']}:{proxy_parts['port']}",
                "username": proxy_parts.get('username'),
                "password": proxy_parts.get('password')
            }
        
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=SELENIUM_HEADLESS,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
            )
            
            context = browser.new_context(
                user_agent=FULL_BROWSER_HEADERS["User-Agent"],
                viewport={'width': 1920, 'height': 1080},
                locale='ru-RU',
                proxy=proxy_config
            )
            
            page = context.new_page()
            
            try:
                page.goto(url, wait_until='networkidle', timeout=30000)
                page.wait_for_timeout(3000)
                
                html = page.content()
                
                if _is_blocked_page(html):
                    raise ValueError("Обнаружена блокировка")
                
                logger.info(f"✅ Playwright успешен: {len(html)} символов")
                return html
                
            finally:
                try:
                    context.close()
                    browser.close()
                except:
                    pass
                
    except ImportError:
        raise ImportError("Playwright не установлен")
    except Exception as e:
        logger.error(f"❌ Playwright ошибка: {e}")
        raise

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ПРОКСИ
# =========================

def _format_proxy_for_browser(proxy_url: str) -> str:
    """Форматирует прокси для браузера"""
    if proxy_url.startswith('http://'):
        proxy_url = proxy_url[7:]
    elif proxy_url.startswith('https://'):
        proxy_url = proxy_url[8:]
    
    if '@' in proxy_url:
        proxy_url = proxy_url.split('@')[1]
    
    return proxy_url

def _parse_proxy_url(proxy_url: str) -> Dict[str, Any]:
    """Парсит URL прокси"""
    result = {'host': '', 'port': '', 'username': None, 'password': None}
    
    try:
        if proxy_url.startswith('http://'):
            proxy_url = proxy_url[7:]
        
        if '@' in proxy_url:
            auth_part, host_part = proxy_url.split('@', 1)
            if ':' in auth_part:
                result['username'], result['password'] = auth_part.split(':', 1)
        else:
            host_part = proxy_url
        
        if ':' in host_part:
            result['host'], port_str = host_part.split(':', 1)
            result['port'] = int(port_str)
            
    except Exception:
        pass
    
    return result

# =========================
# ВАШ СУЩЕСТВУЮЩИЙ КОД (с небольшими улучшениями)
# =========================

def _normalize_proxy_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw

    if re.match(r"^[a-zA-Z0-9+.-]+://", raw):
        return raw

    if "@" in raw:
        left, right = raw.split("@", 1)
        
        def looks_like_host_port(part: str) -> bool:
            host, _, _ = part.partition(":")
            return "." in host and re.search(r"[a-zA-Z]", host) is not None

        if looks_like_host_port(left):
            host_port = left
            creds = right
        else:
            creds = left
            host_port = right

        return f"http://{creds}@{host_port}"

    return f"http://{raw}"

def fetch_html_via_requests(url: str, proxy_url: Optional[str] = None) -> str:
    """Запрос через requests (улучшенные headers)"""
    proxies = None
    if proxy_url:
        normalized_proxy = _normalize_proxy_url(proxy_url)
        proxies = {"http": normalized_proxy, "https": normalized_proxy}
        logger.info(f"🔗 Requests с прокси: {normalized_proxy}")

    try:
        logger.info(f"🌐 Requests: парсим {url}")
        
        session = requests.Session()
        session.headers.update(FULL_BROWSER_HEADERS)  # ← Используем полные headers из статьи
        
        resp = session.get(url, proxies=proxies, timeout=20)
        resp.raise_for_status()

        html = resp.text

        if _is_blocked_page(html):
            logger.warning("⚠️ Обнаружена капча/блокировка (Requests)")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        if len(html) < 500:
            logger.warning(f"⚠️ Очень короткий ответ ({len(html)} символов)")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        return html

    except requests.exceptions.Timeout:
        logger.error("❌ Таймаут при запросе", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)
    except requests.RequestException as e:
        logger.error(f"❌ HTTP ошибка при запросе: {e}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)
    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка в requests: {e}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

# =========================
# УЛУЧШЕННЫЙ ПАРСЕР С МЕТОДАМИ ИЗ СТАТЬИ
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Умный парсер с приоритетами:
    1. Cloudscraper (быстрый обход Cloudflare)
    2. Undetected ChromeDriver (лучший для капчи)
    3. Playwright (альтернатива)
    4. Selenium (ваш текущий)
    5. Requests с прокси
    6. Requests без прокси
    """
    
    # Сохраняем ваш существующий selenium код
    def parse_with_selenium_existing(url: str, proxy_url: Optional[str] = None) -> str:
        """Ваш существующий selenium код"""
        if not SELENIUM_ENABLED:
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)
        
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            
            options = Options()
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            
            if SELENIUM_HEADLESS:
                options.add_argument('--headless=new')
            
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument('--user-agent=' + FULL_BROWSER_HEADERS["User-Agent"])
            
            if proxy_url:
                proxy_for_selenium = proxy_url
                if proxy_for_selenium.startswith('http://'):
                    proxy_for_selenium = proxy_for_selenium[7:]
                if '@' in proxy_for_selenium:
                    proxy_for_selenium = proxy_for_selenium.split('@')[1]
                options.add_argument(f'--proxy-server={proxy_for_selenium}')
            
            os.environ['WDM_LOG_LEVEL'] = '0'
            os.environ['WDM_LOCAL'] = '1'
            
            service = Service(ChromeDriverManager(cache_valid_range=30, path="/tmp/chromedriver").install())
            driver = webdriver.Chrome(service=service, options=options)
            
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            driver.get(url)
            time.sleep(random.uniform(3, 6))
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
            time.sleep(random.uniform(1, 2))
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(1, 2))
            
            html = driver.page_source
            driver.quit()
            
            if _is_blocked_page(html):
                raise ValueError(GENERIC_VACANCY_ERROR_MSG)
            
            return html
            
        except Exception as e:
            logger.error(f"❌ Selenium ошибка: {e}")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)
    
    # Порядок попыток
    methods_to_try = []
    
    # 1. Cloudscraper (если включен)
    if CLOUDSCRAPER_ENABLED:
        methods_to_try.append(("Cloudscraper", lambda: parse_with_cloudscraper(url, PROXY_URL)))
    
    # 2. Undetected ChromeDriver (главный из статьи)
    methods_to_try.append(("Undetected ChromeDriver", lambda: parse_with_undetected_chromedriver(url, PROXY_URL)))
    
    # 3. Playwright (если включен)
    if PLAYWRIGHT_ENABLED:
        methods_to_try.append(("Playwright", lambda: parse_with_playwright(url, PROXY_URL)))
    
    # 4. Ваш Selenium
    if SELENIUM_ENABLED:
        methods_to_try.append(("Selenium", lambda: parse_with_selenium_existing(url, PROXY_URL)))
    
    # 5. Requests с прокси
    if PROXY_URL:
        methods_to_try.append(("Requests с прокси", lambda: fetch_html_via_requests(url, PROXY_URL)))
    
    # 6. Requests без прокси
    methods_to_try.append(("Requests без прокси", lambda: fetch_html_via_requests(url, None)))
    
    last_error = None
    
    for method_name, parser_func in methods_to_try:
        try:
            logger.info(f"🔄 Пробуем {method_name} для {url}")
            html = parser_func()
            text = html_to_text(html)
            
            if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                logger.info(f"✅ {method_name} успешен: {len(text)} символов")
                return text
            else:
                logger.warning(f"⚠️ {method_name}: мало текста")
                last_error = ValueError(GENERIC_VACANCY_ERROR_MSG)
                
        except ImportError as e:
            logger.warning(f"⚠️ {method_name} не установлен: {e}")
            continue
        except Exception as e:
            logger.warning(f"⚠️ {method_name} не сработал: {e}")
            last_error = e
            continue
    
    logger.error("❌ Все методы парсинга не сработали")
    raise ValueError(GENERIC_VACANCY_ERROR_MSG)
