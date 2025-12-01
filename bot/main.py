import os
import logging
from typing import Any, Dict, List, cast
import re
import time
import random
import json
import asyncio
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from io import BytesIO
from PyPDF2 import PdfReader
from playwright.sync_api import sync_playwright

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize OpenAI client
openai_client = None


def get_browser_headers() -> dict:
    """Полные заголовки современного браузера."""
    user_agents = [
        # Chrome последние версии
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
        # Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    ]
    
    headers = {
        'User-Agent': random.choice(user_agents),
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
        'Pragma': 'no-cache',
    }
    
    # Добавляем заголовки для обхода Cloudflare
    headers.update({
        'sec-ch-ua': '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'DNT': '1',
        'Sec-GPC': '1',
    })
    
    return headers


def render_page_with_playwright(url: str,
                                headers: Dict[str, str],
                                timeout_ms: int = 15000) -> str:
    """Fetch fully rendered HTML with Playwright to bypass simple anti-bot walls."""
    logger.info(f"Playwright: rendering {url}")
    extra_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ["user-agent", "host"]
    }
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=headers.get("User-Agent"),
                extra_http_headers=extra_headers,
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_timeout(random.randint(500, 1200))
            html = page.content()
            context.close()
            browser.close()
            logger.info(f"Playwright: rendered {url} (len={len(html)})")
            return html
    except Exception as e:
        logger.info(f"Playwright render failed for {url}: {e}")
        return ""


