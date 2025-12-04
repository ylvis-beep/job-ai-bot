import logging
import re
import time
import random
from io import BytesIO
from typing import Optional, Tuple

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

# Единое "человеческое" сообщение пользователю
GENERIC_VACANCY_ERROR_MSG = (
    "Не удалось автоматически получить текст вакансии с сайта.\n"
    "Пожалуйста, скопируйте и отправьте текст вакансии вручную."
)

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ТЕКСТА
# =========================

def clean_text(raw: str) -> str:
    """Очистка текста: убираем лишние пробелы и пустые строки."""
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
    """
    Извлечение текста из PDF файла.
    """
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
    """Мягкая проверка – похоже ли на URL."""
    if not text:
        return False
    text = text.strip()
    return bool(URL_REGEX.match(text))

def normalize_url(text: str) -> str:
    """Гарантирует, что URL начинается с http/https."""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text

def html_to_text(html: str) -> str:
    """Извлечение текста из HTML."""
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()

        text = soup.get_text(separator="\n")
        return clean_text(text)

    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {e}", exc_info=True)
        return ""

# =========================
# SELENIUM ПАРСЕР (ОСНОВНОЙ)
# =========================

def init_selenium_driver(proxy_url: Optional[str] = None):
    """Инициализация Selenium драйвера с опциональным прокси."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        
        options = Options()
        
        # Базовые настройки
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Реальный User-Agent
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        # Дополнительные опции для обхода детекта
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Добавляем прокси если указан
        if proxy_url:
            normalized_proxy = _normalize_proxy_for_selenium(proxy_url)
            options.add_argument(f'--proxy-server={normalized_proxy}')
            logger.info(f"Используем прокси для Selenium: {normalized_proxy}")
        
        # Настройки для имитации реального браузера
        prefs = {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "profile.default_content_setting_values.notifications": 2
        }
        options.add_experimental_option("prefs", prefs)
        
        # Используем WebDriver Manager для автоматической загрузки драйвера
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        
        # Скрываем WebDriver
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        return driver
        
    except ImportError as e:
        logger.error(f"❌ Selenium не установлен: {e}")
        raise ImportError("Для использования Selenium установите: pip install selenium webdriver-manager")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации Selenium: {e}")
        raise

def _normalize_proxy_for_selenium(proxy_url: str) -> str:
    """Приводит прокси к формату для Selenium."""
    # Удаляем схему если есть
    if proxy_url.startswith(('http://', 'https://')):
        proxy_url = proxy_url.split('://')[1]
    
    # Удаляем авторизацию для Selenium (он не поддерживает в аргументах)
    if '@' in proxy_url:
        # Оставляем только host:port
        proxy_url = proxy_url.split('@')[1]
    
    return proxy_url

def parse_with_selenium(url: str, proxy_url: Optional[str] = None) -> str:
    """
    Парсинг через Selenium с эмуляцией реального браузера.
    Обходит большинство капч и блокировок.
    """
    if not SELENIUM_ENABLED:
        raise ValueError("Selenium отключен в настройках")
    
    driver = None
    try:
        logger.info(f"🦊 Selenium: начинаем парсинг {url}")
        start_time = time.time()
        
        driver = init_selenium_driver(proxy_url)
        
        # Открываем страницу
        driver.get(url)
        
        # Ждем загрузки (случайное время для имитации человека)
        wait_time = random.uniform(2, 5)
        time.sleep(wait_time)
        
        # Прокрутка для имитации пользователя
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.3);")
        time.sleep(random.uniform(0.5, 1.5))
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
        time.sleep(random.uniform(0.5, 1.5))
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(random.uniform(0.5, 1.5))
        
        # Получаем HTML
        html = driver.page_source
        
        # Проверяем на капчу/блокировку
        if _detect_captcha(html):
            logger.warning("⚠️ Selenium: обнаружена капча/блокировка")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)
        
        elapsed = time.time() - start_time
        logger.info(f"✅ Selenium: успешно за {elapsed:.1f} сек, {len(html)} символов")
        
        return html
        
    except Exception as e:
        logger.error(f"❌ Selenium ошибка для {url}: {e}", exc_info=True)
        raise ValueError(f"Selenium не смог обработать страницу: {str(e)}")
        
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

def _detect_captcha(html: str) -> bool:
    """Обнаружение капчи/блокировки в HTML."""
    if not html:
        return True
    
    html_lower = html.lower()
    
    captcha_indicators = [
        "captcha",
        "cloudflare",
        "are you human",
        "access denied",
        "подтвердите что вы не робот",
        "пожалуйста, подтвердите",
        "security check"
    ]
    
    return any(indicator in html_lower for indicator in captcha_indicators)

# =========================
# ПРОКСИ-ПАРСИНГ ЧЕРЕЗ REQUESTS (FALLBACK)
# =========================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

def _normalize_proxy_url(raw: str) -> str:
    """
    Приводит PROXY_URL к виду, который понимает requests.
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    # Уже есть схема
    if re.match(r"^[a-zA-Z0-9+.-]+://", raw):
        return raw

    # Если есть логин/пароль и хост
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
    """
    Запрос HTML через прокси (requests fallback).
    """
    proxies = None
    if proxy_url:
        normalized_proxy = _normalize_proxy_url(proxy_url)
        proxies = {
            "http": normalized_proxy,
            "https": normalized_proxy,
        }
        logger.info(f"🔗 Requests с прокси: {normalized_proxy}")

    try:
        logger.info(f"🌐 Requests: парсим {url}")
        
        session = requests.Session()
        
        # Добавляем cookies от первого визита
        session.get('https://google.com', timeout=2, headers=HEADERS, proxies=proxies)
        
        resp = session.get(
            url,
            headers=HEADERS,
            proxies=proxies,
            timeout=20,
        )

        logger.info(f"Requests статус: {resp.status_code}")
        resp.raise_for_status()

        html = resp.text

        if _detect_captcha(html):
            logger.warning("⚠️ Requests: обнаружена капча")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        if len(html) < 500:
            logger.warning(f"⚠️ Requests: короткий ответ ({len(html)} символов)")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        return html

    except requests.exceptions.Timeout:
        logger.error(f"❌ Таймаут при запросе {url}")
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except requests.exceptions.ProxyError as e:
        logger.error(f"❌ Ошибка прокси: {e}")
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except requests.RequestException as e:
        logger.error(f"❌ HTTP ошибка: {e}")
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка: {e}")
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

