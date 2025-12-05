import logging
import re
import time
import random
import os
from io import BytesIO
from typing import Optional

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

GENERIC_VACANCY_ERROR_MSG = (
    "Не удалось автоматически получить текст вакансии с сайта.\n"
    "Пожалуйста, скопируйте и отправьте текст вакансии вручную."
)

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
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
# SELENIUM ДЛЯ RENDER (ИСПРАВЛЕННЫЙ)
# =========================

def init_selenium_driver(proxy_url: Optional[str] = None):
    """Инициализация Selenium на Render"""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        
        options = Options()
        
        # КРИТИЧЕСКО ВАЖНО для Render!
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        # Обход детекта
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # User-Agent
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        # Прокси для Selenium
        if proxy_url:
            # Убираем схему и авторизацию для Selenium
            proxy_for_selenium = proxy_url
            if proxy_for_selenium.startswith('http://'):
                proxy_for_selenium = proxy_for_selenium[7:]
            if '@' in proxy_for_selenium:
                # Оставляем только host:port
                proxy_for_selenium = proxy_for_selenium.split('@')[1]
            options.add_argument(f'--proxy-server={proxy_for_selenium}')
            logger.info(f"Используем прокси для Selenium: {proxy_for_selenium}")
        
        # НАСТРОЙКИ ДЛЯ RENDER:
        # 1. Устанавливаем переменные окружения для Chrome
        os.environ['WDM_LOG_LEVEL'] = '0'  # Отключаем логи webdriver-manager
        os.environ['WDM_LOCAL'] = '1'       # Используем локальный кэш
        
        # 2. Настройки для webdriver-manager
        service = Service(
            ChromeDriverManager(
                cache_valid_range=30,  # Кэшируем на 30 дней
                path="/tmp/chromedriver"  # Сохраняем в /tmp на Render
            ).install()
        )
        
        # 3. Дополнительные аргументы для Render
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-logging')
        options.add_argument('--log-level=3')
        options.add_argument('--silent')
        
        # Создаем драйвер
        driver = webdriver.Chrome(service=service, options=options)
        
        # Скрываем WebDriver
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        return driver
        
    except ImportError as e:
        logger.error(f"❌ Selenium не установлен: {e}")
        raise ImportError("Установите: pip install selenium webdriver-manager")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Selenium: {e}", exc_info=True)
        raise

def parse_with_selenium(url: str, proxy_url: Optional[str] = None) -> str:
    """Парсинг через Selenium"""
    if not SELENIUM_ENABLED:
        raise ValueError("Selenium отключен")
    
    driver = None
    try:
        logger.info(f"🦊 Selenium: парсим {url}")
        start_time = time.time()
        
        driver = init_selenium_driver(proxy_url)
        
        # Открываем страницу
        driver.get(url)
        
        # Ждем загрузки
        wait_time = random.uniform(3, 6)  # Увеличил время для Render
        time.sleep(wait_time)
        
        # Прокрутка
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
        time.sleep(random.uniform(1, 2))
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(1, 2))
        
        # Получаем HTML
        html = driver.page_source
        
        # Проверяем на капчу
        if detect_captcha(html):
            logger.warning("⚠️ Обнаружена капча/блокировка")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)
        
        elapsed = time.time() - start_time
        logger.info(f"✅ Selenium: успешно за {elapsed:.1f} сек, {len(html)} символов")
        
        return html
        
    except Exception as e:
        logger.error(f"❌ Selenium ошибка: {e}")
        raise ValueError(f"Selenium не смог обработать страницу")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

def detect_captcha(html: str) -> bool:
    """Обнаружение капчи"""
    if not html:
        return True
    
    html_lower = html.lower()
    captcha_indicators = [
        "captcha",
        "cloudflare",
        "are you human",
        "access denied",
        "подтвердите что вы не робот",
    ]
    
    return any(indicator in html_lower for indicator in captcha_indicators)

# =========================
# REQUESTS FALLBACK
# =========================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

def _normalize_proxy_url(raw: str) -> str:
    """Нормализация прокси для requests"""
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
    """Запрос через requests"""
    proxies = None
    if proxy_url:
        normalized_proxy = _normalize_proxy_url(proxy_url)
        proxies = {"http": normalized_proxy, "https": normalized_proxy}
        logger.info(f"🔗 Requests с прокси: {normalized_proxy}")

    try:
        logger.info(f"🌐 Requests: парсим {url}")
        
        session = requests.Session()
        session.headers.update(HEADERS)
        
        resp = session.get(url, proxies=proxies, timeout=20)
        resp.raise_for_status()

        html = resp.text

        if detect_captcha(html):
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        if len(html) < 500:
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        return html

    except requests.exceptions.Timeout:
        raise ValueError("Таймаут при запросе")
    except requests.RequestException:
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)
    except Exception:
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

# =========================
# УМНЫЙ ПАРСЕР С ФОЛБЭКАМИ
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Умный парсер с приоритетами.
    ВАЖНО: На Render сначала пробуем requests, потом Selenium
    """
    methods_to_try = []
    
    # На Render лучше сначала requests (быстрее)
    if PROXY_URL:
        methods_to_try.append(("Requests с прокси", lambda: fetch_html_via_requests(url, PROXY_URL)))
    
    # Потом Selenium (медленнее, но обходит капчи)
    if SELENIUM_ENABLED and PROXY_URL:
        methods_to_try.append(("Selenium с прокси", lambda: parse_with_selenium(url, PROXY_URL)))
    
    # Последний вариант - без прокси
    methods_to_try.append(("Requests без прокси", lambda: fetch_html_via_requests(url, None)))
    
    # Пробуем все методы
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
                
        except ValueError as e:
            # Если это пользовательская ошибка - пробрасываем
            if method_name == methods_to_try[-1][0]:  # Последний метод
                raise e
            continue
        except Exception as e:
            logger.warning(f"⚠️ {method_name} не сработал: {e}")
            continue
    
    # Все методы не сработали
    raise ValueError(GENERIC_VACANCY_ERROR_MSG)