def load_system_prompt() -> str:
    """Load the system prompt from system_prompt.txt if it exists."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "system_prompt.txt")
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "You are a helpful assistant."
    except Exception as e:
        logger.warning(f"Failed to load system_prompt.txt: {e}")
        return "You are a helpful assistant."


# =========================
# Парсинг текста / ссылок / PDF
# =========================

def clean_text(raw: str) -> str:
    """Приводит текст в аккуратный вид."""
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n")
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_url(text: str) -> bool:
    """Проверяем, похожа ли строка на URL."""
    if not text:
        return False
    text = text.strip()
    
    # Быстрая проверка
    if re.match(r'^https?://\S+$', text, re.IGNORECASE):
        return True
    if re.match(r'^www\.\S+\.\S+$', text, re.IGNORECASE):
        return True
    
    try:
        parsed = urlparse(text)
        return bool(parsed.netloc)
    except:
        return False


def extract_text_from_pdf_bytes(data: bytes) -> str:
    """Достаём текст из PDF по сырым байтам."""
    try:
        reader = PdfReader(BytesIO(data))
        pages_text: List[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages_text.append(page_text)
        return clean_text("\n\n".join(pages_text))
    except Exception as e:
        logger.error(f"Error while extracting text from PDF bytes: {e}")
        return ""


def try_smart_parsing(url: str) -> str:
    """
    Умный парсинг с обходом защиты.
    Для tochka.com и подобных сайтов используем специальную стратегию.
    """
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    
    # Специальная обработка для tochka.com
    if 'tochka.com' in domain:
        logger.info(f"Using special strategy for tochka.com")
        return _try_tochka_special(url)
    
    # Для других сайтов - общая стратегия
    return _try_general_parsing(url)


def _try_tochka_special(url: str) -> str:
    """Специальная стратегия для tochka.com."""
    
    # Для tochka.com пробуем получить данные через их возможный API или альтернативные пути
    headers = get_browser_headers()
    
    # Добавляем специфичные заголовки для tochka.com
    headers.update({
        'Referer': 'https://tochka.com/',
        'Origin': 'https://tochka.com',
        'Host': 'tochka.com',
    })
    
    # Try headless browser first to avoid 403 blocks
    logger.info("Playwright attempt for tochka.com URL")
    rendered_html = render_page_with_playwright(url, headers)
    if rendered_html:
        parsed = _parse_html_content(rendered_html, url)
        if parsed:
            return parsed
    else:
        logger.info("Playwright returned empty content for tochka.com")
    
    
    # Пробуем разные эндпоинты
    endpoints_to_try = [
        url,  # Оригинальный URL
        url + '.json',  # Возможный JSON endpoint
        url.replace('/hr/', '/api/vacancies/'),  # Возможный API путь
    ]
    
    for endpoint in endpoints_to_try:
        try:
            logger.info(f"Trying endpoint: {endpoint}")
            
            # Большая задержка для tochka.com
            time.sleep(random.uniform(3, 5))
            
            response = requests.get(
                endpoint,
                headers=headers,
                timeout=30,
                allow_redirects=True,
                verify=True
            )
            
            logger.info(f"Response status for {endpoint}: {response.status_code}")
            
            if response.status_code == 200:
                # Проверяем, не JSON ли это
                content_type = response.headers.get('Content-Type', '').lower()
                if 'json' in content_type:
                    try:
                        data = response.json()
                        return _parse_json_vacancy(data)
                    except:
                        pass
                
                # Пробуем парсить HTML
                return _parse_html_content(response.text, url)
                
        except Exception as e:
            logger.debug(f"Endpoint {endpoint} failed: {e}")
            continue
    
    # Если все эндпоинты не сработали, пробуем получить хотя бы заголовок
    try:
        # Пробуем получить только title страницы
        response = requests.head(url, headers=headers, timeout=10)
        return f"Ссылка на вакансию tochka.com: {url}"
    except:
        raise RuntimeError("TOCHKA_BLOCKED")


def _parse_json_vacancy(data: dict) -> str:
    """Парсинг JSON данных вакансии."""
    result = []
    
    # Пробуем разные возможные структуры
    if isinstance(data, dict):
        # Прямые поля
        for key in ['title', 'name', 'position']:
            if key in data and data[key]:
                result.append(f"Должность: {data[key]}")
                break
        
        # Описание
        for key in ['description', 'content', 'body', 'text']:
            if key in data and data[key]:
                result.append(f"Описание: {data[key]}")
                break
        
        # Требования
        for key in ['requirements', 'qualifications', 'skills', 'experience']:
            if key in data and data[key]:
                if isinstance(data[key], list):
                    result.append(f"{key}: " + ", ".join(str(x) for x in data[key]))
                else:
                    result.append(f"{key}: {data[key]}")
        
        # Обязанности
        for key in ['responsibilities', 'tasks', 'duties']:
            if key in data and data[key]:
                if isinstance(data[key], list):
                    result.append(f"{key}: " + ", ".join(str(x) for x in data[key]))
                else:
                    result.append(f"{key}: {data[key]}")
    
    return "\n".join(result) if result else ""


def _parse_html_content(html: str, url: str) -> str:
    """Парсинг HTML контента."""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        
        # Удаляем ненужные элементы
        for tag in soup(['script', 'style', 'noscript', 'iframe', 'svg']):
            tag.decompose()
        
        # Ищем title
        title = soup.find('title')
        title_text = title.get_text(strip=True) if title else ""
        
        # Ищем h1
        h1 = soup.find('h1')
        h1_text = h1.get_text(strip=True) if h1 else ""
        
        # Ищем основной контент
        content_selectors = [
            'main', 'article', 
            '[class*="vacancy"]', '[class*="job"]', '[class*="description"]',
            '[class*="content"]', '.container', '.wrapper'
        ]
        
        main_content = ""
        for selector in content_selectors:
            try:
                elements = soup.select(selector)
                for elem in elements:
                    text = elem.get_text(separator='\n', strip=True)
                    if len(text) > 200:
                        main_content = text
                        break
                if main_content:
                    break
            except:
                continue
        
        # Собираем результат
        result_parts = []
        if title_text:
            result_parts.append(f"Заголовок: {title_text}")
        if h1_text and h1_text != title_text:
            result_parts.append(f"H1: {h1_text}")
        if main_content:
            result_parts.append(f"Контент:\n{main_content}")
        
        if result_parts:
            return "\n\n".join(result_parts)
        else:
            # Минимальная информация
            return f"Вакансия: {title_text or 'Неизвестно'}\nURL: {url}"
            
    except Exception as e:
        logger.error(f"HTML parsing error: {e}")
        return f"Ссылка на вакансию: {url}"


def _try_general_parsing(url: str) -> str:
    """Общая стратегия парсинга для большинства сайтов."""
    session = requests.Session()
    
    # Полные заголовки браузера
    headers = get_browser_headers()
    
    # Добавляем реферер
    parsed = urlparse(url)
    headers['Referer'] = f"{parsed.scheme}://{parsed.netloc}/"
    logger.info(f"Playwright attempt for general URL: {url}")
    
    session.headers.update(headers)
    
    try:
        # Эмуляция поведения браузера
        # First try rendered page via Playwright to bypass JS/anti-bot gates
        html = render_page_with_playwright(url, headers)
        if html:
            parsed_html = _parse_html_content(html, url)
            if parsed_html:
                return parsed_html
        else:
            logger.info(f"Playwright returned empty content for general URL: {url}")

        
        # 1. Сначала на главную страницу
        try:
            home_url = f"{parsed.scheme}://{parsed.netloc}/"
            session.get(home_url, timeout=10)
            time.sleep(random.uniform(1, 2))
        except:
            pass
        
        # 2. Ждем перед основным запросом
        time.sleep(random.uniform(2, 3))
        
        # 3. Основной запрос
        response = session.get(url, timeout=25, allow_redirects=True)
        
        logger.info(f"General parsing for {url}: {response.status_code}")
        
        if response.status_code == 403:
            # Пробуем с другими заголовками
            time.sleep(3)
            alt_headers = headers.copy()
            alt_headers['User-Agent'] = get_browser_headers()['User-Agent']  # Новый User-Agent
            
            response = requests.get(url, headers=alt_headers, timeout=25)
            if response.status_code == 403:
                raise RuntimeError("ACCESS_DENIED")
        
        if response.status_code != 200:
            response.raise_for_status()
        
        # Парсим контент
        return _parse_html_content(response.text, url)
        
    except requests.HTTPError as e:
        if e.response.status_code == 403:
            raise RuntimeError("ACCESS_DENIED")
        elif e.response.status_code == 404:
            raise RuntimeError("NOT_FOUND")
        else:
            raise RuntimeError(f"HTTP_ERROR_{e.response.status_code}")
    except requests.RequestException as e:
        logger.error(f"Request error: {e}")
        raise RuntimeError("NETWORK_ERROR")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise RuntimeError("PARSING_ERROR")
    finally:
        session.close()


def extract_text_from_url(url: str) -> str:
    """Основная функция для извлечения текста из URL."""
    logger.info(f"Parsing URL: {url}")
    
    # Нормализуем URL
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        return try_smart_parsing(url)
    except RuntimeError as e:
        error_type = str(e)
        logger.warning(f"Smart parsing failed for {url}: {error_type}")
        
        # Для tochka.com всегда возвращаем ссылку как текст
        parsed = urlparse(url)
        if 'tochka.com' in parsed.netloc.lower():
            return f"Вакансия на tochka.com: {url}"
        
        raise e


def prepare_input_text(raw: str) -> str:
    """
    Универсальная функция для обработки ввода.
    ВСЕГДА возвращает текст, никогда не падает с ошибкой.
    """
    if not raw:
        return ""
    
    raw = raw.strip()
    
    # Если это не ссылка, просто чистим текст
    if not is_url(raw):
        return clean_text(raw)
    
    # Если это ссылка - пробуем парсить
    logger.info(f"Processing URL: {raw}")
    
    try:
        text = extract_text_from_url(raw)
        if text and len(text.strip()) > 50:
            return text
        else:
            # Если текст слишком короткий
            return clean_text(f"Ссылка на вакансию: {raw}")
            
    except RuntimeError as e:
        error_type = str(e)
        logger.info(f"Could not parse URL {raw}: {error_type}")
        
        # ВОЗВРАЩАЕМ ССЫЛКУ КАК ТЕКСТ В ЛЮБОМ СЛУЧАЕ
        parsed = urlparse(raw)
        if 'tochka.com' in parsed.netloc.lower():
            return clean_text(f"Вакансия на tochka.com: {raw}")
        elif 'hh.ru' in parsed.netloc.lower():
            return clean_text(f"Вакансия на hh.ru: {raw}")
        else:
            return clean_text(f"Ссылка на вакансию: {raw}")
            
    except Exception as e:
        logger.error(f"Unexpected error parsing URL {raw}: {e}")
        # Все равно возвращаем ссылку как текст
        return clean_text(f"Ссылка на вакансию: {raw}")


# =========================
# Telegram Bot Handlers (остаются без изменений)
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    message = update.message
    if message is None:
        logger.warning("Received /start update without message")
        return

    user = update.effective_user
    if user is None:
        await message.reply_text(
            "Привет! Я твой помощник в поиске работы и по сопроводительным письмам. "
            "Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
        )
        return

    await message.reply_html(
        rf"Привет {user.mention_html()}! Я твой помощник в поиске работы и по сопроводительным письмам. "
        rf"Отправь резюме или ссылку на него, а я подберу формулировки и соберу письма под нужные вакансии"
    )


async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    message = update.message
    if message is None:
        logger.warning("Received /help update without message")
        return

    help_text = """
