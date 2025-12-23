import logging
import re
import time
import random
import os
from io import BytesIO
from typing import Optional, Tuple, List
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader
from fake_useragent import UserAgent

from config import (
    PROXY_URL,
    MIN_MEANINGFUL_TEXT_LENGTH,
    CLOUDSCRAPER_ENABLED,
    FORCE_MOBILE_HH,
    RETRY_COUNT,
    IS_RENDER,
)

logger = logging.getLogger(__name__)

# =========================
# ИНИЦИАЛИЗАЦИЯ ПО СТАТЬЕ
# =========================
ua = UserAgent(browsers=["chrome", "edge", "firefox"], os=["windows", "linux", "macos"])


# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ТЕКСТА / PDF
# =========================

def clean_text(raw: str) -> str:
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

    except Exception as e:
        logger.error(f"❌ Ошибка чтения PDF: {e}", exc_info=True)
        raise ValueError("Не удалось прочитать PDF файл.")


def looks_like_url(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    url_regex = re.compile(r"^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$", re.IGNORECASE)
    return bool(url_regex.match(text))


def normalize_url(text: str) -> str:
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text


def html_to_text(html: str) -> str:
    if not html:
        return ""

    try:
        soup = BeautifulSoup(html, "lxml")

        # Убираем мусорные элементы
        for element in soup(
            ["script", "style", "nav", "footer", "header", "aside", "form", "iframe", "button"]
        ):
            element.decompose()

        # Специальная чистка для hh.ru
        if "hh.ru" in html.lower():
            for element in soup.find_all(
                class_=re.compile(
                    r"(vacancy-serp-item|sidebar|related|similar|recommended|bloko-column)"
                )
            ):
                element.decompose()

        text = soup.get_text(separator="\n", strip=True)
        text = clean_text(text)

        # Убираем совсем короткие строки
        lines = [line for line in text.split("\n") if len(line.strip()) > 5]
        text = "\n".join(lines)

        return text

    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста: {e}")
        return ""


# =========================
# ПРОКСИ
# =========================

def _format_proxy_for_requests(proxy_url: str) -> Optional[dict]:
    if not proxy_url:
        return None

    proxy = proxy_url.strip()

    if proxy.startswith(("http://", "https://", "socks5://")):
        return {"http": proxy, "https": proxy}

    if "@" in proxy:
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    else:
        return {"http": f"http://{proxy}", "https": f"http://{proxy}"}


# =========================
# МЕТОД 1: ПРОСТОЙ ЗАПРОС
# =========================

def _try_simple_request(
    url: str,
    use_proxy: bool = True,
    force_mobile: bool = False,
) -> Tuple[bool, str, Optional[str]]:
    """Улучшенный requests-запрос с динамическим User-Agent"""

    proxies = None
    if use_proxy and PROXY_URL:
        proxies = _format_proxy_for_requests(PROXY_URL)

    try:
        # Динамический User-Agent
        user_agent = ua.random

        # Для HH.ru мобильная версия
        if force_mobile and "hh.ru" in url and not url.startswith(("https://m.hh.ru", "http://m.hh.ru")):
            url = url.replace("https://hh.ru", "https://m.hh.ru")
            url = url.replace("http://hh.ru", "http://m.hh.ru")
            user_agent = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
            "DNT": "1",
        }

        session = requests.Session()
        session.headers.update(headers)

        # Куки, чтобы притвориться живым пользователем
        session.cookies.update(
            {
                "accept": "1",
                "force_cookie_consent": "true",
            }
        )

        # Лёгкая задержка
        time.sleep(random.uniform(1, 3))

        response = session.get(
            url,
            proxies=proxies,
            timeout=15,
            allow_redirects=True,
            verify=False,
        )

        logger.info(f"Запрос: статус {response.status_code}, размер {len(response.text)}")

        html = response.text
        html_lower = html.lower()

        has_captcha = any(
            x in html_lower
            for x in [
                "captcha",
                "cloudflare",
                "access denied",
                "ddos-guard",
                "recaptcha",
                "hcaptcha",
                "подтвердите что вы не робот",
            ]
        )

        if response.status_code == 200 and not has_captcha and len(html) > 1000:
            return True, html, None
        else:
            if has_captcha:
                return False, html, "Капча/блокировка"
            elif response.status_code == 403:
                return False, html, "403 Forbidden"
            elif response.status_code == 429:
                return False, html, "429 Too Many Requests"
            else:
                return False, html, f"HTTP {response.status_code}"

    except Exception as e:
        logger.error(f"❌ Ошибка запроса: {e}")
        return False, "", str(e)


