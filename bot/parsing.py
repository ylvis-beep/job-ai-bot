import logging
import re
import time
import random
import os
import sys
from io import BytesIO
from typing import Optional, Dict, Any, Tuple
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
# ПЕРЕМЕННЫЕ ИЗ ENVIRONMENT
# =========================

CLOUDSCRAPER_ENABLED = os.getenv("CLOUDSCRAPER_ENABLED", "true").lower() == "true"
FORCE_MOBILE_HH = os.getenv("FORCE_MOBILE_HH", "true").lower() == "true"  # По умолчанию мобильная версия
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))

# Обновленные headers с актуальными User-Agent
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
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
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# Mobile headers для HH.ru
MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
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
        soup = BeautifulSoup(html, "html.parser")
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "button", "input", "select", "textarea"]):
            element.decompose()
        
        # Для HH.ru специфично
        if 'hh.ru' in html.lower():
            for element in soup.find_all(['div', 'section'], class_=re.compile(r'(bloko-column|vacancy-serp-item|sidebar|related|similar)')):
                element.decompose()
        
        text = soup.get_text(separator='\n', strip=True)
        text = clean_text(text)
        
        # Удаляем слишком короткие строки
        lines = [line for line in text.split('\n') if len(line.strip()) > 5]
        text = '\n'.join(lines)
        
        return text
        
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста: {e}")
        return ""

# =========================
# ФОРМАТИРОВАНИЕ ПРОКСИ
# =========================

def _format_proxy_for_requests(proxy_url: str) -> Optional[Dict[str, str]]:
    """Форматирует прокси для requests"""
    if not proxy_url:
        return None
    
    proxy = proxy_url.strip()
    
    # Уже есть схема
    if proxy.startswith(('http://', 'https://', 'socks5://')):
        return {
            'http': proxy,
            'https': proxy
        }
    
    # Добавляем схему если нужно
    if not proxy.startswith('http'):
        proxy = f"http://{proxy}"
    
    return {
        'http': proxy,
        'https': proxy
    }

def _format_proxy_for_chrome(proxy_url: str) -> Optional[str]:
    """Форматирует прокси для Chrome"""
    if not proxy_url:
        return None
    
    proxy = proxy_url.strip()
    
    # Удаляем схему для Chrome
    if proxy.startswith('http://'):
        proxy = proxy[7:]
    elif proxy.startswith('https://'):
        proxy = proxy[8:]
    elif proxy.startswith('socks5://'):
        proxy = proxy[9:]
    
    # Chrome не поддерживает user:pass в аргументах
    if '@' in proxy:
        proxy = proxy.split('@')[-1]
    
    return proxy

# =========================
# МЕТОД 1: ПРОСТОЙ ЗАПРОС с улучшенными headers
# =========================

