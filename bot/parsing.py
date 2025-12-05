import cloudscraper
from fake_useragent import UserAgent
import browser_cookie3
import json

# =========================
# CLOUDSCRAPER - ОБХОД CLOUDFLARE (из статьи)
# =========================

def create_cloudscraper_session(proxy_url: Optional[str] = None):
    """
    Создание сессии cloudscraper для обхода Cloudflare.
    Описан в статье как один из лучших методов.
    """
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False,
                'desktop': True,
            }
        )
        
        # Устанавливаем прокси если есть
        if proxy_url:
            normalized_proxy = _normalize_proxy_url(proxy_url)
            scraper.proxies = {
                'http': normalized_proxy,
                'https': normalized_proxy
            }
        
        # Полный набор заголовков как у реального браузера
        ua = UserAgent()
        headers = {
            'User-Agent': ua.chrome,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'TE': 'Trailers',
        }
        
        scraper.headers.update(headers)
        
        # Добавляем cookies от Chrome (если доступны)
        try:
            chrome_cookies = browser_cookie3.chrome(domain_name='.tochka.com')
            for cookie in chrome_cookies:
                scraper.cookies.set_cookie(cookie)
        except:
            pass
        
        return scraper
        
    except Exception as e:
        logger.error(f"❌ Ошибка создания cloudscraper: {e}")
        raise

def parse_with_cloudscraper(url: str, proxy_url: Optional[str] = None) -> str:
    """
    Парсинг через Cloudscraper - основной метод из статьи.
    Обходит Cloudflare, DDoS-GUARD и подобные защиты.
    """
    try:
        logger.info(f"☁️ Cloudscraper: парсим {url}")
        
        scraper = create_cloudscraper_session(proxy_url)
        
        # Имитируем человеческое поведение
        time.sleep(random.uniform(1, 3))
        
        response = scraper.get(url, timeout=30)
        
        if response.status_code == 403:
            logger.warning("⚠️ Cloudscraper получил 403 - попробуем с другими настройками")
            # Пробуем с другими заголовками
            return _retry_with_different_headers(url, proxy_url)
        
        if response.status_code != 200:
            raise ValueError(f"Cloudscraper ошибка: {response.status_code}")
        
        html = response.text
        
        if detect_captcha(html):
            logger.warning("⚠️ Cloudscraper: обнаружена капча")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)
        
        logger.info(f"✅ Cloudscraper успешно: {len(html)} символов")
        return html
        
    except Exception as e:
        logger.error(f"❌ Cloudscraper ошибка: {e}")
        raise

def _retry_with_different_headers(url: str, proxy_url: Optional[str] = None) -> str:
    """Повторная попытка с другими заголовками"""
    try:
        scraper = cloudscraper.create_scraper(
            interpreter='nodejs',  # Пробуем Node.js интерпретатор
            delay=10  # Задержка как у человека
        )
        
        if proxy_url:
            normalized_proxy = _normalize_proxy_url(proxy_url)
            scraper.proxies = {'http': normalized_proxy, 'https': normalized_proxy}
        
        # Альтернативные заголовки
        ua = UserAgent()
        headers = {
            'User-Agent': ua.firefox,  # Пробуем Firefox
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        }
        
        scraper.headers.update(headers)
        time.sleep(random.uniform(2, 4))
        
        response = scraper.get(url, timeout=30)
        return response.text if response.status_code == 200 else ""
        
    except Exception as e:
        logger.error(f"❌ Retry with headers failed: {e}")
        raise

# =========================
# УЛУЧШЕННЫЙ REQUESTS С БРАУЗЕРНЫМИ ЗАГОЛОВКАМИ
# =========================

def get_browser_headers() -> dict:
    """Полный набор заголовков как у реального браузера (из статьи)"""
    ua = UserAgent()
    
    return {
        'User-Agent': ua.chrome,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Cache-Control': 'max-age=0',
        'sec-ch-ua': '"Google Chrome";v="119", "Chromium";v="119", "Not?A_Brand";v="24"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'TE': 'trailers',
    }

