# parsing.py
import logging
import re
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader

from config import PROXY_URL, MIN_MEANINGFUL_TEXT_LENGTH

logger = logging.getLogger(__name__)

# =========================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ТЕКСТА
# =========================

def clean_text(raw: str) -> str:
    """Очистка текста: убираем лишние пробелы и пустые строки."""
    if not raw:
        return ""

    text = raw.replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(lines)
    text = re.sub(r'\n{3,}', '\n\n', text)

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

    except Exception as e:
        logger.error(f"❌ Ошибка чтения PDF: {str(e)}", exc_info=True)
        raise ValueError(f"Не удалось прочитать PDF файл: {str(e)}")


# =========================
# ЛОГИКА РАБОТЫ СО ССЫЛКАМИ
# =========================

URL_REGEX = re.compile(
    r'^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$',
    re.IGNORECASE
)


def looks_like_url(text: str) -> bool:
    """
    Мягкая проверка – похоже ли на URL.
    Поддерживает варианты:
    - https://hh.ru/vacancy/123
    - http://example.com
    - hh.ru/vacancy/123
    - www.hh.ru/vacancy/123
    """
    if not text:
        return False
    text = text.strip()
    return bool(URL_REGEX.match(text))


def normalize_url(text: str) -> str:
    """
    Гарантирует, что URL начинается с http/https.
    'hh.ru/vacancy/123' -> 'https://hh.ru/vacancy/123'
    """
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        return "https://" + text
    return text


def html_to_text(html: str) -> str:
    """Извлечение текста из HTML."""
    try:
        soup = BeautifulSoup(html, 'html.parser')

        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        text = soup.get_text(separator='\n')
        return clean_text(text)

    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {str(e)}", exc_info=True)
        return ""


# =========================
# ПРОКСИ-ПАРСИНГ ЧЕРЕЗ RU-ПРОКСИ
# =========================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}


def fetch_html_via_proxy(url: str) -> str:
    """
    Запрос HTML через RU-прокси (например, Bright Data).
    PROXY_URL задаётся в переменной окружения, например:
    PROXY_URL=http://user:pass@brd.superproxy.io:33335
    """
    if not PROXY_URL:
        raise ValueError(
            "❌ PROXY_URL не задан.\n"
            "Задайте переменную окружения PROXY_URL с адресом прокси, "
            "например: http://user:pass@host:port"
        )

    proxies = {
        "http": PROXY_URL,
        "https": PROXY_URL,
    }

    try:
        logger.info(f"🔗 Парсим ссылку через RU-прокси: {url}")

        resp = requests.get(
            url,
            headers=HEADERS,
            proxies=proxies,
            timeout=30,
        )

        logger.info(f"Прокси ответил со статусом: {resp.status_code}")

        resp.raise_for_status()
        html = resp.text

        # Простейшая проверка на капчу/блокировку
        lower = html.lower()
        if any(m in lower for m in ["captcha", "access denied", "are you human"]):
            logger.warning("⚠️ Похоже на страницу с капчей/блокировкой")
            # не обязательно сразу падать, но для начала можно так:
            raise ValueError("Сайт вернул капчу/блокировку")

        if len(html) < 500:
            logger.warning(f"⚠️ Очень короткий ответ ({len(html)} символов)")
            raise ValueError("Не удалось получить достаточный контент с сайта.")

        return html

    except requests.exceptions.Timeout:
        logger.error(f"❌ Таймаут при запросе {url}", exc_info=True)
        raise TimeoutError("Сайт не отвечает. Попробуйте позже.")

    except Exception as e:
        logger.error(f"❌ Ошибка при запросе {url}: {str(e)}", exc_info=True)
        raise ValueError(f"Ошибка при обработке ссылки: {str(e)}")


def fetch_url_text_via_proxy(url: str) -> str:
    """
    Высокоуровневая функция: получаем HTML через прокси,
    вытаскиваем текст и проверяем, что он достаточно длинный.
    """
    html = fetch_html_via_proxy(url)
    text = html_to_text(html)

    if not text or len(text) < MIN_MEANINGFUL_TEXT_LENGTH:
        raise ValueError(
            "Не удалось получить текст с сайта.\n"
            "Попробуйте скопировать текст вакансии вручную."
        )

    logger.info(f"✅ Ссылка обработана, получено {len(text)} символов текста")
    return text

