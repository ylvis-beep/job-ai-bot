import logging
import re
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PyPDF2 import PdfReader

from config import PROXY_URL, MIN_MEANINGFUL_TEXT_LENGTH

logger = logging.getLogger(__name__)

# Единое "человеческое" сообщение пользователю,
# когда не удалось получить вакансию по ссылке
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
        # Уже «человеческая» ошибка, просто прокидываем выше
        raise

    except Exception as e:
        # В лог пишем подробности, пользователю — аккуратное сообщение
        logger.error(f"❌ Ошибка чтения PDF: {e}", exc_info=True)
        raise ValueError(
            "Не удалось прочитать PDF файл. "
            "Убедитесь, что файл не повреждён и содержит текст, а не только изображения."
        )


# =========================
# ЛОГИКА РАБОТЫ СО ССЫЛКАМИ
# =========================

URL_REGEX = re.compile(
    r"^(https?://)?([a-z0-9.-]+\.[a-z]{2,})(/.*)?$",
    re.IGNORECASE,
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
        soup = BeautifulSoup(html, "html.parser")

        # Удаляем ненужные элементы
        for element in soup(["script", "style", "nav", "footer", "header"]):
            element.decompose()

        text = soup.get_text(separator="\n")
        return clean_text(text)

    except Exception as e:
        logger.error(f"❌ Ошибка извлечения текста из HTML: {e}", exc_info=True)
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


def _normalize_proxy_url(raw: str) -> str:
    """
    Приводит PROXY_URL к виду, который понимает requests.
    Поддерживает форматы:
    - http://user:pass@host:port
    - socks5://user:pass@host:port
    - host:port@user:pass      (как даёт proxy.market)
    - user:pass@host:port
    - host:port
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    # Уже есть схема (http://, https://, socks5:// и т.п.)
    if re.match(r"^[a-zA-Z0-9+.-]+://", raw):
        return raw

    # Если есть логин/пароль и хост, но в произвольном порядке
    if "@" in raw:
        left, right = raw.split("@", 1)

        def looks_like_host_port(part: str) -> bool:
            # Примитивная эвристика: в хосте обычно есть точка и буквы
            host, _, _ = part.partition(":")
            return "." in host and re.search(r"[a-zA-Z]", host) is not None

        if looks_like_host_port(left):
            host_port = left
            creds = right
        else:
            creds = left
            host_port = right

        return f"http://{creds}@{host_port}"

    # Просто host:port — без авторизации
    return f"http://{raw}"


def fetch_html_via_proxy(url: str) -> str:
    """
    Запрос HTML через RU-прокси.
    PROXY_URL может быть:
    - http://user:pass@host:port
    - host:port@user:pass (как у proxy.market)
    - и др. варианты, описанные в _normalize_proxy_url.
    """
    if not PROXY_URL:
        raise ValueError(
            "PROXY_URL не задан. "
            "Задайте переменную окружения PROXY_URL с адресом прокси, "
            "например: pool.proxy.market:10000@login:password"
        )

    proxy_url = _normalize_proxy_url(PROXY_URL)
    proxies = {
        "http": proxy_url,
        "https": proxy_url,
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
            # пользователю говорим просто «не удалось получить текст вакансии»
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        if len(html) < 500:
            logger.warning(f"⚠️ Очень короткий ответ ({len(html)} символов)")
            raise ValueError(GENERIC_VACANCY_ERROR_MSG)

        return html

    except ValueError:
        # Уже пользовательская ошибка, просто пробрасываем
        raise

    except requests.exceptions.Timeout:
        logger.error(f"❌ Таймаут при запросе {url}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except requests.exceptions.ProxyError as e:
        # Здесь как раз будут ошибки вида 407 Proxy Authentication Required
        logger.error(f"❌ Ошибка подключения к прокси: {e}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except requests.RequestException as e:
        # Любые прочие сетевые/HTTP-ошибки
        logger.error(f"❌ HTTP/сетевой сбой при запросе {url}: {e}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    except Exception as e:
        logger.error(f"❌ Непредвиденная ошибка при запросе {url}: {e}", exc_info=True)
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)


def fetch_url_text_via_proxy(url: str) -> str:
    """
    Высокоуровневая функция: получаем HTML через прокси,
    вытаскиваем текст и проверяем, что он достаточно длинный.
    """
    html = fetch_html_via_proxy(url)
    text = html_to_text(html)

    if not text or len(text) < MIN_MEANINGFUL_TEXT_LENGTH:
        logger.warning(
            f"⚠️ Недостаточный объём текста после парсинга ({len(text)} символов)"
        )
        raise ValueError(GENERIC_VACANCY_ERROR_MSG)

    logger.info(f"✅ Ссылка обработана, получено {len(text)} символов текста")
    return text