# =========================
# МЕТОД 2: CLOUDSCRAPER
# =========================

def _try_cloudscraper(url: str) -> Tuple[bool, str, Optional[str]]:
    """Cloudscraper как в статье"""
    if not CLOUDSCRAPER_ENABLED:
        return False, "", "Cloudscraper отключен"

    try:
        import cloudscraper

        logger.info(f"🔄 Cloudscraper для {url}")

        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=10,
        )

        proxies = None
        if PROXY_URL:
            proxies = _format_proxy_for_requests(PROXY_URL)

        user_agent = ua.random

        # Для HH.ru мобильная версия
        if FORCE_MOBILE_HH and "hh.ru" in url and not url.startswith(("https://m.hh.ru", "http://m.hh.ru")):
            url = url.replace("https://hh.ru", "https://m.hh.ru")
            url = url.replace("http://hh.ru", "http://m.hh.ru")
            user_agent = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                "Mobile/15E148 Safari/604.1"
            )

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        response = scraper.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=30,
        )

        if response.status_code == 200:
            html = response.text

            if len(html) > 1000 and "captcha" not in html.lower():
                logger.info("✅ Cloudscraper УСПЕШЕН!")
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
# ПОИСК БИНАРНИКА CHROME
# =========================

def _detect_chrome_binary() -> Optional[str]:
    """
    Пытаемся найти бинарник Chrome:
    - через переменную окружения CHROME_BINARY_PATH
    - через стандартные пути (в т.ч. Render c dpkg -x)
    """
    env_path = os.getenv("CHROME_BINARY_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates: List[str] = [
        "/opt/render/project/.render/chrome/opt/google/chrome/google-chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path

    return None


# =========================
# МЕТОД 3: UNDETECTED CHROMEDRIVER (Selenium + Chrome)
# =========================

def _try_undetected_chromedriver(url: str) -> Tuple[bool, str, Optional[str]]:
    """Undetected ChromeDriver - ключевой метод Selenium/Chrome"""
    try:
        import undetected_chromedriver as uc

        logger.info(f"3. Undetected ChromeDriver для {url}")

        options = uc.ChromeOptions()

        # Бинарник Chrome
        chrome_binary = _detect_chrome_binary()
        if chrome_binary:
            logger.info(f"Используем Chrome binary: {chrome_binary}")
            options.binary_location = chrome_binary

        headless_env = os.getenv("SELENIUM_HEADLESS", "true").lower() == "true"
        if headless_env or IS_RENDER:
            options.add_argument("--headless=new")

        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-features=IsolateOrigins,site-per-process")
        options.add_argument("--disable-logging")
        options.add_argument("--log-level=3")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-notifications")

        # Динамический User-Agent
        user_agent = ua.random
        options.add_argument(f"--user-agent={user_agent}")

        # Прокси для браузера
        if PROXY_URL:
            proxy = PROXY_URL.strip()
            if proxy.startswith("http://"):
                proxy = proxy[7:]
            elif proxy.startswith("https://"):
                proxy = proxy[8:]
            if "@" in proxy:
                proxy = proxy.split("@")[-1]
            options.add_argument(f"--proxy-server={proxy}")

        try:
            driver = uc.Chrome(
                options=options,
                version_main=131,  # под актуальный Chrome
                suppress_welcome=True,
            )

            try:
                # Stealth
                driver.execute_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                driver.execute_script(
                    "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]})"
                )
                driver.execute_script(
                    "Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']})"
                )

                # Мобильная версия HH
                if FORCE_MOBILE_HH and "hh.ru" in url and not url.startswith(
                    ("https://m.hh.ru", "http://m.hh.ru")
                ):
                    url = url.replace("https://hh.ru", "https://m.hh.ru")
                    url = url.replace("http://hh.ru", "http://m.hh.ru")

                driver.get(url)

                # Имитация поведения
                time.sleep(random.uniform(2, 4))

                scroll_height = driver.execute_script("return document.body.scrollHeight")
                scroll_steps = random.randint(3, 6)
                for i in range(scroll_steps):
                    scroll_pos = int((i + 1) * (scroll_height / scroll_steps))
                    driver.execute_script(f"window.scrollTo(0, {scroll_pos});")
                    time.sleep(random.uniform(0.2, 0.5))

                time.sleep(random.uniform(1, 2))

                html = driver.page_source

                if len(html) < 1000:
                    return False, html, "Короткий ответ"

                if any(
                    x in html.lower()
                    for x in ["captcha", "cloudflare", "access denied"]
                ):
                    return False, html, "Капча/блокировка"

                logger.info("✅ Undetected ChromeDriver УСПЕШЕН!")
                return True, html, None

            except Exception as e:
                logger.error(f"Ошибка в ChromeDriver: {e}")
                return False, "", str(e)
            finally:
                try:
                    driver.quit()
                except Exception:
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
# ГЛАВНАЯ ФУНКЦИЯ ПАРСИНГА
# =========================

def fetch_url_text_via_proxy(url: str) -> str:
    """
    Главная функция парсинга:
    1. Cloudscraper
    2. Undetected ChromeDriver
    3. Простой запрос (мобильный)
    4. Простой запрос (десктоп)
    С учётом RETRY_COUNT.
    """
    logger.info(f"🚀 Парсинг вакансии: {url}")

    if not url or not looks_like_url(url):
        raise ValueError("Некорректная ссылка")

    url = normalize_url(url)

    methods = [
        ("Cloudscraper", lambda: _try_cloudscraper(url)),
        ("Undetected ChromeDriver", lambda: _try_undetected_chromedriver(url)),
        ("Простой запрос (мобильный)", lambda: _try_simple_request(url, force_mobile=True)),
        ("Простой запрос (десктоп)", lambda: _try_simple_request(url, force_mobile=False)),
    ]

    logger.info(f"Методы парсинга: {[m[0] for m in methods]}")

    last_error: Optional[object] = None
    attempts = max(1, RETRY_COUNT)

    for attempt in range(1, attempts + 1):
        logger.info(f"🔁 Попытка {attempt}/{attempts}")
        for method_name, method_func in methods:
            try:
                logger.info(f"🔄 Пробуем {method_name}...")
                success, html, error = method_func()

                if success:
                    text = html_to_text(html)

                    if text and len(text) >= MIN_MEANINGFUL_TEXT_LENGTH:
                        logger.info(
                            f"✅ {method_name} УСПЕШЕН! ({len(text)} символов текста)"
                        )
                        return text
                    else:
                        logger.warning(
                            f"⚠️ {method_name}: мало текста ({len(text)} символов)"
                        )
                        continue
                else:
                    logger.warning(f"⚠️ {method_name}: {error}")
                    last_error = error
                    continue

            except Exception as e:
                logger.warning(f"⚠️ {method_name}: исключение {e}")
                last_error = e
                continue

        # Небольшая пауза между ретраями
        time.sleep(random.uniform(1, 2))

    # Все методы не сработали — ОДНО аккуратное сообщение пользователю
    error_msg = (
        "Не удалось получить текст вакансии по ссылке.\n\n"
        "Пожалуйста:\n"
        "1. Откройте ссылку в браузере\n"
        "2. Скопируйте текст\n"
        "3. Отправьте его сюда"
    )

    raise ValueError(error_msg)


# =========================
# ФУНКЦИИ ДЛЯ БОТА (PDF + URL)
# =========================

def parse_resume_from_pdf(pdf_content: bytes) -> str:
    try:
        text = extract_text_from_pdf_bytes(pdf_content)
        if len(text) < 100:
            raise ValueError("Резюме слишком короткое")
        return text
    except Exception:
        raise ValueError("Не удалось прочитать резюме.")


def parse_vacancy_from_url(url: str) -> str:
    try:
        text = fetch_url_text_via_proxy(url)
        if len(text) < 200:
            raise ValueError("Текст вакансии слишком короткий")
        return text
    except ValueError as e:
        # Пользовательские сообщения пробрасываем как есть
        raise e
    except Exception:
        raise ValueError("Не удалось получить вакансию.")