def create_requests_session_with_cookies(proxy_url: Optional[str] = None):
    """
    Создание сессии requests с сохранением cookies и браузерными заголовками.
    Важно для сайтов, которые следят за сессиями.
    """
    session = requests.Session()
    
    # Полные браузерные заголовки
    session.headers.update(get_browser_headers())
    
    # Прокси
    if proxy_url:
        normalized_proxy = _normalize_proxy_url(proxy_url)
        session.proxies = {
            'http': normalized_proxy,
            'https': normalized_proxy
        }
    
    # Сохраняем cookies между запросами
    session.cookies.update(requests.cookies.RequestsCookieJar())
    
    return session

# =========================
# ОБНОВЛЕННЫЙ УМНЫЙ ПАРСЕР С ПРИОРИТЕТАМИ ИЗ СТАТЬИ
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Умный парсер с приоритетами по методологии из статьи:
    1. Cloudscraper + прокси (лучший для Cloudflare)
    2. Selenium + прокси (для JS-сайтов)
    3. Requests с браузерными заголовками + прокси
    4. Requests без прокси
    """
    methods_to_try = []
    
    # 1. Cloudscraper + прокси (ОСНОВНОЙ из статьи)
    if PROXY_URL:
        methods_to_try.append(
            ("Cloudscraper с прокси", 
             lambda: parse_with_cloudscraper(url, PROXY_URL))
        )
    
    # 2. Selenium + прокси (для сложных JS-сайтов)
    if SELENIUM_ENABLED and PROXY_URL:
        methods_to_try.append(
            ("Selenium с прокси", 
             lambda: parse_with_selenium(url, PROXY_URL))
        )
    
    # 3. Requests с полными браузерными заголовками + прокси
    if PROXY_URL:
        methods_to_try.append(
            ("Requests с браузерными заголовками", 
             lambda: _parse_with_browser_headers(url, PROXY_URL))
        )
    
    # 4. Requests без прокси (последний вариант)
    methods_to_try.append(
        ("Requests без прокси", 
         lambda: _parse_with_browser_headers(url, None))
    )
    
    last_error = None
    
    for method_name, parser_func in methods_to_try:
        try:
            logger.info(f"🔄 Пробуем {method_name} для {url}")
            
            # Случайная задержка между попытками (имитация человека)
            if method_name != methods_to_try[0][0]:  # Не для первой попытки
                delay = random.uniform(2, 5)
                time.sleep(delay)
            
            html = parser_func()
            text = html_to_text(html)
            
            if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                logger.info(f"✅ {method_name} успешен: {len(text)} символов")
                return text
            else:
                logger.warning(f"⚠️ {method_name}: мало текста")
                last_error = ValueError(GENERIC_VACANCY_ERROR_MSG)
                
        except Exception as e:
            logger.warning(f"⚠️ {method_name} не сработал: {e}")
            last_error = e
            continue
    
    # Все методы не сработали
    logger.error(f"❌ Все методы парсинга не сработали для {url}")
    raise ValueError(GENERIC_VACANCY_ERROR_MSG)

def _parse_with_browser_headers(url: str, proxy_url: Optional[str] = None) -> str:
    """Парсинг с полными браузерными заголовками"""
    session = create_requests_session_with_cookies(proxy_url)
    
    try:
        # Первый запрос для установки cookies
        session.get('https://google.com', timeout=5)
        time.sleep(random.uniform(1, 2))
        
        # Основной запрос
        response = session.get(url, timeout=20)
        response.raise_for_status()
        
        # Проверяем на капчу
        if detect_captcha(response.text):
            raise ValueError("Обнаружена капча")
        
        return response.text
        
    except Exception as e:
        logger.error(f"❌ Browser headers parse failed: {e}")
        raise

# =========================
# ДОПОЛНИТЕЛЬНО: ОБРАБОТКА 403 И КАПЧИ
# =========================

def is_blocked_response(html: str, status_code: int) -> bool:
    """Определяем, заблокировал ли нас сайт"""
    if status_code == 403:
        return True
    
    if not html:
        return True
    
    html_lower = html.lower()
    
    block_indicators = [
        "access denied",
        "forbidden",
        "blocked",
        "bot detected",
        "security check",
        "работа временно приостановлена",
        "доступ запрещён",
        "ваш ip-адрес заблокирован",
    ]
    
    return any(indicator in html_lower for indicator in block_indicators)

def rotate_user_agent():
    """Ротация User-Agent для обхода блокировок"""
    ua = UserAgent()
    return {
        'chrome': ua.chrome,
        'firefox': ua.firefox,
        'safari': ua.safari,
        'random': ua.random,
    }
