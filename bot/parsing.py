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

# Единое "человеческое" сообщение пользователю
GENERIC_VACANCY_ERROR_MSG = (
    "Не удалось автоматически получить текст вакансии с сайта.\n"
    "Пожалуйста, скопируйте и отправьте текст вакансии вручную."
)

# =========================
# НАСТРОЙКИ ИЗ СТАТЬИ
# =========================

# Включение методов
CLOUDSCRAPER_ENABLED = os.getenv("CLOUDSCRAPER_ENABLED", "true").lower() == "true"
PLAYWRIGHT_ENABLED = os.getenv("PLAYWRIGHT_ENABLED", "false").lower() == "true"  # false из-за проблем на Render

# Полные заголовки браузера
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

# =========================
# ЛОГИКА РАБОТЫ СО ССЫЛКАМИ
# =========================

URL_REGEX = re.compile(
    r"^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$",
    re.IGNORECASE,
)

def looks_like_url(text: str) -> bool:
    """Проверка, похожа ли строка на URL"""
    if not text:
        return False
    text = text.strip()
    return bool(URL_REGEX.match(text))

def normalize_url(text: str) -> str:
    """Нормализация URL"""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text

def html_to_text(html: str) -> str:
    """Извлечение текста из HTML"""
    try:
        soup = BeautifulSoup(html, "html.parser")
        
        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            element.decompose()
        
        # Получаем текст
        text = soup.get_text(separator='\n', strip=True)
        
        # Очищаем
        text = clean_text(text)
        
        # Удаляем лишние пробелы и пустые строки
        lines = [line for line in text.split('\n') if line.strip()]
        text = '\n'.join(lines)
        
        return text
        
    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {e}", exc_info=True)
        return ""

# =========================
# ПРОВЕРКИ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================

def _is_blocked_page(html: str) -> bool:
    """Проверка на капчу/блокировку"""
    if not html or len(html) < 100:
        logger.warning("Слишком короткий HTML, возможно блокировка")
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
        "recaptcha",
    ]
    
    for indicator in block_indicators:
        if indicator in html_lower:
            logger.warning(f"Обнаружен индикатор блокировки: {indicator}")
            return True
    
    # Проверяем структуру капчи
    if 'captcha' in html_lower and ('input' in html_lower or 'form' in html_lower):
        return True
    
    return False

def _normalize_proxy_url(raw: str) -> Optional[str]:
    """Нормализация прокси для requests"""
    if not raw:
        return None
    
    raw = raw.strip()
    logger.info(f"Исходный прокси: {raw[:50]}...")
    
    # Уже есть схема
    if re.match(r"^[a-zA-Z0-9+.-]+://", raw):
        logger.info(f"Прокси уже нормализован: {raw[:50]}...")
        return raw
    
    # Форматы: user:pass@host:port или host:port@user:pass
    if "@" in raw:
        parts = raw.split("@")
        if len(parts) == 2:
            left, right = parts
            
            # Определяем где логин:пароль, а где хост:порт
            if ":" in left and ":" in right:
                # Оба содержат двоеточие, нужно понять что есть что
                # Обычно хост содержит точку
                if "." in left and not "." in right:
                    # left = host:port, right = user:pass
                    host_port, credentials = left, right
                else:
                    # left = user:pass, right = host:port
                    credentials, host_port = left, right
                
                logger.info(f"Формат: {credentials}@{host_port}")
                return f"http://{credentials}@{host_port}"
    
    # Простой host:port
    logger.info(f"Простой формат: {raw}")
    return f"http://{raw}"

# =========================
# CLOUDSCRAPER (ОСНОВНОЙ МЕТОД)
# =========================

def parse_with_cloudscraper(url: str, proxy_url: Optional[str] = None) -> str:
    """Обход Cloudflare через cloudscraper"""
    try:
        import cloudscraper
        
        logger.info(f"☁️ Cloudscraper: начинаем парсинг {url}")
        
        # Создаем scraper
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False
            }
        )
        
        # Настраиваем прокси
        proxies = None
        if proxy_url:
            normalized_proxy = _normalize_proxy_url(proxy_url)
            if normalized_proxy:
                proxies = {
                    'http': normalized_proxy,
                    'https': normalized_proxy
                }
                logger.info(f"Используем прокси: {normalized_proxy[:50]}...")
        
        # Делаем запрос
        logger.info(f"Делаем запрос к {url}")
        start_time = time.time()
        
        response = scraper.get(
            url, 
            headers=FULL_BROWSER_HEADERS,
            proxies=proxies,
            timeout=30
        )
        
        elapsed = time.time() - start_time
        logger.info(f"Cloudscraper ответил за {elapsed:.2f} сек, статус: {response.status_code}")
        
        if response.status_code != 200:
            logger.warning(f"Cloudscraper: статус {response.status_code}")
            if response.status_code == 403:
                raise ValueError("Сайт заблокировал доступ (403)")
            elif response.status_code == 429:
                raise ValueError("Слишком много запросов (429)")
            else:
                response.raise_for_status()
        
        html = response.text
        logger.info(f"Получено {len(html)} символов HTML")
        
        # Проверяем на блокировку
        if _is_blocked_page(html):
            logger.warning("Cloudscraper: обнаружена блокировка")
            raise ValueError("Обнаружена блокировка или капча")
        
        # Проверяем что HTML не пустой
        if len(html) < 500:
            logger.warning(f"Cloudscraper: слишком короткий HTML ({len(html)} символов)")
            # Но не падаем сразу, может быть маленькая страница
        
        logger.info(f"✅ Cloudscraper успешен для {url}")
        return html
        
    except ImportError as e:
        logger.error(f"Cloudscraper не установлен: {e}")
        raise ImportError("Установите: pip install cloudscraper")
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка для {url}: {str(e)}", exc_info=True)
        raise ValueError(f"Cloudscraper не смог обработать: {str(e)}")