# =========================
# УНИВЕРСАЛЬНЫЙ ПАРСЕР (SMART FALLBACK)
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Умный парсер с приоритетами:
    1. Selenium с прокси (лучше всего обходит капчи)
    2. Requests с прокси (быстрее)
    3. Requests без прокси (последний вариант)
    """
    methods_to_try = []
    
    # Определяем какие методы доступны
    if SELENIUM_ENABLED and PROXY_URL:
        methods_to_try.append(("Selenium с прокси", lambda: parse_with_selenium(url, PROXY_URL)))
    
    if PROXY_URL:
        methods_to_try.append(("Requests с прокси", lambda: fetch_html_via_requests(url, PROXY_URL)))
    
    methods_to_try.append(("Requests без прокси", lambda: fetch_html_via_requests(url, None)))
    
    # Пробуем методы по порядку
    for method_name, parser_func in methods_to_try:
        try:
            logger.info(f"🔄 Пробуем {method_name} для {url}")
            html = parser_func()
            
            # Извлекаем текст
            text = html_to_text(html)
            
            # Проверяем качество
            if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                logger.info(f"✅ {method_name} успешен: {len(text)} символов")
                return text
            else:
                logger.warning(f"⚠️ {method_name}: мало текста ({len(text) if text else 0} символов)")
                
        except ValueError as e:
            # Пользовательская ошибка - пробрасываем
            raise e
        except Exception as e:
            logger.warning(f"⚠️ {method_name} не сработал: {e}")
            continue
    
    # Все методы не сработали
    logger.error(f"❌ Все методы парсинга не сработали для {url}")
    raise ValueError(GENERIC_VACANCY_ERROR_MSG)