def _try_simple_request(url: str, use_proxy: bool = True, force_mobile: bool = False) -> Tuple[bool, str, Optional[str]]:
    """
    Пробуем просто зайти как обычный браузер
    """
    proxies = None
    if use_proxy and PROXY_URL:
        proxies = _format_proxy_for_requests(PROXY_URL)
        if proxies:
            logger.info(f"Используем прокси для простого запроса")
    
    try:
        logger.info(f"1. Пробуем простой запрос к {url}")
        
        session = requests.Session()
        
        # Выбираем headers
        if 'hh.ru' in url and force_mobile:
            headers = MOBILE_HEADERS
            # Преобразуем URL в мобильную версию
            url = url.replace('https://hh.ru', 'https://m.hh.ru')
            url = url.replace('http://hh.ru', 'http://m.hh.ru')
            logger.info(f"Используем мобильную версию HH: {url}")
        else:
            headers = BROWSER_HEADERS
        
        session.headers.update(headers)
        
        # Добавляем cookies
        session.cookies.update({
            'accept': '1',
            'force_cookie_consent': 'true',
        })
        
        # Добавляем задержку между запросами
        time.sleep(random.uniform(1, 3))
        
        response = session.get(
            url, 
            proxies=proxies, 
            timeout=20, 
            allow_redirects=True,
            verify=False  # Может помочь с некоторыми SSL ошибками
        )
        
        logger.info(f"Статус: {response.status_code}, размер: {len(response.text)} символов")
        
        html = response.text
        
        # Проверяем на капчу и блокировки
        html_lower = html.lower()
        has_captcha = any(x in html_lower for x in [
            'captcha', 'cloudflare', 'access denied', 
            'are you human', 'подтвердите что вы не робот',
            'ddos-guard', 'recaptcha', 'hcaptcha'
        ])
        
        if response.status_code == 200 and not has_captcha and len(html) > 1000:
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
            session = requests.Session()
            session.headers.update(BROWSER_HEADERS)
            response = session.get(url, timeout=20, verify=False)
            if response.status_code == 200:
                return True, response.text, None
            return False, response.text, f"HTTP {response.status_code}"
        except Exception as e:
            return False, "", f"SSL Error: {e}"
    except Exception as e:
        logger.error(f"❌ Ошибка простого запроса: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 2: CLOUDSCRAPER с улучшениями
# =========================

def _try_cloudscraper(url: str) -> Tuple[bool, str, Optional[str]]:
    """Пробуем Cloudscraper"""
    if not CLOUDSCRAPER_ENABLED:
        return False, "", "Cloudscraper отключен"
    
    try:
        import cloudscraper
        
        logger.info(f"2. Пробуем Cloudscraper для {url}")
        
        # Создаем scraper с разными настройками
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False,
                'desktop': True
            },
            delay=10,
            interpreter='nodejs'
        )
        
        proxies = None
        if PROXY_URL:
            proxies = _format_proxy_for_requests(PROXY_URL)
        
        # Пробуем несколько раз
        for attempt in range(2):
            try:
                response = scraper.get(
                    url, 
                    headers=BROWSER_HEADERS, 
                    proxies=proxies, 
                    timeout=30
                )
                
                logger.info(f"Cloudscraper статус: {response.status_code}")
                
                if response.status_code == 200:
                    html = response.text
                    
                    if len(html) > 1000 and 'captcha' not in html.lower():
                        logger.info(f"✅ Cloudscraper УСПЕШЕН!")
                        return True, html, None
                    else:
                        if attempt == 0:
                            # Ждем и пробуем еще раз
                            time.sleep(random.uniform(5, 10))
                            continue
                        else:
                            return False, html, "Капча или короткий ответ"
                else:
                    return False, response.text, f"HTTP {response.status_code}"
                    
            except Exception as e:
                if attempt == 0:
                    time.sleep(5)
                    continue
                raise
                
        return False, "", "Cloudscraper не смог получить данные"
            
    except ImportError:
        logger.warning("Cloudscraper не установлен")
        return False, "", "Cloudscraper не установлен"
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 3: UNDETECTED CHROMEDRIVER с улучшениями
# =========================