# =========================
# UNDETECTED CHROMEDRIVER
# =========================

def parse_with_undetected_chromedriver(url: str, proxy_url: Optional[str] = None) -> str:
    """Undetected ChromeDriver для сложных сайтов"""
    try:
        import undetected_chromedriver as uc
        
        logger.info(f"🛡️ Undetected ChromeDriver: начинаем {url}")
        
        # Настройки
        options = uc.ChromeOptions()
        
        if SELENIUM_HEADLESS:
            options.add_argument('--headless=new')
            logger.info("Используем headless режим")
        
        # Критически важные аргументы для Render
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-blink-features=AutomationControlled')
        
        # User-Agent
        options.add_argument(f'--user-agent={FULL_BROWSER_HEADERS["User-Agent"]}')
        
        # Прокси
        if proxy_url:
            # Форматируем прокси для Chrome
            proxy_for_chrome = _format_proxy_for_chrome(proxy_url)
            options.add_argument(f'--proxy-server={proxy_for_chrome}')
            logger.info(f"Undetected с прокси: {proxy_for_chrome}")
        
        # Создаем драйвер
        logger.info("Создаем Undetected ChromeDriver...")
        driver = uc.Chrome(
            options=options,
            version_main=120,  # Версия Chrome
            suppress_welcome=True
        )
        
        try:
            # Дополнительные настройки
            driver.execute_cdp_cmd('Network.setUserAgentOverride', {
                "userAgent": FULL_BROWSER_HEADERS["User-Agent"]
            })
            
            # Скрываем автоматизацию
            driver.execute_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
            """)
            
            # Открываем страницу
            logger.info(f"Открываем {url}")
            driver.get(url)
            
            # Ждем загрузки
            wait_time = random.uniform(3, 6)
            logger.info(f"Ждем {wait_time:.1f} секунд...")
            time.sleep(wait_time)
            
            # Прокрутка для имитации пользователя
            logger.info("Прокручиваем страницу...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.3);")
            time.sleep(random.uniform(0.5, 1.5))
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
            time.sleep(random.uniform(0.5, 1.5))
            
            # Получаем HTML
            html = driver.page_source
            logger.info(f"Получено {len(html)} символов")
            
            # Проверяем на блокировку
            if _is_blocked_page(html):
                logger.warning("Undetected ChromeDriver: обнаружена блокировка")
                raise ValueError("Обнаружена блокировка")
            
            logger.info(f"✅ Undetected ChromeDriver успешен для {url}")
            return html
            
        except Exception as e:
            logger.error(f"Ошибка в Undetected ChromeDriver: {e}")
            raise
        finally:
            try:
                driver.quit()
                logger.info("Undetected ChromeDriver закрыт")
            except:
                pass
                
    except ImportError as e:
        logger.error(f"Undetected ChromeDriver не установлен: {e}")
        raise ImportError("Установите: pip install undetected-chromedriver")
    except Exception as e:
        logger.error(f"❌ Undetected ChromeDriver ошибка: {str(e)}", exc_info=True)
        raise ValueError(f"Undetected ChromeDriver не смог обработать: {str(e)}")

def _format_proxy_for_chrome(proxy_url: str) -> str:
    """Форматирует прокси для Chrome"""
    # Убираем схему
    if proxy_url.startswith('http://'):
        proxy_url = proxy_url[7:]
    elif proxy_url.startswith('https://'):
        proxy_url = proxy_url[8:]
    
    # Убираем логин:пароль если есть (Chrome не поддерживает в аргументах)
    if '@' in proxy_url:
        # Оставляем только host:port
        proxy_url = proxy_url.split('@')[1]
    
    return proxy_url

# =========================
# ОСНОВНОЙ REQUESTS ПАРСЕР (FALLBACK)
# =========================

def fetch_html_via_requests(url: str, proxy_url: Optional[str] = None) -> str:
    """Запрос через requests с улучшенной обработкой ошибок"""
    
    proxies = None
    if proxy_url:
        normalized_proxy = _normalize_proxy_url(proxy_url)
        if normalized_proxy:
            proxies = {"http": normalized_proxy, "https": normalized_proxy}
            logger.info(f"Requests с прокси: {normalized_proxy[:50]}...")
        else:
            logger.warning("Не удалось нормализовать прокси URL")
    
    try:
        logger.info(f"🌐 Requests: парсим {url}")
        
        # Создаем сессию
        session = requests.Session()
        session.headers.update(FULL_BROWSER_HEADERS)
        
        # Добавляем cookies для первого визита
        try:
            # Делаем предварительный запрос для получения cookies
            domain = urlparse(url).netloc
            if domain:
                session.get(f"https://{domain}", timeout=5, allow_redirects=True)
                logger.info(f"Получены cookies для {domain}")
        except:
            pass  # Не критично
        
        # Основной запрос
        start_time = time.time()
        response = session.get(url, proxies=proxies, timeout=25, allow_redirects=True)
        elapsed = time.time() - start_time
        
        logger.info(f"Requests ответил за {elapsed:.2f} сек, статус: {response.status_code}")
        
        # Проверяем статус
        if response.status_code != 200:
            logger.warning(f"Requests: HTTP {response.status_code}")
            
            # Пробуем получить текст даже при ошибке
            html = response.text
            logger.info(f"Получено {len(html)} символов при статусе {response.status_code}")
            
            # Но все равно считаем ошибкой
            raise requests.exceptions.HTTPError(f"HTTP {response.status_code}")
        
        html = response.text
        logger.info(f"Получено {len(html)} символов HTML")
        
        # Проверяем на блокировку
        if _is_blocked_page(html):
            logger.warning("Requests: обнаружена блокировка")
            raise ValueError("Обнаружена блокировка или капча")
        
        # Проверяем минимальную длину
        if len(html) < 300:  # Уменьшил порог для теста
            logger.warning(f"Requests: короткий ответ ({len(html)} символов)")
            # Но не падаем сразу
        
        logger.info(f"✅ Requests успешен для {url}")
        return html
        
    except requests.exceptions.Timeout:
        logger.error(f"❌ Таймаут при запросе {url}")
        raise ValueError("Сайт не отвежает (таймаут)")
    except requests.exceptions.ProxyError as e:
        logger.error(f"❌ Ошибка прокси: {e}")
        raise ValueError("Ошибка подключения через прокси")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка запроса к {url}: {e}")
        raise ValueError(f"Ошибка при запросе: {str(e)}")
    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка в requests: {e}", exc_info=True)
        raise ValueError(f"Ошибка обработки: {str(e)}")

# =========================
# УЛУЧШЕННЫЙ УМНЫЙ ПАРСЕР
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Улучшенный парсер с отладкой и надежными fallback'ами
    """
    
    # Проверяем URL
    if not url or not looks_like_url(url):
        logger.error(f"Некорректный URL: {url}")
        raise ValueError("Некорректная ссылка")
    
    logger.info(f"🚀 Начинаем парсинг: {url}")
    logger.info(f"PROXY_URL доступен: {'Да' if PROXY_URL else 'Нет'}")
    logger.info(f"CLOUDSCRAPER_ENABLED: {CLOUDSCRAPER_ENABLED}")
    logger.info(f"SELENIUM_ENABLED: {SELENIUM_ENABLED}")
    
    # Сохраняем ваш существующий Selenium код
    def parse_with_selenium_existing(url: str, proxy_url: Optional[str] = None) -> str:
        """Ваш существующий Selenium код"""
        if not SELENIUM_ENABLED:
            raise ValueError("Selenium отключен")
        
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            from webdriver_manager.chrome import ChromeDriverManager
            
            logger.info(f"🤖 Selenium: начинаем {url}")
            
            options = Options()
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            
            if SELENIUM_HEADLESS:
                options.add_argument('--headless=new')
                logger.info("Selenium в headless режиме")
            
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument(f'--user-agent={FULL_BROWSER_HEADERS["User-Agent"]}')
            
            if proxy_url:
                proxy_for_selenium = proxy_url
                if proxy_for_selenium.startswith('http://'):
                    proxy_for_selenium = proxy_for_selenium[7:]
                if '@' in proxy_for_selenium:
                    proxy_for_selenium = proxy_for_selenium.split('@')[1]
                options.add_argument(f'--proxy-server={proxy_for_selenium}')
                logger.info(f"Selenium с прокси: {proxy_for_selenium}")
            
            # Настройки для Render
            os.environ['WDM_LOG_LEVEL'] = '0'
            os.environ['WDM_LOCAL'] = '1'
            
            logger.info("Устанавливаем ChromeDriver...")
            service = Service(
                ChromeDriverManager(
                    cache_valid_range=30,
                    path="/tmp/chromedriver"
                ).install()
            )
            
            logger.info("Создаем драйвер...")
            driver = webdriver.Chrome(service=service, options=options)
            
            # Скрываем автоматизацию
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            
            logger.info(f"Открываем {url}")
            driver.get(url)
            
            # Ожидание
            wait_time = random.uniform(3, 6)
            logger.info(f"Ждем {wait_time:.1f} секунд...")
            time.sleep(wait_time)
            
            # Прокрутка
            logger.info("Прокручиваем страницу...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.5);")
            time.sleep(random.uniform(1, 2))
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(random.uniform(1, 2))
            
            # Получаем HTML
            html = driver.page_source
            logger.info(f"Selenium получил {len(html)} символов")
            
            # Закрываем драйвер
            driver.quit()
            
            # Проверяем на блокировку
            if _is_blocked_page(html):
                logger.warning("Selenium: обнаружена блокировка")
                raise ValueError("Обнаружена блокировка")
            
            logger.info(f"✅ Selenium успешен для {url}")
            return html
            
        except Exception as e:
            logger.error(f"❌ Selenium ошибка: {str(e)}", exc_info=True)
            raise ValueError(f"Selenium не смог обработать: {str(e)}")
    
    # Определяем порядок методов
    methods_to_try = []
    
    # 1. Cloudscraper (самый легкий и эффективный)
    if CLOUDSCRAPER_ENABLED:
        methods_to_try.append(("Cloudscraper", lambda: parse_with_cloudscraper(url, PROXY_URL)))
        logger.info("Добавлен Cloudscraper в методы")
    
    # 2. Undetected ChromeDriver (лучший для капчи)
    methods_to_try.append(("Undetected ChromeDriver", lambda: parse_with_undetected_chromedriver(url, PROXY_URL)))
    logger.info("Добавлен Undetected ChromeDriver в методы")
    
    # 3. Selenium (ваш текущий)
    if SELENIUM_ENABLED:
        methods_to_try.append(("Selenium", lambda: parse_with_selenium_existing(url, PROXY_URL)))
        logger.info("Добавлен Selenium в методы")
    
    # 4. Requests с прокси
    if PROXY_URL:
        methods_to_try.append(("Requests с прокси", lambda: fetch_html_via_requests(url, PROXY_URL)))
        logger.info("Добавлен Requests с прокси в методы")
    
    # 5. Requests без прокси (последний шанс)
    methods_to_try.append(("Requests без прокси", lambda: fetch_html_via_requests(url, None)))
    logger.info("Добавлен Requests без прокси в методы")
    
    logger.info(f"Всего методов для теста: {len(methods_to_try)}")
    
    # Пробуем все методы
    last_error = None
    
    for method_name, parser_func in methods_to_try:
        try:
            logger.info(f"🔄 Пробуем {method_name} для {url}")
            
            # Парсим HTML
            html = parser_func()
            
            # Извлекаем текст
            text = html_to_text(html)
            logger.info(f"{method_name}: извлечено {len(text)} символов текста")
            
            # Проверяем качество текста
            if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                logger.info(f"✅ {method_name} успешен! Текст: {len(text)} символов")
                return text
            else:
                logger.warning(f"⚠️ {method_name}: мало текста ({len(text) if text else 0} символов)")
                # Пробуем следующий метод
                continue
                
        except ImportError as e:
            logger.warning(f"⚠️ {method_name} не установлен: {e}")
            continue
        except ValueError as e:
            logger.warning(f"⚠️ {method_name} не сработал: {e}")
            last_error = e
            continue
        except Exception as e:
            logger.warning(f"⚠️ {method_name} вызвал исключение: {e}")
            last_error = e
            continue
    
    # Все методы не сработали
    logger.error(f"❌ Все методы парсинга не сработали для {url}")
    logger.error(f"Последняя ошибка: {last_error}")
    
    # Даем более информативное сообщение
    if last_error and "таймаут" in str(last_error).lower():
        error_msg = "Сайт не отвечает слишком долго. Попробуйте позже или другую ссылку."
    elif last_error and ("403" in str(last_error) or "заблокировал" in str(last_error)):
        error_msg = "Сайт заблокировал доступ. Возможно, требуется VPN или другой браузер."
    elif last_error and "прокси" in str(last_error).lower():
        error_msg = "Проблема с подключением через прокси. Попробуйте другую ссылку."
    else:
        error_msg = GENERIC_VACANCY_ERROR_MSG
    
    raise ValueError(error_msg)
