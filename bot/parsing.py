import logging
import re
import time
import random
import os
from io import BytesIO
from typing import Optional, Dict, Any
from urllib.parse import urlparse

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
# ПЕРЕМЕННЫЕ ИЗ ENVIRONMENT (используем напрямую!)
# =========================

# Получаем из переменных окружения
CLOUDSCRAPER_ENABLED = os.getenv("CLOUDSCRAPER_ENABLED", "true").lower() == "true"
FORCE_MOBILE_HH = os.getenv("FORCE_MOBILE_HH", "false").lower() == "true"
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))

# Headers как в статье
BROWSER_HEADERS = {
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
        soup = BeautifulSoup(html, "html.parser")
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            element.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        text = clean_text(text)
        
        # Удаляем слишком короткие строки
        lines = [line for line in text.split('\n') if len(line.strip()) > 10]
        text = '\n'.join(lines)
        
        return text
        
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста: {e}")
        return ""

# =========================
# ФОРМАТИРОВАНИЕ ПРОКСИ
# =========================

def _format_proxy_for_requests(proxy_url: str) -> Optional[str]:
    """Форматирует прокси для requests"""
    if not proxy_url:
        return None
    
    proxy = proxy_url.strip()
    
    # Уже есть схема
    if proxy.startswith(('http://', 'https://', 'socks5://')):
        return proxy
    
    # Формат: user:pass@host:port или host:port@user:pass
    if '@' in proxy:
        parts = proxy.split('@')
        if len(parts) == 2:
            left, right = parts
            # Определяем где логин, где хост
            if ':' in left and ':' in right:
                if '.' in left:  # Первая часть - хост
                    return f"http://{right}@{left}"
                else:  # Первая часть - логин
                    return f"http://{left}@{right}"
    
    # Простой host:port
    return f"http://{proxy}"

def _format_proxy_for_chrome(proxy_url: str) -> Optional[str]:
    """Форматирует прокси для Chrome"""
    if not proxy_url:
        return None
    
    proxy = proxy_url.strip()
    if proxy.startswith('http://'):
        proxy = proxy[7:]
    elif proxy.startswith('https://'):
        proxy = proxy[8:]
    
    # Chrome не поддерживает user:pass в аргументах
    if '@' in proxy:
        proxy = proxy.split('@')[-1]
    
    return proxy

# =========================
# МЕТОД 1: ПРОСТОЙ ЗАПРОС (сначала пробуем просто зайти)
# =========================

def _try_simple_request(url: str, use_proxy: bool = True) -> tuple[bool, str, Optional[str]]:
    """
    Пробуем просто зайти как обычный браузер.
    Возвращает: (успех, html, ошибка_если_есть)
    """
    proxies = None
    if use_proxy and PROXY_URL:
        proxy_formatted = _format_proxy_for_requests(PROXY_URL)
        if proxy_formatted:
            proxies = {'http': proxy_formatted, 'https': proxy_formatted}
            logger.info(f"Используем прокси для простого запроса")
    
    try:
        logger.info(f"1. Пробуем простой запрос к {url}")
        
        session = requests.Session()
        session.headers.update(BROWSER_HEADERS)
        
        # Для HH.ru используем мобильную версию если настроено
        if 'hh.ru' in url and not url.startswith('https://m.hh.ru'):
            if FORCE_MOBILE_HH:
                mobile_url = url.replace('https://hh.ru', 'https://m.hh.ru')
                logger.info(f"Используем мобильную версию HH: {mobile_url}")
                url = mobile_url
        
        response = session.get(url, proxies=proxies, timeout=15, allow_redirects=True)
        
        logger.info(f"Статус: {response.status_code}, размер: {len(response.text)} символов")
        
        html = response.text
        
        # Если 200 и не капча - успех
        if response.status_code == 200:
            # Проверяем на капчу
            html_lower = html.lower()
            has_captcha = any(x in html_lower for x in [
                'captcha', 'cloudflare', 'access denied', 
                'are you human', 'подтвердите что вы не робот'
            ])
            
            if not has_captcha and len(html) > 500:
                logger.info(f"✅ Простой запрос УСПЕШЕН!")
                return True, html, None
            else:
                if has_captcha:
                    return False, html, "Капча/блокировка"
                else:
                    return False, html, "Короткий ответ"
        else:
            if response.status_code == 403:
                return False, html, "403 Forbidden"
            elif response.status_code == 429:
                return False, html, "429 Too Many Requests"
            else:
                return False, html, f"HTTP {response.status_code}"
                
    except Exception as e:
        logger.error(f"❌ Ошибка простого запроса: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 2: CLOUDSCRAPER (если простая попытка не удалась)
# =========================

def _try_cloudscraper(url: str) -> tuple[bool, str, Optional[str]]:
    """Пробуем Cloudscraper если простая попытка не удалась"""
    if not CLOUDSCRAPER_ENABLED:
        return False, "", "Cloudscraper отключен"
    
    try:
        import cloudscraper
        
        logger.info(f"2. Пробуем Cloudscraper для {url}")
        
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
        )
        
        proxies = None
        if PROXY_URL:
            proxy_formatted = _format_proxy_for_requests(PROXY_URL)
            if proxy_formatted:
                proxies = {'http': proxy_formatted, 'https': proxy_formatted}
        
        response = scraper.get(url, headers=BROWSER_HEADERS, proxies=proxies, timeout=30)
        
        logger.info(f"Cloudscraper статус: {response.status_code}")
        
        if response.status_code == 200:
            html = response.text
            
            # Проверяем на капчу
            html_lower = html.lower()
            has_captcha = any(x in html_lower for x in [
                'captcha', 'cloudflare', 'access denied'
            ])
            
            if not has_captcha and len(html) > 500:
                logger.info(f"✅ Cloudscraper УСПЕШЕН!")
                return True, html, None
            else:
                return False, html, "Капча или короткий ответ"
        else:
            return False, response.text, f"HTTP {response.status_code}"
            
    except ImportError:
        logger.warning("Cloudscraper не установлен")
        return False, "", "Cloudscraper не установлен"
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 3: UNDETECTED CHROMEDRIVER (последний шанс)
# =========================

def _try_undetected_chromedriver(url: str) -> tuple[bool, str, Optional[str]]:
    """Undetected ChromeDriver как последний вариант"""
    try:
        import undetected_chromedriver as uc
        
        logger.info(f"3. Пробуем Undetected ChromeDriver для {url}")
        
        options = uc.ChromeOptions()
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        # Обязательные аргументы
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument(f'--user-agent={BROWSER_HEADERS["User-Agent"]}')
        
        # Прокси
        if PROXY_URL:
            proxy_formatted = _format_proxy_for_chrome(PROXY_URL)
            if proxy_formatted:
                options.add_argument(f'--proxy-server={proxy_formatted}')
        
        driver = uc.Chrome(options=options, version_main=120, suppress_welcome=True)
        
        try:
            # Скрываем автоматизацию
            driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                "userAgent": BROWSER_HEADERS["User-Agent"]
            })
            
            driver.execute_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """)
            
            driver.get(url)
            time.sleep(random.uniform(3, 5))
            
            # Прокрутка
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
            time.sleep(1)
            
            html = driver.page_source
            
            if len(html) < 500:
                return False, html, "Короткий ответ"
            
            # Проверка на капчу
            html_lower = html.lower()
            if any(x in html_lower for x in ['captcha', 'cloudflare', 'access denied']):
                return False, html, "Капча/блокировка"
            
            logger.info(f"✅ Undetected ChromeDriver УСПЕШЕН!")
            return True, html, None
            
        except Exception as e:
            logger.error(f"Ошибка в Undetected ChromeDriver: {e}")
            return False, "", str(e)
        finally:
            try:
                driver.quit()
            except:
                pass
                
    except ImportError:
        logger.warning("Undetected ChromeDriver не установлен")
        return False, "", "Undetected ChromeDriver не установлен"
    except Exception as e:
        logger.error(f"❌ Undetected ChromeDriver ошибка: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 4: SELENIUM (резервный)
# =========================

def _try_selenium(url: str) -> tuple[bool, str, Optional[str]]:
    """Ваш существующий Selenium как резервный вариант"""
    if not SELENIUM_ENABLED:
        return False, "", "Selenium отключен"
    
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        
        logger.info(f"4. Пробуем Selenium для {url}")
        
        options = Options()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument(f'--user-agent={BROWSER_HEADERS["User-Agent"]}')
        
        if PROXY_URL:
            proxy_for_selenium = _format_proxy_for_chrome(PROXY_URL)
            if proxy_for_selenium:
                options.add_argument(f'--proxy-server={proxy_for_selenium}')
        
        # Настройки для Render
        os.environ['WDM_LOG_LEVEL'] = '0'
        os.environ['WDM_LOCAL'] = '1'
        
        service = Service(
            ChromeDriverManager(
                cache_valid_range=30,
                path="/tmp/chromedriver"
            ).install()
        )
        
        driver = webdriver.Chrome(service=service, options=options)
        
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        
        driver.get(url)
        time.sleep(random.uniform(3, 6))
        
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
        time.sleep(1)
        
        html = driver.page_source
        driver.quit()
        
        if len(html) < 500:
            return False, html, "Короткий ответ"
        
        html_lower = html.lower()
        if any(x in html_lower for x in ['captcha', 'cloudflare', 'access denied']):
            return False, html, "Капча/блокировка"
        
        logger.info(f"✅ Selenium УСПЕШЕН!")
        return True, html, None
        
    except Exception as e:
        logger.error(f"❌ Selenium ошибка: {e}")
        return False, "", str(e)

# =========================
# ГЛАВНАЯ ФУНКЦИЯ ПАРСИНГА
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Главная функция парсинга с правильной логикой:
    1. Сначала пробуем просто зайти
    2. Если не получилось - Cloudscraper
    3. Если все еще проблема - Undetected ChromeDriver
    4. Последний шанс - Selenium
    """
    logger.info(f"🚀 Начинаем парсинг: {url}")
    logger.info(f"Настройки: Cloudscraper={CLOUDSCRAPER_ENABLED}, Selenium={SELENIUM_ENABLED}")
    logger.info(f"PROXY_URL: {'Есть' if PROXY_URL else 'Нет'}")
    
    # Проверяем URL
    if not url or not looks_like_url(url):
        raise ValueError("Некорректная ссылка")
    
    url = normalize_url(url)
    
    # Методы в порядке приоритета
    methods = [
        ("Простой запрос с прокси", lambda: _try_simple_request(url, use_proxy=True)),
        ("Простой запрос без прокси", lambda: _try_simple_request(url, use_proxy=False)),
    ]
    
    # Добавляем Cloudscraper если включен
    if CLOUDSCRAPER_ENABLED:
        methods.append(("Cloudscraper", lambda: _try_cloudscraper(url)))
    
    # Добавляем Undetected ChromeDriver
    methods.append(("Undetected ChromeDriver", lambda: _try_undetected_chromedriver(url)))
    
    # Добавляем Selenium если включен
    if SELENIUM_ENABLED:
        methods.append(("Selenium", lambda: _try_selenium(url)))
    
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
    
    # Информативное сообщение
    if 'hh.ru' in url:
        error_msg = (
            "HH.ru активно блокирует автоматические запросы.\n"
            "Пожалуйста:\n"
            "1. Откройте ссылку в браузере\n"
            "2. Скопируйте текст вакансии\n" 
            "3. Отправьте его сюда"
        )
    elif last_error and ("403" in str(last_error) or "forbidden" in str(last_error).lower()):
        error_msg = "Сайт заблокировал доступ (403 Forbidden). Попробуйте другую ссылку или скопируйте текст вручную."
    elif last_error and ("429" in str(last_error)):
        error_msg = "Слишком много запросов к сайту. Подождите немного и попробуйте снова."
    else:
        error_msg = GENERIC_VACANCY_ERROR_MSG
    
    raise ValueError(error_msg)