def _try_undetected_chromedriver(url: str) -> Tuple[bool, str, Optional[str]]:
    """Undetected ChromeDriver с улучшенным stealth"""
    try:
        import undetected_chromedriver as uc
        
        logger.info(f"3. Пробуем Undetected ChromeDriver для {url}")
        
        options = uc.ChromeOptions()
        
        # Настройки для Render
        if os.environ.get('RENDER', ''):
            options.binary_location = '/usr/bin/google-chrome'
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            SELENIUM_HEADLESS = True  # На Render всегда headless
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        # Улучшенные stealth настройки
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-features=IsolateOrigins,site-per-process')
        options.add_argument('--disable-web-security')
        options.add_argument('--disable-site-isolation-trials')
        options.add_argument('--disable-logging')
        options.add_argument('--log-level=3')
        options.add_argument('--output=/dev/null')
        options.add_argument('--disable-3d-apis')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--disable-features=AudioServiceOutOfProcess')
        
        # Прокси
        if PROXY_URL:
            proxy_formatted = _format_proxy_for_chrome(PROXY_URL)
            if proxy_formatted:
                options.add_argument(f'--proxy-server={proxy_formatted}')
        
        # Версия Chrome
        chrome_version = 131  # Актуальная версия
        
        try:
            driver = uc.Chrome(
                options=options,
                version_main=chrome_version,
                suppress_welcome=True,
                driver_executable_path='/tmp/chromedriver' if os.environ.get('RENDER') else None
            )
            
            try:
                # Улучшенный stealth
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                driver.execute_script("Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]})")
                driver.execute_script("Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']})")
                
                # Устанавливаем User-Agent
                driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                    "userAgent": BROWSER_HEADERS["User-Agent"],
                    "platform": "Windows"
                })
                
                # Устанавливаем cookies перед заходом
                driver.get("https://google.com")
                time.sleep(1)
                
                # Переходим на целевую страницу
                driver.get(url)
                
                # Имитация поведения человека
                time.sleep(random.uniform(2, 4))
                
                # Прокрутка
                scroll_height = driver.execute_script("return document.body.scrollHeight")
                for i in range(0, scroll_height, random.randint(200, 400)):
                    driver.execute_script(f"window.scrollTo(0, {i});")
                    time.sleep(random.uniform(0.1, 0.3))
                
                time.sleep(random.uniform(1, 2))
                
                # Получаем HTML
                html = driver.page_source
                
                if len(html) < 1000:
                    return False, html, "Короткий ответ"
                
                # Проверка на капчу
                html_lower = html.lower()
                if any(x in html_lower for x in ['captcha', 'cloudflare', 'access denied', 'ddos-guard']):
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
                    
        except Exception as e:
            logger.error(f"Ошибка создания драйвера: {e}")
            return False, "", str(e)
                
    except ImportError:
        logger.warning("Undetected ChromeDriver не установлен")
        return False, "", "Undetected ChromeDriver не установлен"
    except Exception as e:
        logger.error(f"❌ Undetected ChromeDriver ошибка: {e}")
        return False, "", str(e)

# =========================
# МЕТОД 4: SELENIUM с исправленной ошибкой
# =========================

def _try_selenium(url: str) -> Tuple[bool, str, Optional[str]]:
    """Selenium с исправленным webdriver-manager"""
    if not SELENIUM_ENABLED:
        return False, "", "Selenium отключен"
    
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        
        # Импортируем правильно
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.core.os_manager import ChromeType
        
        logger.info(f"4. Пробуем Selenium для {url}")
        
        options = Options()
        
        # Настройки для Render
        if os.environ.get('RENDER', ''):
            options.binary_location = '/usr/bin/google-chrome'
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            SELENIUM_HEADLESS = True
        
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument(f'--user-agent={BROWSER_HEADERS["User-Agent"]}')
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
        
        if PROXY_URL:
            proxy_for_selenium = _format_proxy_for_chrome(PROXY_URL)
            if proxy_for_selenium:
                options.add_argument(f'--proxy-server={proxy_for_selenium}')
        
        # Настройки для Render
        chrome_driver_path = None
        
        if os.environ.get('RENDER'):
            # На Render используем статический chromedriver
            chrome_driver_path = '/usr/local/bin/chromedriver'
            service = Service(chrome_driver_path)
        else:
            # Локально используем менеджер
            os.environ['WDM_LOG_LEVEL'] = '0'
            os.environ['WDM_LOCAL'] = '1'
            
            # ИСПРАВЛЕНИЕ ОШИБКИ: убираем неверные параметры
            driver_manager = ChromeDriverManager()
            chrome_driver_path = driver_manager.install()
            service = Service(chrome_driver_path)
        
        driver = webdriver.Chrome(service=service, options=options)
        
        # Stealth скрипты
        stealth_script = """
        // Overwrite the `languages` property to use a custom getter.
        Object.defineProperty(navigator, 'languages', {
          get: () => ['ru-RU', 'ru', 'en-US', 'en'],
        });
        
        // Overwrite the `plugins` property to use a custom getter.
        Object.defineProperty(navigator, 'plugins', {
          get: () => [1, 2, 3, 4, 5],
        });
        
        // Pass the Webdriver test
        Object.defineProperty(navigator, 'webdriver', {
          get: () => undefined,
        });
        
        // Pass the Chrome test.
        window.chrome = {
          runtime: {},
        };
        
        // Pass the Permissions test.
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
          parameters.name === 'notifications' ?
            Promise.resolve({ state: Notification.permission }) :
            originalQuery(parameters)
        );
        """
        
        driver.execute_script(stealth_script)
        
        # Добавляем cookies
        driver.get("https://google.com")
        time.sleep(1)
        
        # Основной запрос
        driver.get(url)
        time.sleep(random.uniform(3, 6))
        
        # Прокрутка
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
        time.sleep(1)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.8);")
        time.sleep(1)
        
        html = driver.page_source
        driver.quit()
        
        if len(html) < 1000:
            return False, html, "Короткий ответ"
        
        html_lower = html.lower()
        if any(x in html_lower for x in ['captcha', 'cloudflare', 'access denied']):
            return False, html, "Капча/блокировка"
        
        logger.info(f"✅ Selenium УСПЕШЕН!")
        return True, html, None
        
    except Exception as e:
        logger.error(f"❌ Selenium ошибка: {e}", exc_info=True)
        return False, "", str(e)