Доступные команды:
/start - Запустить бота
/help - Показать это сообщение помощи
/update_resume - Загрузите новые файлы с описанием вашего опыта

Я помогу вам с:
- Анализом вакансий
- Сопоставлением с вашим резюме
- Составлением сопроводительных писем
- Подготовкой к собеседованиям

Просто отправьте мне:
1. Ваше резюме (текстом или PDF)
2. Ссылку на интересующую вакансию
3. Или описание вакансии текстом

📌 Совет: Для лучших результатов копируйте текст вакансий вручную.
    """
    await message.reply_text(help_text)


async def update_resume(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /update_resume command."""
    message = update.message
    if message is None:
        logger.warning("Received /update_resume update without message")
        return

    user_data = cast(Dict[str, Any], context.user_data)
    user_data['awaiting_resume'] = True
    user_data.pop('resume', None)
    await message.reply_text("Загрузите новые файлы с описанием вашего опыта")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle user messages with OpenAI."""
    message = update.message
    if message is None:
        logger.warning("Received text update without message in chat handler")
        return

    if not openai_client:
        await message.reply_text(
            "Sorry, OpenAI is not configured. Please set the OPENAI_API_KEY environment variable."
        )
        return

    user_data = cast(Dict[str, Any], context.user_data)

    # Определяем, откуда брать текст: PDF или текст
    user_message: str

    if message.document is not None:
        doc = message.document
        is_pdf = (
            doc.mime_type == "application/pdf"
            or (doc.file_name and doc.file_name.lower().endswith(".pdf"))
        )

        if not is_pdf:
            await message.reply_text(
                "Сейчас я умею обрабатывать только PDF-файлы и текстовые сообщения."
            )
            return

        try:
            file = await doc.get_file()
            bio = BytesIO()
            await file.download_to_memory(out=bio)
            pdf_bytes = bio.getvalue()
            extracted = extract_text_from_pdf_bytes(pdf_bytes)
            if not extracted:
                await message.reply_text(
                    "Не удалось извлечь текст из PDF. Попробуйте другой файл или отправьте текст."
                )
                return
            user_message = extracted
        except Exception as e:
            logger.error(f"Error while downloading/reading PDF: {e}")
            await message.reply_text(
                "Произошла ошибка при чтении PDF-файла. Попробуйте ещё раз или отправьте текст."
            )
            return

    else:  # текстовое сообщение
        raw_text = message.text
        if raw_text is None:
            await message.reply_text("Я могу обрабатывать только текст или PDF-файлы.")
            return
        
        # Показываем, что бот работает
        await message.chat.send_action(action="typing")
        
        # ВСЕГДА получаем текст, даже если это ссылка
        user_message = await asyncio.to_thread(prepare_input_text, raw_text)

    # Если ожидаем новое резюме после /update_resume — сохраняем его и не вызываем OpenAI
    if user_data.get("awaiting_resume"):
        user_data["resume"] = user_message
        user_data["awaiting_resume"] = False
        await message.reply_text(
            "✅ Спасибо! Я обновил информацию о вашем опыте.\n\n"
            "Теперь отправьте вакансию или вопрос, "
            "и я буду использовать это резюме для анализа."
        )
        return

    try:
        # Send typing action to show the bot is processing
        await message.chat.send_action(action="typing")

        system_prompt = load_system_prompt()

        # История диалога по пользователю/чату
        history = cast(List[Dict[str, str]], user_data.get("history", []))
        max_history_messages = 10

        messages: List[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": system_prompt
            }
        ]

        if history:
            messages.extend(history[-max_history_messages:])

        # Если сохранено резюме — добавляем его как отдельное сообщение-контекст
        resume = user_data.get("resume")
        if resume:
            messages.append({
                "role": "user",
                "content": (
                    "Это резюме пользователя. Используй его как основной контекст "
                    "при анализе вакансий, составлении таблиц совпадений и подготовке сопроводительных писем:\n\n"
                    f"{resume}"
                ),
            })

        # Текущее сообщение
        messages.append({
            "role": "user",
            "content": user_message
        })

        # Call OpenAI API
        response = openai_client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            max_completion_tokens=2048
        )

        ai_response = response.choices[0].message.content or (
            "Извините, не удалось сформировать ответ."
        )
        await message.reply_text(ai_response)

        # Обновляем историю: user → assistant
        if ai_response:
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": ai_response})
            user_data["history"] = history[-max_history_messages:]

    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}")
        await message.reply_text(
            "Произошла ошибка при обработке запроса. Пожалуйста, попробуйте ещё раз."
        )


async def error_handler(update: Update,
                        context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error(f"Update {update} caused error {context.error}")


def main() -> None:
    """Start the bot."""
    global openai_client

    # Get the bot token from environment variable
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        print(
            "ERROR: Please set your TELEGRAM_BOT_TOKEN environment variable."
        )
        print("You can get a token from @BotFather on Telegram.")
        return

    if not openai_api_key:
        logger.warning("OPENAI_API_KEY not found in environment variables!")
        print(
            "WARNING: OpenAI API key not set. The bot will run but AI features won't work."
        )
        print("Please set your OPENAI_API_KEY to enable AI responses.")
    else:
        # Initialize OpenAI client
        openai_client = OpenAI(api_key=openai_api_key)
        logger.info("OpenAI client initialized successfully")

    # Create the Application
    application = Application.builder().token(token).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("update_resume", update_resume))

    # Register message handler for текст + PDF
    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.Document.PDF) & ~filters.COMMAND,
            chat,
        )
    )

    # Register error handler
    application.add_error_handler(error_handler)

    # Start the bot
    logger.info("Bot is starting...")
    print("Bot is running! Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