# =========================
# ГЛАВНАЯ ФУНКЦИЯ ПАРСИНГА
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Главная функция парсинга с улучшенной логикой
    """
    logger.info(f"🚀 Начинаем парсинг: {url}")
    logger.info(f"Настройки: Cloudscraper={CLOUDSCRAPER_ENABLED}, Selenium={SELENIUM_ENABLED}")
    logger.info(f"PROXY_URL: {'Есть' if PROXY_URL else 'Нет'}")
    
    # Проверяем URL
    if not url or not looks_like_url(url):
        raise ValueError("Некорректная ссылка")
    
    url = normalize_url(url)
    
    # Определяем приоритет методов в зависимости от сайта
    is_hh = 'hh.ru' in url
    
    # Для HH.ru пробуем сначала мобильную версию
    if is_hh and FORCE_MOBILE_HH and not url.startswith('https://m.hh.ru'):
        mobile_url = url.replace('https://hh.ru', 'https://m.hh.ru')
        logger.info(f"Пробуем мобильную версию: {mobile_url}")
        
        # Пробуем мобильную версию
        success, html, error = _try_simple_request(mobile_url, force_mobile=True)
        if success:
            text = html_to_text(html)
            if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                return text
    
    # Методы в порядке приоритета
    methods = [
        ("Простой запрос (десктоп)", lambda: _try_simple_request(url, use_proxy=True, force_mobile=False)),
        ("Простой запрос (мобильный)", lambda: _try_simple_request(url, use_proxy=True, force_mobile=True)),
        ("Простой запрос без прокси", lambda: _try_simple_request(url, use_proxy=False, force_mobile=False)),
    ]
    
    if CLOUDSCRAPER_ENABLED:
        methods.append(("Cloudscraper", lambda: _try_cloudscraper(url)))
    
    methods.append(("Undetected ChromeDriver", lambda: _try_undetected_chromedriver(url)))
    
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
            "Произошла ошибка,скопируйте и пришлите текст вакансии"
        )
    elif last_error and ("403" in str(last_error)):
        error_msg = "Сайт заблокировал доступ (403 Forbidden). Попробуйте другую ссылку или скопируйте текст вручную."
    elif last_error and ("429" in str(last_error)):
        error_msg = "Слишком много запросов. Подождите 5 минут и попробуйте снова."
    else:
        error_msg = GENERIC_VACANCY_ERROR_MSG
    
    raise ValueError(error_msg)
